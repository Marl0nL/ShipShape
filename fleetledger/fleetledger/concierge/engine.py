"""Drive one sign-in flow in a terminal on the Ship.

The CLI runs in a real terminal (`ssh -tt`), because these tools behave
differently without one. The engine reads its output itself (escape sequences
stripped), matches the provider's patterns, shows you the link (and device
code), and for paste-back flows types the code you paste into the terminal,
so it never appears in a command line, ps or shell history.

Nothing is logged and no transcript is kept: the buffer lives in memory for
the length of the flow. If the patterns miss (a CLI changed its wording), you
can take over the live terminal and finish by hand.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import pexpect

from fleetledger.concierge.providers import Provider
from fleetledger.concierge.redact import tail, visible

# How long to wait for the link (or, after it, the code prompt) before
# offering the live terminal.
PATTERN_TIMEOUT = 60.0
EXIT_GRACE = 30.0
# Once a code has been typed, wait this long for the tool's verdict even if the
# sign-in window closes meanwhile: the code is spent, and the answer matters.
VERDICT_GRACE = 120.0


class Child(Protocol):
    """The parts of pexpect.spawn the engine uses."""

    def read_nonblocking(self, size: int, timeout: float) -> str: ...

    def send(self, text: str) -> int: ...

    def isalive(self) -> bool: ...

    def terminate(self, force: bool = False) -> bool: ...

    def interact(self) -> None: ...

    def close(self) -> None: ...

    @property
    def exitstatus(self) -> int | None: ...


class Prompter(Protocol):
    """How the engine talks to you: concierge/terminal.TerminalPrompter in a terminal,
    web/flows.WebPrompter in the browser."""

    def show_link(self, url: str, code: str | None, expires_in: float) -> None: ...

    def ask_code(self) -> str | None:
        """The code you pasted, or None to cancel. Never echoed or stored."""
        ...

    def waiting(self, remaining: float) -> None: ...

    def offer_live(self, reason: str, seen: str) -> bool:
        """Offer the live terminal; seen is the (redacted) output so far."""
        ...


@dataclass(frozen=True)
class FlowResult:
    outcome: str  # completed | manual | failed | timeout | cancelled
    detail: str = ""  # safe to show and to log: redacted
    exitstatus: int | None = None


def _value(match: re.Match[str]) -> str:
    return match.group(1) if match.re.groups else match.group(0)


def _settled(match: re.Match[str] | None, buffer: str) -> bool:
    """A match counts once more output follows it, so a URL is never cut short."""
    return match is not None and match.end() < len(buffer)


class Flow:
    def __init__(
        self,
        provider: Provider,
        child: Child,
        prompter: Prompter,
        *,
        clock: Callable[[], float] = time.monotonic,
        pattern_timeout: float = PATTERN_TIMEOUT,
        read_timeout: float = 0.5,
    ) -> None:
        self.p = provider
        self.child = child
        self.prompter = prompter
        self.clock = clock
        self.pattern_timeout = pattern_timeout
        self.read_timeout = read_timeout
        self.raw = ""  # everything read, escape sequences included
        self.buffer = ""  # what a person would see: visible(raw)
        self.secrets: list[str] = []
        self.send_from = 0
        self.outcome_from = 0
        self.url: str | None = None
        self.code: str | None = None
        self.shown_at: float | None = None
        self.code_sent = False
        self.sent_at: float | None = None
        self.deadline = 0.0

    # --- output -----------------------------------------------------------
    def _read(self) -> bool:
        """Read what is available; False once the process has ended."""
        try:
            chunk = self.child.read_nonblocking(65536, timeout=self.read_timeout)
        except pexpect.TIMEOUT:
            return True
        except pexpect.EOF:
            return False
        self.raw += chunk
        self.buffer = visible(self.raw)
        return True

    def _answer_prompts(self) -> None:
        for pattern, text in self.p.send:
            match = pattern.search(self.buffer, self.send_from)
            if match:
                self.child.send(text)
                self.send_from = max(self.send_from, match.end())

    def _safe_tail(self) -> str:
        return tail(self.buffer, secrets=self.secrets)

    def _finish(self, outcome: str, detail: str = "") -> FlowResult:
        status = None
        try:
            if self.child.isalive():
                self.child.terminate(force=True)
            self.child.close()
            status = self.child.exitstatus
        except Exception:  # noqa: BLE001 - closing must never mask the outcome
            pass
        self.raw = self.buffer = ""  # no transcript outlives the flow
        return FlowResult(outcome, detail, status)

    # --- the flow ---------------------------------------------------------
    def _maybe_show(self, now: float, deadline: float) -> None:
        """Show the link (and device code) once both are fully on screen."""
        url = self.p.url.search(self.buffer)
        code = self.p.code.search(self.buffer) if self.p.code else None
        if not _settled(url, self.buffer):
            return
        if self.p.code is not None and not _settled(code, self.buffer):
            return
        self.url = _value(url)
        self.code = _value(code) if code else None
        self.shown_at = now
        self.outcome_from = len(self.buffer)
        self.prompter.show_link(self.url, self.code, deadline - now)

    def _maybe_paste(self) -> str | None:
        """Type the pasted code once the CLI asks for it.

        Returns None to carry on, or the reason nothing was typed.
        """
        if not (self.p.prompt and self.p.prompt.search(self.buffer)):
            return None
        typed = (self.prompter.ask_code() or "").strip()
        if not typed:
            return "Cancelled; nothing was typed."
        if self.clock() > self.deadline:
            return "The sign-in window closed before the code arrived; nothing was typed."
        self.secrets.append(typed)
        self.child.send(typed + "\r")
        self.code_sent = True
        self.sent_at = self.clock()
        self.outcome_from = len(self.buffer)
        return None

    def run(self) -> FlowResult:
        start = self.clock()
        deadline = self.deadline = start + self.p.timeout
        paste_back = self.p.mode == "paste-back"
        while True:
            alive = self._read()
            self._answer_prompts()
            now = self.clock()

            if self.shown_at is None:
                self._maybe_show(now, deadline)
            if self.shown_at is not None and paste_back and not self.code_sent:
                reason = self._maybe_paste()
                if reason:
                    return self._finish("cancelled", reason)
                now = self.clock()

            region = self.buffer[self.outcome_from :] if self.shown_at is not None else ""
            if region and self.p.success.search(region):
                return self._wait_exit("completed")
            if region and self.p.failure and self.p.failure.search(region):
                return self._finish("failed", self._safe_tail())

            if not alive:
                return self._ended()
            if self.code_sent:
                if now - self.sent_at > VERDICT_GRACE:
                    return self._finish("timeout", "The tool gave no verdict on the code.")
            elif now > deadline:
                return self._finish("timeout", "The sign-in code expired before it was used.")
            if self.shown_at is None and now - start > self.pattern_timeout:
                return self._live("No sign-in link appeared.")
            waited = now - self.shown_at if self.shown_at is not None else 0
            if paste_back and not self.code_sent and waited > self.pattern_timeout:
                return self._live("The prompt for the code did not appear.")
            if self.shown_at is not None and not paste_back:
                self.prompter.waiting(deadline - now)

    def _ended(self) -> FlowResult:
        """The CLI exited before a success line was seen."""
        result = self._finish("failed", self._safe_tail())
        if result.exitstatus == 0 and self.shown_at is not None:
            # It finished cleanly but said something we did not expect; the
            # checks that follow decide whether the sign-in worked.
            return FlowResult("completed", "", 0)
        return result

    def _wait_exit(self, outcome: str) -> FlowResult:
        end = self.clock() + EXIT_GRACE
        while self.clock() < end and self._read():
            self._answer_prompts()
        return self._finish(outcome)

    def _live(self, reason: str) -> FlowResult:
        if not self.prompter.offer_live(reason, self._safe_tail()):
            return self._finish("failed", reason)
        self.child.interact()
        result = self._finish("manual")
        return FlowResult("manual", reason, result.exitstatus)


def spawn(argv: list[str], *, columns: int = 4096) -> pexpect.spawn:
    """Start argv in a pseudo-terminal wide enough that links never wrap."""
    return pexpect.spawn(
        argv[0],
        argv[1:],
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(50, columns),
        echo=False,
        logfile=None,
    )
