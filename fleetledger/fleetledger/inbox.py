"""Send files to a Ship's agents: its file inbox, `~/inbox/` (contracts/ship-ctl.md,
`ship-ctl inbox`).

`ship-fleet put SHIP FILE...` and the web UI's Inbox tab both run `put` here. Each
file is streamed over FleetLedger's usual SSH login to `ship-ctl inbox put`, which
writes it aside and links it into the inbox only once every byte is there, under
a cleaned name that never replaces another file. Nothing new listens on the Ship.

`ship-fleet inbox ls SHIP` lists what is there; `ship-fleet inbox rm SHIP NAME...`
removes files (an agent moves a file out once it has dealt with it).
"""

from __future__ import annotations

import dataclasses
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fleetledger.errors import FleetError
from fleetledger.inventory import Ship
from fleetledger.shipctl import ShipCtl, ShipCtlError

# The Ship's own limits (ship/lib/shipctl/inbox.py); FleetLedger checks first, so a
# file that cannot go is refused before it is read. A test keeps them the same.
MAX_BYTES = 256 * 1024 * 1024
MAX_FILES = 20  # files in one put
NAME_BYTES = 200
_KEEP = frozenset(" ._-+,@()[]#%=~")
_LEADING = " .-~#"
INBOX = "~/inbox"

Event = Callable[[str, str], None]
FileEvent = Callable[[int, dict[str, Any]], None]


def human(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            text = f"{size:.1f}".removesuffix(".0")
            return f"{int(size)} {unit}" if unit == "bytes" else f"{text} {unit}"
        size /= 1024
    return f"{n} bytes"  # pragma: no cover


def clean_name(raw: str) -> str:
    """The name the Ship will give the file (the same rules as ship-ctl's)."""
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    out = []
    for ch in name:
        cat = unicodedata.category(ch)
        if cat[0] == "Z" or ch.isspace():
            out.append(" ")
        elif cat[0] == "C":
            continue
        elif ch.isalnum() or cat[0] == "M" or ch in _KEEP:
            out.append(ch)
        else:
            out.append("_")
    # NFC again: a dropped format character between a letter and its combining mark
    # leaves a pair that composes now; without this a second pass would change it.
    name = unicodedata.normalize("NFC", "".join(out))
    name = re.sub(r" +", " ", name).lstrip(_LEADING).rstrip(" .")
    if len(name.encode("utf-8")) > NAME_BYTES:
        stem, ext = split_ext(name)
        room = NAME_BYTES - len(ext.encode("utf-8")) - (1 if ext else 0)
        stem = stem.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip(" .")
        name = unicodedata.normalize("NFC", f"{stem}.{ext}" if ext else stem)
    return name or "file"


def split_ext(name: str) -> tuple[str, str]:
    """(stem, extension without its dot): the last dot's, if the extension is short
    (at most 16 bytes) and the stem not empty; a .tar.* pair stays together.
    "report v1.2.pdf" -> ("report v1.2", "pdf"); "a.tar.gz" -> ("a", "tar.gz")."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext.encode("utf-8")) > 16:
        return name, ""
    inner, dot, tar = stem.rpartition(".")
    if dot and inner and tar.lower() == "tar":
        return inner, f"{tar}.{ext}"
    return stem, ext


def numbered(name: str, n: int) -> str:
    """The nth name for a taken one, as the Ship gives it (report-2.pdf)."""
    stem, ext = split_ext(name)
    return f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"


@dataclasses.dataclass(frozen=True)
class Upload:
    """One file to send: where it is here, and the name the person gave it."""

    path: Path
    name: str
    size: int


def too_big(name: str, size: int) -> str:
    return (f"{name} is {human(size)}; the inbox takes files of up to {human(MAX_BYTES)} "
            "each. Copy bigger files another way (scp as agent, say).")


def uploads(paths: Sequence[str | Path]) -> list[Upload]:
    """The files `ship-fleet put` was given, checked before any Ship is touched."""
    if not paths:
        raise FleetError("Which files? Give at least one.")
    if len(paths) > MAX_FILES:
        raise FleetError(f"At most {MAX_FILES} files at once ({len(paths)} given).")
    out = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise FleetError(f"{raw}: not a file." if path.exists() else f"{raw}: no such file.")
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise FleetError(too_big(path.name, size))
        out.append(Upload(path, path.name, size))
    return out


def _ctl(fleet: Any, ship: Ship) -> ShipCtl:
    """ship-ctl on a Ship whose tooling has the inbox (an older one is told to update)."""
    ctl = ShipCtl(fleet.ssh(), ship)
    ctl.ensure_compatible()
    if "inbox" not in ctl.contracts:
        raise FleetError(f"{ship.name}'s tooling predates the file inbox.",
                         hint=f"Update it first: ship-fleet update {ship.name}")
    return ctl


def listing(fleet: Any, ship: Ship) -> dict[str, Any]:
    """What is in the Ship's ~/inbox (ship-ctl inbox list's data)."""
    return _ctl(fleet, ship).call("inbox", "list").data


def put(fleet: Any, ship: Ship, files: Sequence[Upload], *, tell: bool = False,
        on_event: Event | None = None, on_file: FileEvent | None = None) -> Any:
    """Send files to the Ship's inbox, one after another, each streamed (never all in
    memory). One refused file does not stop the others. With tell, the Ship's core
    agents are then told how many arrived (ship-ctl inbox notify). on_file(index,
    state) follows each file: state has name, size, sent, state (waiting, sending,
    stored, failed), stored (its name on the Ship) and error."""
    from fleetledger.ops import Outcome

    on_event = on_event or (lambda _ship, _text: None)
    on_file = on_file or (lambda _i, _state: None)
    states = [{"name": f.name, "size": f.size, "sent": 0, "state": "waiting",
               "stored": None, "error": None} for f in files]
    for i, state in enumerate(states):
        on_file(i, state)
    ctl = _ctl(fleet, ship)
    for i, (upload, state) in enumerate(zip(files, states, strict=True)):
        if upload.size > MAX_BYTES:
            state.update(state="failed", error=too_big(upload.name, upload.size))
            on_file(i, state)
            continue
        state["state"] = "sending"
        on_file(i, state)
        on_event(ship.name, f"sending {upload.name} ({human(upload.size)})")

        def progress(sent: int, i: int = i, state: dict = state) -> None:
            state["sent"] = min(sent, state["size"])
            on_file(i, state)

        try:
            with upload.path.open("rb") as source:
                result = ctl.send("inbox", "put", "--name", clean_name(upload.name),
                                  "--size", str(upload.size), source=source,
                                  on_progress=progress,
                                  timeout=120 + upload.size / (512 * 1024))
        except OSError as exc:
            state.update(state="failed", error=f"cannot read it here ({exc.strerror})")
            on_file(i, state)
            continue
        except ShipCtlError as exc:
            state.update(state="failed", error=exc.message)
            on_file(i, state)
            continue
        if result.ok:
            stored = (result.data or {}).get("name") or clean_name(upload.name)
            state.update(state="stored", stored=stored, sent=upload.size)
            on_event(ship.name, f"{upload.name}: in {INBOX} as {stored}"
                     if stored != upload.name else f"{upload.name}: in {INBOX}")
        else:
            state.update(state="failed", error=(result.error or {}).get("message", "failed"))
            on_event(ship.name, f"{upload.name}: not sent: {state['error']}")
        on_file(i, state)

    stored = [s for s in states if s["state"] == "stored"]
    failed = [s for s in states if s["state"] == "failed"]
    details = [f"{s['name']} is {s['stored']} there" for s in stored if s["stored"] != s["name"]]
    details += [f"{s['name']}: {s['error']}" for s in failed]
    told = _tell(ctl, len(stored)) if tell and stored else None
    if told:
        details.append(told)
    files_word = "file" if len(stored) == 1 else "files"
    summary = (f"{len(stored)} {files_word} in {INBOX}: " + ", ".join(s["stored"] for s in stored)
               if stored else "nothing sent")
    if failed:
        summary += f"; {len(failed)} not sent"
    return Outcome(ship.name, not failed, summary, details, {"files": states, "told": told})


def _tell(ctl: ShipCtl, count: int) -> str:
    try:
        result = ctl.call("inbox", "notify", "--count", str(count), partial=True)
    except ShipCtlError as exc:
        return f"agents not told: {exc.message}"
    data = result.data or {}
    told = data.get("sent") or []
    said = f"told {', '.join(told)}" if told else "no agent told"
    skipped = [f"{s['name']} ({s['reason']})" for s in data.get("skipped") or []]
    if skipped:
        said += "; not told: " + ", ".join(skipped)
    elif not told and result.messages:
        said += f" ({result.messages[0].rstrip('.')})"
    return said


def remove(fleet: Any, ship: Ship, names: Sequence[str]) -> Any:
    """Remove files from the Ship's inbox by their exact names."""
    from fleetledger.ops import Outcome

    names = list(dict.fromkeys(names))
    if not names:
        raise FleetError("Which files? Give their names as `ship-fleet inbox ls` shows them.")
    bad = [n for n in names if not n or "/" in n or n in (".", "..") or "\0" in n]
    if bad:
        raise FleetError(f"'{bad[0]}' is not a file name in the inbox.")
    data = _ctl(fleet, ship).call("inbox", "remove", *names).data or {}
    removed = data.get("removed") or []
    details = [f"not there (moved already?): {n}" for n in data.get("missing") or []]
    details += [f"not a plain file, left alone: {n}" for n in data.get("refused") or []]
    summary = f"removed {', '.join(removed)}" if removed else "nothing removed"
    return Outcome(ship.name, not data.get("refused"), summary, details, data)


# --- the commands -----------------------------------------------------------------


def add_parsers(sub, add) -> None:
    p = add(sub, "put", "send files to a Ship's agents (into its ~/inbox)",
            "Copy files into the Ship's file inbox, ~/inbox, for its agents. Each goes over\n"
            "FleetLedger's SSH login (no new port on the Ship), appears there only once\n"
            "complete, and never replaces a file: a name already taken gets -2, -3 ...\n"
            f"Names are cleaned up (odd characters become _). Up to {MAX_FILES} files of\n"
            f"{human(MAX_BYTES)} each. Agents treat inbox files as untrusted input and move\n"
            "them out once dealt with (the ship skill).\n\n"
            "  ship-fleet put ship-01 ~/Downloads/spec.pdf notes.txt\n"
            "  ship-fleet put ship-01 data.csv --tell    and tell its autostart agents")
    p.add_argument("name", metavar="SHIP")
    p.add_argument("files", nargs="+", metavar="FILE")
    p.add_argument("--tell", action="store_true",
                   help="tell the Ship's autostart agents how many files arrived")
    p.add_argument("--json", action="store_true", help="print the result as JSON")

    p = add(sub, "inbox", "list or remove the files in a Ship's ~/inbox",
            "  ship-fleet inbox ls ship-01            what is there (newest first)\n"
            "  ship-fleet inbox rm ship-01 NAME ...   remove files by their names\n\n"
            "Send files with ship-fleet put.")
    p.add_argument("action", choices=("ls", "rm"))
    p.add_argument("name", metavar="SHIP")
    p.add_argument("files", nargs="*", metavar="NAME", help="rm: file names as ls shows them")
    p.add_argument("--json", action="store_true", help="print the result as JSON")


def _fleet(app):
    from fleetledger.ops import Fleet

    return Fleet.from_app(app)


def _report(app, outcome, as_json: bool) -> int:
    from fleetledger.commands import _report as report

    return report(app, [outcome], as_json)


def cmd_put(args, app) -> int:
    files = uploads(args.files)
    fleet = _fleet(app)
    ship = fleet.inventory.ship(args.name)
    shown: dict[int, int] = {}

    def on_file(i: int, state: dict[str, Any]) -> None:
        if state["state"] != "sending" or state["size"] < 8 * 1024 * 1024:
            return
        tenth = state["sent"] * 10 // max(state["size"], 1)
        if tenth > shown.get(i, 0) and tenth < 10:
            shown[i] = tenth
            app.note(f"{ship.name}: {state['name']} {tenth * 10}%")

    outcome = put(fleet, ship, files, tell=args.tell,
                  on_event=lambda s, text: app.note(f"{s}: {text}"), on_file=on_file)
    return _report(app, outcome, args.json)


def cmd_inbox(args, app) -> int:
    fleet = _fleet(app)
    ship = fleet.inventory.ship(args.name)
    if args.action == "rm":
        return _report(app, remove(fleet, ship, args.files), args.json)
    if args.files:
        raise FleetError("inbox ls takes only the Ship's name.")
    data = listing(fleet, ship)
    if args.json:
        app.say(json.dumps(data, indent=2))
        return 0
    files = data.get("files") or []
    if not files:
        app.say(f"{ship.name}: {data.get('path', INBOX)} is empty.")
    for f in files:
        app.say(f"{human(f['size']):>10}  {f['modified'].replace('T', ' ').rstrip('Z')} UTC  "
                f"{f['name']}")
    if data.get("others"):
        app.say(f"(and {data['others']} folder(s) or link(s), not shown)")
    if data.get("truncated"):
        app.say("(only the newest 500 are shown)")
    return 0
