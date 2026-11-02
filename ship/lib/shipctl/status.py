"""`ship-ctl status --json`: the status document (contracts/ship-status.schema.json).

Each probe runs with its own timeout (5 s at most) and all run in parallel, so the
whole document is ready well inside the 15 s budget. A failed probe leaves its
fields unknown and adds a line to `errors`; it never stops the document.

Nothing here reads or prints a token: sign-ins are checked through each tool's own
status command, and the ADC check discards its output.
"""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import PERMISSION_MODES
from .context import Context, utc_now
from .fsutil import read_json, write_atomic
from .net import status_section as net_section
from .runner import NOT_FOUND_RC, TIMEOUT_RC, Result
from .shiptoml import ShipToml
from .sshtrust import attention as ssh_attention
from .sshtrust import state as ssh_state

SCHEMA = "shipshape.ship-status/1"
PROBE_TIMEOUT = 5.0
if os.environ.get("SHIPSHAPE_ROOT") and os.environ.get("SHIPSHAPE_PROBE_TIMEOUT"):
    PROBE_TIMEOUT = float(os.environ["SHIPSHAPE_PROBE_TIMEOUT"])  # tests only
AUTH_SERVICES = ("claude", "gcloud", "gcloud_adc", "github")
AGENT_STATES = ("idle", "working", "blocked", "done", "unknown")
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$")
COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")
SERVICE_LABELS = {
    "claude": "Claude Code",
    "gcloud": "gcloud",
    "gcloud_adc": "Application Default Credentials",
    "github": "GitHub",
}


def _fail(what: str, result: Result) -> str:
    """A short, secret-free description of a failed probe."""
    if result.rc == NOT_FOUND_RC:
        return f"{what}: command not found"
    if result.rc == TIMEOUT_RC:
        return f"{what}: timed out"
    return f"{what}: exit {result.rc}"


# --- host --------------------------------------------------------------------
def probe_host(ctx: Context) -> tuple[dict, list[str]]:
    errors: list[str] = []
    proc = ctx.paths.proc
    host: dict = {
        "uptime_s": 0,
        "load1": 0.0,
        "cpus": os.cpu_count() or 1,
        "mem_total_mb": 0,
        "mem_used_pct": 0.0,
        "disk_total_gb": 0.0,
        "disk_used_pct": 0.0,
        "reboot_required": ctx.paths.p("/var/run/reboot-required").exists(),
        "updates_pending": None,
    }
    try:
        host["uptime_s"] = int(float((proc / "uptime").read_text().split()[0]))
        host["load1"] = max(0.0, float((proc / "loadavg").read_text().split()[0]))
        meminfo = {}
        for line in (proc / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            meminfo[key] = int(rest.split()[0])
        total = meminfo["MemTotal"]
        avail = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        host["mem_total_mb"] = total // 1024
        host["mem_used_pct"] = round(min(100.0, max(0.0, (total - avail) * 100 / total)), 1)
    except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
        errors.append("host: cannot read /proc")
    try:
        st = os.statvfs(ctx.paths.root)
        total_b = st.f_blocks * st.f_frsize
        free_b = st.f_bavail * st.f_frsize
        host["disk_total_gb"] = round(total_b / 1024**3, 1)
        if total_b:
            host["disk_used_pct"] = round((total_b - free_b) * 100 / total_b, 1)
    except OSError:
        errors.append("host: cannot read the root filesystem")
    notice = ctx.paths.p("/var/lib/update-notifier/updates-available")
    try:
        text = notice.read_text()
        m = re.search(r"(\d+) updates? can be applied immediately", text)
        host["updates_pending"] = int(m.group(1)) if m else 0
    except OSError:
        pass
    return host, errors


# --- provisioning ------------------------------------------------------------
def boot_id(ctx: Context) -> str:
    try:
        return (ctx.paths.proc / "sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def provision_record(ctx: Context) -> dict:
    """ship-provision's own record (internal: /var/lib/shipshape/provision.json)."""
    rec = read_json(ctx.paths.state / "provision.json", {})
    if not isinstance(rec, dict):
        rec = {}
    result = rec.get("result")
    if result not in ("ok", "failed", "running"):
        result = "never"
    if result == "running" and (
        rec.get("boot_id") != boot_id(ctx) or not _pid_alive(rec.get("pid"))
    ):
        result = "failed"  # the run died (crash or reboot) without finishing
    last_run = rec.get("last_run")
    if not isinstance(last_run, str) or not TIMESTAMP.match(last_run):
        last_run = None
    return {"result": result, "last_run": last_run}


def probe_cloud_init(ctx: Context) -> tuple[str, list[str]]:
    r = ctx.runner.run(["cloud-init", "status", "--format", "json"], timeout=PROBE_TIMEOUT)
    if r.rc in (NOT_FOUND_RC, TIMEOUT_RC):
        return "unknown", [_fail("cloud-init status", r)]
    try:
        status = str(json.loads(r.out).get("status", ""))
    except (ValueError, AttributeError):
        return "unknown", ["cloud-init status: unreadable output"]
    if status.startswith("done") or status.endswith("done"):
        return "done", []
    if status in ("running", "not started", "not_started"):
        return "running", []
    if status == "error":
        return "error", []
    if status == "disabled":
        return "disabled", []
    return "unknown", []


# --- herdr ------------------------------------------------------------------
def read_ledger(ctx: Context) -> dict[str, str]:
    doc = ctx.read_home_json(ctx.paths.mode_ledger, {})
    sessions = doc.get("sessions", {}) if isinstance(doc, dict) else {}
    out = {}
    if isinstance(sessions, dict):
        for sid, entry in sessions.items():
            if isinstance(entry, dict) and entry.get("mode") in PERMISSION_MODES:
                out[sid] = entry["mode"]
    return out


def herdr_agents(ctx: Context) -> tuple[str, list[dict] | None, list[str]]:
    """(server state, raw herdr AgentInfo list or None, errors)."""
    r = ctx.runner.run(["herdr", "agent", "list"], timeout=PROBE_TIMEOUT, agent=True)
    if r.rc == NOT_FOUND_RC:
        return "unknown", None, ["herdr: not installed"]
    if r.rc == TIMEOUT_RC:
        return "unknown", None, ["herdr agent list: timed out"]
    if not r.ok:
        return "stopped", None, ["herdr: server not answering"]
    try:
        agents = json.loads(r.out)["result"]["agents"]
        if not isinstance(agents, list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return "running", None, ["herdr agent list: unreadable output"]
    return "running", [a for a in agents if isinstance(a, dict)], []


def agent_entry(raw: dict, ledger: dict[str, str]) -> dict:
    kind = raw.get("agent") or "unknown"
    state = raw.get("agent_status")
    session = raw.get("agent_session") or {}
    sid = session.get("value") if isinstance(session, dict) else None
    entry: dict = {
        "name": raw.get("name") if isinstance(raw.get("name"), str) else None,
        "kind": str(kind),
        "state": state if state in AGENT_STATES else "unknown",
        "mode": ledger.get(sid) if kind == "claude" and sid else None,
    }
    for src, dst in (("pane_id", "pane"), ("workspace_id", "workspace"), ("cwd", "cwd")):
        if isinstance(raw.get(src), str):
            entry[dst] = raw[src]
    return entry


def last_resume(ctx: Context) -> dict | None:
    rec = ctx.read_home_json(ctx.paths.last_resume, None)
    if not isinstance(rec, dict):
        return None
    at, method = rec.get("at"), rec.get("method")
    resumed = rec.get("resumed")
    if not (isinstance(at, str) and TIMESTAMP.match(at)) or method not in (
        "hidden-client",
        "api-fallback",
    ):
        return None
    if not isinstance(resumed, int) or isinstance(resumed, bool) or resumed < 0:
        return None
    out = {"at": at, "method": method, "resumed": resumed}
    failed = rec.get("failed")
    if isinstance(failed, int) and not isinstance(failed, bool) and failed >= 0:
        out["failed"] = failed
    return out


def probe_herdr(ctx: Context) -> tuple[dict, list[str]]:
    errors: list[str] = []
    version = None
    r = ctx.runner.run(["herdr", "--version"], timeout=PROBE_TIMEOUT, agent=True)
    if r.ok and r.out.split():
        version = r.out.split()[-1]
    server, raw, errs = herdr_agents(ctx)
    errors += errs
    ledger = read_ledger(ctx)
    agents = [agent_entry(a, ledger) for a in raw or []]
    return {
        "version": version,
        "server": server,
        "agents": agents,
        "last_resume": last_resume(ctx),
    }, errors


# --- sign-ins ---------------------------------------------------------------
def _auth(state: str, account: str | None = None, detail: str | None = None) -> dict:
    out: dict = {"state": state, "account": account[:254] if account else None}
    if detail:
        out["detail"] = detail[:200]
    return out


def probe_claude_auth(ctx: Context) -> tuple[dict, list[str]]:
    r = ctx.runner.run(["claude", "auth", "status", "--json"], timeout=PROBE_TIMEOUT, agent=True)
    if r.rc in (NOT_FOUND_RC, TIMEOUT_RC):
        return _auth("unknown"), [_fail("claude auth status", r)]
    try:
        doc = json.loads(r.out)
        if not isinstance(doc, dict):
            raise TypeError
    except (ValueError, TypeError):
        return _auth("error"), [_fail("claude auth status", r) + ", unreadable output"]
    if not doc.get("loggedIn"):
        return _auth("missing"), []
    account = doc.get("email")
    if not isinstance(account, str):
        nested = doc.get("account")
        account = nested.get("email") if isinstance(nested, dict) else None
    detail = doc.get("subscriptionType") or doc.get("authMethod")
    return _auth("ok", account, detail if isinstance(detail, str) else None), []


# gcloud copies each command's output into its log files (~/.config/gcloud/logs),
# so `print-access-token` would leave a live token there on every refresh even
# with stdout discarded. Probes never write gcloud logs.
GCLOUD_ENV = {"CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "1"}


def probe_gcloud_auth(ctx: Context) -> tuple[dict, list[str]]:
    r = ctx.runner.run(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
        timeout=PROBE_TIMEOUT,
        agent=True,
        env=GCLOUD_ENV,
    )
    if r.rc in (NOT_FOUND_RC, TIMEOUT_RC):
        return _auth("unknown"), [_fail("gcloud auth list", r)]
    if not r.ok:
        return _auth("error"), [_fail("gcloud auth list", r)]
    accounts = [line.strip() for line in r.out.splitlines() if line.strip()]
    return (_auth("ok", accounts[0]) if accounts else _auth("missing")), []


def probe_adc(ctx: Context) -> tuple[dict, list[str]]:
    # The output is an access token: never captured, never logged.
    r = ctx.runner.run(
        ["gcloud", "auth", "application-default", "print-access-token"],
        timeout=PROBE_TIMEOUT,
        agent=True,
        discard=True,
        env=GCLOUD_ENV,
    )
    if r.rc in (NOT_FOUND_RC, TIMEOUT_RC):
        return _auth("unknown"), [_fail("gcloud ADC check", r)]
    return (_auth("ok") if r.ok else _auth("missing")), []


_GH_ACCOUNT = re.compile(r"\b(?:account|as)\s+([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))\b")


def probe_github_auth(ctx: Context) -> tuple[dict, list[str]]:
    r = ctx.runner.run(
        ["gh", "auth", "status", "--hostname", "github.com"], timeout=PROBE_TIMEOUT, agent=True
    )
    if r.rc in (NOT_FOUND_RC, TIMEOUT_RC):
        return _auth("unknown"), [_fail("gh auth status", r)]
    if not r.ok:
        return _auth("missing"), []
    m = _GH_ACCOUNT.search(r.out + "\n" + r.err)
    return _auth("ok", m.group(1) if m else None), []


AUTH_PROBES = {
    "claude": probe_claude_auth,
    "gcloud": probe_gcloud_auth,
    "gcloud_adc": probe_adc,
    "github": probe_github_auth,
}


def probe_auth(ctx: Context, services: list[str]) -> dict[str, dict]:
    """Check only the given services (used by the tools runner for `needs`)."""
    out = {}
    for svc in services:
        probe = AUTH_PROBES.get(svc)
        out[svc] = probe(ctx)[0] if probe else _auth("unknown")
    return out


# --- claude, tools, units ---------------------------------------------------
def probe_claude_version(ctx: Context) -> tuple[str | None, list[str]]:
    r = ctx.runner.run(["claude", "--version"], timeout=PROBE_TIMEOUT, agent=True)
    if r.ok and r.out.split():
        return r.out.split()[0], []
    return None, [] if r.rc == NOT_FOUND_RC else [_fail("claude --version", r)]


def probe_failed_units(ctx: Context) -> tuple[list[str], list[str]]:
    argv = ["systemctl", "list-units", "--failed", "--plain", "--no-legend", "--no-pager"]
    units: list[str] = []
    errors: list[str] = []
    for agent in (False, True):
        cmd = [argv[0], "--user", *argv[1:]] if agent else argv
        r = ctx.runner.run(cmd, timeout=PROBE_TIMEOUT, agent=agent)
        if not r.ok:
            errors.append(_fail("systemctl --user --failed" if agent else "systemctl --failed", r))
            continue
        units += [line.split()[0] for line in r.out.splitlines() if line.split()]
    return units, errors


def requested_tools(ctx: Context, ship: ShipToml) -> tuple[list[str], list[str]]:
    """Recipe ids ship.toml asks for, directly and through its profiles."""
    ids = list(ship.tools)
    errors: list[str] = []
    for profile in ship.profiles:
        try:
            with (ctx.paths.profiles / f"{profile}.toml").open("rb") as fh:
                tools = tomllib.load(fh).get("tools", [])
        except (OSError, tomllib.TOMLDecodeError):
            errors.append(f"profile {profile}: not in this tooling bundle")
            continue
        ids += [t for t in tools if t not in ids]
    return ids, errors


def tool_states(ctx: Context, ship: ShipToml) -> tuple[dict, list[str]]:
    ids, errors = requested_tools(ctx, ship)
    recorded = read_json(ctx.paths.state / "tools.json", {})
    recorded = recorded if isinstance(recorded, dict) else {}
    out = {}
    for tid in ids:
        rec = recorded.get(tid)
        if not isinstance(rec, dict) or rec.get("state") not in ("ok", "waiting", "failed"):
            out[tid] = {"state": "missing", "version": None}
            continue
        entry: dict = {"state": rec["state"], "version": rec.get("version")}
        if not isinstance(entry["version"], str):
            entry["version"] = None
        if rec["state"] == "waiting":
            entry["waiting_for"] = [w for w in rec.get("waiting_for", []) if isinstance(w, str)]
        out[tid] = entry
    return out, errors


# --- bundle -----------------------------------------------------------------
def bundle_info(ctx: Context) -> tuple[str, str | None, list[str]]:
    manifest = read_json(ctx.paths.bundle_json, None)
    if isinstance(manifest, dict) and SEMVER.match(str(manifest.get("tooling_version", ""))):
        commit = str(manifest.get("commit", ""))
        return manifest["tooling_version"], (commit if COMMIT.match(commit) else None), []
    tooling = str(ctx.versions.get("shipshape", {}).get("tooling", ""))
    if SEMVER.match(tooling):
        return tooling, None, ["bundle.json: not found; tooling version from versions.toml"]
    return "0.0.0", None, ["bundle.json: not found"]


# --- the document -----------------------------------------------------------
def attention_for(doc: dict, failed_units: list[str]) -> list[dict]:
    items: list[dict] = []

    def add(code: str, message: str, subject: str | None = None) -> None:
        item = {"code": code, "message": message[:500]}
        if subject:
            item["subject"] = subject[:100]
        items.append(item)

    if doc["provision"]["result"] == "failed":
        add("provision-failed", "Provisioning failed; see `ship-ctl logs provision`.")
    for agent in doc["herdr"]["agents"]:
        if agent["state"] == "blocked":
            who = agent["name"] or agent.get("pane") or "an agent"
            add("agent-waiting", f"Agent '{who}' is waiting for you.", who)
    for svc, entry in doc["auth"].items():
        label = SERVICE_LABELS.get(svc, svc)
        if svc == "claude" and entry["state"] == "missing":
            add("signin-missing", f"{label} is not signed in.", svc)
        elif entry["state"] == "expiring":
            add("signin-expiring", f"The {label} sign-in expires soon.", svc)
        elif entry["state"] == "expired":
            add("signin-expired", f"The {label} sign-in has expired.", svc)
    for unit in failed_units:
        add("unit-failed", f"{unit} has failed.", unit)
    if (doc.get("net") or {}).get("collector") == "stopped":
        add(
            "net-collector-down",
            "Network visibility is not recording (ship-netwatch is "
            "not running); see `ship-ctl logs system`.",
            "ship-netwatch",
        )
    for code, message in ssh_attention(doc.get("ssh")):
        add(code, message)
    if doc["host"]["reboot_required"]:
        add("reboot-required", "A reboot is needed to finish updates.")
    for tid, tool in doc["tools"].items():
        if tool["state"] == "waiting":
            needs = ", ".join(SERVICE_LABELS.get(s, s) for s in tool.get("waiting_for", []))
            add("tool-waiting", f"{tid} waits for the {needs or 'required'} sign-in.", tid)
        elif tool["state"] == "failed":
            add("tool-failed", f"{tid} failed to install or check; see `ship-ctl logs tools`.", tid)
    return items


def derive_state(doc: dict) -> str:
    prov = doc["provision"]
    if prov["result"] in ("running", "never") or prov["cloud_init"] == "running":
        return "provisioning"
    if doc["auth"]["claude"]["state"] in ("missing", "expired", "error"):
        return "needs-sign-in"
    if doc["herdr"]["agents"]:
        return "running"
    if doc["herdr"]["server"] == "running":
        return "ready"
    return "unknown"


def build(ctx: Context, ship: ShipToml) -> dict:
    started = time.monotonic()
    now = utc_now()
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        f_host = pool.submit(probe_host, ctx)
        f_cloud = pool.submit(probe_cloud_init, ctx)
        f_herdr = pool.submit(probe_herdr, ctx)
        f_claude = pool.submit(probe_claude_version, ctx)
        f_units = pool.submit(probe_failed_units, ctx)
        f_ssh = pool.submit(ssh_state, ctx)
        f_net = pool.submit(net_section, ctx)
        f_auth = {svc: pool.submit(AUTH_PROBES[svc], ctx) for svc in AUTH_SERVICES}
        host, e = f_host.result()
        errors += e
        cloud_init, e = f_cloud.result()
        errors += e
        herdr, e = f_herdr.result()
        errors += e
        claude_version, e = f_claude.result()
        errors += e
        failed_units, e = f_units.result()
        errors += e
        ssh, e = f_ssh.result()
        errors += e
        net, e = f_net.result()
        errors += e
        auth = {}
        for svc, fut in f_auth.items():
            entry, e = fut.result()
            entry["checked_at"] = now
            auth[svc] = entry
            errors += e

    tooling, commit, e = bundle_info(ctx)
    errors += e
    tools, e = tool_states(ctx, ship)
    errors += e
    c = ship.claude
    doc: dict = {
        "schema": SCHEMA,
        "generated_at": now,
        "ship": {
            "name": ship.name,
            "id": ship.id,
            "tags": ship.tags,
            "tooling": tooling,
            "bundle_commit": commit,
        },
        "state": "unknown",
        "attention": [],
        "host": host,
        "provision": {"cloud_init": cloud_init, **provision_record(ctx)},
        "herdr": herdr,
        "claude": {
            "version": claude_version,
            "channel": c["update_channel"],
            "remote_control": c["remote_control"],
            "default_mode": c["default_mode"],
            "allow_bypass": c["allow_bypass"],
        },
        "auth": auth,
        "tools": tools,
        "net": net,
        "ssh": ssh,
        "errors": [],
    }
    doc["state"] = derive_state(doc)
    doc["attention"] = attention_for(doc, failed_units)
    elapsed = time.monotonic() - started
    if elapsed > 12:
        errors.append(f"status took {elapsed:.0f}s")
    doc["errors"] = [e[:500] for e in errors]
    return doc


def write_cache(path: Path, doc: dict) -> None:
    write_atomic(path, json.dumps(doc, indent=2) + "\n", 0o644)
