"""What a FleetLedger computer needs, checked one piece at a time.

`ship-fleet doctor` prints these checks; `ship-fleet setup` walks the same list
and does what is missing. Every check is read-only: it looks at files, asks
ssh-agent what it holds, and (unless offline) asks the Proxmox API and the
local Tailscale. Each result names the exact next command when something is
missing, so a new user is never left guessing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleetledger import paths
from fleetledger.errors import FleetError


@dataclass(frozen=True)
class Item:
    area: str
    name: str
    state: str  # ok | warn | missing | fail | skip
    detail: str = ""
    next: str = ""  # the exact command (or step) that fixes it


Runner = Callable[..., subprocess.CompletedProcess]


def _run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False, **kw)


def empty_pythonpath_entries(value: str | None) -> bool:
    return bool(value) and "" in value.split(os.pathsep)


# --- this computer -------------------------------------------------------------------


def computer(app) -> list[Item]:
    out = []
    v = sys.version_info
    out.append(Item("computer", "Python", "ok" if v >= (3, 12) else "fail",
                    f"{v.major}.{v.minor}.{v.micro}",
                    "" if v >= (3, 12) else "install Python 3.12 or newer, then "
                    "fleetledger/install.sh"))
    pythonpath = os.environ.get("PYTHONPATH")
    if empty_pythonpath_entries(pythonpath):
        out.append(Item("computer", "PYTHONPATH", "warn",
                        f"{pythonpath!r} has an empty entry, read as the current folder "
                        "(ship-fleet itself is protected; other tools are not)",
                        "remove the stray ':' where PYTHONPATH is set (e.g. ~/.bashrc)"))
    missing = [t for t in ("ssh", "ssh-keygen", "ssh-add") if shutil.which(t) is None]
    out.append(Item("computer", "OpenSSH", "fail" if missing else "ok",
                    ("missing: " + ", ".join(missing)) if missing else "ssh, ssh-keygen, ssh-add",
                    "install OpenSSH (e.g. apt install openssh-client)" if missing else ""))
    try:
        repo = app.repo_factory()
        out.append(Item("computer", "ShipShape checkout", "ok", str(repo.root)))
    except FleetError as exc:
        out.append(Item("computer", "ShipShape checkout", "fail", exc.message,
                        "reinstall from a checkout: fleetledger/install.sh"))
    return out


# --- FleetLedger's own setup ------------------------------------------------------------


def fingerprint(public_key: str, run: Runner = _run) -> str | None:
    """SHA256 fingerprint of a public key file, via ssh-keygen -lf; None if that fails."""
    try:
        result = run(["ssh-keygen", "-lf", public_key])
    except (OSError, subprocess.SubprocessError):
        return None
    parts = (result.stdout or "").split()
    return parts[1] if result.returncode == 0 and len(parts) > 1 else None


def agent_fingerprints(run: Runner = _run) -> list[str] | None:
    """Fingerprints ssh-agent holds; None if there is no agent."""
    if not os.environ.get("SSH_AUTH_SOCK"):
        return None
    try:
        result = run(["ssh-add", "-l"])
    except (OSError, subprocess.SubprocessError):  # no ssh-add, or the agent hangs
        return None
    if result.returncode == 2:  # cannot connect to the agent
        return None
    return [line.split()[1] for line in (result.stdout or "").splitlines()
            if len(line.split()) > 1]


def fleet(app) -> tuple[list[Item], Any]:
    """Inventory, admin key (and whether ssh-agent holds it), agent keys, CA."""
    run = getattr(app, "run_cmd", None) or _run
    store = app.store_factory()
    if not store.exists():
        return [Item("fleet", "inventory", "missing", f"no {store.path}",
                     "ship-fleet setup")], None
    try:
        inv = store.load()
    except FleetError as exc:
        return [Item("fleet", "inventory", "fail", exc.message,
                     f"edit {store.path} as the detail says")], None
    out = [Item("fleet", "inventory", "ok", str(store.path))]

    admin, public = inv.admin_key, inv.admin_public_key_path
    if not admin.exists() or not public.exists():
        gone = admin if not admin.exists() else public
        out.append(Item("fleet", "admin key", "missing", f"no {gone}",
                        f"ssh-keygen -t ed25519 -C fleetledger-admin -f {_tilde(admin)}"
                        if not admin.exists() else
                        f"ssh-keygen -y -f {_tilde(admin)} > {_tilde(public)}"))
    else:
        out.append(Item("fleet", "admin key", "ok", str(admin)))
        held = agent_fingerprints(run)
        fpr = fingerprint(str(public), run)
        if held is None and not os.environ.get("SSH_AUTH_SOCK"):
            out.append(Item("fleet", "ssh-agent", "missing",
                            "no ssh-agent (SSH_AUTH_SOCK is not set)",
                            'eval "$(ssh-agent)" && ssh-add ' + _tilde(admin)))
        elif held is None:
            out.append(Item("fleet", "ssh-agent", "fail",
                            "the ssh-agent named by SSH_AUTH_SOCK does not answer "
                            "(or ssh-add is missing)",
                            "ssh-add -l   (shows what is wrong)"))
        elif fpr is None:
            out.append(Item("fleet", "ssh-agent", "fail",
                            f"cannot read the fingerprint of {public} (ssh-keygen -lf failed)",
                            f"ssh-keygen -lf {_tilde(public)}   (shows what is wrong)"))
        elif fpr in held:
            out.append(Item("fleet", "ssh-agent", "ok", f"holds the admin key ({fpr})"))
        else:
            out.append(Item("fleet", "ssh-agent", "missing", "the admin key is not loaded",
                            f"ssh-add {_tilde(admin)}"))
    absent = [str(p) for p in inv.agent_key_paths if not p.exists()]
    out.append(Item("fleet", "your keys for Ships", "fail" if absent else "ok",
                    ("missing: " + ", ".join(absent)) if absent else
                    ", ".join(_tilde(p) for p in inv.agent_key_paths),
                    "make one: ssh-keygen -t ed25519, then edit fleet.agent_keys in ships.toml"
                    if absent else ""))
    ca = inv.doc.get("ca")
    if not ca:
        out.append(Item("fleet", "certificate authority", "missing",
                        "no [ca]: other machines cannot be trusted with certificates",
                        "ship-fleet setup   (or: ship-fleet ca init)"))
    else:
        keys = [paths.expand(ca[k]) for k in ("host_key", "user_key") if k in ca]
        gone = [str(k) for k in keys if not k.exists()]
        out.append(Item("fleet", "certificate authority", "fail" if gone else "ok",
                        ("missing key files: " + ", ".join(gone)) if gone else "host and user CA",
                        "ship-fleet ca init --force" if gone else ""))
    return out, inv


def _tilde(path) -> str:
    try:
        return "~/" + str(path.resolve().relative_to(Path.home().resolve()))
    except ValueError:
        return str(path)


# --- the hypervisor ---------------------------------------------------------------------


def hypervisor(app, inv, *, offline: bool = False) -> list[Item]:
    driver = inv.defaults.get("driver", "manual")
    if driver == "manual":
        return [Item("hypervisor", "driver", "ok",
                     "manual: FleetLedger prints the steps for your hypervisor",
                     "")]
    config = inv.driver_config("proxmox")
    if not config:
        return [Item("hypervisor", "Proxmox", "missing",
                     "defaults.driver is proxmox but there is no [drivers.proxmox]",
                     "ship-fleet setup --redo hypervisor")]
    out = [Item("hypervisor", "Proxmox settings", "ok",
                f"{config['api_url']} node {config['node']}, pool {config['pool']}")]
    from fleetledger.drivers.proxmox_api import token_secret, token_where

    try:
        token_secret(config)
        out.append(Item("hypervisor", "Proxmox token", "ok", "readable in " + token_where(config)))
    except FleetError as exc:
        out.append(Item("hypervisor", "Proxmox token", "fail", exc.message,
                        exc.hint or "ship-fleet setup --redo hypervisor"))
        return out
    if offline:
        out.append(Item("hypervisor", "Proxmox API", "skip", "not checked (--offline)"))
        return out
    factory = getattr(app, "proxmox_api", None)
    try:
        if factory is not None:
            api = factory(config)
        else:
            from fleetledger.drivers.proxmox_api import ProxmoxAPI

            api = ProxmoxAPI(config)
        version = api.get("/version") or {}
        out.append(Item("hypervisor", "Proxmox API", "ok",
                        f"Proxmox VE {version.get('version', '?')}, TLS pin matches"))
    except FleetError as exc:
        out.append(Item("hypervisor", "Proxmox API", "fail", exc.message,
                        exc.hint or "check api_url and tls_fingerprint in ships.toml"))
        return out
    template = config["template_vmid"]
    try:
        conf = api.get(f"/nodes/{config['node']}/qemu/{template}/config") or {}
        ok = bool(conf.get("template"))
        out.append(Item("hypervisor", "template", "ok" if ok else "fail",
                        f"VM {template} ({conf.get('name', '?')})" + ("" if ok else
                                                                      " is not a template"),
                        "" if ok else "see docs/runbooks/proxmox-setup.md, step 2"))
    except FleetError as exc:
        out.append(Item("hypervisor", "template", "fail", exc.message,
                        "make it: docs/runbooks/proxmox-setup.md, step 2"))
    return out


# --- the overlay and the Ships ------------------------------------------------------------


def overlay_items(app, inv) -> list[Item]:
    config = inv.overlay
    if not config:
        return [Item("overlay", "overlay", "skip", "not set up (optional: access off the LAN)",
                     "ship-fleet overlay setup")]
    out = [Item("overlay", "overlay", "ok",
                f"{config.get('provider', 'tailscale')}, keys "
                f"{'minted (OAuth)' if config.get('key_source') == 'tailscale-api' else 'pasted'}")]
    if config.get("key_source") == "tailscale-api":
        from fleetledger.overlay import secret_store

        try:
            secret_store.load(config["api"])
            out.append(Item("overlay", "OAuth secret", "ok",
                            "readable in " + secret_store.where(config["api"])))
        except FleetError as exc:
            out.append(Item("overlay", "OAuth secret", "fail", exc.message,
                            exc.hint or "ship-fleet overlay setup --force"))
    return out


def ships_items(inv) -> list[Item]:
    ships = inv.ships()
    if not ships:
        return [Item("ships", "Ships", "missing", "none yet", "ship-fleet new ship-01")]
    pending = [s.name for s in ships if s.enrolled is None]
    out = [Item("ships", "Ships", "ok", f"{len(ships)} in the inventory")]
    for name in pending:
        out.append(Item("ships", name, "warn", "not enrolled yet",
                        f"ship-fleet status {name} --wait"))
    return out


def everything(app, *, offline: bool = False) -> list[Item]:
    items = computer(app)
    fleet_items, inv = fleet(app)
    items += fleet_items
    if inv is not None:
        items += hypervisor(app, inv, offline=offline)
        items += overlay_items(app, inv)
        items += ships_items(inv)
    return items
