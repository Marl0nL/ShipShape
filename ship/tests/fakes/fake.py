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
        out({"id": "cli", "result": {"type": "agent_list", "agents": agents}})
        return 0
    if a[:2] == ["workspace", "create"]:
        n = h.get("next_ws", 1)
        h["next_ws"] = n + 1
        h.setdefault("workspaces", []).append({"id": f"w{n}", "args": a[2:]})
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
        rest = a[a.index("--") + 1 :] if "--" in a else []
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
                    "value": f"sess-{name}",
                },
                "args": rest,
            }
        )
        save(s)
        out({"result": {"type": "agent_info"}})
        return 0
    if a[:2] == ["agent", "prompt"]:
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
            print(
                f"github.com\n  ✓ Logged in to github.com account {login} (keyring)\n"
                "  - Token: gho_************************************",
                file=sys.stderr,
            )
            return 0
        print("You are not logged into any GitHub hosts.", file=sys.stderr)
        return 1
    return 2


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


def journalctl(s: dict, a: list[str]) -> int:
    out("\n".join(s.get("journal", ["-- No entries --"])))
    return 0


HANDLERS = {
    "herdr": herdr,
    "claude": claude,
    "gcloud": gcloud,
    "gh": gh,
    "npm": npm,
    "dpkg-query": dpkg_query,
    "apt-get": apt_get,
    "cloud-init": cloud_init,
    "systemctl": systemctl,
    "journalctl": journalctl,
    "visudo": lambda s, a: 0,
    "sshd": sshd,
}


def main() -> int:
    name = Path(sys.argv[0]).name
    args = sys.argv[1:]
    with (FAKE / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (FAKE / "calls.jsonl").open("a") as fh:
            fh.write(json.dumps([name, *args]) + "\n")
        return HANDLERS[name](load(), args)


if __name__ == "__main__":
    sys.exit(main())
