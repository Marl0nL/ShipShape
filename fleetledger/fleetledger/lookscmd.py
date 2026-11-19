"""ship-fleet looks: the fleet's Desk looks, one looks folder kept by FleetLedger (looks.py;
contracts/ship-fleet.md, "ship-fleet looks").

Only someone with a shell on FleetLedger runs these, as for every ship-fleet verb: the
trusted machines' bundle keys and the pairing key are forced to their own commands
(authkeys.py), which never reach this one. publish and clear change FleetLedger's state, so
they wait for the vault to be unlocked, as every change does; show only reads, and answers
while it is locked. A copy on standby or retired (ship-fleet migrate) refuses all three.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys

from fleetledger import ca, looks, paths
from fleetledger.concierge import audit
from fleetledger.errors import FleetError
from fleetledger.inventory import now_timestamp

DOC = "contracts/desk-looks.md"
UNPLAIN = re.compile(r"[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")
SEND = ("tar -cf - -C ~/.config/herdr-app/desk-looks . | ssh FLEETLEDGER "
        "ship-fleet looks publish -")


def add_parsers(sub, add) -> None:
    p = add(sub, "looks", "the Desk looks your computers share: store, show or clear them",
            "One looks folder for the Herdr Desk (palettes, presets, tag looks and logos;\n"
            f"{DOC}), kept by FleetLedger for every computer of yours. It is checked\n"
            "whole on the way in, as the Desk checks it, and refused whole when anything\n"
            "is out of place: what was stored before then stays. Data only: nothing in it\n"
            "is ever run. Nothing sends it to a computer yet.\n\n"
            "  ship-fleet looks publish ~/.config/herdr-app/desk-looks   a folder here\n"
            f"  {SEND}\n"
            "                                    from another computer, as a tar stream\n"
            "  ship-fleet looks show [--json]    what is stored: presets, tags, sizes, when\n"
            "  ship-fleet looks clear            forget it")
    p.add_argument("action", choices=("publish", "show", "clear"))
    p.add_argument("source", nargs="?", metavar="FOLDER|-",
                   help="publish: the looks folder, or - for a tar stream on stdin")
    p.add_argument("--json", action="store_true", help="show: print JSON")


def cmd_looks(args, app) -> int:
    if args.action != "publish" and args.source is not None:
        raise FleetError(f"looks {args.action} takes no folder.")
    if args.action != "show" and args.json:
        raise FleetError(f"looks {args.action} has no --json.")
    if args.action == "publish":
        if args.source is None:
            raise FleetError("Usage: ship-fleet looks publish FOLDER|-",
                             hint=f"From another computer: {SEND}")
        return _publish(app, args.source)
    if args.action == "show":
        return _show(app, args.json)
    return _clear(app)


def _address() -> str | None:
    """The address sshd saw this session come from (none outside an ssh session)."""
    words = os.environ.get("SSH_CONNECTION", "").split()
    try:
        return str(ipaddress.ip_address(words[0].split("%", 1)[0])) if words else None
    except ValueError:
        return None


def _source(how: str, folder: str | None) -> dict:
    """Where a publish came from: how, the folder (for one here), the ssh session's address,
    and who ran it, when sshd says (audit.ACTOR)."""
    actor = audit.ACTOR.get() or {}
    return {"how": how, "folder": _line(folder), "address": _address(),
            "actor": _line(actor.get("actor")), "actor_key": _key(actor.get("actor_key"))}


def _key(fp: str | None) -> str | None:
    return fp if isinstance(fp, str) and looks.FINGERPRINT.fullmatch(fp) else None


def _line(text: str | None) -> str | None:
    """Text kept with the folder, as one short line of plain text (looks.SOURCE_TEXT): control
    and direction characters shown as ?, at most 200 characters, none for nothing."""
    if not text:
        return None
    clean = UNPLAIN.sub("?", str(text)).encode("utf-8", "replace").decode("utf-8")[:200]
    return clean if looks.SOURCE_TEXT.fullmatch(clean) else None


def _publish(app, where: str) -> int:
    try:
        if where == "-":
            stream = getattr(app, "stdin_bytes", None)
            if stream is None:
                if app.stdin_isatty():
                    raise FleetError("looks publish - reads a tar stream on its input.",
                                     hint=f"From another computer: {SEND}")
                stream = sys.stdin.buffer
            files = looks.tar_files(looks.read_stream(stream))
            folder = looks.read_folder(looks.from_files(files))
            source = _source("stdin", None)
        else:
            path = paths.expand(where)
            reader = looks.FolderReader(path)
            try:
                folder = looks.read_folder(reader)
            finally:
                reader.close()
            source = _source("folder", paths.tilde(path.resolve()))
    except looks.Refused as exc:
        audit.record("-", "looks-publish", "-", "refused")
        raise FleetError(f"The looks folder is refused, and nothing changed: {exc.why}.",
                         hint=f"Its format is {DOC}.") from None
    before = _current()
    try:
        looks.save(folder, source, now_timestamp())
    except OSError as exc:
        audit.record("-", "looks-publish", folder.sha256[:12], "failed")
        raise FleetError(f"Couldn't store the looks: {exc.strerror or exc}.",
                         hint=f"{paths.tilde(looks.store_path())} is FleetLedger's own file; "
                         "if something else is in its place, remove it, then publish again."
                         ) from None
    audit.record("-", "looks-publish", folder.sha256[:12], "ok")
    same = before is not None and before.folder.sha256 == folder.sha256
    app.say(("Published again, unchanged" if same else "Published") + f": {_summary(folder)}.")
    return 0


def _current() -> looks.Stored | None:
    try:
        return looks.load()
    except looks.Refused:
        return None


def _clear(app) -> int:
    try:
        cleared = looks.clear()
    except OSError as exc:
        raise FleetError(f"Couldn't clear the looks: {exc.strerror or exc}.",
                         hint=f"Remove {paths.tilde(looks.store_path())} by hand.") from None
    if cleared:
        audit.record("-", "looks-clear", "-", "ok")
        app.say("Cleared: FleetLedger keeps no Desk looks now.")
    else:
        app.say("No Desk looks are stored; nothing to clear.")
    return 0


def _summary(folder: looks.Folder) -> str:
    n = len(folder.looks.presets)
    return (f"{n} preset{'' if n == 1 else 's'}, {len(folder.looks.tag_looks)} tag looks, "
            f"{len(folder.files)} files, {_kib(folder.size)}")


def _kib(n: int) -> str:
    return f"{n} bytes" if n < 1024 else f"{n / 1024:.1f} KiB"


def show_doc() -> dict:
    """What `looks show --json` prints (contracts/fleetledger-looks.schema.json): never the
    files' contents, only their names, sizes and what they name."""
    try:
        stored = looks.load()
    except looks.Refused as exc:
        return {"schema": looks.SHOW_SCHEMA, "stored": True, "problem": exc.why}
    if stored is None:
        return {"schema": looks.SHOW_SCHEMA, "stored": False}
    f = stored.folder
    return {
        "schema": looks.SHOW_SCHEMA, "stored": True, "problem": None,
        "published_at": stored.published_at, "from": stored.source,
        "sha256": f.sha256, "bytes": f.size,
        "palettes": [{"id": p["id"], "dark": p["dark"]} for p in f.looks.palettes],
        "presets": list(f.looks.presets),
        "tag_looks": [{"tag": t, "preset": p} for t, p in f.looks.tag_looks],
        "files": [{"name": n, "bytes": len(b)} for n, b in f.files.items()],
    }


def _receivers(app) -> list[str]:
    """The trusted machines whose bundles carry the looks (ship-fleet trust NAME --looks on)."""
    try:
        return [m.name for m in ca.machines(app.store_factory().load())
                if m.looks and not m.revoked]
    except FleetError:
        return []


def _say_receivers(app, sent: bool) -> None:
    names = _receivers(app)
    if not names:
        app.say("  sent to no machine (ship-fleet trust NAME --looks on sends them through "
                "its refresher)")
    elif sent:
        app.say(f"  sent to: {', '.join(names)} (each through its refresher; its refresh key "
                "reads them; the audit log's machine-bundle line says when one went without)")
    else:
        app.say(f"  not sent to {', '.join(names)} until then: their bundles come without "
                "looks, and each keeps what it has")


def _show(app, as_json: bool) -> int:
    doc = show_doc()
    if as_json:
        app.say(json.dumps(doc, indent=2))
        return 0
    if not doc["stored"]:
        app.say("No Desk looks are stored. Publish a looks folder with ship-fleet looks "
                "publish FOLDER|-.")
        if _receivers(app):
            app.say(f"  {', '.join(_receivers(app))}: told there are none, so each refresher "
                    "removes the looks it wrote there")
        return 0
    if doc["problem"]:
        app.say(f"The stored Desk looks are not usable: {doc['problem']}.")
        app.say("  Publish the folder again, or ship-fleet looks clear.")
        _say_receivers(app, False)
        return 1
    src = doc["from"]
    by = " ".join(x for x in (
        f"by {src['actor']}" if src.get("actor") else None,
        f"from {src['address']}" if src.get("address") else None,
        f"(folder {src['folder']})" if src.get("folder") else
        ("(a tar stream)" if src["how"] == "stdin" else None)) if x)
    app.say(f"Desk looks, published {doc['published_at']}{' ' + by if by else ''}")
    app.say(f"  sha256 {doc['sha256']}, {_kib(doc['bytes'])} of "
            f"{looks.TOTAL_BYTES // 1024} KiB")
    app.say("  presets:   " + (", ".join(doc["presets"]) or "none"))
    app.say("  palettes:  " + (", ".join(
        f"{p['id']} ({'dark' if p['dark'] else 'light'})" for p in doc["palettes"]) or "none"))
    app.say("  tag looks: " + (", ".join(
        f"{t['tag']} -> {t['preset']}" for t in doc["tag_looks"]) or "none"))
    for f in doc["files"]:
        app.say(f"  {f['name']:<40} {_kib(f['bytes'])}")
    _say_receivers(app, True)
    return 0

