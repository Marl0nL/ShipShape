#!/usr/bin/env python3
"""Phase 1 acceptance run (W11): the steps that need a person, guided.

Run it yourself, in your own terminal. The sign-in links and codes go only
between you, your browser and the Ship: nothing is recorded.

    python tests/e2e/acceptance.py --config ~/.config/fleetledger/e2e.toml

Or, with the person elsewhere, in relay mode (--relay-links, --relay-inbox,
--relay-status; see Relay below). Then the sign-in links and GitHub's device
code are written to the links file (mode 0600), which is deleted when the run
ends; pasted codes arrive in inbox messages and are scrubbed once used.

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
import os
import re
import secrets
import signal
import sys
import tempfile
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_harness import Fleet, log  # noqa: E402

# Token shapes, plus Google's authorization-code shape, that must never appear
# in a log, history or audit file. (GitHub's and Claude's one-time codes are too
# plain to search for.)
TOKEN_PATTERNS = (
    r"sk-ant-[A-Za-z0-9_-]{8,}|gh[opusr]_[A-Za-z0-9]{20,}"
    r"|ya29\.[A-Za-z0-9_-]{20,}|1//0[A-Za-z0-9_-]{20,}|4/0A[A-Za-z0-9_-]{20,}"
)

results: list[tuple[str, bool, str]] = []


def step(title: str, ok: bool, detail: str = "") -> bool:
    results.append((title, ok, detail))
    line = f"{'PASS' if ok else 'FAIL'}  {title}" + (f"  ({detail})" if detail else "")
    print("\n" + line, flush=True)
    return ok


RELAY: Relay | None = None  # set by --relay-*: someone else relays links and answers


def ask(question: str) -> bool:
    if RELAY is not None:
        return RELAY.ask(question)
    return input(f"\n>>> {question} [y/n] ").strip().lower() in ("y", "yes")


class Relay:
    """Run with nobody at this terminal: a person reached through a relay.

    - Sign-in links (and GitHub's device code) are appended to a 0600 links
      file, announced with a line in a status file.
    - Pasted codes arrive as message files (*.msg) in an inbox folder, as a line
      `claude: <code>` or `gcloud: <code>`. A code is never printed; once read
      it is scrubbed from the message, which then moves to inbox/handled/.
    - Questions about the Claude app go to the status file as needs-decision
      lines; answers arrive as a line `answer: yes` or `answer: no`.
    """

    def __init__(self, links: Path, inbox: Path, status: Path) -> None:
        self.links, self.inbox, self.status = links, inbox, status
        fd = os.open(links, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        os.chmod(links, 0o600)
        self.questions = 0

    def say(self, state: str, text: str) -> None:
        with self.status.open("a") as fh:
            fh.write(f"{state}: {text}\n")

    def write_links(self, text: str) -> None:
        # Opened with 0600 on every write, in case the file was removed meanwhile.
        fd = os.open(self.links, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(text.rstrip() + "\n\n")

    def close(self) -> None:
        self.links.unlink(missing_ok=True)

    def take(self, pattern: re.Pattern[str], what: str, timeout: float) -> str | None:
        """The first group of the first inbox message matching pattern."""
        deadline = time.monotonic() + timeout
        noted: set[str] = set()
        while time.monotonic() < deadline:
            for msg in sorted(self.inbox.glob("*.msg")):
                text = msg.read_text(errors="replace")
                body = text.split("\n--\n", 1)[-1]
                match = pattern.search(body)
                if not match:
                    if msg.name not in noted:
                        print(f"(waiting for {what}; leaving {msg.name} in the inbox)", flush=True)
                        noted.add(msg.name)
                    continue
                value = match.group(1)
                head = text[: len(text) - len(body)]
                # Scrub only the matched value, not every look-alike in the message.
                body = body[: match.start(1)] + "[used]" + body[match.end(1) :]
                msg.write_text(head + body)
                # A message can carry more than one code (or an answer too): it
                # stays in the inbox until every one of them has been used.
                if not UNUSED.search(body):
                    handled = self.inbox / "handled"
                    handled.mkdir(exist_ok=True)
                    msg.rename(handled / msg.name)
                return value
            time.sleep(3)
        return None

    def ask(self, question: str) -> bool:
        self.questions += 1
        self.say("needs-decision", f"[key=acceptance-app-{self.questions}] {question} "
                 "Reply with a line 'answer: yes' or 'answer: no'.")
        print(f"\n>>> asked through the relay: {question}", flush=True)
        answer = self.take(re.compile(r"^\s*answer\s*:\s*(yes|no)\b", re.M | re.I),
                           "an answer", timeout=3600)
        return (answer or "").lower() == "yes"


# A relay line not yet used: a code or an answer whose value is not "[used]".
UNUSED = re.compile(r"^\s*(?:claude|gcloud|gcloud-adc|answer)\s*:\s*(?!\[used\])\S", re.M | re.I)

LABEL_KEYS = {"Claude Code": "claude", "gcloud and ADC": "gcloud", "Google ADC": "gcloud-adc",
              "GitHub": "github"}


class RelayPrompter:
    """The concierge's prompter, through a Relay (see TerminalPrompter)."""

    def __init__(self, relay: Relay, ship: str, label: str) -> None:
        self.relay, self.ship, self.label = relay, ship, label
        self.key = LABEL_KEYS.get(label, label.lower())

    def show_link(self, url: str, code: str | None, expires_in: float) -> None:
        until = time.strftime("%H:%M", time.localtime(time.time() + expires_in))
        lines = [f"== {self.label} sign-in for {self.ship} (valid until about {until})",
                 f"Open: {url}"]
        if code:
            lines.append(f"Enter this code on that page: {code}")
        else:
            lines.append(f"Approve, then send the code the page shows as:  {self.key}: <code>")
        self.relay.write_links("\n".join(lines))
        self.relay.say("working", f"acceptance: {self.label} sign-in link ready in "
                       f"{self.relay.links} (valid until about {until})")
        print(f"{self.label}: link written to the links file", flush=True)

    def ask_code(self) -> str | None:
        pattern = re.compile(rf"^\s*{re.escape(self.key)}\s*:\s*(?!\[used\])(\S+)\s*$", re.M)
        code = self.relay.take(pattern, f"the {self.key} code", timeout=900)
        print(f"{self.label}: code received" if code else f"{self.label}: no code", flush=True)
        return code

    def waiting(self, remaining: float) -> None:
        pass

    def offer_live(self, reason: str, seen: str) -> bool:
        self.relay.write_links(f"!! {self.label}: {reason} (sign-in abandoned)")
        return False


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
    parser.add_argument("--relay-links", type=Path, help="relay mode: file for sign-in links")
    parser.add_argument("--relay-inbox", type=Path, help="relay mode: folder of *.msg replies")
    parser.add_argument("--relay-status", type=Path, help="relay mode: file for status lines")
    args = parser.parse_args()
    relay_args = (args.relay_links, args.relay_inbox, args.relay_status)
    if any(relay_args) and not all(relay_args):
        sys.exit("Relay mode needs --relay-links, --relay-inbox and --relay-status.")
    if all(relay_args):
        global RELAY
        RELAY = Relay(*relay_args)
        RELAY.write_links(f"===== acceptance run started {time.strftime('%H:%M')} =====")

    def stop(signum, frame):
        raise KeyboardInterrupt  # so a plain `kill` still destroys the Ship

    signal.signal(signal.SIGTERM, stop)
    config = tomllib.loads(Path(args.config).expanduser().read_text())
    if "proxmox" not in config.get("drivers", {}):
        sys.exit("The config needs a [drivers.proxmox] section.")
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
        if RELAY is not None:
            RELAY.close()  # the links file: sign-in URLs and device codes
        if args.keep:
            print(f"\nKept {name}; FleetLedger home: {workdir}")
        else:
            fleet.destroy(name)
            fleet.close()
    print("\n=== Summary ===")
    for title, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {title}" + (f"  ({detail})" if detail else ""))
    return 0 if results and all(ok for _, ok, _ in results) else 1


def relay_login(fleet: Fleet, name: str, attempts: int = 3) -> int:
    """Sign in service by service; a link that lapses is replaced with a new one."""
    from fleetledger import cli

    app = cli.App(prompter_factory=lambda ship, label, _open: RelayPrompter(RELAY, ship, label))
    failed = 0
    for service in ("claude", "gcloud", "github"):
        for attempt in range(1, attempts + 1):
            code = cli.main(["login", name, service], app)
            if code == 130:
                raise KeyboardInterrupt
            auth = (fleet.status(name).get("status") or {}).get("auth", {})
            if code == 0 or (auth.get(service) or {}).get("state") == "ok":
                break
            more = "a new link is coming" if attempt < attempts else "giving up on it"
            RELAY.say("working", f"acceptance: the {service} sign-in did not complete "
                      f"(attempt {attempt} of {attempts}); {more}.")
        else:
            failed += 1
    return 1 if failed else 0


def config_tag(fleet: Fleet) -> dict:
    return dict(fleet.store.load().doc.get("tag", {}).get("e2e", {}))


def run(fleet: Fleet, name: str) -> None:
    print(f"Creating {name} (about 10 minutes; nothing to do yet).")
    fleet.cli("new", name, "--tag", "e2e", "--driver", "proxmox",
              "--vmid", str(fleet.fresh_vmid()), "--mode", "auto",
              "--allow-bypass", "--autostart", "main:~/work/main:auto",
              "--autostart", "grinder:~/work/grinder:bypassPermissions", timeout=3600)
    view = fleet.status(name)
    step("Fresh Ship reaches needs-sign-in with no manual SSH", view["state"] == "needs-sign-in",
         view["state"])

    print("\nNow sign the Ship in. For each service, open the link, approve, and paste the "
          "code (or type the device code on GitHub's page).")
    if RELAY is None:
        login_code = fleet.cli("login", name, "all", interactive=True, check=False,
                               timeout=3600).returncode
    else:
        login_code = relay_login(fleet, name)
    view = fleet.status(name)
    expected = config_tag(fleet)
    if "github" not in expected:
        # No GitHub login given: take the one now signed in as the expected one.
        account = ((view.get("status") or {}).get("auth", {}).get("github") or {}).get("account")
        if account:
            with fleet.store.edit() as ed:
                ed.doc["tag"]["e2e"]["github"] = account
            view = fleet.status(name)
    auth = (view.get("status") or {}).get("auth", {})
    wrong = [a for a in view["attention"] if a["code"] == "signin-wrong-account"]
    states = {k: v["state"] for k, v in auth.items()}
    step("Claude, gcloud (with ADC) and GitHub sign in through ship-fleet login",
         login_code == 0 and all(states.get(s) == "ok" for s in
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
    step("With Remote Control off, the sessions drop out of the app",
         ask(f"The app can take a few minutes. Are the '{name}' sessions now gone or "
             "disconnected?"))

    fleet.cli("set", name, "remote_control=on", "default_mode=bypassPermissions")
    done = add_autostart(fleet, name, 'name = "fresh"\ncwd = "~/work/fresh"\n')
    fleet.wait_for("the fresh agent", lambda: agents(fleet.status(name)).get("fresh", {})
                   .get("mode"), timeout=300)
    time.sleep(20)  # long enough for a permission dialog to show as "blocked"
    fresh = agents(fleet.status(name)).get("fresh", {})
    procs = [p for p in agent_processes(fleet, name) if "--resume" not in p]
    step("A bypass-default Ship starts new agents in bypass with no warning dialog",
         done.returncode == 0 and fresh.get("mode") == "bypassPermissions"
         and fresh.get("state") != "blocked"
         and any("--dangerously-skip-permissions" in p for p in procs),
         f"fresh: {fresh.get('mode')}, {fresh.get('state')}")

    # The patterns go in on stdin, so they never appear on a command line (sudo
    # logs command lines to the journal the sweep reads).
    # Per source: "<count> <source>" for each source with a match. Only counts and
    # file names come back, never the matching text.
    sweep = (
        'p="$(mktemp)"; cat > "$p"; sudo -n bash -c "'
        "n=\\$(journalctl -o cat --no-pager | grep -cEf $p); "
        "[ \\$n -gt 0 ] && echo \\$n journal; "
        "grep -cEf $p /var/log/shipshape/*.log /home/*/.bash_history /root/.bash_history "
        "/home/agent/.config/gcloud/logs/*/*.log 2>/dev/null | grep -v ':0$' "
        "| sed -E 's/^(.*):([0-9]+)$/\\2 \\1/'; true\"; "
        'rm -f "$p"'
    )
    found = fleet.ssh_input(name, TOKEN_PATTERNS + "\n", "bash", "-c", sweep,
                            timeout=300).text.split("\n")
    found = [line.strip() for line in found if line.strip()]
    leaks = sum(int(line.split()[0]) for line in found if line.split()[0].isdigit())
    local = sum(len(re.findall(TOKEN_PATTERNS, p.read_text(errors="replace")))
                for p in (fleet.workdir / "data").rglob("*") if p.is_file())
    step("No token or Google sign-in code in any log or shell history, on either side",
         leaks == 0 and local == 0,
         f"Ship matches {leaks} ({'; '.join(found) or 'none'}), FleetLedger matches {local}")

    fleet.cli("logout", name, "all", check=False)
    auth = fleet.status(name)["status"]["auth"]
    step("Signing out clears every sign-in",
         all(auth[s]["state"] != "ok" for s in ("claude", "gcloud", "github")),
         json.dumps({k: v["state"] for k, v in auth.items()}))


if __name__ == "__main__":
    sys.exit(main())
