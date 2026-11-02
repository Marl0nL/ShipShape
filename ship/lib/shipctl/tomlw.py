"""Write TOML with the standard library: enough for ship.toml and herdr's config.

`dumps` serialises the value types ship.toml uses (strings, booleans, integers,
arrays of those, tables, arrays of tables). `set_keys` edits single keys in an
existing TOML file in place, keeping every other line, which is how ship-ctl
manages its few keys in herdr's config.toml.
"""

from __future__ import annotations

import re
import tomllib

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else _string(k)


def _string(s: str) -> str:
    out = ['"']
    for ch in s:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _is_table_array(v: object) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _emit(table: dict, prefix: tuple[str, ...], lines: list[str]) -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict) and not _is_table_array(v)}
    tables = {k: v for k, v in table.items() if isinstance(v, dict)}
    arrays = {k: v for k, v in table.items() if _is_table_array(v)}
    for k, v in scalars.items():
        lines.append(f"{_key(k)} = {_value(v)}")
    for k, v in tables.items():
        name = ".".join(_key(p) for p in (*prefix, k))
        lines.append("")
        lines.append(f"[{name}]")
        _emit(v, (*prefix, k), lines)
    for k, items in arrays.items():
        name = ".".join(_key(p) for p in (*prefix, k))
        for item in items:
            lines.append("")
            lines.append(f"[[{name}]]")
            _emit(item, (*prefix, k), lines)


def dumps(doc: dict, header: str = "") -> str:
    lines: list[str] = [f"# {line}" if line else "#" for line in header.splitlines()]
    _emit(doc, (), lines)
    text = "\n".join(lines).lstrip("\n") + "\n"
    if tomllib.loads(text) != doc:  # never write something that reads back differently
        raise ValueError("TOML round trip changed the document")
    return text


_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def _key_line(key: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(key)}\s*=")


def set_keys(text: str, settings: dict[tuple[str, str], object]) -> str:
    """Set (table, key) = value in TOML text, editing as little as possible.

    table "" is the root table. A key already set elsewhere in the file (for
    example as a dotted key) is not detected; the result is checked with tomllib
    and a ValueError is raised if it does not read back with the wanted values.
    """
    lines = text.splitlines()
    for (table, key), value in settings.items():
        rendered = f"{_key(key)} = {_value(value)}"
        current = ""
        start = 0 if table == "" else None
        end = len(lines)
        found = None
        for i, line in enumerate(lines):
            m = _HEADER.match(line)
            if m:
                if current == table and start is not None and i > start:
                    end = i
                    break
                current = m.group(1).replace(" ", "")
                if current == table:
                    start = i + 1
                continue
            if current == table and _key_line(key).match(line):
                found = i
                break
        if found is not None:
            lines[found] = rendered
        elif start is not None:
            # Insert after the table header's last key (before blank lines and the next table).
            insert = start
            for i in range(start, end):
                if lines[i].strip() and not _HEADER.match(lines[i]):
                    insert = i + 1
            lines.insert(insert, rendered)
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend([f"[{table}]", rendered])
    out = "\n".join(lines) + "\n"
    parsed = tomllib.loads(out)
    for (table, key), value in settings.items():
        node = parsed
        for part in [p for p in table.split(".") if p]:
            node = node.get(part, {})
        if node.get(key) != value:
            raise ValueError(f"could not set {table}.{key}".lstrip("."))
    return out
