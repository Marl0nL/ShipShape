"""`ship-ctl overlay`: join and leave the overlay network (phase 5, contracts/ship-ctl.md).

Only Tailscale (or Headscale, through --login-server). The one-off auth key comes
on stdin from FleetLedger over SSH. It never reaches a command line, a log, the
result or the disk beyond one root-only file in a root-only folder under /run (a
tmpfs), which exists only while `tailscale up` runs. Tailscale SSH stays off:
OpenSSH with the pinned host key remains the only way in.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path

from .context import Context, utc_now
from .errors import CtlError
from .fsutil import read_json, remove, write_atomic
from .shiptoml import ShipToml

PROVIDER = "tailscale"
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,500}$")
TAG_RE = re.compile(r"^tag:[a-z0-9][a-z0-9-]{0,62}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")
ADDRESS_RE = re.compile(r"^[0-9a-fA-F:.]+$")
MAX_KEY_BYTES = 512
JOIN_TIMEOUT = 60

# Tailscale's BackendState -> the status document's overlay.state.
BACKEND = {
    "Running": "running",
    "NeedsLogin": "needs-login",
    "NeedsMachineAuth": "needs-approval",
    "Stopped": "stopped",
    "Starting": "starting",
    "NoState": "starting",
}

# tailscaled uploads its logs to Tailscale by default; Ships send nothing extra.
DAEMON_DROPIN = (
    "# Written by ship-ctl overlay join (phase 5).\n"
    "[Service]\n"
    "Environment=TS_NO_LOGS_NO_SUPPORT=true\n"
)


def state_path(ctx: Context) -> Path:
    return ctx.paths.state / "overlay.json"


def dropin_path(ctx: Context) -> Path:
    return ctx.paths.p("/etc/systemd/system/tailscaled.service.d/50-shipshape.conf")


def listen_conf_path(ctx: Context) -> Path:
    return ctx.paths.p("/etc/ssh/sshd_config.d/20-shipshape-listen.conf")


def read_state(ctx: Context) -> dict:
    doc = read_json(state_path(ctx), {})
    return doc if isinstance(doc, dict) else {}


def joined(ctx: Context) -> bool:
    return bool(read_state(ctx).get("joined"))


def installed() -> bool:
    return shutil.which("tailscale") is not None


# --- status --------------------------------------------------------------------------


def probe(ctx: Context) -> dict | None:
    """`tailscale status --json`, trimmed to what the status document reports."""
    r = ctx.runner.run(["tailscale", "status", "--json"], timeout=5)
    if not r.ok:
        return None
    try:
        doc = json.loads(r.out)
    except ValueError:
        return None
    me = doc.get("Self") or {}
    name = str(me.get("DNSName") or "").rstrip(".") or None
    return {
        "state": BACKEND.get(str(doc.get("BackendState")), "unknown"),
        "name": name if name and len(name) <= 253 else None,
        "addresses": [a for a in me.get("TailscaleIPs") or [] if isinstance(a, str)
                      and ADDRESS_RE.match(a)],
        "tags": [t for t in me.get("Tags") or [] if isinstance(t, str)][:20],
    }


def status(ctx: Context, ship: ShipToml) -> dict | None:
    """The status document's overlay object; None when the overlay tooling is absent."""
    record = read_state(ctx)
    is_joined = bool(record.get("joined"))
    if not installed():
        if not is_joined:
            return None
        live = {"state": "not-installed", "name": None, "addresses": [], "tags": []}
    else:
        live = probe(ctx) or {"state": "off", "name": None, "addresses": [], "tags": []}
    return {
        "provider": PROVIDER,
        "joined": is_joined,
        "ssh_listen": ssh_listen(ship),
        **live,
    }


def ssh_listen(ship: ShipToml) -> str:
    return ship.doc.get("network", {}).get("ssh_listen", "any")


# --- the sshd listen policy (applied by ship-ctl apply) --------------------------------


def listen_conf(addresses: list[str]) -> str:
    """sshd drop-in for ssh_listen = "overlay": deny every login that does not arrive on
    loopback or one of this Ship's overlay addresses. `Match all` ends the block, so
    nothing after this file is pulled into it."""
    allowed = ["127.0.0.1", "::1", *addresses]
    pattern = ",".join(["*", *(f"!{a}" for a in allowed)])
    return (
        '# Written by ship-ctl apply: network.ssh_listen = "overlay" (contracts/settings.md).\n'
        "# Logins are accepted only on loopback and this Ship's overlay addresses.\n"
        f"Match LocalAddress {pattern}\n"
        "    DenyUsers *\n"
        "Match all\n"
    )


def apply_listen(ctx: Context, ship: ShipToml, dry_run: bool = False) -> tuple[bool, list[str]]:
    """Write or remove the listen drop-in. Returns (changed, messages)."""
    path = listen_conf_path(ctx)
    messages: list[str] = []
    addresses = [a for a in read_state(ctx).get("addresses", []) if ADDRESS_RE.match(str(a))]
    if ssh_listen(ship) == "overlay":
        if not joined(ctx) or not addresses:
            # Never lock the Ship out: without overlay addresses, keep listening everywhere.
            messages.append("network.ssh_listen is overlay, but this Ship has not joined the "
                            "overlay; sshd still accepts logins on every interface.")
            changed = remove(path, dry_run=dry_run)
        else:
            changed = write_atomic(path, listen_conf(addresses), 0o644, dry_run=dry_run)
    else:
        changed = remove(path, dry_run=dry_run)
    if changed and not dry_run and not ctx.paths.sandbox:
        check = ctx.runner.run(["sshd", "-t"], timeout=15)
        if not check.ok:
            remove(path)
            raise CtlError("internal", "sshd rejected the listen policy; it was removed")
        ctx.runner.run(["systemctl", "try-reload-or-restart", "ssh"], timeout=30)
    return changed, messages


# --- join and leave -------------------------------------------------------------------


def read_key(stream) -> str:
    raw = stream.buffer.read(MAX_KEY_BYTES + 1) if hasattr(stream, "buffer") else \
        stream.read(MAX_KEY_BYTES + 1).encode()
    if len(raw) > MAX_KEY_BYTES:
        raise CtlError("usage", "the auth key on stdin is too long")
    key = raw.decode("ascii", errors="replace").strip()
    if not KEY_RE.match(key):
        # Say nothing about the key itself: it may be a secret pasted in the wrong place.
        raise CtlError("usage", "stdin does not hold one auth key (one line, letters, "
                       "digits, - and _)")
    return key


def _scrub(text: str, key: str) -> str:
    text = text.replace(key, "[key]")
    text = re.sub(r"tskey-[A-Za-z0-9_-]+", "tskey-[redacted]", text)
    return " ".join(text.split())[:300]


def _write_key(ctx: Context, key: str) -> Path:
    folder = ctx.paths.run / "overlay"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(folder)
    if not ctx.paths.sandbox and (st.st_uid != 0 or st.st_mode & 0o077):
        raise CtlError("internal", f"{folder} is not a private root-owned folder")
    path = folder / f"key-{secrets.token_hex(8)}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    return path


def _drop_key(path: Path) -> None:
    try:
        size = path.stat().st_size
        with path.open("r+b") as fh:
            fh.write(b"\0" * size)
    except OSError:
        pass
    path.unlink(missing_ok=True)


def _ensure_installed(ctx: Context) -> list[str]:
    if installed():
        return []
    r = ctx.runner.run(["apt-get", "install", "-y", "-q", "--no-install-recommends", "tailscale"],
                       timeout=600, env={"DEBIAN_FRONTEND": "noninteractive"})
    if not r.ok:
        ctx.runner.run(["apt-get", "update", "-q"], timeout=600)
        r = ctx.runner.run(["apt-get", "install", "-y", "-q", "--no-install-recommends",
                            "tailscale"], timeout=600, env={"DEBIAN_FRONTEND": "noninteractive"})
    if not r.ok:
        raise CtlError("overlay-failed", "could not install tailscale from the pinned repo")
    return ["tailscale"]


def join(ctx: Context, ship: ShipToml, *, hostname: str, tags: list[str],
         login_server: str | None, key: str) -> tuple[dict, list[str], list[str]]:
    """Returns (overlay status, changed, messages)."""
    if not NAME_RE.match(hostname):
        raise CtlError("usage", "--hostname must be a Ship name")
    if not tags or not all(TAG_RE.match(t) for t in tags):
        raise CtlError("usage", "--tag must be like tag:ship (at least one)")
    if login_server is not None and not URL_RE.match(login_server):
        raise CtlError("usage", "--login-server must be an https:// URL")
    changed = _ensure_installed(ctx)
    if write_atomic(dropin_path(ctx), DAEMON_DROPIN, 0o644):
        changed.append(ctx.paths.agent_path(dropin_path(ctx)))
        ctx.runner.run(["systemctl", "daemon-reload"], timeout=30)
    r = ctx.runner.run(["systemctl", "enable", "--now", "tailscaled"], timeout=60)
    if not r.ok:
        raise CtlError("overlay-failed", "could not start tailscaled")

    key_file = _write_key(ctx, key)
    try:
        argv = [
            "tailscale", "up", f"--auth-key=file:{key_file}", f"--hostname={hostname}",
            f"--advertise-tags={','.join(tags)}", "--ssh=false", "--accept-dns=false",
            "--accept-routes=false", "--reset", f"--timeout={JOIN_TIMEOUT}s",
        ]
        if login_server:
            argv.append(f"--login-server={login_server}")
        up = ctx.runner.run(argv, timeout=JOIN_TIMEOUT + 30)
    finally:
        _drop_key(key_file)
    if not up.ok:
        detail = _scrub(up.err or up.out, key)
        raise CtlError("overlay-failed", f"tailscale up failed: {detail or 'no output'}")

    live = probe(ctx)
    if not live or live["state"] != "running":
        state = live["state"] if live else "unknown"
        raise CtlError("overlay-failed", f"joined, but the overlay is {state}, not running")
    record = {
        "provider": PROVIDER,
        "joined": True,
        "hostname": hostname,
        "tags": tags,
        "login_server": login_server,
        "name": live["name"],
        "addresses": live["addresses"],
        "joined_at": utc_now(),
    }
    if write_atomic(state_path(ctx), json.dumps(record, indent=2) + "\n", 0o600):
        changed.append(ctx.paths.agent_path(state_path(ctx)))
    # Overlay addresses may have changed: refresh the listen policy if it is in use.
    did, messages = apply_listen(ctx, ship)
    if did:
        changed.append(ctx.paths.agent_path(listen_conf_path(ctx)))
    messages.insert(0, f"Joined the overlay as {live['name'] or hostname}.")
    return status(ctx, ship) or {}, changed, messages


def leave(ctx: Context, ship: ShipToml) -> tuple[list[str], list[str]]:
    """Returns (changed, messages). Callers reset ssh_listen to any first."""
    if not joined(ctx) and not installed():
        return [], ["This Ship has not joined the overlay."]
    changed: list[str] = []
    messages: list[str] = []
    if installed():
        out = ctx.runner.run(["tailscale", "logout"], timeout=30)
        if not out.ok:
            messages.append("tailscale logout did not succeed; the node may still be listed "
                            "in your overlay's admin console.")
        ctx.runner.run(["systemctl", "disable", "--now", "tailscaled"], timeout=60)
        changed.append("tailscaled")
    if remove(state_path(ctx)):
        changed.append(ctx.paths.agent_path(state_path(ctx)))
    messages.insert(0, "Left the overlay; sshd accepts logins on every interface again.")
    return changed, messages
