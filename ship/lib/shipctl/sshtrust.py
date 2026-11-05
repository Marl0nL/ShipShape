"""SSH certificates on a Ship: `ship-ctl ssh-trust` and the status `ssh` section.

FleetLedger is the fleet's certificate authority (contracts/ship-ctl.md). This
module installs what it issues, after checking it:

- a host certificate is taken only for the Ship's own pinned host key, with the
  Ship's name among its principals, and not expired. The Ship never takes a
  host key from FleetLedger, so moving to certificates opens no trust gap;
- user CA keys let certificates log in, as `agent` only: sshd reads principals
  from /etc/ssh/shipshape/principals/%u and only the agent file exists;
- a revocation list (KRL) is taken only if ssh-keygen can read it and it does
  not revoke fleet's own key (that would lock FleetLedger out for good).

Every change ends with `sshd -t`; if sshd would refuse the configuration, all
files go back to what they were. Everything here is root-owned under /etc/ssh;
nothing reads or writes the agent's home.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import schema
from .context import Context
from .errors import CtlError
from .fsutil import read_bytes, remove, write_atomic
from .shiptoml import ShipToml

SCHEMA = "shipshape.ssh-trust/1"
HOST_KEY_PUB = "/etc/ssh/ssh_host_ed25519_key.pub"
HOST_CERT = "/etc/ssh/ssh_host_ed25519_key-cert.pub"
CA_DIR = "/etc/ssh/shipshape"
USER_CA = CA_DIR + "/user-ca.pub"
PRINCIPALS_DIR = CA_DIR + "/principals"
KEYS_DIR = CA_DIR + "/keys"  # root-owned authorized keys for the owner's login
REVOKED = CA_DIR + "/revoked-keys"
DROPIN = "/etc/ssh/sshd_config.d/20-shipshape-ca.conf"
FLEET_KEYS = "/home/fleet/.ssh/authorized_keys"
EXPIRING_DAYS = 14
CLOCK_SKEW = timedelta(minutes=5)

FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
KEY_IN_LINE = re.compile(
    r"(?:^|\s)((?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|"
    r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,2})"
)
DROPIN_HEADER = (
    "# Written by ship-ctl from what FleetLedger's certificate authority installed\n"
    "# (ship-ctl ssh-trust); edits are overwritten.\n"
)


def _p(ctx: Context, path: str) -> Path:
    return ctx.paths.p(path)


def agent_principals(ship: ShipToml) -> list[str]:
    """Certificate principals that may log in as agent on this Ship."""
    return ["agent:all", *(f"agent:tag:{t}" for t in ship.tags), f"agent:ship:{ship.name}"]


# --- ssh-keygen ---------------------------------------------------------------
def _keygen(ctx: Context, *args: str, input: str | None = None):
    # -L prints validity in local time; UTC makes it parseable.
    return ctx.runner.run(["ssh-keygen", *args], timeout=10, input=input, env={"TZ": "UTC"})


@contextlib.contextmanager
def _scratch():
    with tempfile.TemporaryDirectory(prefix="ship-ctl-ssh-") as tmp:
        os.chmod(tmp, 0o700)
        yield Path(tmp)


def fingerprint(ctx: Context, key_line: str) -> str | None:
    """SHA256 fingerprint of one public key or certificate line, or None."""
    with _scratch() as tmp:
        f = tmp / "key.pub"
        f.write_text(key_line.strip() + "\n")
        res = _keygen(ctx, "-l", "-E", "sha256", "-f", str(f))
    parts = res.out.split()
    if res.ok and len(parts) >= 2 and FINGERPRINT.match(parts[1]):
        return parts[1]
    return None


def _time(text: str) -> str | None:
    try:
        when = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_cert_listing(text: str) -> dict | None:
    """Parse `ssh-keygen -L` output (run with TZ=UTC). None if it is not a certificate."""
    info: dict = {"principals": [], "valid_after": None, "valid_before": None}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        key, _, value = line.partition(":")
        value = value.strip()
        if key == "Type":
            info["type"] = (
                "host"
                if value.endswith("host certificate")
                else ("user" if value.endswith("user certificate") else "?")
            )
            info["algorithm"] = value.split()[0] if value else ""
        elif key == "Public key":
            info["key_fingerprint"] = value.split()[-1] if value else ""
        elif key == "Signing CA":
            parts = value.split()
            info["ca_fingerprint"] = parts[1] if len(parts) > 1 else ""
        elif key == "Key ID":
            info["key_id"] = value.strip('"')[:200]
        elif key == "Serial":
            with contextlib.suppress(ValueError):
                info["serial"] = int(value)
        elif key == "Valid":
            if value.startswith("from ") and " to " in value:
                start, end = value[5:].split(" to ", 1)
                info["valid_after"], info["valid_before"] = _time(start), _time(end)
            elif value.startswith("after "):
                info["valid_after"] = _time(value[6:])
            elif value.startswith("before "):
                info["valid_before"] = _time(value[7:])
        elif key == "Principals":
            j = i + 1
            while j < len(lines) and lines[j].startswith(("\t\t", "                ")):
                name = lines[j].strip()
                if name and name != "(none)":
                    info["principals"].append(name[:253])
                j += 1
            i = j - 1
        i += 1
    needed = ("type", "key_fingerprint", "ca_fingerprint", "key_id", "serial")
    if not all(k in info for k in needed):
        return None
    return info


def cert_info(ctx: Context, cert_line: str) -> dict | None:
    with _scratch() as tmp:
        f = tmp / "cert.pub"
        f.write_text(cert_line.strip() + "\n")
        res = _keygen(ctx, "-L", "-f", str(f))
    return parse_cert_listing(res.out) if res.ok else None


def public_cert(info: dict) -> dict:
    """The status/ssh-trust shape of a certificate (contracts/ship-status.schema.json)."""
    return {
        "serial": info["serial"],
        "key_id": info["key_id"],
        "principals": info["principals"],
        "valid_after": info["valid_after"],
        "valid_before": info["valid_before"],
        "ca_fingerprint": info["ca_fingerprint"],
    }


# --- checks ---------------------------------------------------------------------
def _invalid(message: str) -> CtlError:
    return CtlError("invalid-input", message)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def check_host_cert(ctx: Context, ship: ShipToml, line: str) -> dict:
    host_pub = read_bytes(_p(ctx, HOST_KEY_PUB))
    if host_pub is None:
        raise _invalid(f"{HOST_KEY_PUB} is missing; cannot check the certificate")
    own = fingerprint(ctx, host_pub.decode("utf-8", "replace"))
    info = cert_info(ctx, line)
    if info is None:
        raise _invalid("host_certificate is not a certificate ssh-keygen can read")
    if info["type"] != "host":
        raise _invalid("host_certificate is a user certificate, not a host certificate")
    if own is None or info["key_fingerprint"] != own:
        raise _invalid("host_certificate certifies a different key than this Ship's host key")
    if ship.name not in info["principals"]:
        raise _invalid(f"host_certificate does not list this Ship's name ({ship.name})")
    after, before = _parse_ts(info["valid_after"]), _parse_ts(info["valid_before"])
    now = _now()
    if before is not None and before <= now:
        raise _invalid("host_certificate has expired")
    if after is not None and after > now + CLOCK_SKEW:
        raise _invalid("host_certificate is not valid yet (check the clocks)")
    return info


def check_user_ca(ctx: Context, lines: list[str]) -> list[str]:
    prints = []
    for line in lines:
        fp = fingerprint(ctx, line)
        if fp is None or not line.startswith("ssh-ed25519 "):
            raise _invalid("user_ca_keys holds a line that is not an ed25519 public key")
        prints.append(fp)
    return prints


def fleet_keys(ctx: Context) -> list[str]:
    """Public keys in fleet's authorized_keys (fleet is the admin user, not agent)."""
    path = _p(ctx, FLEET_KEYS)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return []
    with os.fdopen(fd, "rb") as fh:
        text = fh.read(1 << 20).decode("utf-8", "replace")
    keys = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        m = KEY_IN_LINE.search(line)
        if m:
            keys.append(m.group(1))
    return keys


def check_krl(ctx: Context, data: bytes) -> None:
    host_pub = read_bytes(_p(ctx, HOST_KEY_PUB))
    with _scratch() as tmp:
        krl = tmp / "krl"
        krl.write_bytes(data)
        probe = tmp / "probe.pub"
        probe.write_bytes(host_pub or b"")
        res = _keygen(ctx, "-Q", "-f", str(krl), str(probe))
        if res.rc not in (0, 1):
            raise _invalid("revoked_keys is not a revocation list ssh-keygen can read")
        for n, key in enumerate(fleet_keys(ctx)):
            f = tmp / f"fleet-{n}.pub"
            f.write_text(key + "\n")
            if _keygen(ctx, "-Q", "-f", str(krl), str(f)).rc == 1:
                raise _invalid(
                    "revoked_keys revokes a key fleet logs in with; refusing to lock "
                    "FleetLedger out"
                )


# --- rendering ------------------------------------------------------------------
def render_dropin(ctx: Context) -> str:
    lines = [DROPIN_HEADER.rstrip("\n")]
    if _p(ctx, HOST_CERT).exists():
        lines.append(f"HostCertificate {HOST_CERT}")
    if _p(ctx, USER_CA).exists():
        lines.append(f"TrustedUserCAKeys {USER_CA}")
        lines.append(f"AuthorizedPrincipalsFile {PRINCIPALS_DIR}/%u")
    if _p(ctx, REVOKED).exists():
        lines.append(f"RevokedKeys {REVOKED}")
    keys = _p(ctx, KEYS_DIR)
    owners = sorted(f.name for f in keys.iterdir()) if keys.is_dir() else []
    if owners:
        # The owner's keys live here, root-owned, so nothing is written into a home;
        # everyone else keeps ~/.ssh/authorized_keys. sshd adds up AllowUsers lines, so
        # this lets the owner in beside fleet and agent (10-shipshape.conf).
        lines.append(f"AuthorizedKeysFile .ssh/authorized_keys {KEYS_DIR}/%u")
        lines.append("AllowUsers " + " ".join(owners))
    return "\n".join(lines) + "\n"


def render_files(ctx: Context, ship: ShipToml, dry_run: bool = False) -> list[Path]:
    """The principals file and the sshd drop-in, from ship.toml and what is installed.

    Called by `ship-ctl apply` too, so tag changes and new bundles keep them in step.
    Returns the paths that changed.
    """
    changed = []
    principals = _p(ctx, PRINCIPALS_DIR + "/agent")
    text = "\n".join(agent_principals(ship)) + "\n"
    if write_atomic(principals, text, 0o644, dry_run=dry_run):
        changed.append(principals)
    # A principals file for fleet must never exist: certificates never log in as fleet.
    fleet = _p(ctx, PRINCIPALS_DIR + "/fleet")
    if remove(fleet, dry_run=dry_run):
        changed.append(fleet)
    # The owner's login takes the same certificates as agent (your machines), and the
    # owner's own keys; any other principals or keys file is stale and goes.
    owner = (ship.owner or {}).get("name")
    if owner:
        own = _p(ctx, PRINCIPALS_DIR + "/" + owner)
        if write_atomic(own, text, 0o644, dry_run=dry_run):
            changed.append(own)
        keys = _p(ctx, KEYS_DIR + "/" + owner)
        lines = "".join(k.strip() + "\n" for k in ship.owner.get("keys", []))
        if write_atomic(keys, lines, 0o644, dry_run=dry_run):
            changed.append(keys)
    for folder, keep in ((PRINCIPALS_DIR, {"agent", owner}), (KEYS_DIR, {owner})):
        d = _p(ctx, folder)
        for f in sorted(d.iterdir()) if d.is_dir() else []:
            if f.name not in keep and remove(f, dry_run=dry_run):
                changed.append(f)
    dropin = _p(ctx, DROPIN)
    if write_atomic(dropin, render_dropin(ctx), 0o644, dry_run=dry_run):
        changed.append(dropin)
    return changed


def check_sshd(ctx: Context) -> str | None:
    """`sshd -t`: None if sshd accepts the configuration, else its complaint."""
    argv = ["sshd", "-t"]
    if ctx.paths.sandbox:
        argv += ["-f", str(ctx.paths.p("/etc/ssh/sshd_config"))]
    res = ctx.runner.run(argv, timeout=15)
    if res.ok:
        return None
    lines = [ln for ln in (res.err or res.out).splitlines() if ln.strip()]
    return (lines[-1] if lines else f"exit {res.rc}")[:300]


def reload_sshd(ctx: Context) -> str | None:
    res = ctx.runner.run(["systemctl", "try-reload-or-restart", "ssh.service"], timeout=30)
    return None if res.ok else f"reloading sshd failed (exit {res.rc})"


def apply_files(
    ctx: Context, ship: ShipToml, dry_run: bool = False
) -> tuple[list[Path], list[str]]:
    """For `ship-ctl apply`: render, and if anything changed, check and reload sshd."""
    snapshot = _snapshot(ctx)
    changed = render_files(ctx, ship, dry_run=dry_run)
    messages: list[str] = []
    if changed and not dry_run:
        problem = check_sshd(ctx)
        if problem:
            _restore(ctx, snapshot)
            raise CtlError("internal", f"sshd refused the certificate settings: {problem}")
        note = reload_sshd(ctx)
        if note:
            messages.append(note)
    return changed, messages


# --- install ---------------------------------------------------------------------
MANAGED = (
    HOST_CERT,
    USER_CA,
    REVOKED,
    DROPIN,
    PRINCIPALS_DIR + "/agent",
    PRINCIPALS_DIR + "/fleet",
)


def _snapshot(ctx: Context) -> dict[str, tuple[bytes, int] | None]:
    snap: dict[str, tuple[bytes, int] | None] = {}
    for path in MANAGED:
        p = _p(ctx, path)
        data = read_bytes(p)
        snap[path] = None if data is None else (data, p.stat().st_mode & 0o7777)
    return snap


def _restore(ctx: Context, snap: dict[str, tuple[bytes, int] | None]) -> None:
    for path, old in snap.items():
        p = _p(ctx, path)
        if old is None:
            remove(p)
        else:
            write_atomic(p, old[0], old[1])


def load_document(ctx: Context, text: str) -> dict:
    if "\r" in text:
        raise _invalid("the ssh-trust document must not contain carriage returns")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise _invalid(f"the ssh-trust document is not JSON: {exc}") from exc
    try:
        errors = schema.load(ctx.paths.schema("ssh-trust")).errors(doc)
    except OSError as exc:
        raise CtlError("internal", f"cannot read the ssh-trust schema: {exc}") from exc
    if errors:
        raise _invalid("the ssh-trust document is invalid: " + "; ".join(errors[:3]))
    # JSON Schema's $ also matches before a final newline: refuse any newline outright,
    # so nothing can smuggle a second line into a file sshd reads.
    lines = [doc.get("host_certificate") or "", *doc.get("user_ca_keys", [])]
    if any("\n" in line for line in lines):
        raise _invalid("host_certificate and user_ca_keys must be single lines")
    return doc


def install(ctx: Context, ship: ShipToml, doc: dict) -> tuple[dict, list[str], list[str]]:
    """Check everything first, then write, then sshd -t (rolling back on failure)."""
    cert_line = doc.get("host_certificate")
    if cert_line is not None:
        check_host_cert(ctx, ship, cert_line)
    if "user_ca_keys" in doc:
        check_user_ca(ctx, doc["user_ca_keys"])
    krl = None
    if "revoked_keys" in doc:
        try:
            krl = base64.b64decode(doc["revoked_keys"], validate=True)
        except ValueError as exc:
            raise _invalid("revoked_keys is not valid base64") from exc
        check_krl(ctx, krl)

    snapshot = _snapshot(ctx)
    changed: list[Path] = []
    try:
        if cert_line is not None:
            p = _p(ctx, HOST_CERT)
            if write_atomic(p, cert_line.strip() + "\n", 0o644):
                changed.append(p)
        if "user_ca_keys" in doc:
            p = _p(ctx, USER_CA)
            keys = doc["user_ca_keys"]
            did = (
                write_atomic(p, "".join(k.strip() + "\n" for k in keys), 0o644)
                if keys
                else remove(p)
            )
            if did:
                changed.append(p)
        if krl is not None:
            p = _p(ctx, REVOKED)
            if write_atomic(p, krl, 0o644):
                changed.append(p)
        changed += render_files(ctx, ship)
        problem = check_sshd(ctx) if changed else None
    except BaseException:
        _restore(ctx, snapshot)
        raise
    if problem:
        _restore(ctx, snapshot)
        raise _invalid(f"sshd refused the new configuration, nothing changed: {problem}")
    messages = []
    if changed:
        note = reload_sshd(ctx)
        messages.append(note or "Installed; sshd reloaded.")
    else:
        messages.append("Nothing to change.")
    data, _ = state(ctx)
    return data, [ctx.paths.agent_path(p) for p in changed], messages


# --- status ------------------------------------------------------------------------
def state(ctx: Context) -> tuple[dict, list[str]]:
    """The status document's `ssh` section, plus probe errors."""
    errors: list[str] = []
    cert = None
    raw = read_bytes(_p(ctx, HOST_CERT))
    if raw is not None:
        info = cert_info(ctx, raw.decode("utf-8", "replace"))
        if info is None:
            errors.append("ssh: cannot read the host certificate")
        else:
            cert = public_cert(info)
    user_ca = []
    raw = read_bytes(_p(ctx, USER_CA))
    if raw is not None:
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                fp = fingerprint(ctx, line)
                if fp:
                    user_ca.append(fp)
                else:
                    errors.append("ssh: cannot read a user CA key")
    principals = []
    raw = read_bytes(_p(ctx, PRINCIPALS_DIR + "/agent"))
    if raw is not None:
        principals = [
            ln.strip()[:100] for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip()
        ]
    krl = read_bytes(_p(ctx, REVOKED))
    doc = {
        "host_certificate": cert,
        "user_ca": user_ca,
        "agent_principals": principals,
        "revoked_keys_sha256": hashlib.sha256(krl).hexdigest() if krl is not None else None,
    }
    return doc, errors


def attention(ssh: dict | None) -> list[tuple[str, str]]:
    """(code, message) pairs for the status document."""
    cert = (ssh or {}).get("host_certificate")
    if not cert or not cert.get("valid_before"):
        return []
    before = _parse_ts(cert["valid_before"])
    now = _now()
    if before <= now:
        return [
            ("host-cert-expired", "The SSH host certificate has expired; run `ship-fleet ca sign`.")
        ]
    if before - now <= timedelta(days=EXPIRING_DAYS):
        days = max(1, (before - now).days)
        return [
            (
                "host-cert-expiring",
                f"The SSH host certificate expires in {days} day(s); run `ship-fleet ca sign`.",
            )
        ]
    return []
