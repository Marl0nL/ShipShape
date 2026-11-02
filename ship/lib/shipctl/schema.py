"""A small JSON Schema (draft 2020-12) validator for the keywords contracts/ uses.

Ships have only the Python standard library, so ship-ctl cannot use the
`jsonschema` package. This covers exactly the keywords in contracts/*.schema.json;
ship/tests checks that it agrees with `jsonschema` on every contract example and
fails loudly on any keyword it does not know.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Keywords that only annotate; they never make a document invalid.
_ANNOTATIONS = {
    "$schema",
    "$id",
    "$defs",
    "title",
    "description",
    "default",
    "format",
    "examples",
}

_SUPPORTED = _ANNOTATIONS | {
    "type",
    "const",
    "enum",
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "required",
    "properties",
    "additionalProperties",
    "propertyNames",
    "maxProperties",
    "items",
    "prefixItems",
    "uniqueItems",
    "minItems",
    "maxItems",
    "contains",
    "$ref",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "if",
    "then",
    "else",
}


class UnsupportedSchema(Exception):
    """The schema uses a keyword this validator does not implement."""


def _type_ok(value: object, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise UnsupportedSchema(f"type {name!r}")


def _equal(a: object, b: object) -> bool:
    """JSON equality: 1 == 1.0, but True != 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _path(parts: tuple) -> str:
    return "/".join(str(p) for p in parts) or "(root)"


class Validator:
    def __init__(self, schema: dict):
        self.schema = schema

    def _resolve(self, ref: str) -> dict:
        if not ref.startswith("#/"):
            raise UnsupportedSchema(f"$ref {ref!r}")
        node: object = self.schema
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise UnsupportedSchema(f"unresolvable $ref {ref!r}")
            node = node[part]
        assert isinstance(node, dict)
        return node

    def errors(self, doc: object) -> list[str]:
        out: list[str] = []
        self._check(doc, self.schema, (), out)
        return out

    def is_valid(self, doc: object) -> bool:
        return not self.errors(doc)

    def _check(self, v: object, s: object, at: tuple, out: list[str]) -> None:
        if s is True:
            return
        if s is False:
            out.append(f"{_path(at)}: not allowed")
            return
        if not isinstance(s, dict):
            raise UnsupportedSchema(f"schema node {s!r}")
        unknown = set(s) - _SUPPORTED
        if unknown:
            raise UnsupportedSchema(", ".join(sorted(unknown)))
        where = _path(at)

        if "$ref" in s:
            self._check(v, self._resolve(s["$ref"]), at, out)
        if "type" in s:
            names = s["type"] if isinstance(s["type"], list) else [s["type"]]
            if not any(_type_ok(v, n) for n in names):
                out.append(f"{where}: expected {' or '.join(names)}")
                return
        if "const" in s and not _equal(v, s["const"]):
            out.append(f"{where}: must be {json.dumps(s['const'])}")
        if "enum" in s and not any(_equal(v, e) for e in s["enum"]):
            out.append(f"{where}: must be one of {', '.join(json.dumps(e) for e in s['enum'])}")

        if isinstance(v, str):
            if "pattern" in s and not re.search(s["pattern"], v):
                out.append(f"{where}: does not match {s['pattern']}")
            if "minLength" in s and len(v) < s["minLength"]:
                out.append(f"{where}: shorter than {s['minLength']}")
            if "maxLength" in s and len(v) > s["maxLength"]:
                out.append(f"{where}: longer than {s['maxLength']}")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "minimum" in s and v < s["minimum"]:
                out.append(f"{where}: less than {s['minimum']}")
            if "maximum" in s and v > s["maximum"]:
                out.append(f"{where}: more than {s['maximum']}")

        if isinstance(v, dict):
            for key in s.get("required", []):
                if key not in v:
                    out.append(f"{where}: missing {key}")
            if "maxProperties" in s and len(v) > s["maxProperties"]:
                out.append(f"{where}: more than {s['maxProperties']} properties")
            props = s.get("properties", {})
            for key, value in v.items():
                if "propertyNames" in s:
                    self._check(key, s["propertyNames"], at + (key,), out)
                if key in props:
                    self._check(value, props[key], at + (key,), out)
                elif "additionalProperties" in s:
                    extra = s["additionalProperties"]
                    if extra is False:
                        out.append(f"{where}: unexpected key {key}")
                    else:
                        self._check(value, extra, at + (key,), out)

        if isinstance(v, list):
            if "minItems" in s and len(v) < s["minItems"]:
                out.append(f"{where}: fewer than {s['minItems']} items")
            if "maxItems" in s and len(v) > s["maxItems"]:
                out.append(f"{where}: more than {s['maxItems']} items")
            if s.get("uniqueItems"):
                for i, item in enumerate(v):
                    if any(_equal(item, other) for other in v[:i]):
                        out.append(f"{_path(at + (i,))}: duplicate item")
            prefix = s.get("prefixItems", [])
            for i, (item, sub) in enumerate(zip(v, prefix, strict=False)):
                self._check(item, sub, at + (i,), out)
            if "items" in s:
                for i, item in enumerate(v[len(prefix) :], start=len(prefix)):
                    self._check(item, s["items"], at + (i,), out)
            if "contains" in s and not any(self._ok(item, s["contains"]) for item in v):
                out.append(f"{where}: no item matches 'contains'")

        for sub in s.get("allOf", []):
            self._check(v, sub, at, out)
        if "anyOf" in s and not any(self._ok(v, sub) for sub in s["anyOf"]):
            out.append(f"{where}: matches none of the allowed forms")
        if "oneOf" in s:
            matches = sum(1 for sub in s["oneOf"] if self._ok(v, sub))
            if matches != 1:
                out.append(f"{where}: must match exactly one allowed form (matched {matches})")
        if "not" in s and self._ok(v, s["not"]):
            out.append(f"{where}: matches a forbidden form")
        if "if" in s:
            branch = "then" if self._ok(v, s["if"]) else "else"
            if branch in s:
                self._check(v, s[branch], at, out)

    def _ok(self, v: object, s: object) -> bool:
        errs: list[str] = []
        self._check(v, s, (), errs)
        return not errs


def load(path: Path) -> Validator:
    return Validator(json.loads(path.read_text(encoding="utf-8")))
