"""The Ship's sign-in registry, from the sign-in files in its tooling bundle.

The files are `signins/<id>.toml` (contracts/signin.schema.json): the same files
FleetLedger drives the logins with (fleetledger/signinreg.py), carried to the Ship
in the tooling bundle. From them come:

- the status auth keys `ship-ctl status` reports, in FleetLedger's order: each
  sign-in's ([signin] label, order) keys, its [[check]] services in order, or its
  own key (its id with '-' as '_');
- how each key is checked: the [status] table of the file whose own key it is,
  either a built-in check in status.py (`builtin`) or a command (signincheck.py);
- the flows `ship-ctl notify-signin` and `ship-ctl signin check` take: every file.

What the Ship does when a sign-in turns ok stays in code, keyed by id (cli.py: claude
starts autostart, github clones waiting repos), as does which missing sign-in is an
attention item of its own (CORE).
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from . import schema
from .errors import CtlError

# A missing sign-in is always an attention item for these (status.attention_for).
CORE = frozenset({"claude"})
# A command check's defaults (contracts/signin.schema.json, status).
DEFAULT_EVERY = 300
DEFAULT_EXPIRING_WITHIN = 24 * 3600
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(duration: str) -> int:
    """'90s', '5m', '24h', '2d' in seconds."""
    m = re.fullmatch(r"([0-9]{1,6})([smhd])", duration)
    if not m:
        raise CtlError("internal", f"{duration!r} is not a duration such as 30s, 5m, 24h or 2d")
    return int(m.group(1)) * _UNITS[m.group(2)]


def status_key(file_id: str) -> str:
    """A file's own status key: its id with '-' as '_'."""
    return file_id.replace("-", "_")


@dataclass(frozen=True)
class Status:
    """A [status] table: how the Ship checks one status key."""

    builtin: str | None = None  # a check in status.py (BUILTIN_PROBES)
    run: str | None = None  # else this command, as agent
    every: int = DEFAULT_EVERY
    format: str = "adapter"  # adapter | shipshape
    ok_json: str | None = None
    account: str | None = None  # stdout | json:<path> | re:<one group>
    expires_at: str | None = None  # json:<path> | re:<one group>
    expiring_within: int = DEFAULT_EXPIRING_WITHIN
    missing_exit: tuple[int, ...] | None = None  # None: every non-zero exit is missing
    discard_output: bool = False
    env: tuple[tuple[str, str], ...] = ()
    digest: str = ""  # of the table: a cached result is for this check only


@dataclass(frozen=True)
class SignIn:
    """One status auth key."""

    key: str
    label: str  # attention items, tool-waiting lines and notices
    status: Status
    file: str  # the sign-in file whose own key this is


@dataclass(frozen=True)
class Flow:
    """One sign-in file: what notify-signin and signin check take."""

    id: str
    label: str
    keys: tuple[str, ...]  # the status keys it signs in


@dataclass(frozen=True)
class Registry:
    signins: tuple[SignIn, ...]  # the status document's auth keys, in order
    flows: dict[str, Flow]
    checks: dict[str, SignIn]  # every key a file checks (also those of flows only)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.signins)

    @property
    def labels(self) -> dict[str, str]:
        return {k: s.label for k, s in self.checks.items()}

    def label(self, key: str) -> str:
        found = self.checks.get(key)
        return found.label if found else key

    def notice_label(self, flow_id: str) -> str:
        """How a notice names what this flow signed in: its keys' labels."""
        return " and ".join(self.label(k) for k in self.flows[flow_id].keys)

    def flow(self, name: str) -> Flow | None:
        """A flow by its id, or by its own status key (gcloud_adc: gcloud-adc)."""
        if name in self.flows:
            return self.flows[name]
        return next((f for f in self.flows.values() if status_key(f.id) == name), None)


def _fail(path: Path, message: str) -> CtlError:
    return CtlError("internal", f"sign-in file {path.name}: {message}")


def _one_group(path: Path, what: str, spec: str | None) -> None:
    if not spec or not spec.startswith("re:"):
        return
    try:
        groups = re.compile(spec[3:]).groups
    except re.error as exc:
        raise _fail(path, f"status.{what} is not a valid pattern: {exc}") from None
    if groups != 1:
        raise _fail(path, f"status.{what} needs exactly one group in its pattern")


def read(path: Path, validator: schema.Validator) -> dict:
    """One sign-in file, checked against the contract and its own rules."""
    try:
        with path.open("rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _fail(path, str(exc)) from None
    if (doc.get("secrets") or {}).get("store") == "keyring":
        raise _fail(path, "secrets.store = keyring: a Ship has no unlocked password store; "
                          "pin the tool to its file store")
    errors = validator.errors(doc)
    if errors:
        raise _fail(path, errors[0])
    if doc["id"] != path.stem:
        raise _fail(path, f"id {doc['id']!r} does not match the file name")
    st = doc.get("status")
    if st is not None:
        services = [c["service"] for c in doc.get("check", [])]
        if services and status_key(doc["id"]) not in services:
            raise _fail(path, f"[status] checks {status_key(doc['id'])}, which is none of its "
                              "[[check]] services")
        _one_group(path, "account", st.get("account"))
        _one_group(path, "expires_at", st.get("expires_at"))
    return doc


def _status(table: dict) -> Status:
    digest = hashlib.sha256(json.dumps(table, sort_keys=True).encode()).hexdigest()[:16]
    if "builtin" in table:
        return Status(builtin=table["builtin"], digest=digest)
    return Status(
        run=table["run"],
        every=seconds(table.get("every", f"{DEFAULT_EVERY}s")),
        format=table.get("format", "adapter"),
        ok_json=table.get("ok_json"),
        account=table.get("account"),
        expires_at=table.get("expires_at"),
        expiring_within=seconds(table.get("expiring_within", f"{DEFAULT_EXPIRING_WITHIN}s")),
        missing_exit=tuple(table["missing_exit"]) if "missing_exit" in table else None,
        discard_output=bool(table.get("discard_output", False)),
        env=tuple(sorted(table.get("env", {}).items())),
        digest=digest,
    )


def build(folder: Path, schema_path: Path) -> Registry:
    validator = schema.load(schema_path)
    docs = {p.stem: read(p, validator) for p in sorted(folder.glob("*.toml"))}
    checks: dict[str, SignIn] = {}
    flows: dict[str, Flow] = {}
    for fid, doc in docs.items():
        keys = tuple(c["service"] for c in doc.get("check", [])) or (status_key(fid),)
        flows[fid] = Flow(fid, doc["label"], keys)
        if "status" in doc:
            key = status_key(fid)
            try:
                st = _status(doc["status"])
            except CtlError as exc:
                raise _fail(folder / f"{fid}.toml", f"status: {exc.message}") from None
            checks[key] = SignIn(key, doc["status"].get("label", doc["label"]), st, fid)
    for flow in flows.values():
        unchecked = [k for k in flow.keys if k not in checks]
        if unchecked:
            raise _fail(folder / f"{flow.id}.toml", f"no sign-in file checks {unchecked[0]} "
                        f"(a [status] in {unchecked[0].replace('_', '-')}.toml)")
    own = sorted((doc["signin"]["order"], fid) for fid, doc in docs.items()
                 if "order" in doc.get("signin", {}))
    seen: dict[str, str] = {}
    ordered: list[SignIn] = []
    for _, fid in own:
        for key in flows[fid].keys:
            if key in seen:
                raise _fail(folder / f"{fid}.toml", f"status key {key} is already "
                                                    f"{seen[key]}'s")
            seen[key] = fid
            ordered.append(checks[key])
    return Registry(signins=tuple(ordered), flows=flows, checks=checks)


@cache
def _cached(folder: Path, schema_path: Path) -> Registry:
    return build(folder, schema_path)


def registry(paths) -> Registry:
    """The registry from this ship-ctl's bundle (paths: a Paths)."""
    return _cached(paths.signins, paths.schema("signin"))
