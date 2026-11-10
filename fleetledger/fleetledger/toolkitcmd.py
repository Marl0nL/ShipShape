"""ship-fleet toolkit: add, list, check and show your own toolkits (toolkits.py).

A toolkit is a folder of yours (contracts/toolkit.md); ships.toml [toolkit.<id>]
names it. FleetLedger reads and checks it, and lists its tools and profiles beside
the catalogue's. It does not send toolkits to Ships yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fleetledger import paths, toolkits
from fleetledger.errors import FleetError, NotFound

DOC = "contracts/toolkit.md"


def inventory_or_none(app) -> Any:
    """The inventory, or None where there is none yet (a toolkit check in CI)."""
    store = app.store_factory()
    return store.load() if store.exists() else None


def add_parsers(sub, add) -> None:
    p = add(sub, "toolkit", "your own toolkits: tools, profiles and sign-ins from a folder",
            "Your own toolkits: folders of tool recipes, profiles, sign-ins and skills,\n"
            "laid out like ShipShape's catalogue (" + DOC + "). FleetLedger reads them\n"
            "from the folder ships.toml [toolkit.<id>] names, checks them, and lists\n"
            "their tools and profiles as <toolkit>/<id>. It does not send toolkits to\n"
            "Ships yet.\n\n"
            "  ship-fleet toolkit add mykit ~/toolkits/mykit   check it, then add it\n"
            "  ship-fleet toolkit ls                           every toolkit, and its state\n"
            "  ship-fleet toolkit check mykit                  check an added toolkit\n"
            "  ship-fleet toolkit check ./mykit                check a folder (its own CI)\n"
            "  ship-fleet toolkit show mykit                   its tools, profiles, sign-ins")
    p.add_argument("action", choices=("add", "ls", "check", "show"))
    p.add_argument("rest", nargs="*", metavar="ID|PATH")
    p.add_argument("--allow-root", action="store_true",
                   help="add, or check a folder: let its recipes run as root")
    p.add_argument("--only-tag", action="append", default=[], metavar="TAG",
                   help="add: only Ships with this tag may have it (repeatable; enforced "
                   "once toolkits reach Ships)")
    p.add_argument("--option", action="append", default=[], metavar="NAME=VALUE",
                   help="add: a value its sign-ins and profiles use (repeatable)")


def cmd_toolkit(args, app) -> int:
    rest = list(args.rest)
    if args.action == "ls":
        if rest:
            raise FleetError("toolkit ls takes no arguments.")
        return _ls(app)
    if args.action == "add":
        if len(rest) != 2:
            raise FleetError("Usage: ship-fleet toolkit add ID PATH")
        return _add(app, rest[0], rest[1], args)
    if len(rest) != 1:
        raise FleetError(f"Usage: ship-fleet toolkit {args.action} "
                         f"{'ID|PATH' if args.action == 'check' else 'ID'}")
    if args.action == "check":
        return _check(app, rest[0], args.allow_root)
    return _show(app, rest[0])


def _catalogue(app, inv):
    from fleetledger.catalogue import Catalogue

    return Catalogue.from_repo(app.repo_factory(), inv)


def _config(inv, kit_id: str) -> toolkits.Config:
    for cfg in toolkits.configured(inv):
        if cfg.id == kit_id:
            return cfg
    known = ", ".join(c.id for c in toolkits.configured(inv)) or "none yet"
    raise NotFound(f"No toolkit '{kit_id}' in ships.toml (toolkits: {known}).",
                   hint="ship-fleet toolkit add ID PATH")


def _summary(kit: toolkits.Toolkit) -> str:
    def n(count: int, word: str) -> str:
        return f"{count} {word}{'' if count == 1 else 's'}"

    return ", ".join([n(len(kit.recipes), "tool"), n(len(kit.profiles), "profile"),
                      n(len(kit.signins), "sign-in"), n(len(kit.skills), "skill")])


def _options(values: list[str]) -> dict[str, str]:
    import re

    out: dict[str, str] = {}
    for item in values:
        name, sep, value = item.partition("=")
        if not sep or not re.match(r"^[a-z][a-z0-9_]{0,31}$", name):
            raise FleetError(f"--option '{item}': give NAME=VALUE, NAME in a-z, 0-9 and _.")
        out[name] = value
    return out


def _add(app, kit_id: str, folder: str, args) -> int:
    store = app.store_factory()
    inv = store.load()
    if kit_id in {c.id for c in toolkits.configured(inv)}:
        raise FleetError(f"Toolkit {kit_id} is already in ships.toml.",
                         hint=f"Change [toolkit.{kit_id}] in {paths.tilde(store.path)} "
                         "to point it elsewhere.")
    root = Path(folder).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    cfg = toolkits.Config(id=kit_id, path=root, allow_root=args.allow_root,
                          only_tags=tuple(dict.fromkeys(args.only_tag)) or None,
                          options=_options(args.option))
    catalogue = _catalogue(app, inv)
    kit = toolkits.load_config(cfg, base=catalogue,
                               taken=toolkits.taken_by(catalogue.toolkits.values()))
    entry: dict[str, Any] = {"path": paths.tilde(kit.root)}
    if cfg.allow_root:
        entry["allow_root"] = True
    if cfg.only_tags:
        entry["only_tags"] = list(cfg.only_tags)
    if cfg.options:
        entry["options"] = dict(cfg.options)
    with store.edit() as ed:
        ed.set_toolkit(kit_id, entry)
    app.say(f"Added toolkit {kit.id} {kit.version} ({_summary(kit)}) from "
            f"{paths.tilde(kit.root)}.")
    app.say("Its tools and profiles are listed as "
            f"{kit.id}/<id> (ship-fleet tools ls); FleetLedger does not send toolkits to "
            "Ships yet.")
    return 0


def _ls(app) -> int:
    from fleetledger.commands import table

    inv = inventory_or_none(app)
    configs = toolkits.configured(inv)
    if not configs:
        app.say(f"No toolkits yet: ship-fleet toolkit add ID PATH ({DOC}).")
        return 0
    catalogue = _catalogue(app, inv)
    rows = []
    for cfg in configs:
        kit = catalogue.toolkits.get(cfg.id)
        tags = ",".join(cfg.only_tags) if cfg.only_tags else "-"
        if kit is None:
            rows.append([cfg.id, "-", "does not load", "-", "-", "-", tags,
                         paths.tilde(cfg.path)])
            continue
        rows.append([kit.id, kit.version, "ok", ",".join(sorted(kit.recipes)) or "-",
                     ",".join(sorted(kit.profiles)) or "-", ",".join(kit.signins) or "-",
                     tags, paths.tilde(cfg.path)])
    app.say(table(rows, ["TOOLKIT", "VERSION", "STATE", "TOOLS", "PROFILES", "SIGN-INS",
                         "ONLY TAGS", "PATH"]))
    for kit_id in sorted(catalogue.broken):
        app.say(f"\n{kit_id}: ship-fleet toolkit check {kit_id} says why.")
    return 0


def _check(app, what: str, allow_root: bool) -> int:
    inv = inventory_or_none(app)
    configs = {c.id: c for c in toolkits.configured(inv)}
    catalogue = _catalogue(app, inv)
    looks_like_path = "/" in what or what.startswith((".", "~"))
    if what in configs and not looks_like_path:
        cfg = configs[what]
        kit = toolkits.load_config(cfg, base=catalogue, taken=toolkits.taken_by(
            catalogue.toolkits.values(), but=cfg.id))
    elif Path(what).expanduser().is_dir():
        # A folder: checked as it would be added, against the toolkits already added
        # (bar one of its own id: the same toolkit elsewhere).
        kit = toolkits.load(Path(what).expanduser(), allow_root=allow_root, base=catalogue,
                            taken=toolkits.taken_by(catalogue.toolkits.values()))
        other = configs.get(kit.id)
        if other is not None and other.path.resolve() != kit.root:
            app.say(f"Note: ships.toml's toolkit {kit.id} is another folder "
                    f"({paths.tilde(other.path)}).")
    else:
        raise NotFound(f"'{what}' is neither a toolkit in ships.toml nor a folder.")
    app.say(f"{kit.id} {kit.version} is a usable toolkit: {_summary(kit)}.")
    return 0


def _show(app, kit_id: str) -> int:
    from fleetledger.commands import table

    inv = inventory_or_none(app)
    cfg = _config(inv, kit_id)
    catalogue = _catalogue(app, inv)
    if kit_id in catalogue.broken:
        app.say(f"{kit_id}: {paths.tilde(cfg.path)}")
        raise FleetError(catalogue.broken[kit_id])
    kit = catalogue.toolkits[kit_id]
    app.say(f"{kit.id} {kit.version}: {kit.description}")
    app.say(f"  folder:     {paths.tilde(kit.root)}")
    app.say(f"  only tags:  {', '.join(cfg.only_tags) if cfg.only_tags else '(any Ship)'}")
    app.say(f"  root:       {'allowed' if cfg.allow_root else 'not allowed'}")
    if cfg.options:
        app.say(f"  options:    {', '.join(f'{k}={v}' for k, v in sorted(cfg.options.items()))}")
    if kit.signins:
        app.say(f"  sign-ins:   {', '.join(kit.signins)}")
    if kit.skills:
        app.say(f"  skills:     {', '.join(kit.skills)}")
    if kit.instructions:
        app.say(f"  instructions: {', '.join(kit.instructions)}")
    if kit.files:
        app.say(f"  vendored:   {len(kit.files)} file(s), pinned by SHA-256")
    recipes = [r for r in catalogue.recipes.values() if r.toolkit == kit.id]
    if recipes:
        app.say("")
        app.say(table([[r.id, r.kind, ",".join(r.needs) or "-", r.description]
                       for r in sorted(recipes, key=lambda r: r.id)],
                      ["TOOL", "KIND", "NEEDS", "DESCRIPTION"]))
    profiles = [p for p in catalogue.profiles.values() if p.toolkit == kit.id]
    if profiles:
        app.say("")
        app.say(table([[p.id, ",".join(p.tools), p.description]
                       for p in sorted(profiles, key=lambda p: p.id)],
                      ["PROFILE", "TOOLS", "DESCRIPTION"]))
    return 0
