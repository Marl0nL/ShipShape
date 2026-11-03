"""Just enough HuJSON (JSON with comments and trailing commas) for tailnet policies.

Tailscale policy files are HuJSON. FleetLedger reads them to check them and, only
when you opt in, adds the Ships' rules to them. It never rewrites a file from
parsed JSON (that would drop your comments and layout): it inserts new entries as
text right after the opening bracket of the right section, which HuJSON's
trailing commas make always valid, and then parses the result to be sure.
"""

from __future__ import annotations

import json
from typing import Any


class HuJSONError(ValueError):
    pass


def _scan(text: str):
    """Yield (index, char) for every character outside strings and comments."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i = _string_end(text, i)
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise HuJSONError("unclosed /* comment")
            i = end + 2
            continue
        yield i, c
        i += 1


def _string_end(text: str, i: int) -> int:
    """Index just past the string that starts at text[i] (a quote)."""
    j, n = i + 1, len(text)
    while j < n and text[j] != '"':
        j += 2 if text[j] == "\\" else 1
    if j >= n:
        raise HuJSONError("unterminated string")
    return j + 1


def to_json(text: str) -> str:
    """Standard JSON: comments replaced by spaces, trailing commas dropped. Strings
    are copied as they are, whatever they contain."""
    out: list[str] = []
    last_comma: int | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            end = _string_end(text, i)
            out.append(text[i:end])
            last_comma = None
            i = end
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise HuJSONError("unclosed /* comment")
            out.append(" ")
            i = end + 2
            continue
        if c == ",":
            last_comma = len(out)
        elif c in "}]" and last_comma is not None:
            out[last_comma] = " "
            last_comma = None
        elif not c.isspace():
            last_comma = None
        out.append(c)
        i += 1
    return "".join(out)


def parse(text: str) -> Any:
    try:
        return json.loads(to_json(text))
    except (json.JSONDecodeError, HuJSONError) as exc:
        raise HuJSONError(f"not valid HuJSON: {exc}") from None


def top_level_values(text: str) -> dict[str, int]:
    """Top-level key -> index where its value starts."""
    result: dict[str, int] = {}
    depth = 0
    i, n = 0, len(text)
    expect_key = False
    current: str | None = None
    while i < n:
        c = text[i]
        if c == '"':
            end = _string_end(text, i)
            if depth == 1 and expect_key:
                current = json.loads(text[i:end])
                expect_key = False
            i = end
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise HuJSONError("unclosed /* comment")
            i = end + 2
            continue
        if c == ":" and depth == 1 and current is not None:
            k = i + 1
            while k < n:  # skip spaces and comments between the colon and the value
                if text[k].isspace():
                    k += 1
                elif text.startswith("//", k):
                    end = text.find("\n", k)
                    k = n if end < 0 else end
                elif text.startswith("/*", k):
                    end = text.find("*/", k + 2)
                    if end < 0:
                        raise HuJSONError("unclosed /* comment")
                    k = end + 2
                else:
                    break
            result[current] = k
            current = None
        if c in "{[":
            depth += 1
            if depth == 1:
                expect_key = True
        elif c in "}]":
            depth -= 1
        elif c == "," and depth == 1:
            expect_key = True
        i += 1
    return result


def insert_first(text: str, key: str, entry: str) -> str:
    """Insert `entry,` as the first item of the top-level object or array at key."""
    starts = top_level_values(text)
    if key not in starts or text[starts[key]] not in "{[":
        raise HuJSONError(f"no top-level object or array {key!r}")
    at = starts[key] + 1
    return text[:at] + "\n    " + entry + "," + text[at:]


def add_key(text: str, key: str, value: str) -> str:
    """Add a top-level `"key": value,` right after the opening brace."""
    first = next((i for i, c in _scan(text) if not c.isspace()), None)
    if first is None or text[first] != "{":
        raise HuJSONError("the policy is not an object")
    return text[:first + 1] + f"\n  {json.dumps(key)}: {value},\n" + text[first + 1:]
