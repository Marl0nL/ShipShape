"""ship-fleet machine-setup: what `shipshape-setup` (the installer at the checkout's root)
needs from FleetLedger to set up a computer as a Herdr client, over an ssh session that
already has a shell here (contracts/machine-setup.schema.json; contracts/ship-fleet.md).

    ship-fleet machine-setup --json       the data: FleetLedger's own user, names and host
                                          key; the trusted machines (name, public key, how
                                          their key reaches FleetLedger); the SHA-256 of the
                                          refresher and of the package
    ship-fleet machine-setup --package    the package: a gzipped tar of this checkout's
                                          shipshape-setup and herdr/ (install scripts, the
                                          Desk layout and themes, the Desk's three mods and
                                          the Fleet panel), byte for byte the same for the
                                          same checkout
    ship-fleet machine-setup --installer  shipshape-setup alone, for a computer with no
                                          checkout

It only reads: it signs nothing, trusts nothing, and changes no file. It answers only on
FleetLedger's own VM, as the copy that acts (where `ship-fleet trust` prints its
first-install line and the refresher works), since the data names that VM's host key.
A trusted machine's bundle-only key never reaches it: its line runs `machine-bundle`
whatever is asked. The installer checks every value it reads against the same patterns
the refresher uses, fetches the refresher itself through that machine's own key (its
bundle, with this VM's host key pinned) and runs it only when its SHA-256 is the one
given here: the check the pasted line makes.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path
from typing import Any

from fleetledger import appliance, authkeys, ca, refresher, trustcmd
from fleetledger.errors import FleetError

SCHEMA = "shipshape.machine-setup/1"
INSTALLER = "shipshape-setup"
# What the package holds, under the checkout's root: the installer, and herdr/ less its
# tests. Each folder whole (no __pycache__); a link only to a file inside herdr/.
PACKAGE = (INSTALLER, "herdr/install-desk.sh", "herdr/install-fleet.sh", "herdr/README.md",
           "herdr/layouts", "herdr/themes", "herdr/mods/desk", "herdr/mods/desk-agents",
           "herdr/mods/desk-spaces", "herdr/mods/fleet")
MAX_PACKAGE = 8 * 1024 * 1024  # the installer refuses more
ACCESS = {"installed": "bundle", "own-line": "shell"}


def _files(root: Path) -> list[tuple[str, Path]]:
    herdr = (root / "herdr").resolve()
    out: list[tuple[str, Path]] = []
    for item in PACKAGE:
        top = root / item
        found = [top] if top.is_file() else sorted(
            p for p in top.rglob("*") if "__pycache__" not in p.parts
            and (p.is_file() or p.is_symlink()))
        if not found or not (top.is_file() or top.is_dir()):
            raise FleetError(f"This checkout has no {item}, so it cannot make the Herdr "
                             "package.", hint="Update FleetLedger: ship-fleet self-update.")
        for p in found:
            real = p.resolve()
            if p.is_symlink() and not (real.is_file() and real.is_relative_to(herdr)):
                raise FleetError(f"{p.relative_to(root)} links outside herdr/; not packed.")
            out.append((p.relative_to(root).as_posix(), real))
    return sorted(out)


def package(root: Path) -> bytes:
    """The package, the same bytes for the same files: names sorted, owner 0, times 0,
    modes 0644 or 0755, gzip with no name or time."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, real in _files(root):
            data = real.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if os.access(real, os.X_OK) else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    zipped = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=zipped, mtime=0) as gz:
        gz.write(raw.getvalue())
    data = zipped.getvalue()
    if len(data) > MAX_PACKAGE:
        raise FleetError(f"The Herdr package is {len(data)} bytes, over the installer's "
                         f"{MAX_PACKAGE}.")
    return data


def _vm(store, inv) -> dict[str, Any]:
    import getpass

    me = appliance.fleet_self(inv)
    if not me or not me.get("host_key") or not authkeys.adds_here(store, inv):
        raise FleetError("ship-fleet machine-setup answers on FleetLedger's own VM, as the "
                         "copy that acts: computers set themselves up from there.",
                         hint="FleetLedger on this computer: ship-fleet trust "
                              "--this-computer sets it up here; another computer gets its "
                              "bundle from ship-fleet trust NAME.")
    hosts = list(dict.fromkeys([*me.get("names", []), me["name"]]))
    return {"name": me["name"], "user": getpass.getuser(), "hosts": hosts,
            "host_key": " ".join(str(me["host_key"]).split()[:2])}


def document(app, store) -> dict[str, Any]:
    inv = store.load()
    vm = _vm(store, inv)
    root = Path(app.repo_factory().root)
    states = authkeys.states(store, inv) or {}
    here = trustcmd.this_computer()
    machines = []
    for m in ca.machines(inv):
        blob = authkeys.ed25519_blob(m.public_key)
        if m.revoked or blob is None or (here and m.name == here["machine"]):
            continue
        machines.append({"name": m.name, "key": f"ssh-ed25519 {blob}",
                         "access": ACCESS.get(states.get(m.name) or "", "none")})
    pkg = package(root)
    return {"schema": SCHEMA, "fleetledger": vm,
            "refresher_sha256": hashlib.sha256(refresher.script()).hexdigest(),
            "package": {"sha256": hashlib.sha256(pkg).hexdigest(), "bytes": len(pkg)},
            "machines": machines}


def _binary_out(app) -> Any:
    out = getattr(app, "out_bytes", None)
    if out is not None:
        return out
    return sys.stdout.buffer


def cmd_machine_setup(args, app) -> int:
    store = app.store_factory()
    picked = [x for x in ("json", "package", "installer") if getattr(args, x)]
    if len(picked) != 1:
        raise FleetError("Say what to print: --json, --package or --installer.",
                         hint="A computer sets itself up with the installer: shipshape-setup "
                              "--herdr-client --fleetledger USER@THIS-VM (from a ShipShape "
                              "checkout, or ship-fleet machine-setup --installer here).")
    inv = store.load()
    _vm(store, inv)  # the same refusal for each
    root = Path(app.repo_factory().root)
    if args.json:
        app.say(json.dumps(document(app, store), indent=2))
        return 0
    if args.installer:
        app.say((root / INSTALLER).read_text().rstrip("\n"))
        return 0
    data = package(root)
    out = _binary_out(app)
    out.write(data)
    out.flush()
    return 0


def add_parsers(sub, add) -> None:
    p = add(sub, "machine-setup", "what shipshape-setup reads to set a computer up (JSON, "
            "package)",
            "What the installer, shipshape-setup, reads over ssh to set a computer up as a\n"
            "Herdr client: --json (FleetLedger's own names and host key, the trusted\n"
            "machines, the SHA-256 of the refresher and of the package), --package (the\n"
            "installer and the Desk and Fleet panel mods, a gzipped tar), --installer\n"
            "(shipshape-setup alone). Only reads; only on FleetLedger's own VM.")
    p.add_argument("--json", action="store_true", help="the setup data")
    p.add_argument("--package", action="store_true", help="the package, gzipped tar")
    p.add_argument("--installer", action="store_true", help="shipshape-setup alone")
