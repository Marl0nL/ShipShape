#!/usr/bin/env python3
"""Stand-ins for the commands ship-ctl calls, so tests never touch the real ones.

Installed into a test's bin folder under each command's name (herdr, claude, gh,
...). Behaviour comes from $FAKE_DIR/scenario.json; every call is appended to
$FAKE_DIR/calls.jsonl. Commands that change state (herdr agent start, npm install,
apt-get install, ...) write the scenario back so later calls see the change.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path

FAKE = Path(os.environ["FAKE_DIR"])
SCENARIO = FAKE / "scenario.json"


def load() -> dict:
    return json.loads(SCENARIO.read_text())


def save(s: dict) -> None:
    tmp = SCENARIO.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    os.replace(tmp, SCENARIO)


def out(obj) -> None:
    print(json.dumps(obj) if not isinstance(obj, str) else obj)


def ledger_hook(event: dict) -> None:
    """What the mode-ledger hook does in a real agent: claude runs it at SessionStart
    (mode from its launch flags) and SessionEnd. Run here as the real hook, in the
    agent's environment (HOME is the agent's home)."""
    hook = Path(__file__).resolve().parents[2] / "files/hooks/mode_ledger.py"
    import importlib.util

    spec = importlib.util.spec_from_file_location("mode_ledger", hook)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    directory = mod.ledger_dir()
    sid = event["session_id"]
    if event["hook_event_name"] == "SessionStart":
        mode = mod.mode_from_argv(event.pop("argv"))
        mod.record(directory, sid, mode if mode in mod.MODES else None, started=True,
                   terminal=event.get("terminal"))
    else:
        mod.record(directory, sid, ended=event["reason"])


def end_session(ag: dict, reason: str = "prompt_input_exit") -> None:
    sid = (ag.get("agent_session") or {}).get("value")
    if sid:
        ledger_hook({"hook_event_name": "SessionEnd", "session_id": sid, "reason": reason})


def panes(h: dict) -> dict[str, dict]:
    """Every pane: those workspace create made (or a test listed), and those live
    agents sit in."""
    out = {p["pane_id"]: dict(p) for p in h.get("panes", [])}
    for ag in h.get("agents", []):
        pid = ag.get("pane_id")
        if pid and pid not in out:
            out[pid] = {"pane_id": pid, "workspace_id": ag.get("workspace_id") or pid.split(":")[0],
                        "cwd": ag.get("cwd")}
    for p in out.values():
        ag = next((x for x in h.get("agents", []) if x.get("pane_id") == p["pane_id"]), None)
        p["agent"] = ag.get("agent") if ag else None
    return out


def herdr(s: dict, a: list[str]) -> int:
    h = s.setdefault("herdr", {})
    if a == ["--version"]:
        out(f"herdr {h.get('version', '0.9.5')}")
        return 0
    if not h.get("server", True):
        print(
            json.dumps({"error": {"code": "server_unavailable", "message": "no server"}}),
            file=sys.stderr,
        )
        return 1
    agents = h.setdefault("agents", [])
    if a[:2] == ["agent", "list"]:
        settling = [ag for ag in agents if "settle_in" in ag]
        for ag in settling:  # herdr sees Escape take effect a few polls later
            ag["settle_in"] -= 1
            if ag["settle_in"] <= 0:
                del ag["settle_in"]
                ag["agent_status"] = "idle"
        if settling:
            save(s)
        shown = [{k: v for k, v in ag.items() if k != "settle_in"} for ag in agents]
        out({"id": "cli", "result": {"type": "agent_list", "agents": shown}})
        return 0
    if a[:2] == ["pane", "list"]:
        if h.get("pane_list_fails"):
            print(json.dumps({"error": {"code": "x", "message": "x"}}), file=sys.stderr)
            return 1
        out({"result": {"type": "pane_list", "panes": list(panes(h).values())}})
        return 0
    if a[:2] == ["workspace", "create"]:
        n = h.get("next_ws", 1)
        h["next_ws"] = n + 1
        h.setdefault("workspaces", []).append({"id": f"w{n}", "args": a[2:]})
        cwd = a[a.index("--cwd") + 1] if "--cwd" in a else None
        h.setdefault("panes", []).append({"pane_id": f"w{n}:p1", "workspace_id": f"w{n}",
                                          "cwd": cwd})
        save(s)
        out(
            {
                "result": {
                    "workspace": {"workspace_id": f"w{n}"},
                    "tab": {"tab_id": f"w{n}:t1"},
                    "root_pane": {"pane_id": f"w{n}:p1"},
                }
            }
        )
        return 0
    if a[:2] == ["agent", "start"]:
        name, pane = a[2], a[a.index("--pane") + 1]
        if name in h.get("fail_start", []):
            print(
                json.dumps({"error": {"code": "agent_not_ready", "message": "x"}}), file=sys.stderr
            )
            return 1
        here = panes(h).get(pane)
        if here is None or here["agent"]:  # it must be a pane at its shell prompt
            code = "pane_not_found" if here is None else "pane_not_at_shell"
            print(json.dumps({"error": {"code": code, "message": "x"}}), file=sys.stderr)
            return 1
        rest = a[a.index("--") + 1 :] if "--" in a else []
        if "--resume" in rest and rest[rest.index("--resume") + 1] in h.get("lost_sessions", []):
            print(json.dumps({"error": {"code": "agent_not_ready", "message": "claude exited: "
                                        "No conversation found"}}), file=sys.stderr)
            return 1
        # `claude --resume <id>` continues that session; anything else is a new one.
        session = rest[rest.index("--resume") + 1] if "--resume" in rest else f"sess-{name}"
        if "--resume" not in rest and any(ag.get("agent_session", {}).get("value") == session
                                          for ag in h.get("exited", [])):
            session = f"sess-{name}-{h.get('next_session', 2)}"
            h["next_session"] = h.get("next_session", 2) + 1
        agents.append(
            {
                "name": name,
                "agent": "claude",
                "agent_status": "idle",
                "pane_id": pane,
                "workspace_id": pane.split(":")[0],
                "tab_id": pane + "t",
                "terminal_id": "t",
                "focused": False,
                "revision": 1,
                "agent_session": {
                    "source": "herdr:claude",
                    "agent": "claude",
                    "kind": "id",
                    "value": session,
                },
                "args": rest,
                **({"cwd": here["cwd"]} if here.get("cwd") else {}),
            }
        )
        save(s)
        ledger_hook({"hook_event_name": "SessionStart", "session_id": session, "argv": rest})
        out({"result": {"type": "agent_info"}})
        return 0
    def find(target: str) -> dict | None:
        return next((ag for ag in agents if target in (ag.get("name"), ag.get("pane_id"))), None)

    if a[:2] in (["agent", "send-keys"], ["pane", "send-keys"]):
        ag = find(a[2])
        if ag is None:
            print(json.dumps({"error": {"code": "agent_not_found", "message": "x"}}),
                  file=sys.stderr)
            return 1
        h.setdefault("keys", []).append(a[2:])
        if "esc" in a[3:] and ag.get("agent_status") in ("working", "blocked", "unknown"):
            if ag.get("name") in h.get("slow_esc", []):
                ag["settle_in"] = h.get("slow_esc_polls", 2)
            else:
                ag["agent_status"] = "idle"
        if a[3:] == ["ctrl+c", "ctrl+c"] and ag.get("name") in h.get("exit_on_ctrl_c", []):
            agents.remove(ag)
            h.setdefault("exited", []).append(ag)
            end_session(ag)
        save(s)
        out({"result": {}})
        return 0
    if a[:2] == ["agent", "prompt"] and a[3:4] == ["/exit"]:
        ag = find(a[2])
        if ag is None or ag.get("agent_status") == "blocked":
            code = "agent_blocked" if ag else "agent_not_found"
            print(json.dumps({"error": {"code": code, "message": "x"}}), file=sys.stderr)
            return 1
        h.setdefault("prompts", []).append(a[2:4])
        if ag.get("name") not in h.get("stubborn", []) + h.get("exit_on_ctrl_c", []):
            agents.remove(ag)  # claude exits; herdr clears the name with it
            h.setdefault("exited", []).append(ag)
            end_session(ag)
        save(s)
        out({"result": {}})
        return 0
    if a[:2] == ["agent", "prompt"]:
        if a[2] in h.get("fail_prompt", []):
            print(json.dumps({"error": {"code": "agent_not_found", "message": "x"}}),
                  file=sys.stderr)
            return 1
        h.setdefault("prompts", []).append(a[2:4])
        save(s)
        out({"result": {}})
        return 0
    if a[:2] == ["agent", "rename"]:
        for ag in agents:
            if ag.get("pane_id") == a[2]:
                ag["name"] = a[3]
        save(s)
        out({"result": {}})
        return 0
    return 2


def claude(s: dict, a: list[str]) -> int:
    c = s.setdefault("claude", {})
    if not c.get("installed", True):
        return 127
    if a == ["--version"]:
        out(f"{c.get('version', '2.1.316')} (Claude Code)")
        return 0
    if a == ["--help"]:
        out(
            "Usage: claude [options]\n  -c, --continue  Continue\n"
            + (
                "  -n, --name <name>  Set a display name for this session\n"
                if c.get("name_flag", True)
                else ""
            )
        )
        return 0
    if c.get("auth_garbage") and a[:2] == ["auth", "status"]:
        out("not json")
        return 0
    if c.get("hang") and a[:2] == ["auth", "status"]:
        import time

        time.sleep(30)
    if a[:2] == ["auth", "status"]:
        if c.get("logged_in"):
            out(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "email": c.get("email"),
                    "subscriptionType": "max",
                }
            )
        else:
            out({"loggedIn": False, "authMethod": "none"})
        return 0
    if a[:2] == ["mcp", "get"]:
        return 0 if a[2] in c.get("mcp", []) else 1
    if a[:2] == ["mcp", "add"]:
        c.setdefault("mcp", []).append(a[4])
        save(s)
        return 0
    if a[:2] == ["plugin", "list"]:
        out("\n".join(c.get("plugins", [])))
        return 0
    if a[:2] == ["plugin", "install"]:
        c.setdefault("plugins", []).append(a[2])
        save(s)
        return 0
    if a[:3] == ["plugin", "marketplace", "add"]:
        return 0
    return 2


def gcloud(s: dict, a: list[str]) -> int:
    g = s.setdefault("gcloud", {})
    if a[:2] == ["auth", "list"]:
        if g.get("account"):
            out(g["account"])
        return 0
    if a[:3] == ["auth", "application-default", "print-access-token"]:
        if g.get("adc"):
            out("ya29.FAKE-TOKEN-MUST-NEVER-APPEAR")
            return 0
        return 1
    return 2


def gh(s: dict, a: list[str]) -> int:
    login = s.setdefault("gh", {}).get("login")
    if a[:2] == ["auth", "status"]:
        if login:
            # scopes: the token's scopes (default gh's minimum); None: no scopes line.
            scopes = s["gh"].get("scopes", ["gist", "read:org", "repo"])
            listed = ("\n  - Token scopes: " + ", ".join(f"'{x}'" for x in scopes)
                      if scopes is not None else "")
            print(
                f"github.com\n  ✓ Logged in to github.com account {login} (keyring)\n"
                "  - Active account: true\n"
                "  - Git operations protocol: https\n"
                "  - Token: gho_************************************" + listed,
                file=sys.stderr,
            )
            return 0
        print("You are not logged into any GitHub hosts.", file=sys.stderr)
        return 1
    if a[:2] == ["auth", "setup-git"]:
        return 0 if login else 1
    return 2


def git(s: dict, a: list[str]) -> int:
    """git clone --quiet -- https://github.com/OWNER/REPO.git DEST: public repos clone,
    private ones only with gh signed in (git then uses gh's credentials)."""
    if a[:1] != ["clone"]:
        return 2
    url, dest = a[-2], a[-1]
    repo = url.removeprefix("https://github.com/").removesuffix(".git")
    repos = s.setdefault("git", {})
    if repos.get("offline"):
        print("fatal: unable to access 'https://github.com/x/': Could not resolve host: "
              "github.com", file=sys.stderr)
        return 128
    signed_in = bool(s.get("gh", {}).get("login"))
    if repo in repos.get("public", []) or (signed_in and repo in repos.get("private", [])):
        Path(dest, ".git").mkdir(parents=True)
        Path(dest, "CLAUDE.md").write_text(f"# {repo}\n")
        return 0
    print("fatal: could not read Username for 'https://github.com': terminal prompts disabled",
          file=sys.stderr)
    return 128


def npm(s: dict, a: list[str]) -> int:
    if a[:2] != ["install", "-g"]:
        return 2
    prefix = Path(a[a.index("--prefix") + 1])
    root = Path(os.environ.get("SHIPSHAPE_ROOT", "/"))
    if not str(prefix).startswith(str(root)):
        prefix = root / str(prefix).lstrip("/")
    fails = s.get("npm", {}).get("fail", [])
    for spec in [
        x for x in a[2:] if not x.startswith("-") and x != str(a[a.index("--prefix") + 1])
    ]:
        at = spec.rfind("@")
        name, version = spec[:at], spec[at + 1 :]
        if name in fails:
            return 1
        pkg = prefix / "lib/node_modules" / name
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "package.json").write_text(json.dumps({"name": name, "version": version}))
        exe = prefix / "bin" / name.split("/")[-1]
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text(f"#!/bin/sh\necho '{version}'\n")
        exe.chmod(0o755)
    s.setdefault("npm", {}).setdefault("installs", []).append(a[2:])
    save(s)
    return 0


def dpkg_query(s: dict, a: list[str]) -> int:
    pkg = a[-1]
    if pkg in s.setdefault("apt", {}).get("installed", []):
        out("install ok installed")
        return 0
    return 1


def apt_get(s: dict, a: list[str]) -> int:
    apt = s.setdefault("apt", {})
    if a and a[0] == "install":
        pkgs = [x for x in a[1:] if not x.startswith("-")]
        if set(pkgs) & set(apt.get("fail", [])):
            return 100
        apt.setdefault("installed", []).extend(pkgs)
        save(s)
    return 0


def cloud_init(s: dict, a: list[str]) -> int:
    out({"status": s.get("cloud_init", "done")})
    return 0


def systemctl(s: dict, a: list[str]) -> int:
    if "tailscaled" in a and a[:1] in (["enable"], ["disable"]):
        s.setdefault("tailscale", {})["daemon"] = a[0] == "enable"
        save(s)
        return 0
    if a[:1] == ["is-active"]:
        state = s.get("active", {}).get(a[-1], "inactive")
        out(state)
        return 0 if state == "active" else 3
    words = [w for w in a if not w.startswith("-")]
    if words[:1] in (["enable"], ["disable"], ["is-enabled"]):
        enabled = s.setdefault("enabled_units", [])
        unit = words[1]
        if words[0] == "is-enabled":
            return 0 if unit in enabled else 1
        if words[0] == "enable" and unit not in enabled:
            enabled.append(unit)
        if words[0] == "disable" and unit in enabled:
            enabled.remove(unit)
        save(s)
        return 0
    units = s.get("failed_user" if "--user" in a else "failed_units", [])
    out("\n".join(f"{u} loaded failed failed X" for u in units))
    return 0


def sshd(s: dict, a: list[str]) -> int:
    """`sshd -t`: fails when the scenario says so, or when a configured file is missing."""
    if s.get("sshd_refuses"):
        print(s["sshd_refuses"], file=sys.stderr)
        return 255
    root = os.environ.get("SHIPSHAPE_ROOT", "")
    dropin = Path(root) / "etc/ssh/sshd_config.d/20-shipshape-ca.conf"
    if dropin.exists():
        for line in dropin.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("/") and "%u" not in parts[1]:
                if not (Path(root) / parts[1].lstrip("/")).exists():
                    print(f"{parts[1]}: No such file or directory", file=sys.stderr)
                    return 255
    return 0


def vnstat(s: dict, a: list[str]) -> int:
    if "vnstat" not in s:
        print("Error: Unable to open database", file=sys.stderr)
        return 1
    out(s["vnstat"])
    return 0


def journalctl(s: dict, a: list[str]) -> int:
    out("\n".join(s.get("journal", ["-- No entries --"])))
    return 0


def nft(s: dict, a: list[str]) -> int:
    """nftables: remembers which tables are loaded and the last rules file."""
    n = s.setdefault("nft", {"tables": []})
    if n.get("fail"):
        print("Error: syntax error", file=sys.stderr)
        return 1
    if a[:2] == ["list", "table"]:
        return 0 if " ".join(a[2:4]) in n["tables"] else 1
    if a[:2] == ["delete", "table"]:
        table = " ".join(a[2:4])
        if table not in n["tables"]:
            return 1
        n["tables"].remove(table)
        save(s)
        return 0
    if a[:2] == ["-c", "-f"]:
        return 0
    if a[:1] == ["-f"]:
        text = Path(a[1]).read_text()
        n["loaded"] = text
        n["loads"] = n.get("loads", 0) + 1
        for line in text.splitlines():
            if line.startswith("table ") and line.endswith("{"):
                table = " ".join(line.split()[1:3])
                if table not in n["tables"]:
                    n["tables"].append(table)
        save(s)
        return 0
    return 2


def logger(s: dict, a: list[str]) -> int:
    """`logger -t TAG -- MESSAGE`: the line is kept, as the journal would."""
    s.setdefault("logged", []).append({"tag": a[a.index("-t") + 1], "message": a[-1]})
    save(s)
    return 0


def timedatectl(s: dict, a: list[str]) -> int:
    """`timedatectl show --property=Timezone --value` and `set-timezone ZONE`.
    The cloud image starts on Etc/UTC; `timedatectl_fail` makes both fail."""
    if s.get("timedatectl_fail"):
        print("Failed to connect to bus: No such file or directory", file=sys.stderr)
        return 1
    if a[:1] == ["show"]:
        print(s.get("timezone", "Etc/UTC"))
        return 0
    if a[:1] == ["set-timezone"]:
        s["timezone"] = a[1]
        save(s)
        return 0
    return 2


def ip(s: dict, a: list[str]) -> int:
    """`ip -4|-6 -j route show default`."""
    routes = s.get("routes", {"-4": [{"dst": "default", "gateway": "192.0.2.1"}], "-6": []})
    out(routes.get(a[0], []))
    return 0


def tailscale(s: dict, a: list[str]) -> int:
    """The overlay (phase 5). scenario["tailscale"]: backend, name, ips, tags, up_fails."""
    t = s.setdefault("tailscale", {})
    if a[:1] == ["status"]:
        if not t.get("daemon", False):
            print("failed to connect to local tailscaled", file=sys.stderr)
            return 1
        out({"BackendState": t.get("backend", "NeedsLogin"),
             "Self": {"DNSName": t.get("name", "") + ".", "TailscaleIPs": t.get("ips", []),
                      "Tags": t.get("tags", []), "ID": "nExample"}})
        return 0
    if a[:1] == ["up"]:
        key_arg = next(x for x in a if x.startswith("--auth-key="))
        # Record what the key file held while `up` ran, so tests can check it was there.
        t["key_seen"] = Path(key_arg.removeprefix("--auth-key=file:")).read_text().strip()
        if t.get("up_fails"):
            save(s)
            print(f"backend error: invalid key: {t['key_seen']} not valid", file=sys.stderr)
            return 1
        t.update(backend=t.get("after_up", "Running"), daemon=True,
                 name=t.get("name") or "ship-01.example-tailnet.ts.net",
                 ips=t.get("ips") or ["192.0.2.10", "fd7a:115c:a1e0::a"],
                 tags=[x.split("=", 1)[1] for x in a if x.startswith("--advertise-tags=")])
        save(s)
        return 0
    if a[:1] == ["logout"]:
        t.update(backend="NeedsLogin", name="", ips=[], tags=[])
        save(s)
        return 0
    return 0


def findmnt(s: dict, a: list[str]) -> int:
    print(s.get("disk", {}).get("root", "/dev/sda1"))
    return 0


def lsblk(s: dict, a: list[str]) -> int:
    print(s.get("disk", {}).get("parent", "sda"))
    return 0


def growpart(s: dict, a: list[str]) -> int:
    disk = s.setdefault("disk", {})
    if disk.get("full"):
        print("NOCHANGE: partition 1 is size 50329567. it cannot be grown")
        return 1
    disk["full"] = True
    disk["fs_behind"] = True  # the filesystem is now smaller than its partition
    save(s)
    return 0


def resize2fs(s: dict, a: list[str]) -> int:
    disk = s.setdefault("disk", {})
    if not disk.get("fs_behind"):
        print("The filesystem is already 6291195 (4k) blocks long.  Nothing to do!",
              file=sys.stderr)
        return 0
    disk["fs_behind"] = False
    disk.setdefault("resized", []).append(a[-1])
    save(s)
    return 0


def getent(s: dict, a: list[str]) -> int:
    """`getent ahosts NAME`: answers unless the scenario says DNS is not up yet."""
    if not s.get("dns", True):
        return 2
    print(f"192.0.2.53      STREAM {a[-1]}")
    return 0


def useradd(s: dict, a: list[str]) -> int:
    """Adds the user to the sandbox's /etc/passwd, as the real one would."""
    name = a[-1]
    passwd = Path(os.environ["SHIPSHAPE_ROOT"]) / "etc/passwd"
    passwd.parent.mkdir(parents=True, exist_ok=True)
    with passwd.open("a") as fh:
        home = Path("/home") / name
        fh.write(f"{name}:x:1001:1001:ShipShape owner:{home}:/bin/bash\n")
    return 0


# --- herdr-core (hc) ----------------------------------------------------------------
LAUNCHER = "/opt/shipshape/bin/agent-launch"
SHELLS = ("sh", "bash", "zsh", "fish", "nu", "dash", "ksh", "tcsh")


def hc_error(kind: str, message: str = "x") -> int:
    """hc's error line on stderr, and its exit code for that kind."""
    print(json.dumps({"error": {"kind": kind, "message": message, "hint": ""}}), file=sys.stderr)
    return {"not_found": 3, "unavailable": 5, "invalid_params": 6, "blocked": 9,
            "finished": 11}.get(kind, 1)


def hc_short(t: dict) -> dict:
    """A terminal as `hc ls --json` and `hc run --bg --json` give it."""
    out = {k: t[k] for k in ("address", "name", "state", "exit_code", "reason", "cwd", "turn")
           if t.get(k) is not None}
    if isinstance(t.get("status"), dict):
        out["status"] = t["status"]["state"]
    out["agent"] = (t.get("status") or {}).get("app") or os.path.basename(t["argv"][0])
    return out


def hc_find(h: dict, target: str, live_only: bool = False) -> dict | None:
    """By address, else by name (a live terminal first)."""
    terms = h.get("terminals", [])
    hit = next((t for t in terms if t["address"] == target), None)
    if hit is None:
        named = [t for t in terms if t.get("name") == target]
        hit = next((t for t in named if t["state"] == "live"), None) or (
            None if live_only else next(iter(named), None))
    return hit


def hc_tick(t: dict) -> None:
    """A poll of a terminal: a lost kept one gets its shell after recover_in polls; an
    Escape takes effect after settle_in."""
    if t["state"] == "live" and t.get("foreground") is None and "recover_in" in t:
        t["recover_in"] -= 1
        if t["recover_in"] <= 0:
            del t["recover_in"]
            t["foreground"] = {"pid": 4242, "name": "bash", "argv": ["bash"], "cwd": t["cwd"]}
    if "settle_in" in t:
        t["settle_in"] -= 1
        if t["settle_in"] <= 0:
            del t["settle_in"]
            t["status"] = {"app": "claude-code", "state": "idle"}


def hc_claude(s: dict, h: dict, t: dict, argv: list[str]) -> None:
    """claude starts in t through the launcher with argv: `--resume X` continues X,
    `--session-id X` is a new session X; a lost session ends it at once."""
    rest = argv[1:]
    sid = None
    for flag in ("--resume", "--session-id"):
        if flag in rest:
            sid = rest[rest.index(flag) + 1]
    if "--resume" in rest and sid in h.get("lost_sessions", []):
        t.update(state="finished", exit_code=1, reason="exited", foreground=None, status=None)
        return
    sid = sid or f"sess-{t.get('name')}"
    t["foreground"] = {"pid": 4343, "name": "claude", "argv": ["claude", *rest], "cwd": t["cwd"]}
    t["status"] = {"app": "claude-code", "state": "idle"}
    t["claude_args"] = rest
    t["session"] = sid  # what claude really runs (the label is what ShipShape said)
    ledger_hook({"hook_event_name": "SessionStart", "session_id": sid, "argv": rest,
                 "terminal": t["address"]})


def hc_end(t: dict, code: int = 0) -> None:
    sid = t.get("session")
    t.update(state="finished", exit_code=code, reason="exited", foreground=None, status=None)
    if sid and code == 0:
        ledger_hook({"hook_event_name": "SessionEnd", "session_id": sid,
                     "reason": "prompt_input_exit"})


def hc(s: dict, a: list[str]) -> int:
    h = s.setdefault("hc", {})
    terms = h.setdefault("terminals", [])
    if a in (["--version"], ["-V"]):
        out(f"herdr-core {h.get('version', '0.1.19')}")
        return 0
    cmd, rest = a[0], [x for x in a[1:] if x != "--json"]
    if cmd in ("kill",) or a[:2] == ["server", "stop"]:
        h.setdefault("forbidden", []).append(a)  # tests check ShipShape never does these
        save(s)
        return 0
    if a[:2] == ["server", "start"]:
        h["server"] = True
        save(s)
        out({"version": h.get("version", "0.1.19"), "pid": 1320, "uptime_ms": 9})
        return 0
    if not h.get("server", True):
        return hc_error("unavailable", "cannot start the core")
    if cmd == "ls":
        shown = [t for t in terms if t["state"] == "live" or "--all" in rest]
        for t in shown:
            hc_tick(t)
        save(s)
        out({"terminals": [hc_short(t) for t in shown],
             "hidden_finished": sum(t["state"] != "live" for t in terms) if "--all" not in rest
             else 0})
        return 0
    if cmd == "get":
        t = hc_find(h, rest[0])
        if t is None:
            return hc_error("not_found")
        if t["address"] in h.get("get_fails", []):
            return hc_error("unavailable", "timed out")
        hc_tick(t)
        save(s)
        out({k: v for k, v in t.items() if k not in ("recover_in", "settle_in", "claude_args",
                                                     "session")})
        return 0
    if cmd == "run":
        argv = rest[rest.index("--") + 1:]
        opts = rest[:rest.index("--")]
        name = opts[opts.index("--name") + 1] if "--name" in opts else None
        labels = dict(opts[i + 1].split("=", 1) for i, x in enumerate(opts) if x == "--label")
        if name in h.get("race_run", []):  # another ship-ctl started it a moment before
            h["race_run"].remove(name)
            hc(s, ["run", "--json", *opts, "--", *argv])
            s = load()
            h = s["hc"]
        if name and any(t.get("name") == name and t["state"] == "live" for t in h["terminals"]):
            return hc_error("name_taken", f"{name} is taken")
        if name in h.get("fail_run", []):
            return hc_error("spawn_failed", 'cannot start "x"')
        record = Path(os.environ.get("SHIPSHAPE_ROOT", "/")) / "var/lib/shipshape/agents.json"
        if name and record.is_file():  # what the agent record says as the agent starts
            entry = json.loads(record.read_text()).get("agents", {}).get(name) or {}
            h.setdefault("recorded_at_run", {})[name] = entry.get("session")
        n = h.get("next", 1)
        h["next"] = n + 1
        t = {"address": f"t{n}", "name": name, "argv": argv, "labels": labels,
             "cwd": opts[opts.index("--cwd") + 1] if "--cwd" in opts else "/home/agent",
             "state": "live", "keep": "--keep" in opts, "created_ms": 1, "screen": "exact",
             "cols": 80, "rows": 24, "status": None, "turn": 0, "exit_code": None,
             "reason": None, "foreground": {"pid": 4141, "name": os.path.basename(argv[0]),
                                            "argv": argv, "cwd": None}}
        t["foreground"]["cwd"] = t["cwd"]
        h["terminals"].append(t)
        h.setdefault("runs", []).append(a)
        if argv[:1] == [LAUNCHER]:
            hc_claude(s, h, t, argv)
        save(s)
        out(hc_short(t))
        return 0
    t = hc_find(h, rest[0], live_only=True) if rest else None
    if cmd in ("send", "keys", "label", "rm") and t is None:
        t = hc_find(h, rest[0]) if rest else None
        if t is None:
            return hc_error("not_found")
    if cmd == "rm":
        if t["state"] == "live":
            return hc_error("live", f"{t['address']} is live")
        h["terminals"].remove(t)
        h.setdefault("removed", []).append(t["address"])
        save(s)
        out({"removed": t["address"]})
        return 0
    if cmd == "label":
        for kv in rest[1:]:
            k, _, v = kv.partition("=")
            if v:
                t["labels"][k] = v
            else:
                t["labels"].pop(k, None)
        save(s)
        out(t)
        return 0
    if t["state"] != "live":
        return hc_error("finished", f"{t['address']} has finished")
    if cmd == "keys":
        keys = rest[1:]
        h.setdefault("keys", []).append([t["address"], *keys])
        status = t.get("status") or {}
        if "esc" in keys and status.get("state") in ("working", "blocked"):
            if t.get("name") in h.get("slow_esc", []):
                t["settle_in"] = h.get("slow_esc_polls", 2)
            else:
                t["status"] = {"app": "claude-code", "state": "idle"}
        if keys == ["ctrl+c", "ctrl+c"] and t.get("name") in h.get("exit_on_ctrl_c", []):
            hc_end(t)
        save(s)
        out({"turn": 0, "status": (t.get("status") or {}).get("state")})
        return 0
    if cmd == "send":
        if "--force" in rest:
            h.setdefault("forbidden", []).append(a)
        text = rest[rest.index("--") + 1] if "--" in rest else rest[-1]
        if t.get("foreground") is None:
            return hc_error("unavailable", f"{t['address']} is waiting for its new shell")
        if (t.get("status") or {}).get("state") == "blocked" and "--force" not in rest:
            return hc_error("blocked", f"{t['address']} is blocked")
        if t.get("name") in h.get("fail_send", []):
            return hc_error("unavailable")
        h.setdefault("sent", []).append([t["address"], text])
        fg = t["foreground"]["name"]
        if fg in SHELLS and "exec " in text and t.get("name") in h.get("slow_shell", []):
            pass  # the shell has not read the line yet
        elif fg in SHELLS and "exec " in text:
            import shlex

            words = shlex.split(text)
            if words[:1] == ["cd"]:  # cd -- DIR && exec ...
                t["cwd"] = t["foreground"]["cwd"] = words[2]
            argv = words[words.index("exec") + 1:]
            t["argv_typed"] = argv
            if argv[:1] == [LAUNCHER]:
                hc_claude(s, h, t, argv)
        elif text == "/exit" and fg == "claude":
            if t.get("name") not in h.get("stubborn", []) + h.get("exit_on_ctrl_c", []):
                hc_end(t)
        save(s)
        out({"turn": 1, "status": (t.get("status") or {}).get("state"), "started": None})
        return 0
    return 2


HANDLERS = {
    "herdr": herdr,
    "hc": hc,
    "claude": claude,
    "gcloud": gcloud,
    "gh": gh,
    "git": git,
    "useradd": useradd,
    "findmnt": findmnt,
    "lsblk": lsblk,
    "growpart": growpart,
    "resize2fs": resize2fs,
    "npm": npm,
    "dpkg-query": dpkg_query,
    "apt-get": apt_get,
    "cloud-init": cloud_init,
    "systemctl": systemctl,
    "journalctl": journalctl,
    "visudo": lambda s, a: 0,
    "tailscale": tailscale,
    "sshd": sshd,
    "vnstat": vnstat,
    "nft": nft,
    "ip": ip,
    "timedatectl": timedatectl,
    "logger": logger,
    "getent": getent,
}


def main() -> int:
    name = Path(sys.argv[0]).name
    args = sys.argv[1:]
    with (FAKE / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (FAKE / "calls.jsonl").open("a") as fh:
            fh.write(json.dumps([name, *args]) + "\n")
        # Which user's environment each call ran in (agent=True sets the agent's HOME).
        with (FAKE / "homes.jsonl").open("a") as fh:
            fh.write(json.dumps({"argv": [name, *args], "home": os.environ.get("HOME")}) + "\n")
        return HANDLERS[name](load(), args)


if __name__ == "__main__":
    sys.exit(main())
