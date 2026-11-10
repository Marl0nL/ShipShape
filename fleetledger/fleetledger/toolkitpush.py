"""Send your toolkits to the Ships that may have them (contracts/toolkit.md, "On a Ship").

A toolkit goes only to a Ship that meets ships.toml [toolkit.<id>] only_tags (one of
those tags, in the inventory), as its own archive (toolkitpack.py), applied with
`ship-ctl toolkit apply`. A Ship whose tooling predates toolkits (no `toolkit` in
`ship-ctl version`) is told to update first; nothing is half-sent to it.

`ship-fleet toolkit push`, `update`, `tools add` and a new Ship's enrolment all send
through here.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from typing import Any

from fleetledger import ops, toolkitpack, toolkits
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory, Ship
from fleetledger.shipctl import ShipCtl, ShipCtlError

CONTRACT = "toolkit"


def why_not(cfg: toolkits.Config | None, tags: Iterable[str], ship_name: str) -> str | None:
    """Why the Ship may not have this toolkit (only_tags), or None if it may."""
    if cfg is None or cfg.only_tags is None:
        return None
    tags = list(tags)
    if set(cfg.only_tags) & set(tags):
        return None
    wanted = " or ".join(cfg.only_tags) if cfg.only_tags else "(no tag: none)"
    return (f"toolkit {cfg.id} is only for Ships tagged {wanted} (ships.toml only_tags), and "
            f"{ship_name} is tagged {', '.join(tags) or 'nothing'}")


def allowed(catalogue: Any, inv: Inventory, ship: Ship) -> list[Any]:
    """The loaded toolkits this Ship may have, in id order."""
    tags = inv.effective_tags(ship)
    return [kit for _, kit in sorted(catalogue.toolkits.items())
            if why_not(kit.config, tags, ship.name) is None]


def broken_allowed(inv: Inventory, catalogue: Any, ship: Ship) -> list[str]:
    """Toolkits this Ship may have that do not load: said, not sent."""
    tags = inv.effective_tags(ship)
    return [f"Toolkit {cfg.id} was not sent: it does not load (ship-fleet toolkit check "
            f"{cfg.id})." for cfg in toolkits.configured(inv)
            if cfg.id in catalogue.broken and why_not(cfg, tags, ship.name) is None]


class Archives:
    """Each toolkit packed once per command, shared by every Ship it goes to."""

    def __init__(self, contracts=None):
        self.contracts = contracts
        self._lock = threading.Lock()
        self._done: dict[str, toolkitpack.Archive | FleetError] = {}

    def get(self, kit: Any) -> toolkitpack.Archive:
        with self._lock:
            if kit.id not in self._done:
                try:
                    self._done[kit.id] = toolkitpack.archive(kit, self.contracts)
                except FleetError as exc:
                    self._done[kit.id] = exc
            got = self._done[kit.id]
        if isinstance(got, FleetError):
            raise got
        return got


def supports(ctl: ShipCtl, ship: Ship) -> None:
    """Refuse a Ship whose tooling predates toolkits: update it first."""
    ctl.ensure_compatible()
    if CONTRACT not in ctl.contracts:
        raise FleetError(f"{ship.name}'s tooling predates toolkits, so it cannot take one yet.",
                         hint=f"Update it first (ship-fleet update {ship.name}); its toolkits "
                         "are sent with the update.")


def on_ship(doc: dict[str, Any] | None) -> dict[str, tuple[str, bool]]:
    """Toolkit id -> (release, root allowed), as the Ship's status reports them."""
    if not doc:
        return {}
    return {k["id"]: (k["release"], k.get("allow_root") is True)
            for k in doc.get("toolkits") or ()
            if isinstance(k, dict) and k.get("state") == "ok"}


def send(fleet: ops.Fleet, inv: Inventory, ship: Ship, kit: Any, archive: toolkitpack.Archive,
         *, ctl: ShipCtl | None = None, force: bool = False,
         present: dict[str, tuple[str, bool]] | None = None) -> tuple[bool, str, list[str]]:
    """Apply one toolkit on one Ship. Returns (ok, summary, details); raises FleetError
    when it was not applied. `present`: what the Ship has (skip one already there)."""
    ctl = ctl or ShipCtl(fleet.ssh(inv), ship)
    supports(ctl, ship)
    why = why_not(kit.config, inv.effective_tags(ship), ship.name)
    if why is not None:  # never: callers choose allowed Ships; a second line all the same
        raise FleetError(f"{ship.name}: not sent: {why}.")
    allow_root = kit.config is not None and kit.config.allow_root
    if present is not None and present.get(kit.id) == (archive.release, allow_root) \
            and not force:
        return True, f"toolkit {kit.id} {archive.version}: already there", []
    name = f"toolkit-{kit.id}-{archive.sha256[:16]}.tar.gz"
    path = ops.upload(fleet, inv, ship, name, archive.data, archive.sha256, "the toolkit")
    args = ["apply", "--archive", path]
    if allow_root:
        args.append("--allow-root")
    if force:
        args.append("--force")
    try:
        result = ctl.call("toolkit", *args, timeout=1800, partial=True)
    finally:
        ops._remove_upload(fleet, inv, ship, path)
    err = result.error or {}
    if not result.ok and err.get("code") == "toolkit-rollback":
        raise FleetError(f"{ship.name}: toolkit {kit.id} not sent: {err.get('message')}",
                         hint=f"To roll it back on purpose: ship-fleet toolkit push {kit.id} "
                         "--force (on that Ship's tag), or bump the version in its "
                         "toolkit.toml.")
    if not result.ok and err.get("code") != "tool-failed":
        raise ShipCtlError(f"{ship.name}: toolkit {kit.id} not applied: {err.get('message')}",
                           code=err.get("code"))
    summary = f"toolkit {kit.id} {archive.version} applied"
    if not result.ok:
        return False, f"{summary}, but its tools did not all install", list(result.messages)
    return True, summary, list(result.messages)


def send_allowed(fleet: ops.Fleet, inv: Inventory, ship: Ship, catalogue: Any,
                 archives: Archives, *, only: Sequence[str] | None = None,
                 ctl: ShipCtl | None = None,
                 present: dict[str, tuple[str, bool]] | None = None,
                 force: bool = False) -> tuple[bool, list[str], list[str]]:
    """Every toolkit this Ship may have (or those of `only` it may have). Returns (all
    applied with their tools, lines to show, ids of toolkits not applied). A Ship whose
    tooling predates toolkits raises FleetError."""
    kits = [k for k in allowed(catalogue, inv, ship) if only is None or k.id in only]
    lines: list[str] = []
    missing: list[str] = []
    if not kits:
        return True, broken_allowed(inv, catalogue, ship) if only is None else [], []
    ctl = ctl or ShipCtl(fleet.ssh(inv), ship)
    supports(ctl, ship)
    if present is None:
        present = on_ship(ops._require_status(ops._fetch(fleet, inv, ship)))
    ok = True
    for kit in kits:
        try:
            archive = archives.get(kit)
            done, summary, details = send(fleet, inv, ship, kit, archive, ctl=ctl,
                                          present=present, force=force)
        except FleetError as exc:
            ok = False
            missing.append(kit.id)
            lines.append(exc.message if exc.message.startswith(ship.name) else
                         f"Toolkit {kit.id} not sent: {exc.message}")
            continue
        ok = ok and done
        lines.append(summary[:1].upper() + summary[1:] + ".")
        lines += details[1:] if done else details
    if only is None:
        lines += broken_allowed(inv, catalogue, ship)
    return ok, lines, missing


def push(fleet: ops.Fleet, ships: Sequence[Ship], kit_id: str, *, force: bool = False,
         on_event: ops.Event | None = None) -> list[ops.Outcome]:
    """`ship-fleet toolkit push ID`: to each of these Ships that may have it."""
    from fleetledger.catalogue import Catalogue

    repo = fleet.repo_factory()
    inv = fleet.inventory
    catalogue = Catalogue.from_repo(repo, inv)
    if kit_id in catalogue.broken:
        raise FleetError(f"Toolkit {kit_id} does not load, so it is not sent.\n"
                         f"{catalogue.broken[kit_id]}", hint=f"ship-fleet toolkit check {kit_id}")
    kit = catalogue.toolkits.get(kit_id)
    if kit is None:
        known = ", ".join(sorted(catalogue.toolkits)) or "none yet"
        raise FleetError(f"No toolkit '{kit_id}' in ships.toml (toolkits: {known}).",
                         hint="ship-fleet toolkit add ID PATH")
    archive = Archives(repo.contracts).get(kit)  # a toolkit too big stops here, once

    def one(ship: Ship, event: ops.Event) -> ops.Outcome:
        why = why_not(kit.config, inv.effective_tags(ship), ship.name)
        if why is not None:
            event(ship.name, "skipped: may not have it")
            return ops.Outcome(ship.name, True, f"skipped: {why}", data={"sent": False})
        event(ship.name, f"sending toolkit {kit.id} {archive.version} "
                         f"({len(archive.data) // 1024} KiB)")
        present = on_ship(ops._require_status(ops._fetch(fleet, inv, ship)))
        done, summary, details = send(fleet, inv, ship, kit, archive, present=present,
                                      force=force)
        event(ship.name, summary)
        return ops.Outcome(ship.name, done, summary, details,
                           {"sent": True, "release": archive.release})

    return ops.for_each(fleet, ships, one, on_event)


def after_enrol(app, store, name: str, say) -> None:
    """A new Ship: send it the toolkits it may have (best effort, as certificates are)."""
    from fleetledger.catalogue import Catalogue

    fleet = ops.Fleet.from_app(app)
    inv = store.load()
    if not toolkits.configured(inv):
        return
    ship = inv.ship(name)
    try:
        repo = app.repo_factory()
        catalogue = Catalogue.from_repo(repo, inv)
        _, lines, _ = send_allowed(fleet, inv, ship, catalogue, Archives(repo.contracts))
    except FleetError as exc:
        say(f"Toolkits not sent yet ({exc.message}); run `ship-fleet toolkit push ID --tag "
            f"TAG` once it is ready.")
        return
    for line in lines:
        say(line)
