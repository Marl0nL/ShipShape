"""The concierge in your terminal: links on screen, codes read without echo."""

from __future__ import annotations

import getpass
import sys
import webbrowser
from typing import TextIO


def _minutes(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"about {minutes} minute{'s' if minutes != 1 else ''}"


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
    ) -> None:
        self.ship = ship
        self.label = label
        self.stream = stream or sys.stderr
        self.open_browser = open_browser
        self.read_secret = read_secret
        self.read_line = read_line
        self._ticking = False

    def _say(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def show_link(self, url: str, code: str | None, expires_in: float) -> None:
        self._say(f"\n{self.label} sign-in for {self.ship}")
        self._say(f"  1. Open {url}")
        if code:
            self._say(f"  2. Enter the code {code} and approve.")
        else:
            self._say("  2. Approve, then copy the code the page shows.")
        self._say(f"  It stays valid for {_minutes(expires_in)}.")
        if self.open_browser:
            webbrowser.open(url)

    def ask_code(self) -> str | None:
        try:
            return self.read_secret("  Paste the code (hidden; empty to cancel): ")
        except (EOFError, KeyboardInterrupt):
            return None

    def waiting(self, remaining: float) -> None:
        if not self.stream.isatty():
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
