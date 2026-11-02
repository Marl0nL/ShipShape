#!/usr/bin/env python3
"""Phase 1 acceptance run (W11): the steps that need a person, guided.

Run it yourself, in your own terminal. The sign-in links and codes go only
between you, your browser and the Ship: nothing is recorded.

    python tests/e2e/acceptance.py --config ~/.config/fleetledger/e2e.toml

The config is a ships.toml with [drivers.proxmox] (the scoped token) and
[tag.e2e] naming the accounts the test Ship should sign in as:

    [tag.e2e]
    claude = "you@example.com"
    gcloud = "you@example.com"
    github = "example-user"

It creates one Ship, signs it in (you approve each sign-in), checks the
accounts against the tag, lets the autostart agents start (one auto, one
bypass), asks you to look at the Claude app, adds a bypass agent with no
prompt of its own, reboots and checks all three came back in their modes,
switches Remote Control off, checks a bypass-default
Ship starts new agents in bypass without a prompt, looks for leaked tokens,
signs out, and destroys the Ship. About 30 to 45 minutes; you are needed for
the sign-ins (a few minutes) and two looks at the Claude app.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shlex
import sys
import tempfile
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_harness import Fleet, log  # noqa: E402

# Token shapes that must never appear in a log, history or audit file.
TOKEN_PATTERNS = (
    r"sk-ant-[A-Za-z0-9_-]{8,}|gh[opusr]_[A-Za-z0-9]{20,}"
    r"|ya29\.[A-Za-z0-9_-]{20,}|1//0[A-Za-z0-9_-]{20,}"
)

results: list[tuple[str, bool, str]] = []


def step(title: str, ok: bool, detail: str = "") -> bool:
    results.append((title, ok, detail))
    line = f"{'PASS' if ok else 'FAIL'}  {title}" + (f"  ({detail})" if detail else "")
    print("\n" + line, flush=True)
    return ok


def ask(question: str) -> bool:
    return input(f"\n>>> {question} [y/n] ").strip().lower() in ("y", "yes")


def agents(view: dict) -> dict[str, dict]:
    doc = view.get("status") or {}
    return {a["name"]: a for a in doc.get("herdr", {}).get("agents", []) if a.get("name")}


def agent_processes(fleet: Fleet, name: str) -> list[str]:
    out = fleet.ssh(name, "bash", "-c", "ps -o args= -u agent | grep -E '(^|/)claude( |$)' || true")
    return [line for line in out.text.splitlines() if line.strip()]


def add_autostart(fleet: Fleet, name: str, entry: str):
    """Append an [[autostart]] entry to the Ship's ship.toml and run autostart."""
    ship_toml = fleet.ssh(name, "cat", "/etc/shipshape/ship.toml").text
    done = fleet.ssh_input(name, ship_toml + "\n[[autostart]]\n" + entry,
                           "sudo", "-n", "/usr/local/bin/ship-ctl", "set", "--file", "-", "--json")
    fleet.ssh(name, "sudo", "-n", "/usr/local/bin/ship-ctl", "autostart", "--json", timeout=300)
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", required=True,
                        help="ships.toml with [drivers.proxmox] and [tag.e2e]")
    parser.add_argument("--keep", action="store_true", help="do not destroy the Ship at the end")
    args = parser.parse_args()
    config = tomllib.loads(Path(args.config).expanduser().read_text())
    if "e2e" not in config.get("tag", {}):
        sys.exit("The config needs a [tag.e2e] section with the accounts to expect.")

    workdir = Path(tempfile.mkdtemp(prefix="shipshape-acceptance-"))
    fleet = Fleet(workdir, config)
    name = f"acc-{secrets.token_hex(2)}"
    try:
        run(fleet, name)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if args.keep:
            print(f"\nKept {name}; FleetLedger home: {workdir}")
        else:
            fleet.destroy(name)
            fleet.close()
    print("\n=== Summary ===")
    for title, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {title}" + (f"  ({detail})" if detail else ""))
    return 0 if results and all(ok for _, ok, _ in results) else 1


def run(fleet: Fleet, name: str) -> None:
    print(f"Creating {name} (about 10 minutes; nothing to do yet).")
    fleet.cli("new", name, "--tag", "e2e", "--driver", "proxmox", "--mode", "auto",
              "--allow-bypass", "--autostart", "main:~/work/main:auto",
              "--autostart", "grinder:~/work/grinder:bypassPermissions", timeout=3600)
    view = fleet.status(name)
    step("Fresh Ship reaches needs-sign-in with no manual SSH", view["state"] == "needs-sign-in",
         view["state"])

    print("\nNow sign the Ship in. For each service, open the link, approve, and paste the "
          "code (or type the device code on GitHub's page).")
    proc = fleet.cli("login", name, "all", interactive=True, check=False, timeout=3600)
    view = fleet.status(name)
    auth = (view.get("status") or {}).get("auth", {})
    wrong = [a for a in view["attention"] if a["code"] == "signin-wrong-account"]
    states = {k: v["state"] for k, v in auth.items()}
    step("Claude, gcloud (with ADC) and GitHub sign in through ship-fleet login",
         proc.returncode == 0 and all(states.get(s) == "ok" for s in
                                      ("claude", "gcloud", "gcloud_adc", "github")), str(states))
    step("Signed-in accounts match the tag", not wrong, "; ".join(a["message"] for a in wrong))

    with fleet.store.edit() as ed:
        real = ed.doc["tag"]["e2e"]["claude"]
        ed.doc["tag"]["e2e"]["claude"] = "someone-else@example.com"
    flagged = any(a["code"] == "signin-wrong-account" and a.get("subject") == "claude"
                  for a in fleet.status(name)["attention"])
    with fleet.store.edit() as ed:
        ed.doc["tag"]["e2e"]["claude"] = real
    step("A Ship on another account than its tag expects is flagged as the wrong account",
         flagged)

    log("waiting for the autostart agents (main: auto, grinder: bypass)")
    view = fleet.wait_for(
        "both autostart agents",
        lambda: (v := fleet.status(name)) and {"main", "grinder"} <= set(agents(v)) and v,
        timeout=600,
    )
    found = agents(view)
    step("Autostart starts each agent in its own mode",
         found["main"]["mode"] == "auto" and found["grinder"]["mode"] == "bypassPermissions",
         f"main {found['main']['mode']}, grinder {found['grinder']['mode']}")
    step("With Remote Control on, both sessions show in the Claude app",
         ask(f"Open the Claude app (Code tab). Do two sessions starting with '{name}' show?"))

    # A bypass agent whose autostart entry has no prompt of its own: ship-ctl
    # sends the default first prompt, so the ledger records its mode.
    add_autostart(fleet, name, 'name = "silent"\ncwd = "~/work/silent"\n'
                  'mode = "bypassPermissions"\n')
    view = fleet.wait_for(
        "the no-prompt agent in the ledger",
        lambda: (v := fleet.status(name)) and agents(v).get("silent", {}).get("mode") and v,
        timeout=300,
    )
    step("An autostart agent with no prompt of its own is recorded in its mode",
         agents(view)["silent"]["mode"] == "bypassPermissions")

    fleet.reboot(name)
    trio = {"main", "grinder", "silent"}
    view = fleet.wait_for(
        "agents back after the reboot",
        lambda: (v := fleet.status(name)) and trio <= set(agents(v))
        and v["status"]["herdr"]["last_resume"] and v,
        timeout=600,
    )
    found = agents(view)
    procs = agent_processes(fleet, name)
    resumed = [p for p in procs if "--resume" in p]
    bypassed = [p for p in resumed if "--dangerously-skip-permissions" in p]
    step("All three agents survive a reboot with nobody attached, each in its own mode "
         "(the no-prompt bypass agent included)",
         found["main"]["mode"] == "auto" and found["grinder"]["mode"] == "bypassPermissions"
         and found["silent"]["mode"] == "bypassPermissions"
         and len(resumed) >= 3 and len(bypassed) == 2,
         f"resumed {len(resumed)}, with bypass flag {len(bypassed)}; "
         f"last_resume {view['status']['herdr']['last_resume']}")
    step("Remote Control reconnects after the reboot",
         ask(f"In the Claude app, are the '{name}' sessions connected again (three now)?"))

    fleet.cli("set", name, "remote_control=off")
    fleet.reboot(name)
    fleet.wait_for("agents back", lambda: trio <= set(agents(fleet.status(name))), timeout=600)
    step("With Remote Control off, neither session shows",
         ask(f"Give it a minute. Are the '{name}' sessions now gone or disconnected in the app?"))

    fleet.cli("set", name, "remote_control=on", "default_mode=bypassPermissions")
    done = add_autostart(fleet, name, 'name = "fresh"\ncwd = "~/work/fresh"\n')
    time.sleep(45)
    fresh = agents(fleet.status(name)).get("fresh", {})
    procs = [p for p in agent_processes(fleet, name) if "--resume" not in p]
    step("A bypass-default Ship starts new agents in bypass with no warning dialog",
         done.returncode == 0 and fresh.get("mode") == "bypassPermissions"
         and fresh.get("state") != "blocked"
         and any("--dangerously-skip-permissions" in p for p in procs),
         f"fresh: {fresh.get('mode')}, {fresh.get('state')}")

    sweep = (
        "{ journalctl -o cat --no-pager; cat /var/log/shipshape/*.log /home/*/.bash_history "
        "/root/.bash_history 2>/dev/null; } | grep -cE " + shlex.quote(TOKEN_PATTERNS) + " || true"
    )
    leaks = fleet.ssh(name, "sudo", "-n", "bash", "-c", sweep, timeout=300).text.strip()
    local = sum(len(re.findall(TOKEN_PATTERNS, p.read_text(errors="replace")))
                for p in (fleet.workdir / "data").rglob("*") if p.is_file())
    step("No code or token in any log or shell history, on either side",
         leaks in ("0", "") and local == 0,
         f"Ship matches {leaks or 0}, FleetLedger matches {local}")

    fleet.cli("logout", name, "all", check=False)
    auth = fleet.status(name)["status"]["auth"]
    step("Signing out clears every sign-in",
         all(auth[s]["state"] != "ok" for s in ("claude", "gcloud", "github")),
         json.dumps({k: v["state"] for k, v in auth.items()}))


if __name__ == "__main__":
    sys.exit(main())
