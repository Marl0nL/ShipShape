"""FleetLedger as the fleet's SSH certificate authority (phase 3).

Two CA keys, both on your computer and passphrase-protected (ships.toml [ca]):

- the host CA signs each Ship's host certificate. It certifies the host key
  already pinned in the inventory, so a Ship moves from a pinned key to a
  certificate without anyone ever trusting a key first seen on the network. The
  certificate lists every name the Ship is reached by.
- the user CA signs short-lived certificates for your machines ([machine.<m>]),
  for the logins each may use (agent, to attach to the agent; owner, your own
  login with sudo), on the Ships its scope allows: <login>:all,
  <login>:tag:<tag>, <login>:ship:<name>. The Ship takes agent:* for agent and
  owner:* for the owner login, so one never opens the other.

Ships get what they need through `ship-ctl ssh-trust` (contracts/ship-ctl.md):
the host certificate, the user CA public keys and a revocation list (KRL) of
untrusted machines' keys, plus the serials of certificates whose machine's
scope has since been narrowed. Machines get a bundle (`ship-fleet trust`): their
certificate, an SSH config block per Ship, one @cert-authority line, and herdr
saved machines.

Signing goes through ssh-agent when the CA key is loaded there (ssh-keygen -U),
so the private key never has to be read from disk; otherwise ssh-keygen asks for
its passphrase. Private keys are never read, copied or logged by FleetLedger.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fleetledger import paths, sshkeys
from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import Inventory, Ship

HOST_CA_COMMENT = "shipshape-host-ca"
USER_CA_COMMENT = "shipshape-user-ca"
HOST_CERT_DAYS = 90
USER_CERT_HOURS = 168
RENEW_WITHIN = timedelta(days=30)
BACKDATE = timedelta(minutes=5)  # tolerate clocks a little behind
CERT_TYPE = "ssh-ed25519-cert-v01@openssh.com"
ED25519_LINE = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/]+={0,2}( [^\n]*)?$")
CERT_LINE = re.compile(r"^ssh-ed25519-cert-v01@openssh\.com [A-Za-z0-9+/]+={0,2}( [^\n]*)?$")
PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}$")
SCOPE = re.compile(r"^(all|tag:[a-z0-9][a-z0-9-]{0,30}|ship:[a-z0-9][a-z0-9-]{0,30})$")
LOGINS = ("agent", "owner")  # what a machine's certificate may log in as

Runner = Callable[..., subprocess.CompletedProcess]


# --- configuration -------------------------------------------------------------
def _read_public(path: Path, what: str) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(
            f"Cannot read {what} ({path}).", hint="Run `ship-fleet ca init`."
        ) from exc
    line = text.splitlines()[0] if text else ""
    if not ED25519_LINE.match(line):
        raise ConfigError(f"{what} ({path}) is not an ed25519 public key.")
    return line


@dataclass(frozen=True)
class CAConfig:
    host_key: Path
    user_key: Path
    host_cert_days: int = HOST_CERT_DAYS
    user_cert_hours: int = USER_CERT_HOURS
    previous_user_keys: tuple[str, ...] = ()
    revoked_host_keys: tuple[str, ...] = ()

    @property
    def host_public_path(self) -> Path:
        return self.host_key.with_name(self.host_key.name + ".pub")

    @property
    def user_public_path(self) -> Path:
        return self.user_key.with_name(self.user_key.name + ".pub")

    @property
    def host_public(self) -> str:
        return _read_public(self.host_public_path, "the host CA public key")

    @property
    def user_public(self) -> str:
        return _read_public(self.user_public_path, "the user CA public key")

    def user_ca_keys(self) -> list[str]:
        """What Ships trust: the current user CA, then any being rotated out."""
        keys = [self.user_public]
        keys += [k for k in self.previous_user_keys if _blob(k) != _blob(keys[0])]
        return keys


def config(inventory: Inventory) -> CAConfig | None:
    raw = inventory.doc.get("ca")
    if not raw:
        return None
    return CAConfig(
        host_key=paths.expand(raw["host_key"]),
        user_key=paths.expand(raw["user_key"]),
        host_cert_days=int(raw.get("host_cert_days", HOST_CERT_DAYS)),
        user_cert_hours=int(raw.get("user_cert_hours", USER_CERT_HOURS)),
        previous_user_keys=tuple(raw.get("previous_user_keys", ())),
        revoked_host_keys=tuple(raw.get("revoked_host_keys", ())),
    )


def require(inventory: Inventory) -> CAConfig:
    ca = config(inventory)
    if ca is None:
        raise ConfigError(
            "FleetLedger has no certificate authority yet.", hint="Run `ship-fleet ca init`."
        )
    return ca


_blob = sshkeys.blob
fingerprint = sshkeys.fingerprint  # OpenSSH SHA256 fingerprint of a public key line


# --- machines ---------------------------------------------------------------------
@dataclass(frozen=True)
class Machine:
    name: str
    public_key: str
    added: str
    scope: tuple[str, ...]
    cert_hours: int | None = None
    last_serial: int | None = None
    last_valid_before: str | None = None
    revoked: str | None = None
    issued: tuple[dict[str, Any], ...] = ()
    logins: tuple[str, ...] = LOGINS  # absent in ships.toml: both, as before
    logins_recorded: bool = True

    @classmethod
    def from_entry(cls, name: str, entry: dict[str, Any]) -> Machine:
        return cls(
            name=name,
            public_key=entry["public_key"],
            added=entry["added"],
            scope=tuple(entry["scope"]),
            cert_hours=entry.get("cert_hours"),
            last_serial=entry.get("last_serial"),
            last_valid_before=entry.get("last_valid_before"),
            revoked=entry.get("revoked"),
            issued=tuple(dict(i) for i in entry.get("issued", ())),
            logins=tuple(x for x in LOGINS if x in entry.get("logins", LOGINS)),
            logins_recorded="logins" in entry,
        )

    @property
    def principals(self) -> list[str]:
        return [principal_for(s, login) for login in self.logins for s in self.scope]

    def outlived(self) -> list[dict[str, Any]]:
        """Certificates issued for a wider scope than the machine has now."""
        now = set(self.principals)
        return [i for i in self.issued if not set(i["principals"]) <= now]

    def reaches(self, inventory: Inventory, ship: Ship) -> bool:
        tags = inventory.effective_tags(ship)
        for s in self.scope:
            if s == "all" or s == f"ship:{ship.name}":
                return True
            if s.startswith("tag:") and s[4:] in tags:
                return True
        return False


def machines(inventory: Inventory) -> list[Machine]:
    return [Machine.from_entry(n, e) for n, e in sorted(inventory.doc.get("machine", {}).items())]


def machine(inventory: Inventory, name: str) -> Machine | None:
    entry = inventory.doc.get("machine", {}).get(name)
    return Machine.from_entry(name, entry) if entry else None


def principal_for(scope: str, login: str = "agent") -> str:
    if not SCOPE.match(scope):
        raise FleetError(f"Unknown scope {scope!r}: use all, tag:<tag> or ship:<name>.")
    if login not in LOGINS:
        raise FleetError(f"Unknown login {login!r}: use agent or owner.")
    return f"{login}:{scope}"


def parse_logins(text: str) -> list[str]:
    """--logins agent | owner | both (or agent,owner)."""
    wanted = list(LOGINS) if text.strip() == "both" else [
        t.strip() for t in text.split(",") if t.strip()]
    if not wanted or any(t not in LOGINS for t in wanted):
        raise FleetError("--logins: agent (attach to the agent), owner (your own login, "
                         "with sudo) or both.")
    return [t for t in LOGINS if t in wanted]


def logins_said(logins) -> str:
    return {("agent",): "the agent's login", ("owner",): "your own login (sudo)",
            LOGINS: "the agent's login and your own (sudo)"}[tuple(logins)]


def host_principals(ship: Ship) -> list[str]:
    """Every name the Ship can be reached by, plus its bare name, in order, no repeats."""
    names = [ship.name, *ship.names]
    out: list[str] = []
    for n in names:
        if n.lower() not in (o.lower() for o in out):
            out.append(n)
    return out


# --- time -------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _keygen_time(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y%m%d%H%M%SZ")


# --- signing ------------------------------------------------------------------------
def loaded_agent_keys(runner: Runner = subprocess.run) -> set[str]:
    """Key blobs ssh-agent holds (public halves only, from ssh-add -L)."""
    if not os.environ.get("SSH_AUTH_SOCK"):
        return set()
    try:
        proc = runner(["ssh-add", "-L"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    return {_blob(line) for line in proc.stdout.splitlines() if line.strip()}


@dataclass
class Signer:
    """ssh-keygen -s, through ssh-agent when the CA key is loaded there."""

    runner: Runner = subprocess.run
    agent_keys: Callable[[], set[str]] | None = None
    keygen: str = "ssh-keygen"
    _loaded: set[str] | None = field(default=None, init=False)

    def _in_agent(self, public_line: str) -> bool:
        if self._loaded is None:
            self._loaded = (self.agent_keys or (lambda: loaded_agent_keys(self.runner)))()
        return _blob(public_line) in self._loaded

    def sign(
        self,
        ca_private: Path,
        ca_public: str,
        subject_key: str,
        *,
        host: bool,
        key_id: str,
        principals: Sequence[str],
        valid_from: datetime,
        valid_to: datetime,
        serial: int,
        options: Sequence[str] = (),
    ) -> str:
        if not ED25519_LINE.match(subject_key.strip()):
            raise FleetError("Only ed25519 keys are certified.")
        for p in principals:
            if not PRINCIPAL.match(p):
                raise FleetError(f"Cannot use {p!r} as a certificate principal.")
        if not principals:
            raise FleetError("A certificate needs at least one principal.")
        with tempfile.TemporaryDirectory(prefix="fleetledger-sign-") as tmp:
            folder = Path(tmp)
            os.chmod(folder, 0o700)
            subject = folder / "subject.pub"
            subject.write_text(subject_key.strip() + "\n", encoding="utf-8")
            argv = [self.keygen, "-q"]
            if self._in_agent(ca_public):
                ca_pub = folder / "ca.pub"
                ca_pub.write_text(ca_public.strip() + "\n", encoding="utf-8")
                argv += ["-U", "-s", str(ca_pub)]
            else:
                argv += ["-s", str(ca_private)]
            argv += ["-I", key_id, "-n", ",".join(principals), "-z", str(serial),
                     "-V", f"{_keygen_time(valid_from)}:{_keygen_time(valid_to)}"]
            if host:
                argv.append("-h")
            for opt in options:
                argv += ["-O", opt]
            argv.append(str(subject))
            # stdin and the terminal stay connected: ssh-keygen may ask for the
            # CA passphrase. Its output never holds a secret.
            proc = self.runner(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            cert = folder / "subject-cert.pub"
            if proc.returncode != 0 or not cert.exists():
                why = (proc.stderr or "").strip().splitlines()
                raise FleetError(
                    "ssh-keygen could not sign the certificate"
                    + (f": {why[-1]}" if why else "."),
                    hint="Load the CA key with `ssh-add` (or type its passphrase when asked).",
                )
            line = cert.read_text(encoding="utf-8").strip()
        if not CERT_LINE.match(line):
            raise FleetError("ssh-keygen produced something that is not an ed25519 certificate.")
        return line


def new_serial() -> int:
    return secrets.randbits(62) + 1


def sign_host(signer: Signer, ca: CAConfig, ship: Ship, *, now: datetime | None = None) -> str:
    """A host certificate for the Ship's pinned key (never a key from the network)."""
    now = now or utc_now()
    return signer.sign(
        ca.host_key,
        ca.host_public,
        ship.host_key_type_and_blob + f" {ship.name}",
        host=True,
        key_id=f"{ship.name} host {stamp(now)}",
        principals=host_principals(ship),
        valid_from=now - BACKDATE,
        valid_to=now + timedelta(days=ca.host_cert_days),
        serial=new_serial(),
    )


def sign_user(
    signer: Signer, ca: CAConfig, m: Machine, *, hours: int | None = None,
    now: datetime | None = None,
) -> tuple[str, int, datetime]:
    """A user certificate for a trusted machine: (line, serial, valid until)."""
    if m.revoked:
        raise FleetError(f"{m.name} was untrusted on {m.revoked}; it is never certified again.",
                         hint="Trust the machine under a new name with a new key.")
    now = now or utc_now()
    hours = hours or m.cert_hours or ca.user_cert_hours
    until = now + timedelta(hours=hours)
    serial = new_serial()
    line = signer.sign(
        ca.user_key,
        ca.user_public,
        m.public_key,
        host=False,
        key_id=f"machine {m.name} {stamp(now)}",
        principals=m.principals,
        valid_from=now - BACKDATE,
        valid_to=until,
        serial=serial,
        # A shell and herdr need a terminal; nothing else (no forwarding, no ~/.ssh/rc).
        options=("clear", "permit-pty"),
    )
    return line, serial, until


def issued_record(m: Machine, ca: CAConfig, serial: int, until: datetime, *,
                  now: datetime | None = None) -> list[dict[str, Any]]:
    """The machine's `issued` list with a new certificate added.

    Every certificate is recorded until it lapses, so a narrower scope later can
    revoke the wider ones still out there. Lapsed entries are dropped here only
    if they are not in the revocation list (dropping one that is would change the
    list without pushing it); scope changes drop the rest.
    """
    now = now or utc_now()
    current = set(m.principals)
    keep = [i for i in m.issued
            if not (_lapsed(i, now) and set(i["principals"]) <= current)]
    return [*keep, {"serial": serial, "valid_before": stamp(until),
                    "principals": list(m.principals), "ca": fingerprint(ca.user_public)}]


def _lapsed(entry: dict[str, Any], now: datetime) -> bool:
    until = parse_stamp(entry.get("valid_before"))
    return until is None or until <= now


def unlapsed(issued: Iterable[dict[str, Any]], *, now: datetime | None = None
             ) -> list[dict[str, Any]]:
    now = now or utc_now()
    return [i for i in issued if not _lapsed(i, now)]


# --- revocation list ----------------------------------------------------------------
def revoked_keys(inventory: Inventory) -> list[str]:
    return sorted(" ".join(m.public_key.split()[:2]) for m in machines(inventory) if m.revoked)


def revoked_serials(inventory: Inventory) -> dict[str, list[int]]:
    """{user CA key line: serials} of certificates wider than their machine's scope.

    Untrusted machines are left out: their key is revoked, which covers every
    certificate they hold. Serials from a CA no longer trusted are left out too.
    """
    cfg = config(inventory)
    if cfg is None:
        return {}
    by_fp = {fingerprint(k): " ".join(k.split()[:2]) for k in cfg.user_ca_keys()}
    out: dict[str, set[int]] = {}
    for m in machines(inventory):
        if m.revoked:
            continue
        for entry in m.outlived():
            key = by_fp.get(entry.get("ca", ""))
            if key:
                out.setdefault(key, set()).add(int(entry["serial"]))
    return {k: sorted(v) for k, v in sorted(out.items())}


def _krl_specs(inventory: Inventory) -> list[tuple[str | None, str]]:
    """[(CA key line or None, spec text)]: keys first, then one spec per CA."""
    serials = revoked_serials(inventory)
    keys = "".join(f"key: {k}\n" for k in revoked_keys(inventory))
    specs: list[tuple[str | None, str]] = []
    for n, (ca_key, numbers) in enumerate(serials.items()):
        text = "".join(f"serial: {s}\n" for s in numbers)
        specs.append((ca_key, (keys if n == 0 else "") + text))
    return specs or [(None, keys)]


def _krl_spec_hash(specs: list[tuple[str | None, str]]) -> str:
    if specs[0][0] is None:  # keys only: the same hash as before serials existed
        return hashlib.sha256(specs[0][1].encode()).hexdigest()
    return hashlib.sha256(json.dumps(specs).encode()).hexdigest()


def krl_dir() -> Path:
    return paths.data_dir() / "ca"


def build_krl(inventory: Inventory, runner: Runner = subprocess.run,
              keygen: str = "ssh-keygen") -> bytes:
    """The KRL of untrusted machines' keys and outlived certificates' serials.

    ssh-keygen stamps each KRL with the time, so it is cached and keeps the same
    bytes (and the same hash in Ships' status) while nothing has been revoked.
    Serials are per CA: the CA's public key goes on the command line (-s), and
    a second CA's serials are added to the same list (-u).
    """
    specs = _krl_specs(inventory)
    spec_hash = _krl_spec_hash(specs)
    folder = paths.private_dir(krl_dir())
    cached, marker = folder / "revoked-keys.krl", folder / "revoked-keys.spec-sha256"
    try:
        if marker.read_text().strip() == spec_hash and cached.exists():
            return cached.read_bytes()
    except OSError:
        pass
    with tempfile.TemporaryDirectory(prefix="fleetledger-krl-") as tmp:
        out = Path(tmp) / "krl"
        for n, (ca_key, spec) in enumerate(specs):
            spec_file = Path(tmp) / f"spec{n}"
            spec_file.write_text(spec, encoding="utf-8")
            argv = [keygen, "-q", "-k"] + (["-u"] if n else []) + ["-f", str(out)]
            if ca_key:
                ca_file = Path(tmp) / f"ca{n}.pub"
                ca_file.write_text(ca_key + "\n", encoding="utf-8")
                argv += ["-s", str(ca_file)]
            proc = runner([*argv, str(spec_file)], capture_output=True, text=True)
            if proc.returncode != 0 or not out.exists():
                raise FleetError("ssh-keygen could not build the revocation list.")
        data = out.read_bytes()
    paths.write_private(cached, data)
    paths.write_private(marker, (spec_hash + "\n").encode())
    return data


def cached_krl(inventory: Inventory) -> bytes | None:
    """The cached KRL if it matches today's revocations, without running ssh-keygen."""
    folder = krl_dir()
    try:
        marker = (folder / "revoked-keys.spec-sha256").read_text().strip()
        if marker == _krl_spec_hash(_krl_specs(inventory)):
            return (folder / "revoked-keys.krl").read_bytes()
    except OSError:
        pass
    return None


def krl_revokes(data: bytes, key_line: str, runner: Runner = subprocess.run,
                keygen: str = "ssh-keygen") -> bool:
    with tempfile.TemporaryDirectory(prefix="fleetledger-krl-") as tmp:
        krl = Path(tmp) / "krl"
        krl.write_bytes(data)
        key = Path(tmp) / "key.pub"
        key.write_text(key_line.strip() + "\n")
        proc = runner([keygen, "-Q", "-f", str(krl), str(key)], capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        raise FleetError("ssh-keygen could not read the revocation list.")
    return proc.returncode == 1


# --- what Ships get -----------------------------------------------------------------
def trust_document(
    ca: CAConfig, krl: bytes, host_certificate: str | None = None
) -> dict[str, Any]:
    doc: dict[str, Any] = {"schema": "shipshape.ssh-trust/1"}
    if host_certificate:
        doc["host_certificate"] = host_certificate
    doc["user_ca_keys"] = ca.user_ca_keys()
    doc["revoked_keys"] = base64.b64encode(krl).decode("ascii")
    return doc


@dataclass(frozen=True)
class ShipCertState:
    """What a Ship's status says about its certificates, judged against the CA."""

    supported: bool  # the Ship has phase 3 tooling (status has `ssh`)
    cert: dict[str, Any] | None
    needs_cert: bool
    reason: str | None
    trust_behind: bool


def judge(ca: CAConfig, ship: Ship, status: dict[str, Any] | None, krl: bytes | None,
          *, now: datetime | None = None) -> ShipCertState:
    if not status or "ssh" not in status:
        return ShipCertState(False, None, False, "the Ship predates certificates", False)
    ssh = status["ssh"]
    cert = ssh.get("host_certificate")
    now = now or utc_now()
    reason = None
    if cert is None:
        reason = "no host certificate"
    elif cert["ca_fingerprint"] != fingerprint(ca.host_public):
        reason = "signed by another CA"
    elif set(cert["principals"]) != set(host_principals(ship)):
        reason = "names changed"
    else:
        before = parse_stamp(cert.get("valid_before"))
        if before is not None and before - now <= RENEW_WITHIN:
            reason = "expires soon" if before > now else "expired"
    wanted_ca = [fingerprint(k) for k in ca.user_ca_keys()]
    behind = ssh.get("user_ca") != wanted_ca
    if krl is not None:
        behind = behind or ssh.get("revoked_keys_sha256") != hashlib.sha256(krl).hexdigest()
    return ShipCertState(True, cert, reason is not None, reason, behind)


def attention(ca: CAConfig | None, ship: Ship, status: dict[str, Any],
              krl: bytes | None) -> list[dict[str, str]]:
    """FleetLedger's own attention items about certificates (contracts/ship-ctl.md)."""
    if ca is None:
        return []
    try:
        state = judge(ca, ship, status, krl)
    except ConfigError:
        return []
    if not state.supported:
        return []
    found = []
    if state.cert is None or state.reason in ("signed by another CA", "names changed"):
        found.append({
            "code": "host-cert-missing",
            "subject": "ssh",
            "message": f"No current SSH host certificate ({state.reason}); run "
            f"`ship-fleet ca sign {ship.name}`.",
        })
    if state.trust_behind:
        found.append({
            "code": "ssh-trust-behind",
            "subject": "ssh",
            "message": "The Ship's user CA keys or revocation list are out of date; run "
            f"`ship-fleet ca sign {ship.name}`.",
        })
    return found


# --- what trusted machines get ------------------------------------------------------
BUNDLE_DIR_ON_MACHINE = "~/.ssh/shipshape"


@dataclass(frozen=True)
class Bundle:
    folder: Path
    ships: list[str]
    cert_valid_before: datetime


def known_hosts_text(ca: CAConfig, ships: Iterable[Ship]) -> str:
    names = sorted({n for s in ships for n in host_principals(s)})
    lines = ["# ShipShape: trust Ships' host certificates (written by ship-fleet trust)."]
    if names:
        lines.append(f"@cert-authority {','.join(names)} {ca.host_public}")
    for key in ca.revoked_host_keys:
        lines.append(f"@revoked * {' '.join(key.split()[:2])}")
    return "\n".join(lines) + "\n"


def dial_name(ship: Ship, resolves) -> str:
    """The name to dial a Ship by from here: the first of its names that resolves on
    this computer (an address always does), else its first name. A .local name with
    no mDNS resolution here falls through to the address recorded when it started."""
    return next((n for n in ship.names if resolves(n)), ship.names[0])


def ssh_config_text(machine_name: str, ships: Iterable[Ship], identity: str,
                    hostnames: Mapping[str, str] | None = None, user: str = "agent") -> str:
    base = BUNDLE_DIR_ON_MACHINE
    out = [
        f"# ShipShape Ships for {machine_name} (written by ship-fleet trust).",
        "# Regenerate with `ship-fleet trust` rather than editing.",
    ]
    for ship in ships:
        out += [
            "",
            f"Host {ship.name}",
            f"  HostName {(hostnames or {}).get(ship.name, ship.names[0])}",
            f"  User {user}",
            f"  IdentityFile {identity}",
            f"  CertificateFile {base}/{machine_name}-cert.pub",
            "  IdentitiesOnly yes",
            f"  UserKnownHostsFile {base}/known_hosts",
            "  GlobalKnownHostsFile /dev/null",
            "  StrictHostKeyChecking yes",
            "  HostKeyAlgorithms ssh-ed25519-cert-v01@openssh.com",
            "  UpdateHostKeys no",
            "  ForwardAgent no",
            "  ForwardX11 no",
        ]
    return "\n".join(out) + "\n"


INSTALL_SH = r"""#!/bin/sh
# Install this ShipShape trust bundle for the current user (run it from the
# bundle's folder, on the machine it was made for). Idempotent.
#
#   sh install.sh            # SSH config, certificate, CA line, herdr saved machines
#   sh install.sh --no-herdr # everything except herdr
set -eu
here=$(cd "$(dirname "$0")" && pwd)
dest="$HOME/.ssh/shipshape"
herdr=1
[ "${1:-}" = "--no-herdr" ] && herdr=0

umask 077
mkdir -p "$HOME/.ssh" "$dest"
chmod 700 "$HOME/.ssh" "$dest"
for f in config known_hosts @CERT@; do
  cp "$here/$f" "$dest/$f.tmp" && chmod 600 "$dest/$f.tmp" && mv "$dest/$f.tmp" "$dest/$f"
done

key=@IDENTITY_SH@
if [ ! -f "$key" ]; then
  echo "Missing $key: this bundle is for the key whose public half was trusted." >&2
  echo "Copy that private key there, or re-run ship-fleet trust with --identity." >&2
  exit 1
fi

include="Include $dest/config"
# A ~/.ssh/config that is a link (to a dotfiles folder, say) is written through, so
# the link and the file it points at stay one.
cfg="$HOME/.ssh/config"
if [ -L "$cfg" ]; then
  cfg=$(readlink -f "$cfg") || { echo "Cannot follow the link ~/.ssh/config." >&2; exit 1; }
fi
if [ ! -f "$cfg" ] || ! grep -qxF "$include" "$cfg"; then
  # Include must come before any Host block to apply everywhere: put it first.
  tmp="$cfg.shipshape-tmp"
  { echo "$include"; if [ -f "$cfg" ]; then cat "$cfg"; fi; } > "$tmp"
  chmod 600 "$tmp" && mv "$tmp" "$cfg"
  echo "Added '$include' to the top of ~/.ssh/config."
fi

if [ "$herdr" = 1 ]; then
  if command -v herdr >/dev/null 2>&1; then
    known=$(herdr machine list --json 2>/dev/null || true)
    for ship in @SHIPS@; do
      if printf '%s' "$known" | grep -q "\"$ship\""; then
        echo "herdr already has $ship."
      else
        herdr machine add "$ship" --label "$ship" --remote-session default </dev/null \
          || echo "herdr could not add $ship; check \`ssh $ship true\` first." >&2
      fi
    done
  else
    echo "herdr is not installed here; skipping saved machines."
  fi
fi
echo "Done. Try: ssh @FIRST@ true   (then herdr --remote @FIRST@)"
"""


# herdr-core Ships (ship.toml runtime = "core"): the herdr TUI cannot attach to them,
# and a herdr TUI that lists one starts a stray herdr server there over SSH, which
# could resume sessions herdr-core is running. So their herdr saved machine goes,
# found by its label or SSH target (both the Ship's name, as this script adds them;
# never another field, such as its session) and removed by that profile's id (32 hex
# digits; an entry whose id cannot be read is said, not guessed at), and hc gets
# the Ship instead, when it is installed here. Nothing is installed: no --install,
# which would copy hc over.
INSTALL_CORE_SH = r"""
# herdr-core Ships: hc attaches to them, the herdr TUI cannot.
core=@CORE@
if [ "$herdr" = 1 ]; then
  if command -v herdr >/dev/null 2>&1; then
    known=$(herdr machine list --json 2>/dev/null || true)
    for ship in $core; do
      ids=$(printf '%s' "$known" | tr -d '\n' | tr '{' '\n' | tr '}' '\n' \
        | grep -E "\"(label|target)\" *: *\"$ship\"" \
        | grep -oE '"([a-z_]*_)?id" *: *"[0-9a-f]{32}"' | grep -oE '[0-9a-f]{32}' || true)
      for id in $ids; do
        if herdr machine remove "$id" </dev/null >/dev/null; then
          echo "Removed $ship from herdr's saved machines: it runs herdr-core (use hc)."
        else
          echo "herdr could not remove $ship; run: herdr machine remove $id" >&2
        fi
      done
      if [ -z "$ids" ] && printf '%s' "$known" | grep -qE "\"(label|target)\" *: *\"$ship\""; then
        echo "herdr still lists $ship, which runs herdr-core: remove it" \
          "(herdr machine list, then herdr machine remove <id>)." >&2
      fi
    done
  fi
  if command -v hc >/dev/null 2>&1; then
    known=$(hc machine ls --json 2>/dev/null || true)
    for ship in $core; do
      if printf '%s' "$known" | grep -qE "\"name\" *: *\"$ship\""; then
        echo "hc already has $ship."
      elif err=$(hc machine add "$ship" --name "$ship" </dev/null 2>&1); then
        echo "hc added $ship."
      elif printf '%s' "$err" | grep -q already; then
        echo "hc already has $ship."
      else
        echo "hc could not add $ship; check \`ssh $ship true\` first." >&2
      fi
    done
  else
    echo "hc (herdr-core) is not installed here; skipping @CORE_SAID@." \
      "Install it, then run this again, or use the herdr desktop app."
  fi
fi
"""


def install_script(machine_name: str, ships: Sequence[str], identity: str, *,
                   herdr: bool = True, core: Collection[str] = ()) -> str:
    """herdr=False: a machine that may not log in as the agent gets no herdr saved
    machines (herdr attaches to the agent), and its hint says so. core: the Ships
    that run herdr-core: hc gets them and herdr loses them (INSTALL_CORE_SH). With
    none the script is exactly as before core existed."""
    if identity.startswith("~/"):
        ident = '"$HOME/' + identity[2:].replace('"', "") + '"'
    else:
        ident = shlex.quote(identity)
    on_core = [s for s in ships if s in core] if herdr else []
    on_herdr = [s for s in ships if s not in on_core]
    first = ships[0] if ships else "<ship>"
    script = (
        INSTALL_SH.replace("@CERT@", f"{machine_name}-cert.pub")
        .replace("@IDENTITY_SH@", ident)
        .replace("@SHIPS@", " ".join(shlex.quote(s) for s in on_herdr) if herdr else "")
        .replace("@FIRST@", first)
    )
    if not herdr:
        script = script.replace("   (then herdr --remote " + first + ")",
                                "   (your own login; herdr needs the agent's)")
    if on_core:
        # Ship names are host names (no spaces or globs): $core splits into them.
        block = (INSTALL_CORE_SH.replace("@CORE@", "'" + " ".join(on_core).replace("'", "") + "'")
                 .replace("@CORE_SAID@", ", ".join(on_core)))
        script = (script.replace("herdr saved machines\n", "herdr and hc saved machines\n", 1)
                  .replace("# everything except herdr\n", "# everything except herdr and hc\n", 1)
                  .replace("\necho \"Done. Try:", block.rstrip("\n") + "\necho \"Done. Try:", 1))
        if first in on_core:
            script = script.replace("   (then herdr --remote " + first + ")",
                                    "   (then hc attach " + first + "/<agent>, or hc)")
    return script


def write_bundle(
    folder: Path,
    *,
    ca: CAConfig,
    machine_name: str,
    cert: str,
    ships: Sequence[Ship],
    identity: str,
    valid_before: datetime,
    hostnames: Mapping[str, str] | None = None,
    user: str = "agent",
    core: Collection[str] = (),
) -> Bundle:
    """core: the Ships that run herdr-core (attach.cached_runtime), for install.sh."""
    paths.private_dir(folder)
    files = {
        f"{machine_name}-cert.pub": cert + "\n",
        "known_hosts": known_hosts_text(ca, ships),
        "config": ssh_config_text(machine_name, ships, identity, hostnames, user),
        # herdr attaches to the agent: only for a machine that may log in as agent.
        "install.sh": install_script(machine_name, [s.name for s in ships], identity,
                                     herdr=user == "agent", core=core),
        "bundle.json": json.dumps({
            "schema": "shipshape.trust-bundle/1",
            "machine": machine_name,
            "ships": [s.name for s in ships],
            "certificate_valid_before": stamp(valid_before),
        }, indent=2) + "\n",
    }
    for name, text in files.items():
        paths.write_private(folder / name, text.encode("utf-8"))
    return Bundle(folder, [s.name for s in ships], valid_before)


def bundle_dir(machine_name: str) -> Path:
    return paths.data_dir() / "trust" / machine_name
