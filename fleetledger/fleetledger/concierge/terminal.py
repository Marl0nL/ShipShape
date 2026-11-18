"""The concierge in your terminal: the link on a line of its own (a hyperlink in a
terminal), a one-time code shown as you paste it, a secret read without echo."""

from __future__ import annotations

import getpass
import re
import sys
import webbrowser
from typing import TextIO

# What an OSC 8 hyperlink may carry: printable ASCII, no spaces (the spec's URI bytes).
_LINKABLE = re.compile(r"[\x21-\x7e]+")
# Bracketed-paste markers, in case the terminal sends them to a plain line read.
_PASTE_MARKS = re.compile(r"\x1b\[20[01]~")


def _minutes(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"about {minutes} minute{'s' if minutes != 1 else ''}"


def hyperlink(url: str) -> str:
    """url as an OSC 8 hyperlink whose text is the url itself, so what you see is what
    opens; as it is when it is not a plain URL."""
    if not _LINKABLE.fullmatch(url):
        return url
    return f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\"


def received(code: str) -> str:
    """One short line saying what was pasted: all of a short code, the ends of a long one."""
    shown = code if len(code) <= 40 else f"{code[:16]}…{code[-12:]}"
    return f"  Received {shown} ({len(code)} characters)."


def _read_stdin_line(prompt: str, stream: TextIO) -> str:
    """One line from stdin, echoed by the terminal as usual; the prompt on stream."""
    stream.write(prompt)
    stream.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\r\n")


class TerminalPrompter:
    def __init__(
        self,
        ship: str,
        label: str,
        *,
        stream: TextIO | None = None,
        open_browser: bool = False,
        read_secret=getpass.getpass,
        read_line=input,
        read_code=None,
    ) -> None:
        self.ship = ship
        self.label = label
        self.stream = stream or sys.stderr
        self.open_browser = open_browser
        self.read_secret = read_secret
        self.read_line = read_line
        # (prompt) -> the line typed, echoed: the one-time code
        self.read_code = read_code or (lambda prompt: _read_stdin_line(prompt, self.stream))
        self._ticking = False

    def _say(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def _isatty(self) -> bool:
        try:
            return self.stream.isatty()
        except (AttributeError, ValueError):
            return False

    def show_link(self, url: str, code: str | None, expires_in: float) -> None:
        self._say(f"\n{self.label} sign-in for {self.ship}")
        self._say(f"  Open this URL (valid for {_minutes(expires_in)}):")
        # On a line of its own, undecorated, so a terminal that finds plain URLs finds
        # all of it, and it copies whole; a hyperlink too where the terminal takes them.
        self._say(hyperlink(url) if self._isatty() else url)
        if code:
            self._say(f"  Enter the code {code} there and approve.")
        else:
            self._say("  Approve, then copy the code the page shows and paste it below.")
        if self.open_browser:
            webbrowser.open(url)

    def _ask(self, read, prompt: str) -> str | None:
        """read(prompt) until it answers; the first Ctrl+C asks for a second to cancel."""
        warned = False
        while True:
            try:
                return read(prompt)
            except EOFError:
                return None
            except KeyboardInterrupt:
                if warned:
                    self._say()
                    return None
                warned = True
                self._say("\n  Press Ctrl+C again to cancel the sign-in, or paste the code.")

    def ask_code(self) -> str | None:
        """A pasted answer that may be a secret: read without echo."""
        answer = self._ask(self.read_secret,
                           "  Paste it here and press Enter (hidden; empty to cancel): ")
        if answer and answer.strip():
            self._say(f"  Received {len(answer.strip())} characters.")
        return answer

    def ask_one_time_code(self) -> str | None:
        """A one-time code: shown as it is typed or pasted, and confirmed after Enter."""
        self._say("  Paste the code here and press Enter (empty to cancel):")
        answer = self._ask(self.read_code, "  > ")
        if answer is None:
            return None
        answer = _PASTE_MARKS.sub("", answer).strip()
        if answer:
            self._say(received(answer))
        return answer

    def waiting(self, remaining: float) -> None:
        if not self._isatty():
            return
        minutes, seconds = divmod(int(max(0, remaining)), 60)
        left = f"{minutes}:{seconds:02d}"
        self.stream.write(f"\r  Waiting for you to finish in the browser ({left} left) ")
        self.stream.flush()
        self._ticking = True

    def offer_live(self, reason: str, seen: str) -> bool:
        if self._ticking:
            self._say()
        self._say(f"\n  {reason} The sign-in tool may have changed its wording.")
        if seen:
            self._say("  Its last output (redacted):")
            for line in seen.splitlines():
                self._say(f"    {line}")
        if not sys.stdin.isatty():
            return False
        try:
            answer = self.read_line("  Take over the Ship's terminal and finish by hand? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            return False
        if answer.strip().lower() in ("y", "yes"):
            self._say("  You are now in the Ship's terminal. Nothing is recorded.")
            return True
        return False
