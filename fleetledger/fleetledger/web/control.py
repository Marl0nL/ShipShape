"""The running web UI's private control socket (docs/runbooks/fleetledger-service.md).

`ship-fleet ui` (and the service, `ship-fleet ui --service`) listens on a Unix socket
in FleetLedger's private runtime folder, so commands run as you on the same computer,
over ssh included, can ask it things no file can answer:

- link: a one-time sign-in link for this computer's address (127.0.0.1:PORT). Its
  token lives only in the running UI's memory, as the terminal's links do; a
  service has no terminal, so `ship-fleet ui link --local` asks here instead.
- status: its port and remote address, whether it is a service, whether it is
  stopping, and the jobs still running (`ship-fleet service status`).

Only you can reach it: the socket is 0600 in a 0700 folder, and each connection's
peer must be your own user id (SO_PEERCRED), or it is closed unanswered. One request
per connection: one line of JSON in (at most MAX_REQUEST bytes), one line out. The
link is never logged or written to a file here: the answer goes to the caller only.

A second UI on the same computer finds the socket answering and runs without one
(the first keeps it).
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fleetledger import paths
from fleetledger.errors import FleetError

SOCKET = "ui.sock"
MAX_REQUEST = 4096
TIMEOUT = 5.0
OPS = ("link", "status")
NOT_RUNNING = ("No ship-fleet ui is running here (as this user) to ask.")
NOT_RUNNING_HINT = ("Start it (ship-fleet ui, or ship-fleet service status if it runs as a "
                    "service). Over ssh, log in as the user it runs as.")


def socket_path() -> Path:
    return paths.runtime_socket(SOCKET, "Set FLEETLEDGER_RUNTIME_DIR to a shorter private "
                                        "folder.", per_home=True)


def peer_uid(conn: socket.socket) -> int | None:
    """The user id of the process at the other end of a Unix socket (Linux), else None."""
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
    except (OSError, AttributeError):
        return None
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


class Control:
    """The listening side, in the running UI: a thread answering one request per
    connection. handlers: op -> () -> a JSON-able dict."""

    def __init__(self, handlers: dict[str, Callable[[], dict[str, Any]]],
                 path: Path | None = None) -> None:
        self.handlers = handlers
        self.path = path
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> bool:
        """Listen, unless another live UI holds the socket (then False: run without).
        A socket nobody answers on is a leftover of a UI that stopped, and is replaced."""
        path = self.path or socket_path()
        self.path = path
        if path.exists() or path.is_symlink():
            if _answers(path):
                return False
            path.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(str(path))
        except OSError:
            sock.close()
            return False
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        sock.listen(8)
        sock.settimeout(1.0)
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, name="ui-control", daemon=True)
        self._thread.start()
        return True

    def _serve(self) -> None:
        sock = self._sock
        while sock is not None and not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                try:
                    self._answer(conn)
                except Exception:  # noqa: BLE001 - one bad caller never stops the socket
                    pass

    def _answer(self, conn: socket.socket) -> None:
        if peer_uid(conn) != os.getuid():
            return  # not you: closed unanswered
        conn.settimeout(TIMEOUT)
        data = b""
        while b"\n" not in data and len(data) <= MAX_REQUEST:
            chunk = conn.recv(1024)
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_REQUEST:
            return
        try:
            request = json.loads(data.split(b"\n", 1)[0] or b"{}")
        except ValueError:
            request = None
        op = request.get("op") if isinstance(request, dict) else None
        handler = self.handlers.get(op) if op in OPS else None
        if handler is None:
            answer: dict[str, Any] = {"ok": False, "error": "unknown request"}
        else:
            try:
                answer = {"ok": True, **handler()}
            except FleetError as exc:
                answer = {"ok": False, "error": exc.message}
        conn.sendall((json.dumps(answer) + "\n").encode("utf-8"))

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None and self.path is not None:
            try:  # wake the accept loop now, rather than at its next one-second tick
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as poke:
                    poke.settimeout(0.5)
                    poke.connect(str(self.path))
            except OSError:
                pass
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self.path is not None and self._thread is not None:
            self.path.unlink(missing_ok=True)


def _answers(path: Path) -> bool:
    try:
        ask("status", path=path)
    except FleetError:
        return False
    return True


def ask(op: str, path: Path | None = None) -> dict[str, Any]:
    """Ask the running UI (op: link or status). FleetError when none answers."""
    path = path or socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    try:
        try:
            sock.connect(str(path))
            sock.sendall((json.dumps({"op": op}) + "\n").encode("utf-8"))
            data = b""
            while b"\n" not in data and len(data) < 64 * 1024:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError as exc:
            raise FleetError(NOT_RUNNING, hint=NOT_RUNNING_HINT) from exc
    finally:
        sock.close()
    try:
        answer = json.loads(data.split(b"\n", 1)[0])
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise FleetError(NOT_RUNNING, hint=NOT_RUNNING_HINT)
    if not answer.get("ok"):
        raise FleetError(f"The running ship-fleet ui refused: {answer.get('error', '?')}.")
    return answer
