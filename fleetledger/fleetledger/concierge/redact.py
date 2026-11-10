"""Clean terminal output for matching, and redact anything that might be secret.

The concierge keeps no transcript. The only remote text it ever shows you is
the sign-in link and device code it extracted, and on failure a few lines of
output passed through redact() first.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (e.g. hyperlinks), keeps nothing
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI (colours, cursor moves)
    r"|\x1b[@-Z\\-_]"  # other two-byte escapes
)
QUERY = re.compile(r"(https?://[^\s?#]+)[?#]\S*")
LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=.]{24,}")  # also catches UUIDs
EMAIL = re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# EMAIL, starting only where a run of local-part characters starts. EMAIL.sub
# alone re-scans a long run (a base64 blob, a progress bar) from each of its
# positions, which is quadratic; a match from inside a run is the same as one
# from its start, so nothing is lost.
EMAIL_AT_RUN = re.compile(r"(?<![\w.+-])" + EMAIL.pattern)


def clean(text: str) -> str:
    """Terminal output without escape sequences or carriage returns."""
    text = ANSI.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch >= " ")


def visible(raw: str) -> str:
    """clean() of everything read so far, minus an escape sequence still arriving.

    Reads can split a sequence (an OSC 8 hyperlink is hundreds of bytes); its
    first half must not be cleaned on its own, or its leftovers would run into
    the link next to it. So a trailing unfinished sequence waits for more input.
    """
    cut = raw.rfind("\x1b")
    if cut != -1 and not ANSI.match(raw, cut):
        raw = raw[:cut]
    return clean(raw)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove typed codes, URL query strings, email addresses and long token-like
    strings (UUIDs included). Run it on clean() text: an escape sequence inside a
    value would hide it from these patterns."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[code]")
    text = QUERY.sub(r"\1?[…]", text)
    text = redact_emails(text)
    return LONG_TOKEN.sub("[redacted]", text)


def redact_emails(text: str) -> str:
    """EMAIL.sub("[email]", text), in linear time."""
    parts, pos = [], 0
    while found := EMAIL_AT_RUN.search(text, pos):
        parts += [text[pos:found.start()], "[email]"]
        pos = found.end()
        # A match can end where the next local part starts ("a@example.com_c@example.org"):
        # EMAIL.sub would match there too, with no break in the run before it.
        while found := EMAIL.match(text, pos):
            parts.append("[email]")
            pos = found.end()
    parts.append(text[pos:])
    return "".join(parts)


def tail(text: str, lines: int = 4, secrets: Iterable[str] = ()) -> str:
    kept = [line.strip() for line in text.splitlines() if line.strip()][-lines:]
    return redact("\n".join(kept), secrets)
