"""The login concierge (W9): sign a Ship in to a service from your computer.

For each service FleetLedger runs the tool's own no-browser sign-in on the Ship,
as agent, in a terminal over SSH; shows you the link (and device code); types
the code you paste into that terminal; then checks the account now signed in
against the one the Ship's tags expect. Tokens are created on the Ship and stay
there; only the one-time code crosses the network. The audit log records who,
what and when, never a secret.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from fleetledger import signinreg
from fleetledger.concierge import audit
from fleetledger.concierge.engine import Child, Flow, FlowResult, Prompter, spawn
from fleetledger.concierge.providers import Check, Provider
from fleetledger.concierge.redact import clean
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory, Ship
from fleetledger.shipctl import ShipCtl
from fleetledger.ssh import SSH, as_agent

__all__ = ["CheckResult", "LoginResult", "login", "logout", "verify"]


@dataclass(frozen=True)
class CheckResult:
    service: str
    signed_in: bool
    account: str | None
    matches: bool | None  # None: no account, or no expectation
    expected: tuple[str, ...] = ()


@dataclass
class LoginResult:
    ship: str
    provider: str
    outcome: str  # ok | wrong-account | failed | timeout | cancelled
    detail: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def account(self) -> str | None:
        accounts = [c.account for c in self.checks if c.account]
        return accounts[0] if accounts else None


def _json_path(doc: object, path: str) -> object:
    for key in path.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(key)
    return doc


def run_check(ssh: SSH, inventory: Inventory, ship: Ship, check: Check) -> CheckResult:
    result = ssh.run(ship, as_agent(check.run), timeout=90)
    # Escapes off first: a login printed in bold (or a login shell's OSC 3008
    # terminal-context line) must never end up inside the account name.
    text = clean(result.text)
    signed_in = result.returncode == 0
    account = None
    doc = None
    if check.ok_json or (check.account or "").startswith("json:"):
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            doc = None
            signed_in = False
    if signed_in and check.ok_json:
        signed_in = bool(_json_path(doc, check.ok_json))
    if signed_in and check.account == "stdout":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        account = lines[0] if lines else None
    elif signed_in and check.account and check.account.startswith("json:"):
        value = _json_path(doc, check.account[5:])
        account = str(value) if value else None
    expected = inventory.expected_account(ship, check.service)
    matches = expected.matches(account) if signed_in else None
    return CheckResult(check.service, signed_in, account, matches, tuple(sorted(expected.accounts)))


def verify(ssh: SSH, inventory: Inventory, ship: Ship, provider: Provider) -> list[CheckResult]:
    return [run_check(ssh, inventory, ship, c) for c in provider.checks]


def _outcome(checks: list[CheckResult]) -> str:
    if not all(c.signed_in for c in checks):
        return "failed"
    if any(c.matches is False for c in checks):
        return "wrong-account"
    return "ok"


def login(
    ssh: SSH,
    inventory: Inventory,
    ship: Ship,
    provider: Provider,
    prompter: Prompter,
    *,
    spawner: Callable[[list[str]], Child] = spawn,
    flow_options: dict | None = None,
) -> LoginResult:
    # Checks the pinned host key and opens the shared connection the terminal reuses.
    reached = ssh.run(ship, ["true"], timeout=60)
    provider, said = with_chosen_scopes(ssh, inventory, ship, provider)
    argv = ssh.argv(ship, reached.host, as_agent(provider.run), tty=True)
    flow: FlowResult = Flow(provider, spawner(argv), prompter, **(flow_options or {})).run()

    result = LoginResult(ship.name, provider.id, flow.outcome, flow.detail, notes=said)
    if flow.outcome in ("completed", "manual"):
        for command in provider.after:
            done = ssh.run(ship, as_agent(command), timeout=120)
            if done.returncode != 0:
                result.notes.append(f"'{command}' exited {done.returncode}.")
        result.checks = verify(ssh, inventory, ship, provider)
        result.outcome = _outcome(result.checks)
        if result.outcome == "failed" and not result.detail:
            result.detail = "The sign-in finished, but the check says it is not signed in."
    audit.record(ship.name, "login", provider.id, result.outcome, result.account)
    if result.outcome == "ok":
        result.notes += notify_core_agents(ssh, ship, provider)
    return result


def with_chosen_scopes(ssh: SSH, inventory: Inventory, ship: Ship,
                       provider: Provider) -> tuple[Provider, list[str]]:
    """The sign-in that gets the Ship's token the extra scopes it chose (ghscopes).

    From the Ship's status as it is now: signed in with other scopes, the token is
    refreshed in place (only the chosen extras added or dropped, the account kept);
    otherwise a plain sign-in asks for them. A provider without scopes is as it is."""
    if not provider.takes_scopes:
        return provider, []
    from fleetledger import ghscopes, status

    doc = status.fetch(ssh, inventory, ship).status
    wanted = ghscopes.chosen(ship, doc)
    differ = ghscopes.difference(wanted, ghscopes.granted(doc))
    if differ:
        changes = ([f"add {', '.join(differ.missing)}"] if differ.missing else []) + (
            [f"drop {', '.join(differ.extra)}, which the Ship does not choose"]
            if differ.extra else [])
        return (provider.refreshing(differ.missing, differ.extra),
                [f"This sign-in refreshes the token's scopes, to {' and '.join(changes)} "
                 "(gh auth refresh)."])
    if wanted:
        return (provider.with_scopes(wanted),
                [f"This sign-in asks for the extra scopes {', '.join(wanted)}."])
    return provider, []


# Providers `ship-ctl notify-signin` knows (contracts/ship-ctl.md): every registered flow.
NOTIFY_SERVICES = signinreg.registry().provider_ids


def notify_core_agents(ssh: SSH, ship: Ship, provider: Provider) -> list[str]:
    """Tell the Ship's core ([[autostart]]) agents the sign-in is there now.

    Agents started before it may already have looked and found it missing. Only
    the service name crosses the wire: the Ship reads the account itself. A
    failure here never fails the sign-in; it comes back as a note.
    """
    if provider.id not in NOTIFY_SERVICES:
        return []
    try:
        done = ShipCtl(ssh, ship).call("notify-signin", provider.id, timeout=90)
    except FleetError as exc:
        return [f"The Ship's agents were not told about this sign-in: {exc.message}"]
    sent = done.data.get("sent") or []
    notes = [f"Told {', '.join(sent)} that {provider.label} is signed in."] if sent else []
    notes += [f"{s.get('name')} was not told ({s.get('reason')})."
              for s in done.data.get("skipped") or []]
    return notes


def logout(ssh: SSH, ship: Ship, provider: Provider) -> bool:
    done = ssh.run(ship, as_agent(provider.logout), timeout=120)
    ok = done.returncode == 0
    audit.record(ship.name, "logout", provider.id, "ok" if ok else "failed")
    return ok
