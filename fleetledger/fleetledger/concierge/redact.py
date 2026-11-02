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
    text = EMAIL.sub("[email]", text)
    return LONG_TOKEN.sub("[redacted]", text)


def tail(text: str, lines: int = 4, secrets: Iterable[str] = ()) -> str:
    kept = [line.strip() for line in text.splitlines() if line.strip()][-lines:]
    return redact("\n".join(kept), secrets)
