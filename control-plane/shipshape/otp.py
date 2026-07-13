"""One-time passphrase for remote auth refresh.

The operator pre-registers a passphrase in the TUI/CLI; it is handed to the agent
out-of-band (phone -> Claude remote control) and submitted via the broker. It is
stored salted-hashed, is single-use, and expires quickly. Single-use is the
control that preserves the credential TTL bound: a compromised agent cannot renew
its own access without a fresh operator-issued passphrase. The passphrase
authorises a FIXED action (GCP key refresh) — the agent chooses nothing else.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

DEFAULT_TTL = 900  # 15 minutes
MAX_ATTEMPTS = 5


def _file(state_dir: Path) -> Path:
    return state_dir / "otp.json"


def _hash(salt: str, passphrase: str) -> str:
    # PBKDF2 so a weak operator-chosen phrase resists offline cracking if the
    # (agent-unreachable) state dir ever leaks. Cheap enough for an on-demand OTP.
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt.encode(), 200_000).hex()


def _write(state_dir: Path, data: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    f = _file(state_dir)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, f)


def register(state_dir: Path, passphrase: str, ttl: int = DEFAULT_TTL, now: float | None = None) -> None:
    now = time.time() if now is None else now
    salt = secrets.token_hex(16)
    _write(
        state_dir,
        {"salt": salt, "hash": _hash(salt, passphrase), "expires_at": now + ttl,
         "used": False, "attempts": 0},
    )


def generate(state_dir: Path, ttl: int = DEFAULT_TTL, now: float | None = None) -> str:
    phrase = secrets.token_urlsafe(12)
    register(state_dir, phrase, ttl, now)
    return phrase


def validate_and_consume(state_dir: Path, passphrase: str, now: float | None = None) -> tuple[bool, str]:
    now = time.time() if now is None else now
    data = None
    f = _file(state_dir)
    if f.exists():
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            data = None
    if not data:
        return False, "no passphrase registered"
    if data.get("used"):
        return False, "passphrase already used"
    if data.get("attempts", 0) >= MAX_ATTEMPTS:
        return False, "too many attempts — register a new passphrase"
    if now > data.get("expires_at", 0):
        return False, "passphrase expired"
    if not hmac.compare_digest(data.get("hash", ""), _hash(data.get("salt", ""), passphrase)):
        data["attempts"] = data.get("attempts", 0) + 1
        _write(state_dir, data)
        return False, "passphrase incorrect"
    # consume BEFORE the caller acts, so a crash cannot enable replay
    data["used"] = True
    _write(state_dir, data)
    return True, "ok"


def status(state_dir: Path, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    f = _file(state_dir)
    if not f.exists():
        return {}
    try:
        d = json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        "registered": True,
        "used": bool(d.get("used")),
        "expired": now > d.get("expires_at", 0),
        "expires_in": max(0, int(d.get("expires_at", 0) - now)),
        "attempts": d.get("attempts", 0),
    }
