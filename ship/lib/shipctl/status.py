"""`ship-ctl status --json`: the status document (contracts/ship-status.schema.json).

Each probe runs with its own timeout (5 s at most) and all run in parallel, so the
whole document is ready well inside the 15 s budget. A failed probe leaves its
fields unknown and adds a line to `errors`; it never stops the document.

Nothing here reads or prints a token: sign-ins are checked through each tool's own
status command, and the ADC check discards its output. Which sign-ins there are, and
how each is checked, comes from the sign-in files (signins.py, signincheck.py).
"""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import PERMISSION_MODES, overlay, signincheck, signins, toolkits
from .context import Context, utc_now
from .errors import CtlError
from .fsutil import read_json, write_atomic
from .net import status_section as net_section
from .runner import NOT_FOUND_RC, TIMEOUT_RC, Result
from .shiptoml import GITHUB_SCOPES, ShipToml
from .sshtrust import attention as ssh_attention
from .sshtrust import state as ssh_state

SCHEMA = "shipshape.ship-status/1"
PROBE_TIMEOUT = 5.0
if os.environ.get("SHIPSHAPE_ROOT") and os.environ.get("SHIPSHAPE_PROBE_TIMEOUT"):
    PROBE_TIMEOUT = float(os.environ["SHIPSHAPE_PROBE_TIMEOUT"])  # tests only
AGENT_STATES = ("idle", "working", "blocked", "done", "unknown")
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$")
COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")


def _fail(what: str, result: Result) -> str:
    """A short, secret-free description of a failed probe."""
    if result.rc == NOT_FOUND_RC:
        return f"{what}: command not found"
    if result.rc == TIMEOUT_RC:
        return f"{what}: timed out"
    return f"{what}: exit {result.rc}"


# --- host --------------------------------------------------------------------
TMP_MASK = "/etc/systemd/system/tmp.mount"  # ship-provision's mask (step_disk_tmp)


def tmp_state(ctx: Context) -> dict:
    """What /tmp is now and from the next boot: "tmpfs" (in memory) or "disk".

    fs comes from this process's mount table (the last mount on /tmp wins; none
    means a plain folder of /); next_boot is "disk" once tmp.mount is masked.
    """
    fs = "unknown"
    try:
        found = ""
        for line in (ctx.paths.proc / "self/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) > 2 and parts[1] == "/tmp":
                found = parts[2]
        fs = "tmpfs" if found == "tmpfs" else "disk"
    except OSError:
        pass
    try:
        masked = os.readlink(ctx.paths.p(TMP_MASK)) == "/dev/null"
    except OSError:
        masked = False
    return {"fs": fs, "next_boot": "disk" if masked else "tmpfs"}


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
        "tmp": tmp_state(ctx),
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


def agent_entry(raw: dict, ledger: dict[str, str], rt=None) -> dict:
    """One herdr.agents item from what the runtime (rt, herdr's by default) lists;
    mode from the mode ledger (build() falls back to the agent record's)."""
    if rt is None:
        from .runtime import Herdr as rt
    report = rt.report(raw)
    sid = rt.view(raw)["session"]
    entry = {k: report[k] for k in ("name", "kind", "state")}  # mode after state, as before
    entry["mode"] = ledger.get(sid) if entry["kind"] == "claude" and sid else None
    entry.update(report)
    return entry


def last_resume(ctx: Context) -> dict | None:
    """herdr.last_resume: this Ship's last resume after a boot. ship-ctl resumes
    agents from its agent record through herdr's API ("api-fallback", a name the
    contract had for that already); until a Ship's first boot on that tooling, the
    record ship-resume wrote ("hidden-client") is reported."""
    from .record import load

    resume = load(ctx)["resume"]
    if resume is not None:
        return {"at": resume["at"], "method": "api-fallback", "resumed": resume["resumed"],
                "failed": resume["failed"]}
    rec = ctx.read_home_json(ctx.paths.last_resume, None)
    if not isinstance(rec, dict):
        return None
    at, method = rec.get("at"), rec.get("method")
    resumed = rec.get("resumed")
    if not (isinstance(at, str) and TIMESTAMP.match(at)) or method != "hidden-client":
        return None
    if not isinstance(resumed, int) or isinstance(resumed, bool) or resumed < 0:
        return None
    out = {"at": at, "method": method, "resumed": resumed}
    failed = rec.get("failed")
    if isinstance(failed, int) and not isinstance(failed, bool) and failed >= 0:
        out["failed"] = failed
    return out


def probe_herdr(ctx: Context, ship: ShipToml | None = None
                ) -> tuple[dict, list[str], list[dict] | None]:
    """(the herdr block, errors, the live agents as runtime.py describes them). The
    block is the runtime's, herdr's server or herdr-core (`runtime` says which)."""
    from .runtime import for_ship

    rt = for_ship(ctx, ship)
    errors: list[str] = []
    version = rt.version(PROBE_TIMEOUT)
    server, raw, errs = rt.raw_agents(PROBE_TIMEOUT)
    errors += errs
    ledger = read_ledger(ctx)
    agents = [agent_entry(a, ledger, rt) for a in raw or []]
    live = None if raw is None else [rt.view(a) for a in raw]
    return {
        "runtime": rt.kind,
        "version": version,
        "server": server,
        "agents": agents,
        "last_resume": last_resume(ctx),
    }, errors, live


HERDR_BIN = "/usr/local/bin/herdr"
CORE_BIN = "/usr/local/bin/herdr-core"  # `hc` is a link to it


def _restart_pending(ctx: Context, binary: str, names: tuple[str, ...], verb: bytes) -> bool:
    """True when a running server (argv[0] named one of names, argv[1] verb) is not
    the installed binary.

    A converge replaces the binary with a new file; the running server keeps the old
    one (its /proc/<pid>/exe points at the replaced inode) until it restarts. Only the
    agent's processes are looked at, and only stat() and cmdline are read, so nothing
    an agent controls is opened or followed for writing; at worst an agent could make
    this report a needless restart.
    """
    try:
        installed = os.stat(ctx.paths.p(binary))
    except OSError:
        return False
    owner = ctx.agent_owner
    try:
        entries = [e for e in ctx.paths.proc.iterdir() if e.name.isdigit()]
    except OSError:
        return False
    for entry in entries:
        try:
            if owner is not None and entry.stat().st_uid != owner[0]:
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if Path(argv[0].decode("utf-8", "replace")).name not in names or argv[1:2] != [verb]:
                continue
            running = os.stat(entry / "exe")
        except OSError:
            continue
        if (running.st_dev, running.st_ino) != (installed.st_dev, installed.st_ino):
            return True
    return False


def herdr_restart_pending(ctx: Context) -> bool:
    """True when a running `herdr server` is not the installed herdr binary (it
    switches at the next reboot)."""
    return _restart_pending(ctx, HERDR_BIN, ("herdr",), b"server")


def core_restart_pending(ctx: Context) -> bool:
    """True when the running herdr-core (`herdr-core serve`, which the core starts as
    its own transient user unit) is not the installed /usr/local/bin/herdr-core.
    Its holders, which keep each terminal's program, are not counted: they stay on
    the binary they started with until their terminal ends, by design."""
    return _restart_pending(ctx, CORE_BIN, ("herdr-core", "hc"), b"serve")


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
# "- Token scopes: 'gist', 'read:org', 'repo'" (older gh: "✓ Token scopes: gist, repo").
_GH_SCOPES = re.compile(r"Token scopes:[ \t]*([^\r\n]*)")
_GH_SCOPE = re.compile(r"^[a-z][a-z0-9_:]{0,39}$")


def github_scopes(text: str) -> list[str] | None:
    """The scopes gh auth status lists for the first (active) account, or None if it
    lists none (a token without scopes, or gh too old to say)."""
    m = _GH_SCOPES.search(text)
    if not m:
        return None
    listed = m.group(1).strip()
    if listed in ("", "none"):
        return []
    found = [s.strip().strip("'\"") for s in listed.split(",")]
    scopes = sorted({s for s in found if _GH_SCOPE.match(s)})
    return scopes[:64] if scopes else None


def probe_github_auth(ctx: Context) -> tuple[dict, list[str]]:
    r = ctx.runner.run(
        ["gh", "auth", "status", "--hostname", "github.com"], timeout=PROBE_TIMEOUT, agent=True
    )
    if r.rc in (NOT_FOUND_RC, TIMEOUT_RC):
        return _auth("unknown"), [_fail("gh auth status", r)]
    if not r.ok:
        return _auth("missing"), []
    text = r.out + "\n" + r.err
    m = _GH_ACCOUNT.search(text)
    entry = _auth("ok", m.group(1) if m else None)
    entry["scopes"] = github_scopes(text)
    return entry, []


# The built-in checks a sign-in file names ([status] builtin).
BUILTIN_PROBES = {
    "claude": probe_claude_auth,
    "gcloud": probe_gcloud_auth,
    "adc": probe_adc,
    "github": probe_github_auth,
}


def probe_auth(ctx: Context, services: list[str]) -> dict[str, dict]:
    """Check only the given services (used by the tools runner for `needs`)."""
    reg = signins.registry(ctx.paths)
    return {svc: found for svc, (found, _) in signincheck.check(ctx, reg, services).items()}


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
            path, _ = toolkits.profile_path(ctx.paths, ship, profile)
            with path.open("rb") as fh:
                tools = toolkits.profile_tools(ctx.paths, ship, profile, tomllib.load(fh))
        except CtlError as exc:
            errors.append(f"profile {profile}: {exc.message}")
            continue
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
def attention_for(doc: dict, failed_units: list[str], herdr_pending: bool = False,
                  core_pending: bool = False, labels: dict[str, str] | None = None
                  ) -> list[dict]:
    """labels: how each sign-in is named (signins.Registry.labels)."""
    items: list[dict] = []
    labels = labels or {}

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
            # herdr-core says what it waits for, in Claude's own words.
            what = {"permission": " to approve something", "question": " to answer a question",
                    "auth": " to sign in"}.get(agent.get("blocked_kind") or "", "")
            said = f": {agent['message']}" if agent.get("message") else "."
            add("agent-waiting", f"Agent '{who}' is waiting for you{what}{said}", who)
    for svc, entry in doc["auth"].items():
        label = labels.get(svc, svc)
        if svc in signins.CORE and entry["state"] == "missing":
            add("signin-missing", f"{label} is not signed in.", svc)
        elif entry["state"] == "expiring":
            add("signin-expiring", f"The {label} sign-in expires soon.", svc)
        elif entry["state"] == "expired":
            add("signin-expired", f"The {label} sign-in has expired.", svc)
    differ = scopes_differ(doc)
    if differ:
        add("signin-scopes-differ", differ, "github")
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
    if doc["host"].get("tmp") == {"fs": "tmpfs", "next_boot": "disk"}:
        add(
            "tmp-reboot-pending",
            "/tmp is still in memory (tmpfs), where it can run out of room for new "
            "files; the next reboot puts it on the disk.",
            "/tmp",
        )
    if herdr_pending:
        add(
            "herdr-restart-pending",
            "herdr was updated; the running server is the old version until the next reboot.",
            "herdr",
        )
    if core_pending:
        add(
            "core-restart-pending",
            "herdr-core was updated; the running core is the old version until the next "
            "reboot. Agents are not affected.",
            "herdr-core",
        )
    ov = doc.get("overlay")
    if ov and ov["joined"] and ov["state"] != "running":
        add("overlay-down", f"The overlay network is {ov['state']}; the Ship cannot be reached "
            "off the LAN.", "overlay")
    for tid, tool in doc["tools"].items():
        if tool["state"] == "waiting":
            needs = ", ".join(labels.get(s, s) for s in tool.get("waiting_for", []))
            add("tool-waiting", f"{tid} waits for the {needs or 'required'} sign-in.", tid)
        elif tool["state"] == "failed":
            add("tool-failed", f"{tid} failed to install or check; see `ship-ctl logs tools`.", tid)
    return items


def scopes_differ(doc: dict) -> str | None:
    """What is wrong with the GitHub token's scopes, against ship.toml github.scopes,
    or None. Only the extra scopes a Ship may choose count (shiptoml.GITHUB_SCOPES):
    gh's own minimum, and anything outside the choice, are left alone. Nothing is said
    while GitHub is not signed in, or the token's scopes are not known."""
    entry = doc["auth"].get("github") or {}
    granted = entry.get("scopes")
    if entry.get("state") not in ("ok", "expiring") or granted is None:
        return None
    wanted = set((doc.get("settings") or {}).get("github", {}).get("scopes", []))
    missing = sorted(wanted - set(granted))
    extra = sorted(set(granted) & set(GITHUB_SCOPES) - wanted)
    if not missing and not extra:
        return None
    if not extra:
        return (f"The GitHub sign-in lacks {', '.join(missing)}, chosen for this Ship: sign "
                "in to GitHub again with the chosen scopes.")
    # A scope the token has but the Ship does not choose was most likely given by hand
    # and is wanted: keeping it (ticking it) comes first; signing in again drops it.
    has = ", ".join(extra)
    lacks = f"lacks {', '.join(missing)}, chosen for this Ship, and " if missing else ""
    return (f"The GitHub sign-in {lacks}has {has}, not chosen for this Ship. To keep {has}, "
            "tick it in the Ship's GitHub scopes (Settings); signing in to GitHub again "
            f"with the chosen scopes drops it.")


def settings_section(ship: ShipToml) -> dict:
    """What ship.toml says now, defaults filled in: the values `ship-ctl set` manages
    (contracts/ship-status.schema.json, settings), so FleetLedger shows a Ship's
    settings as the Ship has them."""
    c = ship.claude
    keep = ("name", "cwd", "mode", "repo")
    return {
        "ship": {"tags": list(ship.tags), "timezone": ship.timezone},
        "claude": {"remote_control": c["remote_control"], "default_mode": c["default_mode"],
                   "allow_bypass": bool(c["allow_bypass"]),
                   "update_channel": c["update_channel"],
                   "session_prefix": c["session_prefix"]},
        "agent": {"sudo": ship.agent_sudo},
        "network": {"lan_guard": ship.lan_guard, "lan_guard_allow": ship.lan_guard_allow,
                    "ssh_listen": overlay.ssh_listen(ship)},
        "updates": {"auto_reboot": bool(ship.updates["auto_reboot"]),
                    "reboot_time": ship.updates["reboot_time"]},
        "github": {"scopes": ship.github_scopes},
        "wake": {"after_boot": bool(ship.wake["after_boot"]),
                 "delay_minutes": int(ship.wake["delay_minutes"]), "text": ship.wake["text"]},
        "owner": (ship.owner or {}).get("name"),
        "autostart": [{k: e[k] for k in keep if e.get(k)} for e in ship.autostart],
    }


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
    reg = signins.registry(ctx.paths)
    with ThreadPoolExecutor(max_workers=10) as pool:
        f_host = pool.submit(probe_host, ctx)
        f_cloud = pool.submit(probe_cloud_init, ctx)
        f_herdr = pool.submit(probe_herdr, ctx, ship)
        f_claude = pool.submit(probe_claude_version, ctx)
        f_units = pool.submit(probe_failed_units, ctx)
        f_overlay = pool.submit(overlay.status, ctx, ship)
        f_ssh = pool.submit(ssh_state, ctx)
        f_net = pool.submit(net_section, ctx)
        f_pending = pool.submit(herdr_restart_pending, ctx)
        f_core_pending = pool.submit(core_restart_pending, ctx)
        f_auth = pool.submit(signincheck.check, ctx, reg, reg.keys)
        host, e = f_host.result()
        errors += e
        cloud_init, e = f_cloud.result()
        errors += e
        herdr, e, live = f_herdr.result()
        errors += e
        claude_version, e = f_claude.result()
        errors += e
        failed_units, e = f_units.result()
        errors += e
        ssh, e = f_ssh.result()
        errors += e
        net, e = f_net.result()
        errors += e
        overlay_doc = f_overlay.result()
        auth = {}
        for svc, (entry, e) in f_auth.result().items():
            auth[svc] = entry
            errors += e

    tooling, commit, e = bundle_info(ctx)
    errors += e
    tools, e = tool_states(ctx, ship)
    errors += e
    kits = toolkits.status_section(ctx, ship)
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
        "toolkits": kits,
        "overlay": overlay_doc,
        "net": net,
        "ssh": ssh,
        "settings": settings_section(ship),
        "errors": [],
    }
    from . import record, switch  # they import this module
    from .autostart import repo_attention

    doc["agent_record"], found = record.view(ctx, ship, live)
    recorded = {a["name"]: a["mode"] for a in doc["agent_record"]["agents"]}
    modes = {id(a): recorded.get(n) for n, a in found.items()}
    # herdr.agents and live come from the same list, in its order.
    for entry, agent in zip(herdr["agents"], live or [], strict=False):
        if entry["mode"] is None and entry["kind"] == "claude":
            entry["mode"] = modes.get(id(agent))
    doc["state"] = derive_state(doc)
    doc["attention"] = attention_for(doc, failed_units, f_pending.result(),
                                     f_core_pending.result(), reg.labels)
    doc["attention"] += [{"code": code, "message": message[:500], "subject": who[:100]}
                         for code, message, who in [*record.attention(doc["agent_record"]),
                                                    *switch.attention(doc["agent_record"]),
                                                    *repo_attention(ctx, ship)]]
    elapsed = time.monotonic() - started
    if elapsed > 12:
        errors.append(f"status took {elapsed:.0f}s")
    doc["errors"] = [e[:500] for e in errors]
    return doc


def write_cache(path: Path, doc: dict) -> None:
    write_atomic(path, json.dumps(doc, indent=2) + "\n", 0o644)
