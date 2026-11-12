"""FleetLedger on its own small VM: `ship-fleet appliance` (docs/runbooks/fleetledger-vm.md).

- seed: a cloud-init seed ISO for a stock Ubuntu cloud image (the one versions.toml
  pins for Ships, checked by its sha256), reusing the Ship seed's ISO and host-key
  code. The VM gets a user `fleetledger` that runs FleetLedger (no sudo) and a user
  `vmadmin` for the system itself (Tailscale, updates; sudo), both for your keys only
  (no passwords, root off), this ShipShape checkout cloned at this very commit and
  installed, the service unit installed but not started (`ship-fleet migrate`
  needs it stopped), a root-owned /usr/local/bin/ship-fleet that runs the fleetledger
  user's pipx install (so a plain `ssh fleetledger@<vm> ship-fleet ...` finds it; it
  refuses anyone else), lingering on, a reboot that lets running jobs finish, and no
  swap. It pins the VM's own host key, made here, so the first ssh to it is
  checked (ship-fleet migrate push uses the pin by itself), and records the VM as
  `[fleet] self`. Then it prints the Proxmox `qm` steps: the VM goes in **no pool**,
  so the API token FleetLedger holds cannot even see it.
- show: what `[fleet] self` says, and the tailnet rules for its identity.
- forget: drop `[fleet] self` (the VM is gone).

`[fleet] self` is a guard: no Ship may have its name, VM id or host key (the
inventory refuses it), the Proxmox driver refuses every call on its VM id, and the
manual driver prints no step for it, so FleetLedger can never destroy, resize,
reboot, snapshot or update itself. doctor checks the token cannot see it.

The VM's tailnet identity is the tag `tag:fleetledger`, with two rules beside the
Ships' (`ship-fleet overlay policy` prints them once `[fleet] self` is set): it may
reach the Ships on port 22, and your own devices may reach it on 22 (ssh, unlock)
and 443 (the web UI through Tailscale Serve); a test proves Ships cannot reach it.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from fleetledger import paths
from fleetledger.errors import ConfigError, FleetError
from fleetledger.repo import git as git_in
from fleetledger.service import DROP_IN, REBOOT_DROP_IN

USER = "fleetledger"
TAG = "tag:fleetledger"
PORT = 8765
RUNBOOK = "docs/runbooks/fleetledger-vm.md"
ALIAS = "fleetledger-self"  # the pin's name in FleetLedger's own known_hosts
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._/~-]+$")
SIZE = {"cores": 2, "memory_mb": 4096, "disk_gb": 40}
# Not "admin": that name is taken on Ubuntu (cloud-init cannot create it).
ADMIN = "vmadmin"
# /usr/local/bin/ship-fleet on the VM: ssh runs a command without a login shell, so
# without it pipx's ~/.local/bin is not on PATH.
WRAPPER_FILE = Path(__file__).parent / "seed" / "appliance-ship-fleet.sh"
WRAPPER = WRAPPER_FILE.read_text()
SSHD = ("# FleetLedger's VM: keys only, its two users only.\nPasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\nPermitRootLogin no\nAllowUsers fleetledger vmadmin\n")


# --- [fleet] self: the guard ------------------------------------------------------------------


def fleet_self(doc: Any) -> dict[str, Any] | None:
    """[fleet] self from an inventory (or its document), or None."""
    doc = getattr(doc, "doc", doc)
    entry = ((doc or {}).get("fleet") or {}).get("self")
    return dict(entry) if entry else None


def self_vmid(inv: Any) -> int | None:
    entry = fleet_self(inv)
    return int(entry["vmid"]) if entry and entry.get("vmid") is not None else None


def _blob(key: str) -> str:
    return " ".join(str(key).split()[:2])


def clashes(doc: Any) -> list[str]:
    """Ships that would be FleetLedger's own VM (store.semantic_errors)."""
    entry = fleet_self(doc)
    if not entry:
        return []
    out = []
    for name, ship in ((doc or {}).get("ship") or {}).items():
        why = []
        if name == entry.get("name"):
            why.append("its name")
        vmid = (ship.get("driver_ref") or {}).get("vmid")
        if entry.get("vmid") is not None and vmid is not None and int(vmid) == int(entry["vmid"]):
            why.append(f"its VM id {vmid}")
        if entry.get("host_key") and ship.get("host_key") and \
                _blob(ship["host_key"]) == _blob(entry["host_key"]):
            why.append("its host key")
        if why:
            out.append(f"Ship '{name}' has {' and '.join(why)} of FleetLedger's own VM "
                       "([fleet] self): FleetLedger never manages itself")
    return out


def refuse_as_ship(inv: Any, *, name: str | None = None, vmid: Any = None,
                   host_key: str | None = None) -> None:
    """A Ship about to be made or adopted that would be FleetLedger's own VM: refused
    plainly, before anything is written or recorded (the inventory would refuse it too,
    but as a broken file)."""
    entry = fleet_self(inv)
    if not entry:
        return
    what = []
    if name is not None and name == entry.get("name"):
        what.append(f"the name {name}")
    if vmid is not None and entry.get("vmid") is not None and str(vmid) == str(entry["vmid"]):
        what.append(f"VM id {vmid}")
    if host_key and entry.get("host_key") and _blob(host_key) == _blob(entry["host_key"]):
        what.append("that host key")
    if what:
        raise FleetError(f"{' and '.join(what).capitalize()} belong{'s' if len(what) == 1 else ''}"
                         " to FleetLedger's own VM ([fleet] self): it is never a Ship.",
                         hint="Choose another name or VM id; ship-fleet appliance show says "
                              "what the VM uses.")


def refuse_self(vmid: Any, protected: Any) -> None:
    """A driver call on FleetLedger's own VM id: refused, whatever else says so."""
    if protected is not None and vmid is not None and str(vmid) == str(protected):
        raise FleetError(f"VM {vmid} is FleetLedger's own VM ([fleet] self): FleetLedger never "
                         "destroys, resizes, stops, snapshots or updates itself.",
                         hint=f"Manage that VM on Proxmox yourself ({RUNBOOK}).")


# --- the seed --------------------------------------------------------------------------------


def checkout_source(app: Any, url: str | None) -> tuple[str, str]:
    """(https URL to clone from, commit) of this checkout: the VM runs the same commit
    (ship-fleet migrate refuses another). Refuses uncommitted changes and a commit no
    remote branch has (the VM could not fetch it)."""
    repo = app.repo_factory()
    root = str(repo.root)

    def git(*args: str) -> str:
        proc = git_in(root, *args, timeout=60)
        if proc.returncode != 0:
            raise ConfigError(f"git {' '.join(args)} failed in the checkout.")
        return proc.stdout.strip()

    commit = git("rev-parse", "HEAD")
    if git("status", "--porcelain", "--", "ship", "versions.toml", "contracts", "fleetledger"):
        raise FleetError("The checkout has uncommitted changes: the VM clones this commit, so "
                         "commit and push them first.")
    if not git("branch", "-r", "--contains", commit):
        raise FleetError(f"No remote branch has commit {commit[:12]}, so the VM cannot fetch "
                         "it.", hint="Push it first (git push).")
    url = url or git("remote", "get-url", "origin")
    if not URL_RE.match(url):
        raise FleetError("The VM clones ShipShape over https with no credentials; give that "
                         "address: --repo https://example.com/you/ShipShape.git")
    return url, commit


def user_data(name: str, host_key: Any, login_keys: list[str], repo_url: str, commit: str,
              port: int) -> str:
    """The appliance's cloud-config. Holds one secret only: the VM's own host key."""
    from fleetledger.seed.cloudinit import dump_yaml

    home = "/home/" + USER
    as_user = ("runuser -u fleetledger -- env HOME=/home/fleetledger "
               "XDG_RUNTIME_DIR=/run/user/$(id -u fleetledger) ")
    install = (f"git clone --no-checkout {shlex.quote(repo_url)} {home}/ShipShape && "
               f"git -C {home}/ShipShape checkout --detach {commit} && "
               f"{home}/ShipShape/fleetledger/install.sh")
    doc = {
        "hostname": name,
        "preserve_hostname": False,
        "ssh_pwauth": False,
        "disable_root": True,
        "ssh_deletekeys": True,
        "ssh_genkeytypes": ["ed25519"],  # no unpinned rsa or ecdsa keys beside the pinned one
        "ssh_keys": {"ed25519_private": host_key.private, "ed25519_public": host_key.public},
        "users": [
            {"name": USER, "gecos": "FleetLedger", "shell": "/bin/bash",
             "lock_passwd": True, "ssh_authorized_keys": list(login_keys)},
            {"name": ADMIN, "gecos": "FleetLedger VM admin", "shell": "/bin/bash",
             "lock_passwd": True, "sudo": "ALL=(ALL:ALL) NOPASSWD: ALL",
             "ssh_authorized_keys": list(login_keys)},
        ],
        "package_update": True,
        # libnss-mdns: Ships' .local names; qemu-utils and libguestfs-tools: image builds.
        "packages": ["git", "pipx", "openssh-client", "libnss-mdns", "qemu-utils",
                     "libguestfs-tools"],
        "swap": {"size": 0},
        "write_files": [
            # Named to sort after Ubuntu's user@.service.d/timeout.conf (service.DROP_IN).
            {"path": "/etc/systemd/system/user@.service.d/" + DROP_IN,
             "owner": "root:root", "permissions": "0644", "content": REBOOT_DROP_IN},
            {"path": "/etc/ssh/sshd_config.d/10-fleetledger.conf", "owner": "root:root",
             "permissions": "0644", "content": SSHD},
            {"path": "/usr/local/bin/ship-fleet", "owner": "root:root",
             "permissions": "0755", "content": WRAPPER},
        ],
        "runcmd": [
            ["bash", "-c", "swapoff -a; systemctl daemon-reload; systemctl reload ssh || true"],
            ["loginctl", "enable-linger", USER],
            ["bash", "-c", "for i in $(seq 60); do [ -S /run/user/$(id -u fleetledger)/bus ] "
                           "&& break; sleep 2; done"],
            ["bash", "-c", as_user + "bash -lc " + shlex.quote(install)],
            ["bash", "-c", as_user + "bash -lc " + shlex.quote(
                f"~/.local/bin/ship-fleet service install --port {port} --no-start")],
        ],
    }
    return "#cloud-config\n" + dump_yaml(doc)


def login_keys(inv: Any, given: list[str]) -> list[str]:
    """The public keys that may log in to the VM: those given, else your keys for Ships."""
    from fleetledger.seed.cloudinit import read_public_keys

    files = [paths.expand(k) for k in given] or (
        list(inv.agent_key_paths) if inv is not None else [])
    if not files:
        raise FleetError("Say which public key may log in to the VM: --key ~/.ssh/id_ed25519.pub")
    keys: list[str] = []
    for f in files:
        keys += read_public_keys(Path(f), "a key for the VM")
    return list(dict.fromkeys(keys))


def seed_path(name: str) -> Path:
    return paths.data_dir() / "seeds" / f"appliance-{name}.iso"


def qm_steps(app: Any, inv: Any, name: str, vmid: int, iso: Path) -> list[str]:
    """The Proxmox steps, as root on the host. No --pool: the token never sees the VM."""
    px = (inv.driver_config("proxmox") if inv is not None else None) or {}
    ubuntu = app.repo_factory().versions["ubuntu"]
    image = ubuntu["images"]["amd64"]
    seed_storage = px.get("seed_storage", "<seed-storage>")
    disk_storage = px.get("disk_storage", "<disk-storage>")
    bridge = px.get("bridge", "<bridge>")
    remote = f"/var/tmp/{image['file']}"
    q = shlex.quote
    return [
        f"# 1. Upload {iso} to the {seed_storage} storage as an ISO image named {iso.name}.",
        "# 2. On the Proxmox host, as root:",
        f"wget -O {q(remote)} {q(ubuntu['base_url'].rstrip('/') + '/' + image['file'])}",
        f"echo {q(image['sha256'] + '  ' + remote)} | sha256sum -c",
        "# Nested virtualisation (fast image builds on the VM): 'Y' or '1' here means yes.",
        "cat /sys/module/kvm_intel/parameters/nested 2>/dev/null || "
        "cat /sys/module/kvm_amd/parameters/nested",
        f"qm create {vmid} --name {q(name)} --memory {SIZE['memory_mb']} "
        f"--cores {SIZE['cores']} --cpu host --ostype l26 "
        f"--net0 {q('virtio,bridge=' + bridge)} --scsihw virtio-scsi-pci "
        f"--scsi0 {q(disk_storage + ':0,import-from=' + remote)} "
        f"--ide2 {q(seed_storage + ':iso/' + iso.name + ',media=cdrom')} "
        "--boot order=scsi0 --serial0 socket --vga serial0 --onboot 1",
        f"qm resize {vmid} scsi0 {SIZE['disk_gb']}G",
        f"qm start {vmid}",
        f"rm {q(remote)}",
        "# No --pool, on purpose: FleetLedger's API token sees only its pool, so it cannot "
        "touch its own VM.",
        "# Nested virtualisation off: use --cpu x86-64-v2-AES instead, and build images on "
        "your desktop (ship-fleet image build there).",
    ]


def record_self(store: Any, entry: dict[str, Any]) -> None:
    import tomlkit

    with store.edit() as ed:
        table = tomlkit.inline_table()
        for key, value in entry.items():
            table[key] = value
        ed.doc["fleet"]["self"] = table


def pinned_line(entry: dict[str, Any]) -> str:
    """The VM's host key, as a line for ~/.ssh/known_hosts."""
    hosts = ",".join(dict.fromkeys([entry["name"], *entry.get("names", [])]))
    return f"{hosts} {_blob(entry['host_key'])}"


def cmd_seed(args: Any, app: Any) -> int:
    from fleetledger.seed import cloudinit, hostkey, iso

    name = args.name
    if not NAME_RE.match(name):
        raise FleetError("The VM's name: lower-case letters, digits and -, up to 31.")
    names = list(dict.fromkeys(args.host or [f"{name}.local"]))
    if not all(HOST_RE.match(h) for h in names):
        raise FleetError("--host: a name to reach the VM by, as a Ship's names are.")
    store = app.store_factory()
    inv = store.load() if store.exists() else None
    if inv is not None:
        if fleet_self(inv) and not args.force:
            raise FleetError(f"FleetLedger's own VM is recorded already ({fleet_self(inv)['name']}"
                             ").", hint="ship-fleet appliance show; --force makes a new one.")
        if name in {s.name for s in inv.ships()}:
            raise FleetError(f"{name} is a Ship's name.")
        if any(str(s.driver_ref.get("vmid")) == str(args.vmid) for s in inv.ships()):
            raise FleetError(f"VM id {args.vmid} is a Ship's.")
        first, last = ((inv.driver_config("proxmox") or {}).get("vmid_range")
                       or [None, None])
        if first is not None and first <= args.vmid <= last:
            raise FleetError(f"VM id {args.vmid} is in the Ships' range ({first}-{last}); give "
                             "FleetLedger's own VM one outside it.")
    keys = login_keys(inv, args.key)
    url, commit = checkout_source(app, args.repo)
    key = hostkey.generate(f"{name} host key")
    path = seed_path(name)
    paths.private_dir(path.parent)
    files = {"user-data": user_data(name, key, keys, url, commit, args.port),
             "meta-data": cloudinit.meta_data(name, cloudinit.new_instance_id(name)),
             "network-config": cloudinit.network_config()}
    iso.write(files, path)
    entry = {"name": name, "vmid": args.vmid, "names": names,
             "host_key": " ".join(key.public.split()[:2])}
    if inv is not None:
        record_self(store, entry)
    from fleetledger.concierge import audit

    audit.record("-", "appliance-seed", "appliance", "ok")
    app.say(f"Seed for FleetLedger's own VM: {paths.tilde(path)} (0600). It holds the VM's "
            "private host key: delete it here and from Proxmox's storage once the VM has "
            "booted.")
    app.say(f"The VM's host key: {key.fingerprint}" + (
        "; recorded as [fleet] self, so ship-fleet migrate push checks it by itself." if inv
        else "; there is no fleet here to record it in, so check it by hand at first ssh."))
    app.say(f"It clones ShipShape at {commit[:12]} (this checkout's commit) from {url}.")
    app.say("")
    for step in qm_steps(app, inv, name, args.vmid, path):
        app.say(step)
    app.say("")
    app.say(f"For ssh to it from your computers, add this line to ~/.ssh/known_hosts:\n  "
            f"{pinned_line(entry)}")
    app.say(f"Then follow {RUNBOOK}: Tailscale, the vault, ship-fleet migrate push, unlock.")
    return 0


def cmd_show(args: Any, app: Any) -> int:
    store = app.store_factory()
    entry = fleet_self(store.load()) if store.exists() else None
    if entry is None:
        app.say("No FleetLedger VM is recorded ([fleet] self). Make one: ship-fleet appliance "
                "seed --name fleetledger --vmid N")
        return 0
    from fleetledger.seed.hostkey import fingerprint

    app.say(f"FleetLedger's own VM: {entry['name']}"
            + (f", VM id {entry['vmid']}" if entry.get("vmid") is not None else "")
            + f", reached as {', '.join(entry.get('names') or [entry['name']])}; host key "
            f"{fingerprint(entry['host_key'])}.")
    app.say("FleetLedger never acts on it: no Ship may be it, and no driver call touches it.")
    app.say(f"known_hosts line: {pinned_line(entry)}")
    return 0


HOST_KEY_PUB = Path("/etc/ssh/ssh_host_ed25519_key.pub")  # this computer's own (public)


def cmd_here(args: Any, app: Any) -> int:
    """Record the computer this runs on as FleetLedger's own VM: after a backup was
    restored onto a new VM, whose inventory still names the old one."""
    import socket

    store = app.store_factory()
    if not store.exists():
        raise FleetError("There is no fleet here to record it in.")
    inv = store.load()
    name = socket.gethostname().split(".")[0].lower()
    if not NAME_RE.match(name):
        raise FleetError(f"This computer's hostname ({name}) is not one a VM name can be.")
    try:
        key = " ".join(HOST_KEY_PUB.read_text(encoding="ascii").split()[:2])
    except (OSError, UnicodeDecodeError) as exc:
        raise FleetError(f"Cannot read this computer's host key ({HOST_KEY_PUB}).") from exc
    if not key.startswith("ssh-ed25519 "):
        raise FleetError(f"{HOST_KEY_PUB} is not an ed25519 host key.")
    names = list(dict.fromkeys(args.host or [f"{name}.local"]))
    if not all(HOST_RE.match(h) for h in names):
        raise FleetError("--host: a name to reach the VM by, as a Ship's names are.")
    for ship in inv.ships():
        if ship.name == name or str(ship.driver_ref.get("vmid")) == str(args.vmid) or \
                _blob(ship.host_key) == key:
            raise FleetError(f"Ship {ship.name} has this computer's name, VM id or host key; "
                             "this computer cannot be FleetLedger's own VM and that Ship too.")
    first, last = (inv.driver_config("proxmox") or {}).get("vmid_range") or [None, None]
    if first is not None and first <= args.vmid <= last:
        raise FleetError(f"VM id {args.vmid} is in the Ships' range ({first}-{last}).")
    old = fleet_self(inv)
    entry = {"name": name, "vmid": args.vmid, "names": names, "host_key": key}
    record_self(store, entry)
    from fleetledger.concierge import audit

    audit.record("-", "appliance-here", "appliance", "ok")
    app.say(f"Recorded this computer ({name}, VM id {args.vmid}) as FleetLedger's own VM"
            + (f", in place of {old['name']} (VM id {old.get('vmid')})" if old else "")
            + ": FleetLedger never acts on it.")
    app.say(f"For ssh to it from your computers, the known_hosts line:\n  {pinned_line(entry)}")
    return 0


def cmd_forget(args: Any, app: Any) -> int:
    store = app.store_factory()
    if not store.exists() or fleet_self(store.load()) is None:
        app.say("No FleetLedger VM is recorded.")
        return 0
    with store.edit() as ed:
        del ed.doc["fleet"]["self"]
    app.say("Forgot [fleet] self. The VM itself is untouched (FleetLedger never touches it).")
    return 0


def add_parsers(sub, add) -> None:
    p = add(sub, "appliance", "FleetLedger on its own small VM: the seed and its guard",
            "FleetLedger on its own small, always-on VM: a cloud-init seed for a stock Ubuntu\n"
            "cloud image, the VM recorded as [fleet] self (FleetLedger never acts on it),\n"
            f"and the Proxmox steps. See {RUNBOOK}.\n\n"
            "  ship-fleet appliance seed --name fleetledger --vmid 120\n"
            "  ship-fleet appliance here --vmid 121     on a new VM, after a restore\n"
            "  ship-fleet appliance show\n"
            "  ship-fleet appliance forget")
    verbs = p.add_subparsers(dest="appliance_command", metavar="<action>", title="actions")
    s = verbs.add_parser("seed", help="write the VM's seed ISO, pin its host key, print the steps")
    s.add_argument("--name", default="fleetledger", help="its hostname (default fleetledger)")
    s.add_argument("--vmid", type=int, required=True,
                   help="its Proxmox VM id: one outside the Ships' range")
    s.add_argument("--host", action="append", default=[],
                   help="a name to reach it by (repeatable; default NAME.local)")
    s.add_argument("--key", action="append", default=[], metavar="PUB",
                   help="a public key that may log in to it (repeatable; default your keys for "
                        "Ships)")
    s.add_argument("--repo", metavar="URL",
                   help="https address to clone ShipShape from (default: this checkout's origin)")
    s.add_argument("--port", type=int, default=PORT, help=f"the web UI's port (default {PORT})")
    s.add_argument("--force", action="store_true", help="replace the recorded VM")
    h = verbs.add_parser("here", help="record this computer as FleetLedger's own VM (after "
                                      "a backup is restored onto a new one)")
    h.add_argument("--vmid", type=int, required=True, help="its Proxmox VM id")
    h.add_argument("--host", action="append", default=[],
                   help="a name to reach it by (repeatable; default NAME.local)")
    verbs.add_parser("show", help="what [fleet] self says")
    verbs.add_parser("forget", help="drop [fleet] self (the VM is gone)")


def cmd_appliance(args: Any, app: Any) -> int:
    verb = getattr(args, "appliance_command", None) or "show"
    return {"seed": cmd_seed, "show": cmd_show, "forget": cmd_forget,
            "here": cmd_here}[verb](args, app)


def policy_with_self(text: str) -> str:
    """The Ships' access rules (overlay policy) plus FleetLedger's own VM's: its tag,
    owned by admins; it may reach the Ships on 22; your devices may reach it on 22 and
    443; and tests that a Ship cannot reach it, and it can reach a Ship."""
    import json

    doc = json.loads(text)
    ship_tags = sorted(doc.get("tagOwners") or {})
    doc.setdefault("tagOwners", {})[TAG] = ["autogroup:admin"]
    doc.setdefault("acls", []).extend([
        {"action": "accept", "src": [TAG], "dst": [f"{tag}:22" for tag in ship_tags]},
        {"action": "accept", "src": ["autogroup:member"], "dst": [f"{TAG}:22", f"{TAG}:443"]},
    ])
    tests = doc.setdefault("tests", [])
    if ship_tags:
        tests.append({"src": ship_tags[0], "deny": [f"{TAG}:22", f"{TAG}:443"]})
        tests.append({"src": TAG, "accept": [f"{ship_tags[0]}:22"]})
    return json.dumps(doc, indent=2) + "\n"


def known_hosts_lines(inv: Any) -> list[str]:
    """FleetLedger's own known_hosts gets the VM's pin under ALIAS (ssh.py)."""
    entry = fleet_self(inv)
    return [f"{ALIAS} {_blob(entry['host_key'])}"] if entry and entry.get("host_key") else []


def push_options(inv: Any, host: str) -> list[str]:
    """ssh options for migrate push to FleetLedger's own VM: check its pinned host key."""
    entry = fleet_self(inv)
    target = host.rsplit("@", 1)[-1]
    if not entry or target not in {entry["name"], *entry.get("names", [])}:
        return []
    from fleetledger.ssh import SSH

    known = SSH(inv).sync_known_hosts()
    return [f"UserKnownHostsFile={known}", "GlobalKnownHostsFile=/dev/null",
            f"HostKeyAlias={ALIAS}"]

