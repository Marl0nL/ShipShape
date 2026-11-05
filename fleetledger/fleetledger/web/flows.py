"""The login concierge in the browser.

The same engine and rules as `ship-fleet login` (fleetledger.concierge): the
tool's own sign-in runs on the Ship as agent, in a terminal over SSH; you see
the link (and device code); the code you paste is typed into that terminal and
nowhere else; the account is checked against the Ship's tags; the audit log
records who, what and when.

Only the front end differs. WebPrompter is the engine's Prompter: it publishes
the link to the page and waits for the code the page posts. WebChild wraps the
terminal so that "take over the live terminal" bridges it to a WebSocket in
the browser instead of this process's own terminal.

What is kept, and for how long:
- the pasted code: handed straight to the engine through a one-slot queue,
  never stored on the flow, never logged, never echoed back to the page;
- the sign-in link and device code: on the flow only while it waits for you,
  wiped when it finishes;
- live terminal output: a bounded in-memory buffer while you have the terminal,
  dropped when it closes. No transcript is ever written.
"""

from __future__ import annotations

import itertools
import queue
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pexpect

from fleetledger import concierge
from fleetledger.concierge import engine, providers
from fleetledger.concierge.providers import Provider
from fleetledger.errors import FleetError

# A sign-in link is shown as a link only if it is https and has no characters
# that could break out of a URL; anything else is shown as text (a tampered tool
# on a Ship could print anything).
SAFE_URL = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s\"'<>\\`]*)?$")
MAX_CODE = 2048
LIVE_DECISION_WAIT = 600.0
LIVE_IDLE = 900.0
TERMINAL_BUFFER = 64 * 1024
MAX_INPUT = 4096
KEEP_FINISHED = 50

# States a flow moves through.
QUEUED = "queued"
STARTING = "starting"  # reaching the Ship, waiting for the link
LINK = "link"  # link shown; device-code flows wait for you here
CODE = "code"  # paste-back: the tool asks for the code
CHECKING = "checking"  # code typed (or device flow done); waiting for the verdict and checks
LIVE_OFFERED = "live-offered"
LIVE = "live"
DONE = "done"
WAITING_FOR_YOU = (LINK, CODE, LIVE_OFFERED, LIVE)


def safe_url(url: str | None) -> str | None:
    return url if url and SAFE_URL.match(url) else None


class TerminalBridge:
    """The live terminal between the engine's thread and a WebSocket."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._out: deque[str] = deque()
        self._out_size = 0
        self._offset = 0  # characters dropped from the front of _out
        self._input: queue.Queue[str] = queue.Queue(maxsize=256)
        self.closed = threading.Event()
        self.ended = threading.Event()
        self.last_activity = clock()

    def output(self, text: str) -> None:
        with self._lock:
            self._out.append(text)
            self._out_size += len(text)
            while self._out_size > TERMINAL_BUFFER and len(self._out) > 1:
                dropped = self._out.popleft()
                self._out_size -= len(dropped)
                self._offset += len(dropped)

    def read_since(self, position: int) -> tuple[str, int]:
        """Output after `position` (a running character count), and the new position."""
        with self._lock:
            text = "".join(self._out)
            end = self._offset + len(text)
            start = max(position, self._offset)
            return text[start - self._offset :], end

    def send(self, text: str) -> bool:
        if self.closed.is_set() or not text or len(text) > MAX_INPUT:
            return False
        self.last_activity = self.clock()
        try:
            self._input.put_nowait(text)
        except queue.Full:
            return False
        return True

    def drain(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self._input.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        self.closed.set()

    def wipe(self) -> None:
        with self._lock:
            self._out.clear()
            self._out_size = 0
        self.drain()


@dataclass
class WebFlow:
    """One sign-in of one Ship to one service, as the pages show it."""

    id: str
    batch: str
    ship: str
    provider: str
    label: str
    mode: str
    state: str = QUEUED
    url: str | None = None
    unsafe_url: bool = False
    device_code: str | None = None
    deadline: float | None = None  # wall clock, for the countdown
    reason: str | None = None  # why the live terminal is offered
    seen: str | None = None  # redacted last output, when the live terminal is offered
    outcome: str | None = None  # ok | wrong-account | failed | timeout | cancelled
    detail: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    version: int = 0
    started_at: float = field(default_factory=time.time)
    terminal: TerminalBridge | None = None
    _codes: queue.Queue[str | None] = field(default_factory=lambda: queue.Queue(maxsize=1))
    _live: queue.Queue[bool] = field(default_factory=lambda: queue.Queue(maxsize=1))
    _cancel: threading.Event = field(default_factory=threading.Event)
    _child: Any = None

    @property
    def done(self) -> bool:
        return self.state == DONE

    @property
    def needs_you(self) -> bool:
        return self.state in WAITING_FOR_YOU

    def set(self, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(self, key, value)
        self.version += 1

    # --- from the page -------------------------------------------------------
    def submit_code(self, code: str) -> str | None:
        """Hand a pasted code to the engine. Returns why it was refused, or None."""
        code = (code or "").strip()
        if self.mode != "paste-back":
            return "This sign-in does not take a pasted code."
        if self.state not in (LINK, CODE):
            return "This sign-in is not waiting for a code."
        if not code:
            return "Paste the code first."
        if len(code) > MAX_CODE or any(ch < " " for ch in code):
            return "That does not look like a sign-in code."
        try:
            self._codes.put_nowait(code)
        except queue.Full:
            return "A code is already on its way."
        self.set(state=CHECKING)
        return None

    def decide_live(self, take_over: bool) -> bool:
        if self.state != LIVE_OFFERED:
            return False
        try:
            self._live.put_nowait(take_over)
        except queue.Full:
            return False
        return True

    def cancel(self) -> None:
        self._cancel.set()
        for q, value in ((self._codes, None), (self._live, False)):
            try:
                q.put_nowait(value)
            except queue.Full:
                pass
        if self.terminal is not None:
            self.terminal.close()
        child = self._child
        if child is not None:
            try:
                child.terminate(force=True)
            except Exception:  # noqa: BLE001 - it may have ended already
                pass

    def finish(self, outcome: str, detail: str = "", **extra: Any) -> None:
        if self.terminal is not None:
            self.terminal.close()
            self.terminal.wipe()
        self.set(
            state=DONE,
            outcome=outcome,
            detail=detail,
            url=None,
            device_code=None,
            seen=None,
            deadline=None,
            terminal=None,
            **extra,
        )


class WebPrompter:
    """The engine's Prompter (concierge.engine.Prompter), answered from the browser."""

    def __init__(self, flow: WebFlow, *, wall: Callable[[], float] = time.time) -> None:
        self.flow = flow
        self.wall = wall

    def show_link(self, url: str, code: str | None, expires_in: float) -> None:
        shown = safe_url(url)
        self.flow.set(
            state=LINK,
            url=shown,
            unsafe_url=shown is None,
            device_code=code,
            deadline=self.wall() + max(0.0, expires_in),
        )

    def ask_code(self) -> str | None:
        flow = self.flow
        if flow.state == LINK:
            # No version bump: the page shows LINK and CODE alike, and a redraw
            # would wipe a code being pasted.
            flow.state = CODE
        remaining = (flow.deadline or self.wall()) - self.wall()
        try:
            # Wait a little past the window: the engine itself refuses a late code.
            code = flow._codes.get(timeout=max(1.0, remaining + 5))
        except queue.Empty:
            return None
        if flow._cancel.is_set():
            return None
        if flow.state != CHECKING:
            flow.set(state=CHECKING)
        return code

    def waiting(self, remaining: float) -> None:
        pass  # the page counts down from the deadline itself

    def offer_live(self, reason: str, seen: str) -> bool:
        flow = self.flow
        if flow._cancel.is_set():
            return False
        flow.set(state=LIVE_OFFERED, reason=reason, seen=seen)
        try:
            take_over = flow._live.get(timeout=LIVE_DECISION_WAIT)
        except queue.Empty:
            take_over = False
        if take_over and not flow._cancel.is_set():
            flow.set(state=LIVE, terminal=TerminalBridge())
            return True
        return False


class WebChild:
    """The engine's terminal child, with interact() bridged to the browser."""

    def __init__(self, child: Any, flow: WebFlow, clock: Callable[[], float] = time.monotonic):
        self._child = child
        self._flow = flow
        self._clock = clock
        flow._child = child

    def __getattr__(self, name: str) -> Any:
        return getattr(self._child, name)

    @property
    def exitstatus(self) -> int | None:
        return self._child.exitstatus

    def interact(self) -> None:
        """Pump the terminal to and from the page until it ends or you leave it."""
        bridge = self._flow.terminal or TerminalBridge(self._clock)
        while not bridge.closed.is_set() and not self._flow._cancel.is_set():
            try:
                bridge.output(self._child.read_nonblocking(65536, timeout=0.2))
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                break
            for text in bridge.drain():
                self._child.send(text)
            if self._clock() - bridge.last_activity > LIVE_IDLE:
                break
            if not self._child.isalive():
                try:  # whatever it printed last
                    bridge.output(self._child.read_nonblocking(65536, timeout=0.1))
                except (pexpect.TIMEOUT, pexpect.EOF):
                    pass
                break
        bridge.ended.set()


def _check_dict(check: concierge.CheckResult) -> dict[str, Any]:
    return {
        "service": check.service,
        "signed_in": check.signed_in,
        "account": check.account,
        "matches": check.matches,
        "expected": list(check.expected),
    }


class Concierge:
    """Every sign-in started from the browser, single or batch."""

    def __init__(
        self,
        app: Any,  # cli.App
        *,
        on_finished: Callable[[str], None] | None = None,
        provider_loader: Callable[[str], list[Provider]] = providers.resolve,
        flow_options: dict[str, Any] | None = None,
    ) -> None:
        self.app = app
        self.flow_options = flow_options  # engine.Flow options (tests shorten timeouts)
        self.on_finished = on_finished or (lambda ship: None)
        self.provider_loader = provider_loader
        self._lock = threading.Lock()
        self._flows: dict[str, WebFlow] = {}
        self._order: list[str] = []
        self._counter = itertools.count(1)

    # --- reading --------------------------------------------------------------
    def get(self, flow_id: str) -> WebFlow | None:
        with self._lock:
            return self._flows.get(flow_id)

    def flows(self) -> list[WebFlow]:
        with self._lock:
            return [self._flows[i] for i in reversed(self._order)]

    def batch(self, batch_id: str) -> list[WebFlow]:
        with self._lock:
            return [self._flows[i] for i in self._order if self._flows[i].batch == batch_id]

    def active(self) -> list[WebFlow]:
        return [f for f in self.flows() if not f.done]

    @property
    def version(self) -> int:
        with self._lock:
            return sum(f.version for f in self._flows.values()) + len(self._flows)

    # --- starting -------------------------------------------------------------
    def start(self, ships: list[str], service: str) -> str:
        """Queue one flow per Ship and provider; they run one after another.

        Returns the batch id. A Ship and provider with a flow still open is not
        queued twice.
        """
        from fleetledger import signins

        chosen = self.provider_loader(service)
        batch_id = secrets.token_hex(6)
        queued: list[tuple[WebFlow, Provider]] = []
        try:
            inventory = self.app.store_factory().load()
        except FleetError:
            inventory = None
        skipped = 0
        with self._lock:
            busy = {(f.ship, f.provider) for f in self._flows.values() if not f.done}
            for ship in ships:
                for provider in chosen:
                    if (ship, provider.id) in busy:
                        continue
                    if (inventory is not None and inventory.has_ship(ship)
                            and not signins.provider_on(inventory.ship(ship), provider.id)):
                        skipped += 1  # off on this Ship: no sign-in flow
                        continue
                    flow = WebFlow(
                        id=f"{next(self._counter)}-{secrets.token_hex(4)}",
                        batch=batch_id,
                        ship=ship,
                        provider=provider.id,
                        label=provider.label,
                        mode=provider.mode,
                    )
                    self._flows[flow.id] = flow
                    self._order.append(flow.id)
                    queued.append((flow, provider))
            self._trim()
        if not queued:
            raise FleetError("Those Ships do not use that sign-in." if skipped else
                             "Those sign-ins are already under way.")
        threading.Thread(
            target=self._run_batch, args=(queued,), name=f"signin-{batch_id}", daemon=True
        ).start()
        return batch_id

    def _trim(self) -> None:
        finished = [i for i in self._order if self._flows[i].done]
        for flow_id in finished[: max(0, len(finished) - KEEP_FINISHED)]:
            del self._flows[flow_id]
            self._order.remove(flow_id)

    def _run_batch(self, queued: list[tuple[WebFlow, Provider]]) -> None:
        for flow, provider in queued:
            if flow._cancel.is_set():
                flow.finish("cancelled", "Cancelled before it started.")
                continue
            self.run_flow(flow, provider)

    def run_flow(self, flow: WebFlow, provider: Provider) -> None:
        flow.set(state=STARTING)
        base_spawner = self.app.spawner or engine.spawn
        try:
            inv = self.app.store_factory().load()
            ship = inv.ship(flow.ship)
            ssh = self.app.ssh_factory(inv)
            result = concierge.login(
                ssh,
                inv,
                ship,
                provider,
                WebPrompter(flow),
                spawner=lambda argv: WebChild(base_spawner(argv), flow),
                flow_options=self.flow_options,
            )
        except FleetError as exc:
            flow.finish("failed", exc.message)
        except Exception as exc:  # noqa: BLE001 - never show internals; they could hold output
            flow.finish("failed", f"Internal error ({type(exc).__name__}).")
        else:
            outcome = "cancelled" if flow._cancel.is_set() else result.outcome
            detail = result.detail
            if outcome == "cancelled" and not detail:
                detail = "Cancelled."
            flow.finish(
                outcome,
                detail,
                checks=[_check_dict(c) for c in result.checks],
                notes=list(result.notes),
            )
        finally:
            flow._child = None
        try:
            self.on_finished(flow.ship)
        except Exception:  # noqa: BLE001 - a status refresh must not break the concierge
            pass

    def cancel_all(self) -> None:
        for flow in self.active():
            flow.cancel()
