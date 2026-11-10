"""Questions FleetLedger asks in a terminal: a line, one of a few choices, a secret, and
a typed confirmation before something that cannot be undone.

All of them ask through the app (app.confirm, app.ask_secret, app.stdin_isatty), so
tests answer for the person.
"""

from __future__ import annotations

import getpass
import sys

from fleetledger.errors import FleetError


def ask(app, prompt: str, default: str = "") -> str:
    """One line, stripped; default when it is empty."""
    answer = app.confirm(prompt).strip()
    return answer or default


def choose(app, prompt: str, choices: tuple[str, ...], default: str) -> str:
    """One of choices (any case), asked again until it is one."""
    while True:
        answer = ask(app, f"{prompt} [{'/'.join(choices)}] ({default}): ", default).lower()
        if answer in choices:
            return answer
        app.note(f"Please answer one of: {', '.join(choices)}.")


def secret(app, prompt: str, *, strip: bool = True) -> str:
    """A secret, not shown as it is typed (app.ask_secret, else getpass on stderr).
    strip=False keeps it exactly as typed, as a passphrase may start or end in a space."""
    read = getattr(app, "ask_secret", None) or (
        lambda p: getpass.getpass(p, stream=sys.stderr))
    answer = read(prompt)
    return answer.strip() if strip else answer


def typed(app, question: str, word: str, *, no_terminal: str, refused: str,
          exact: bool = False) -> None:
    """Go on only when the person types word: FleetError(no_terminal) when nobody can
    be asked, FleetError(refused) on any other answer. A word such as yes or off is
    taken in any case; exact=True (a Ship's name) takes it only as it is spelt."""
    if not app.stdin_isatty():
        raise FleetError(no_terminal)
    answer = app.confirm(question).strip()
    if (answer if exact else answer.lower()) != word:
        raise FleetError(refused)
