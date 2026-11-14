"""What the web UI can change on a Ship, in one place.

The UI never reimplements an operation: settings, tags, logs, update, reboot
and joining the overlay are the shared day-2 operations (fleetledger.ops) that
`ship-fleet` runs too; sign-out is the `ship-fleet logout` command itself. So
the CLI and the UI always do exactly the same thing.

Update, reboot and join take minutes, so they run in the background as jobs
(web.jobs): the page that starts one returns at once, and the Fleet page's Activity
and the Ship's page show each one's progress and outcome.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
from collections.abc import Callable
from typing import Any

from fleetledger import commands, ops
from fleetledger.errors import FleetError


@dataclasses.dataclass(frozen=True)
class Outcome:
    ship: str
    ok: bool
    summary: str
    details: tuple[str, ...] = ()
    refused: bool = False  # a shutdown lock refused its reboot: offer Force reboot

    @classmethod
    def of(cls, outcome: ops.Outcome) -> Outcome:
        reboot = outcome.data.get("reboot") or {}  # an update's reboot
        refused = "refused" in outcome.data or "refused" in reboot
        return cls(outcome.ship, outcome.ok, outcome.summary, tuple(outcome.details), refused)


def _fleet(app: Any) -> ops.Fleet:
    return ops.Fleet.from_app(app)


def _guard(ship: str, call: Callable[[], list[ops.Outcome]]) -> Outcome:
    """One Ship's outcome; a refusal before any Ship was touched becomes a failure."""
    try:
        results = call()
    except FleetError as exc:
        return Outcome(ship, False, exc.message + (f" {exc.hint}" if exc.hint else ""))
    return Outcome.of(results[0]) if results else Outcome(ship, True, "No change.")


def set_settings(app: Any, ship: str, assignments: list[str]) -> Outcome:
    fleet = _fleet(app)
    return _guard(ship, lambda: ops.set_settings(fleet, [fleet.inventory.ship(ship)],
                                                 assignments))


def set_agent_remote_control(app: Any, ship: str, agent: str, on: bool) -> Outcome:
    fleet = _fleet(app)
    return _guard(ship, lambda: [ops.set_agent_remote_control(
        fleet, fleet.inventory.ship(ship), [agent], on)])


def set_tags(app: Any, ship: str, tags: list[str]) -> Outcome:
    changes = ["none", *(f"+{t}" for t in dict.fromkeys(tags))]
    fleet = _fleet(app)
    return _guard(ship, lambda: ops.set_tags(fleet, [fleet.inventory.ship(ship)], changes))


def logs(app: Any, ship: str, source: str, lines: int = 300) -> tuple[bool, list[str]]:
    fleet = _fleet(app)
    try:
        return True, ops.logs(fleet, fleet.inventory.ship(ship), source, lines)
    except FleetError as exc:
        return False, [exc.message]


def logout(app: Any, ship: str, service: str) -> Outcome:
    """`ship-fleet logout` with its output captured (never on the server's stdout)."""
    from fleetledger.web.jobs import BrowserApp

    buf = io.StringIO()
    sub = BrowserApp(app, buf)
    try:
        code = commands.cmd_logout(argparse.Namespace(name=ship, service=service), sub) or 0
    except FleetError as exc:
        return Outcome(ship, False, exc.message + (f" {exc.hint}" if exc.hint else ""))
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    return Outcome(ship, code == 0, lines[0] if lines else "Done.", tuple(lines[1:]))


# --- update, reboot and join, in the background (web.jobs) -------------------


KINDS = ("update", "reboot", "join")  # what start_operation runs
TITLES = {"update": "Update", "reboot": "Reboot", "join": "Overlay join"}


def _join(app: Any, ships, event) -> list[ops.Outcome]:
    """Join Ships to the overlay from the browser (keys from the API: no terminal)."""
    from fleetledger import access, trustcmd

    store = app.store_factory()
    results = []
    for ship in ships:
        said: list[str] = []
        try:
            access.join_one(app, store, ship.name, say=said.append)
            ok, summary = True, "on the overlay"
        except FleetError as exc:
            ok, summary = False, f"not joined: {exc.message}"
        for line in said:
            event(ship.name, line.removeprefix(f"{ship.name}: "))
        results.append(ops.Outcome(ship.name, ok, summary, said))
    try:  # this computer's SSH config now prefers the overlay names
        trustcmd.trust_this_computer(app, store, lambda line: event("*", line), create=False)
    except FleetError:
        pass
    return results


def start_operation(jobs: Any, app: Any, kind: str, ships: list[str],
                    force: bool = False) -> Any:
    """Update, reboot or join these Ships as a background job (refused if one of
    them is busy). force: reboot even past a shutdown lock (ops.reboot_one)."""
    from fleetledger.web.jobs import BrowserApp

    if kind not in KINDS:
        raise FleetError(f"Unknown operation '{kind}'.")
    if force and kind != "reboot":
        raise FleetError("Only a reboot can be forced.")

    def run(job) -> None:
        def event(ship: str, text: str) -> None:
            job.write(text if ship == "*" else f"{ship}: {text}")

        quiet = BrowserApp(app, job)
        fleet = _fleet(quiet)
        inv = fleet.inventory
        chosen = [inv.ship(name) for name in ships]
        # In the background, so wait: the outcome then says whether each Ship came
        # back with its agents in their modes, as the CLI does.
        if kind == "join":
            results = _join(quiet, chosen, event)
        elif kind == "update":
            results = ops.update(fleet, chosen, reboot_policy="auto", wait=True,
                                 on_event=event)
        else:
            results = ops.reboot(fleet, chosen, wait=True, on_event=event, force=force)
        job.outcomes = [Outcome.of(r) for r in results]
        job.ok = all(o.ok for o in job.outcomes)

    return jobs.start(kind, list(ships), run, "Force reboot" if force else TITLES[kind])


def tool_choices(app: Any, view: Any) -> dict[str, Any]:
    """The Settings tab's Tools: the catalogue's tools, each with its state on the
    Ship (status `tools`; None while the Ship has not reported). No checkout: none.

    `toolkits`: your toolkits' tools, grouped by toolkit, beside them. On a Ship's page
    a toolkit's tools are a choice where the Ship may have the toolkit (its only_tags),
    and say why not where it may not, or where its tooling predates toolkits; on the
    fleet's page (no view) they are listed, to add per Ship."""
    from fleetledger import toolkitcmd
    from fleetledger.catalogue import Catalogue

    try:
        try:
            inv = toolkitcmd.inventory_or_none(app)
        except FleetError:
            inv = None  # the catalogue still lists; the page says what is wrong elsewhere
        catalogue = Catalogue.from_repo(app.repo_factory(), inv)
    except FleetError as exc:
        return {"list": [], "error": exc.message, "known": False, "toolkits": []}
    doc = view.status if view is not None else None
    on_ship = (doc or {}).get("tools") or {}
    recipes = sorted(catalogue.recipes.values(), key=lambda r: r.id)
    ship = view.ship if view is not None and inv is not None else None
    return {"list": [(r.id, r.description, (on_ship.get(r.id) or {}).get("state"))
                     for r in recipes if r.toolkit is None],
            "error": None, "known": doc is not None,
            "toolkits": toolkit_groups(catalogue, inv, ship, doc)}


def toolkit_groups(catalogue: Any, inv: Any = None, ship: Any = None,
                   doc: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Each toolkit with its tools, in id order; one that does not load says why.

    With a Ship (and its status), each group says whether its tools are a choice there:
    `why_not` is None when they are, else the reason; each tool has its state there."""
    on_ship = (doc or {}).get("tools") or {}
    groups = []
    for kit in catalogue.toolkits.values():
        why = "Add them on a Ship's Settings, or with ship-fleet tools add."
        if ship is not None and doc is not None:
            from fleetledger.toolkitpush import why_not

            why = why_not(kit.config, inv.effective_tags(ship), ship.name)
            if why is None and "toolkits" not in doc:
                why = (f"{ship.name}'s tooling predates toolkits: update it first "
                       f"(ship-fleet update {ship.name}).")
            elif why is not None:
                why = why[:1].upper() + why[1:] + "."
        groups.append({
            "id": kit.id, "version": kit.version, "description": kit.description,
            "error": None, "why_not": why,
            "tools": [(r.id, r.description, (on_ship.get(r.id) or {}).get("state"))
                      for r in sorted(catalogue.recipes.values(), key=lambda r: r.id)
                      if r.toolkit == kit.id]})
    groups += [{"id": kid, "version": None, "description": None, "error": why,
                "why_not": None, "tools": []} for kid, why in catalogue.broken.items()]
    return sorted(groups, key=lambda g: g["id"])


def change_tools(jobs: Any, app: Any, name: str, ticked: list[str], view: Any):
    """Tools ticked and unticked on the Settings tab, as one job running
    `ship-fleet tools add|remove NAME ID...` (ops.change_tools, as the CLI does)."""
    from fleetledger.web.jobs import run_cli

    choices = tool_choices(app, view)
    if not choices["known"]:
        raise FleetError("Its tools are not known until it answers: Check now first.")
    offered = list(choices["list"])
    refused: dict[str, str] = {}
    for group in choices["toolkits"]:
        for tool in group["tools"]:
            if group["why_not"] is None:
                offered.append(tool)
            else:
                refused[tool[0]] = group["why_not"]
    known = {t for t, _, _ in offered}
    if set(ticked) & set(refused):
        tool = sorted(set(ticked) & set(refused))[0]
        raise FleetError(f"{tool}: {refused[tool]}")
    if set(ticked) - known:
        raise FleetError("Tools: not one of the catalogue's or your toolkits'.")
    installed = {t for t, _, state in offered if state}
    add = sorted(set(ticked) - installed)
    remove = sorted(installed - set(ticked))
    if not add and not remove:
        raise FleetError("Nothing to change.")

    def run(job: Any) -> int:
        code = 0
        if add:
            code = run_cli(app, ["tools", "add", name, *add], job)
        if remove and code == 0:
            code = run_cli(app, ["tools", "remove", name, *remove], job)
        return code

    said = [*(["add " + ", ".join(add)] if add else []),
            *(["remove " + ", ".join(remove)] if remove else [])]
    return jobs.start("tools", [name], run, f"Tools on {name}: {'; '.join(said)}")


POWER = {"start": "Start", "stop": "Stop"}


def start_power(jobs: Any, app: Any, verb: str, name: str):
    """Start or stop a Ship's VM: `ship-fleet start|stop NAME` itself, as a job.
    Refused while something else runs on that Ship."""
    from fleetledger.web.jobs import RecordingApp, run_cli

    if verb not in POWER:
        raise FleetError("Unknown power action.")
    # RecordingApp: the driver's answer goes on the card (_outcome.html), so the
    # manual driver's step shows as "by hand" with Copy, not as "done".
    return jobs.start(verb, [name],
                      lambda job: run_cli(RecordingApp(app, job), [verb, name], job),
                      f"{POWER[verb]} {name}'s VM")


def start_agent_restart(jobs: Any, app: Any, name: str, agents: list[str], *,
                        everyone: bool = False, mode: str | None = None,
                        fresh: bool = False, force: bool = False,
                        poll: Callable[[list[str]], Any] | None = None):
    """Restart running agents on one Ship in place, as one job running
    `ship-fleet agent restart NAME ...` (ops.restart_agents, as the CLI does). Checked
    here first, so a bad choice is refused on the page, not in a failed job. With poll,
    the job checks the Ship before it ends, so the page it refreshes shows the new modes."""
    from fleetledger.web.jobs import run_cli

    agents = list(dict.fromkeys(agents))
    if bool(agents) == everyone:
        raise FleetError("Which agents? Choose at least one.")
    if not all(ops.AGENT_NAME_RE.match(a) for a in agents):
        raise FleetError("Not an agent name.")
    if mode is not None and mode not in ops.MODES:
        raise FleetError("Mode: not one of the choices.")
    argv = ["agent", "restart", name, *(["--all"] if everyone else agents)]
    argv += [*(["--mode", mode] if mode else []), *(["--fresh"] if fresh else []),
             *(["--force"] if force else [])]
    who = "every agent" if everyone else ", ".join(agents)
    title = f"Restart {who} on {name}" + (f" in {mode}" if mode else "")

    def run(job: Any) -> int:
        code = run_cli(app, argv, job)
        if poll is not None:
            poll([name])
        return code

    return jobs.start("agent-restart", [name], run, title)
