"""A Ship's file inbox, on its Inbox tab: drop files for its agents, see what is
there, remove what is done with.

- The drop zone's script (static/app.js) posts each file's raw bytes to
  /ships/NAME/inbox/upload, one request per file, with the CSRF token in a header
  (the gate's one exception to "plain form posts", web/security.py). The bytes are
  streamed to a private spool file on this computer (0600, in a 0700 folder under
  FleetLedger's data folder), never held in memory, and the answer is a spool id.
- Once every file is spooled, the script posts an ordinary form naming those ids;
  that starts one job sending them to the Ship with the same shared operation
  `ship-fleet put` runs (fleetledger.inbox.put). The job's card shows each file's
  progress and where it went; the spool files are deleted when it ends.
- A spooled file belongs to the session and Ship it was sent for and is dropped
  after SPOOL_TTL if never sent. The spool folder is emptied when the UI starts.
- The list is `ship-ctl inbox list`, read in a worker thread when the tab opens;
  Remove is `ship-fleet inbox rm`'s operation, after the confirm dialog.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from fleetledger import inbox, paths
from fleetledger.errors import FleetError
from fleetledger.web.security import MAX_UPLOAD

SPOOL_TTL = 3600  # seconds an upload waits to be sent
NAME_MAX = 255  # characters of the original name kept (for messages and the card)
PROGRESS_STEP = 0.02  # the card redraws when a file is 2% further along


@dataclasses.dataclass
class Spooled:
    id: str
    session: str
    ship: str
    name: str  # as the person's computer named it (control characters dropped)
    size: int
    path: Path
    at: float
    complete: bool = False


class Spool:
    """Uploads waiting to be sent to a Ship, on disk only you can read."""

    def __init__(self, root: Path | None = None, clock=time.time) -> None:
        self.root = root
        self.clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, Spooled] = {}
        self._ready = False

    def _folder(self) -> Path:
        if self.root is None:
            self.root = paths.data_dir() / "inbox-spool"
        if not self._ready:  # leftovers of an earlier run are never sent
            shutil.rmtree(self.root, ignore_errors=True)
            paths.private_dir(self.root)
            self._ready = True
        return self.root

    def new(self, session: str, ship: str, name: str, size: int) -> Spooled:
        with self._lock:
            self._expire_locked()
            mine = [i for i in self._items.values() if i.session == session and i.ship == ship]
            if len(mine) >= inbox.MAX_FILES:
                raise FleetError(f"At most {inbox.MAX_FILES} files at once are waiting to be "
                                 "sent: send them, or drop new ones in an hour, when "
                                 "unsent ones are let go.")
            folder = self._folder()
            item_id = secrets.token_hex(12)
            item = Spooled(item_id, session, ship, name, size, folder / item_id, self.clock())
            self._items[item_id] = item
        return item

    def take(self, session: str, ship: str, ids: list[str]) -> list[Spooled]:
        """The complete uploads with these ids, for this session and Ship, now owned
        by the caller (who discards them)."""
        with self._lock:
            self._expire_locked()
            found = [self._items.get(i) for i in dict.fromkeys(ids)]
            if not ids or any(i is None or i.session != session or i.ship != ship
                              or not i.complete for i in found):
                raise FleetError("Those files are no longer waiting to be sent (an hour "
                                 "passed, or FleetLedger restarted): drop them again.")
            for item in found:
                del self._items[item.id]
        return found

    def discard(self, items: list[Spooled]) -> None:
        with self._lock:
            for item in items:
                self._items.pop(item.id, None)
                with contextlib.suppress(OSError):
                    item.path.unlink()

    def _expire_locked(self) -> None:
        now = self.clock()
        for item in [i for i in self._items.values() if now - i.at > SPOOL_TTL]:
            del self._items[item.id]
            with contextlib.suppress(OSError):
                item.path.unlink()


def spool_of(ui: Any) -> Spool:
    if ui.spool is None:
        ui.spool = Spool()
    return ui.spool


def display_name(raw: str) -> str:
    """The file's own name as the browser sent it (percent-encoded), without any
    control character, for messages; the Ship cleans it again before storing it."""
    try:
        name = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        name = ""
    name = "".join(ch for ch in name if ch.isprintable())[:NAME_MAX].strip()
    return name.replace("\\", "/").rsplit("/", 1)[-1] or "file"


# --- the tab ------------------------------------------------------------------------


def tab_listing(app: Any, name: str) -> dict[str, Any]:
    """What the Inbox tab shows: the files there, or why there is no list."""
    from fleetledger.ops import Fleet

    fleet = Fleet.from_app(app)
    found: dict[str, Any] = {"inbox": None, "inbox_error": None, "inbox_hint": None,
                             "INBOX_MAX": inbox.MAX_BYTES, "INBOX_FILES": inbox.MAX_FILES}
    try:
        found["inbox"] = inbox.listing(fleet, fleet.inventory.ship(name))
    except FleetError as exc:
        found["inbox_error"], found["inbox_hint"] = exc.message, exc.hint
    return found


async def tab_context(app: Any, name: str) -> dict[str, Any]:
    return await run_in_threadpool(tab_listing, app, name)


# --- uploading -----------------------------------------------------------------------


def _json_error(status: int, text: str) -> JSONResponse:
    return JSONResponse({"error": text}, status_code=status)


async def upload(request: Request) -> Response:
    """One file's bytes, streamed to the spool (the gate checked CSRF and the size)."""
    from fleetledger.web.app import _known_ship, session_of, ui_of

    name = _known_ship(request)
    if name is None:
        return _json_error(404, "No such Ship in the inventory.")
    ui = ui_of(request)
    size = request.scope["state"].get("upload_length", 0)
    shown = display_name(request.headers.get("x-file-name", ""))
    if size > min(inbox.MAX_BYTES, MAX_UPLOAD):
        return _json_error(413, inbox.too_big(shown, size))
    if ui.jobs.busy([name]):  # its send would be refused: say so before the upload
        return _json_error(409, f"Something is running on {name}; drop the files again "
                           "once it has finished.")
    spool = spool_of(ui)
    try:
        item = spool.new(session_of(request).id, name, shown, size)
    except FleetError as exc:
        return _json_error(429, exc.message)
    got = 0
    try:
        fd = os.open(item.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            async for chunk in request.stream():
                got += len(chunk)
                if got > size:
                    break
                await run_in_threadpool(fh.write, chunk)
    except ClientDisconnect:
        spool.discard([item])
        return Response(status_code=400)
    except OSError as exc:
        spool.discard([item])
        return _json_error(507, f"Cannot keep {shown} here to send it ({exc.strerror}).")
    if got != size:
        spool.discard([item])
        return _json_error(400, f"The upload of {shown} was cut short; drop it again.")
    item.complete = True
    return JSONResponse({"id": item.id, "name": shown, "size": size,
                         "stored_as": inbox.clean_name(shown)})


def start_send(ui: Any, name: str, items: list[Spooled], tell: bool) -> Any:
    """Send spooled files to a Ship as one job (fleetledger.inbox.put, as `ship-fleet
    put` runs it). The spool files go once the job ends, whatever happened."""
    from fleetledger.ops import Fleet
    from fleetledger.web import actions
    from fleetledger.web.jobs import BrowserApp

    files = [inbox.Upload(item.path, item.name, item.size) for item in items]
    count = f"{len(files)} file{'s' if len(files) != 1 else ''}"

    def run(job: Any) -> bool:
        drawn: dict[int, float] = {}
        job.result["files"] = [{"name": f.name, "size": f.size, "sent": 0, "state": "waiting",
                                "stored": None, "error": None} for f in files]

        def on_file(i: int, state: dict[str, Any]) -> None:
            job.result["files"][i] = dict(state)
            done = state["sent"] / max(state["size"], 1)
            if state["state"] != "sending" or done - drawn.get(i, -1) >= PROGRESS_STEP:
                drawn[i] = done
                job.version += 1  # its card redraws (the htmx poll)

        try:
            fleet = Fleet.from_app(BrowserApp(ui.app, job))
            outcome = inbox.put(fleet, fleet.inventory.ship(name), files, tell=tell,
                                on_event=lambda ship, text: job.write(f"{ship}: {text}"),
                                on_file=on_file)
        finally:
            spool_of(ui).discard(items)
        job.result["told"] = outcome.data.get("told")
        job.outcomes = [actions.Outcome.of(outcome)]
        return outcome.ok

    try:
        return ui.jobs.start("inbox", [name], run, f"Send {count} to {name}'s inbox")
    except FleetError:
        spool_of(ui).discard(items)
        raise


async def send(request: Request) -> Response:
    """The spooled files, sent to the Ship as a job (the drop zone posts this form)."""
    from fleetledger.web.app import (
        NO_SUCH_SHIP,
        _known_ship,
        back,
        form,
        not_found,
        one,
        session_of,
        ui_of,
    )

    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    ids = [i for i in fields.get("file", []) if i]
    where = f"/ships/{name}/inbox"
    if len(ids) > inbox.MAX_FILES:
        ui.flash(session, "bad", f"At most {inbox.MAX_FILES} files at once.")
        return back(where)
    try:
        items = spool_of(ui).take(session.id, name, ids)
        start_send(ui, name, items, tell=one(fields, "tell") == "yes")
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(where)
    word = "file" if len(items) == 1 else "files"
    ui.flash(session, "ok", f"Sending {len(items)} {word} to {name}; the progress is above.")
    return back(where)


async def remove(request: Request) -> Response:
    """Remove one file from the inbox (ship-fleet inbox rm), after the confirm dialog."""
    from fleetledger.ops import Fleet
    from fleetledger.web.actions import Outcome
    from fleetledger.web.app import (
        NO_SUCH_SHIP,
        _flash_outcome,
        _known_ship,
        back,
        form,
        not_found,
        one,
        session_of,
        ui_of,
    )

    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    where = f"/ships/{name}/inbox"
    values = fields.get("name", [])
    target = values[0] if len(values) == 1 else ""  # exactly as listed: never stripped
    if one(fields, "confirm") != "yes":
        ui.flash(session, "bad", "Tick the box to confirm.")
        return back(where)

    def run() -> Outcome:
        fleet = Fleet.from_app(ui.app)
        try:
            return Outcome.of(inbox.remove(fleet, fleet.inventory.ship(name), [target]))
        except FleetError as exc:
            return Outcome(name, False, exc.message + (f" {exc.hint}" if exc.hint else ""))

    _flash_outcome(ui, session, await run_in_threadpool(run))
    return back(where)


def routes() -> list[Route]:
    return [
        Route("/ships/{name}/inbox/upload", upload, methods=["POST"]),
        Route("/ships/{name}/inbox/send", send, methods=["POST"]),
        Route("/ships/{name}/inbox/remove", remove, methods=["POST"]),
    ]
