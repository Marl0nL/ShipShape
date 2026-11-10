"""Sign-in checks on the Ship: one auth entry per status key (contracts/signin.schema.json).

A key's [status] is a built-in check (status.py BUILTIN_PROBES), or a command run as
agent with `bash -c`, read in one of two ways:

- adapter: the exit code (0 signed in; `missing_exit`, or by default any other code,
  not signed in; anything else an error), then `ok_json`, `account` and `expires_at`
  from stdout;
- shipshape: the native status line, one line of JSON on stdout,
  {"shipshape_signin": 1, "state": ..., "account": ..., "expires_at": ..., "detail": ...}.

A command has 5 seconds (status.PROBE_TIMEOUT); one that takes longer shows
`unknown`, so a slow check never holds up the status document. Only the first 64 KiB
of stdout is read, stderr is never kept, and the output is dropped once parsed:
only the state, account, expiry and detail are kept. With `discard_output` nothing
is read at all, for a check whose output is a token. A sign-in that ends within
`expiring_within` is `expiring`, one past its end `expired`.

Results are kept in /run/shipshape/signin-checks.json (root only, cleared at boot)
for the check's `every`, so the status refresh every 30 seconds does not run every
check every time. `ship-ctl signin check` always runs them, and keeps what it found.
Built-in checks run every time, as they always have.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from .context import Context
from .fsutil import read_json, write_atomic
from .runner import NOT_FOUND_RC, TIMEOUT_RC, Result
from .signins import Registry, SignIn, Status

OUTPUT_LIMIT = 64 * 1024
LINE_TAG = "shipshape_signin"
NATIVE_STATES = ("ok", "missing", "expiring", "expired", "error")


def entry(state: str, account: object = None, detail: object = None,
          expires_at: str | None = None) -> dict:
    """An auth entry (ship-status.schema.json authEntry), without checked_at."""
    out: dict = {"state": state,
                 "account": account.strip()[:254] or None if isinstance(account, str) else None}
    if isinstance(detail, str) and detail.strip():
        out["detail"] = detail.strip()[:200]
    if expires_at:
        out["expires_at"] = expires_at
    return out


def stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fail(what: str, result: Result) -> str:
    if result.rc == NOT_FOUND_RC:
        return f"{what}: command not found"
    if result.rc == TIMEOUT_RC:
        return f"{what}: timed out"
    return f"{what}: exit {result.rc}"


def _json_path(doc: object, path: str) -> object:
    for part in path.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
    return doc


def parse_time(value: object) -> float | None:
    """RFC 3339 (any offset) or epoch seconds, as epoch seconds; None if neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and re.fullmatch(r"\s*[0-9]+(\.[0-9]+)?\s*", value):
        value = float(value)
    if isinstance(value, int | float):
        return float(value) if 0 < value < 1e11 else None
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("z", "Z"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None  # no offset: cannot tell when
    return when.timestamp()


def _extract(spec: str, out: str, doc: object) -> object:
    """A value named by `stdout`, `json:<path>` or `re:<one group>`."""
    if spec == "stdout":
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        return lines[0] if lines else None
    if spec.startswith("json:"):
        value = _json_path(doc, spec[5:])
        return value if isinstance(value, str | int | float) and not isinstance(value, bool) \
            else None
    m = re.search(spec[3:], out)
    return m.group(1) if m else None


def _expiry(state: str, expires: float | None, st: Status, now: float) -> str:
    """ok becomes expiring or expired by the sign-in's end."""
    if state != "ok" or expires is None:
        return state
    if expires <= now:
        return "expired"
    if expires - now <= st.expiring_within:
        return "expiring"
    return state


def _native(out: str) -> dict | None:
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get(LINE_TAG) == 1:
            return doc
    return None


def read_native(key: str, r: Result, st: Status, now: float) -> tuple[dict, list[str]]:
    what = f"{key} sign-in check"
    doc = _native(r.out)
    if doc is None:
        return entry("error"), [_fail(what, r) + ", no status line" if r.rc else
                                f"{what}: no status line"]
    state = doc.get("state")
    if state not in NATIVE_STATES:
        return entry("error"), [f"{what}: unknown state in its status line"]
    errors = []
    expires = None
    if doc.get("expires_at") is not None:
        expires = parse_time(doc["expires_at"])
        if expires is None:
            errors.append(f"{what}: unreadable expires_at")
    state = _expiry(state, expires, st, now)
    return entry(state, doc.get("account") if state != "missing" else None, doc.get("detail"),
                 stamp(expires) if expires is not None else None), errors


def read_adapter(key: str, r: Result, st: Status, now: float) -> tuple[dict, list[str]]:
    what = f"{key} sign-in check"
    if r.rc != 0:
        missing = st.missing_exit is None or r.rc in st.missing_exit
        return (entry("missing"), []) if missing else (entry("error"), [_fail(what, r)])
    if st.discard_output:
        return entry("ok"), []
    doc = None
    wants_json = any(s and s.startswith("json:") for s in (st.account, st.expires_at))
    if st.ok_json or wants_json:
        try:
            doc = json.loads(r.out)
        except ValueError:
            return entry("error"), [f"{what}: unreadable output"]
    if st.ok_json and not _json_path(doc, st.ok_json):
        return entry("missing"), []
    account = _extract(st.account, r.out, doc) if st.account else None
    errors = []
    expires = None
    if st.expires_at:
        raw = _extract(st.expires_at, r.out, doc)
        expires = parse_time(raw) if raw is not None else None
        if raw is not None and expires is None:
            errors.append(f"{what}: unreadable expires_at")
    state = _expiry("ok", expires, st, now)
    return entry(state, str(account) if account is not None else None, None,
                 stamp(expires) if expires is not None else None), errors


def run_command(ctx: Context, key: str, st: Status, now: float) -> tuple[dict, list[str]]:
    from .status import PROBE_TIMEOUT

    r = ctx.runner.run(["bash", "-c", st.run or ""], timeout=PROBE_TIMEOUT, agent=True,
                       env=dict(st.env), discard=st.discard_output, limit=OUTPUT_LIMIT)
    try:
        if r.rc in (NOT_FOUND_RC, TIMEOUT_RC):
            return entry("unknown"), [_fail(f"{key} sign-in check", r)]
        if st.format == "shipshape":
            return read_native(key, r, st, now)
        return read_adapter(key, r, st, now)
    finally:
        r.out = ""  # parsed: the output itself is never kept


def check_one(ctx: Context, signin: SignIn, now: float) -> tuple[dict, list[str]]:
    """One key's entry, checked now, with checked_at."""
    if signin.status.builtin:
        from .status import BUILTIN_PROBES

        found, errors = BUILTIN_PROBES[signin.status.builtin](ctx)
    else:
        found, errors = run_command(ctx, signin.key, signin.status, now)
    found["checked_at"] = stamp(now)
    return found, errors


def _cache_hit(cache: dict, signin: SignIn, now: float) -> dict | None:
    kept = cache.get(signin.key)
    if not isinstance(kept, dict) or kept.get("digest") != signin.status.digest:
        return None
    at = kept.get("at")
    if not isinstance(at, int | float) or not 0 <= now - at < signin.status.every:
        return None
    found = kept.get("entry")
    return dict(found) if isinstance(found, dict) and "state" in found else None


def check(ctx: Context, reg: Registry, keys: list[str] | tuple[str, ...], *,
          fresh: bool = False) -> dict[str, tuple[dict, list[str]]]:
    """(entry, errors) for each key, in parallel. A command check's result comes from
    the cache while it is younger than its `every`, unless fresh; what ran is kept."""
    now = time.time()
    cache = read_json(ctx.paths.signin_cache, {})
    cache = cache if isinstance(cache, dict) else {}
    out: dict[str, tuple[dict, list[str]]] = {}
    todo = []
    for key in keys:
        signin = reg.checks.get(key)
        if signin is None:
            out[key] = ({**entry("unknown"), "checked_at": stamp(now)},
                        [f"{key}: no sign-in file checks it"])
            continue
        hit = None if fresh or signin.status.builtin else _cache_hit(cache, signin, now)
        if hit is not None:
            out[key] = (hit, [])
        else:
            todo.append(signin)
    if todo:
        with ThreadPoolExecutor(max_workers=min(10, len(todo))) as pool:
            futures = {s.key: pool.submit(check_one, ctx, s, now) for s in todo}
            for key, fut in futures.items():
                out[key] = fut.result()
    ran = {s.key: s for s in todo if not s.status.builtin}
    if ran:
        keep = {k: v for k, v in cache.items() if k in reg.checks and k not in ran}
        for key, signin in ran.items():
            found = out[key][0]
            if found["state"] != "unknown":  # a timed-out check runs again next time
                keep[key] = {"digest": signin.status.digest, "at": now, "entry": found}
        try:
            write_atomic(ctx.paths.signin_cache, json.dumps(keep) + "\n", 0o600)
        except OSError as exc:
            ctx.log(f"cannot keep sign-in check results: {exc}")
    return {key: out[key] for key in keys}


def signed_in(found: dict) -> bool:
    """Signed in, ending soon or not."""
    return found.get("state") in ("ok", "expiring")
