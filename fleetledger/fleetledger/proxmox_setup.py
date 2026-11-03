"""Set up the Proxmox driver from three answers: the address, a token id and its secret.

Everything else (node, pool, storages, bridge, template) is read through the API
with that token and offered for you to confirm. What the token cannot see or is
not allowed to do is never guessed: setup prints the exact commands to run as
root on the Proxmox host, and you run setup again afterwards.

The certificate's fingerprint is shown before it is pinned, and the token secret
is checked against the API (through that pin) before it is stored: in the OS
keyring by default, or a 0600 file. It never appears in argv, output or
ships.toml.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fleetledger import paths
from fleetledger.drivers import proxmox_api
from fleetledger.errors import FleetError
from fleetledger.overlay import secret_store
from fleetledger.overlay_setup import _ask, _choose, _plausible_secret, _secret, _tilde

SECRET_FILE = "proxmox-token"
TOKEN_ID_RE = re.compile(r"^[^@!\s]+@[^!\s]+![A-Za-z][A-Za-z0-9_.-]*$")  # as the schema
HOST_RE = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

# What the token needs, per path (docs/runbooks/proxmox-setup.md, step 1).
ROLES = {
    "ShipShapeVM": ("VM.Allocate", "VM.Clone", "VM.Audit", "VM.PowerMgmt", "VM.Config.Disk",
                    "VM.Config.CDROM", "VM.Config.CPU", "VM.Config.Memory",
                    "VM.Config.Network", "VM.Config.Options", "VM.GuestAgent.Audit",
                    "Pool.Audit"),
    "ShipShapeSeeds": ("Datastore.Audit", "Datastore.AllocateTemplate", "Datastore.Allocate"),
    "ShipShapeDisks": ("Datastore.Audit", "Datastore.AllocateSpace"),
    "PVESDNUser": ("SDN.Use",),
}
SNAPSHOT_PRIVS = ("VM.Snapshot", "VM.Snapshot.Rollback")


def parse_address(text: str) -> tuple[str, int, str]:
    """host, port and api_url from "host", "host:port" or a URL."""
    value = text.strip()
    value = re.sub(r"^https?://", "", value, flags=re.I).split("/", 1)[0]
    host, port = value, 8006
    match = re.match(r"^(\[[^\]]+\]|[^:]+)(?::(\d+))?$", value)
    if match:
        host = match.group(1)
        if match.group(2):
            port = int(match.group(2))
    if not value or not match or not HOST_RE.match(host) or not 0 < port < 65536:
        raise FleetError(f"'{text.strip()}' is not a host name, address or host:port.")
    return host, port, f"https://{host}:{port}/api2/json"


@dataclass
class Names:
    """The names the root commands use; placeholders until known."""

    token_id: str = "shipshape@pve!build"
    pool: str = "shipshape"
    seeds: str = "shipshape-seeds"
    disk: str = "$DISK_STORAGE"
    bridge: str = "$BRIDGE"

    @property
    def user(self) -> str:
        return self.token_id.split("!", 1)[0]

    @property
    def token_name(self) -> str:
        return self.token_id.split("!", 1)[1]


def _q(value: str) -> str:
    """Quote for the root shell; leave $VARIABLES for the user to set."""
    return value if value.startswith("$") else shlex.quote(value)


def _role_line(role: str) -> list[str]:
    if role.startswith("PVE"):
        return []  # built in
    privs = " ".join(ROLES[role])
    return [f'pveum role add {role} --privs "{privs}" 2>/dev/null || '
            f'pveum role modify {role} --privs "{privs}"']


def _grant(path: str, role: str, n: Names) -> list[str]:
    target = path if path.startswith("/") and "$" not in path else f'"{path}"'
    return [*_role_line(role),
            f"pveum acl modify {target} --users {_q(n.user)} --roles {role}",
            f"pveum acl modify {target} --tokens '{n.token_id}' --roles {role}"]


def _variables(n: Names) -> list[str]:
    out = []
    if n.disk.startswith("$"):
        out.append("DISK_STORAGE=CHANGE-ME     # where VM disks go: see pvesm status")
    if n.bridge.startswith("$"):
        out.append("BRIDGE=CHANGE-ME           # the bridge your LAN is on, often vmbr0")
    return out


def user_and_token_commands(n: Names) -> list[str]:
    """Everything from step 1 of the runbook, with the names known so far."""
    return [
        *_variables(n),
        f'pveum pool add {_q(n.pool)} --comment "ShipShape Ships"   # if it does not exist yet',
        *seed_storage_commands(n, grants=False),
        f"pveum user add {_q(n.user)} --comment \"ShipShape, API only\"",
        f"pveum user token add {_q(n.user)} {_q(n.token_name)} --privsep 1 "
        "--expire $(date -d '+90 days' +%s)   # prints the secret once",
        *grant_commands(n),
        f"pveum user token permissions {_q(n.user)} {_q(n.token_name)}   # review",
    ]


def seed_storage_commands(n: Names, *, grants: bool = True) -> list[str]:
    folder = f"/var/lib/{n.seeds}"
    out = [f"mkdir -p {_q(folder)} && chmod 700 {_q(folder)}",
           f"pvesm add dir {_q(n.seeds)} --path {_q(folder)} --content iso"]
    if grants:
        out += _grant(f"/storage/{n.seeds}", "ShipShapeSeeds", n)
    return out


def grant_commands(n: Names) -> list[str]:
    seen: set[str] = set()
    out = []
    for path, role in ((f"/pool/{n.pool}", "ShipShapeVM"),
                       (f"/storage/{n.seeds}", "ShipShapeSeeds"),
                       (f"/storage/{n.disk}", "ShipShapeDisks"),
                       (f"/sdn/zones/localnetwork/{n.bridge}", "PVESDNUser")):
        for line in _grant(path, role, n):
            if line not in seen:
                seen.add(line)
                out.append(line)
    return out


def template_commands(n: Names, versions: Mapping[str, Any], vmid: int = 9000) -> list[str]:
    """Step 2 of the runbook, with the Ubuntu image pinned in versions.toml."""
    ubuntu = versions["ubuntu"]
    image = ubuntu["images"]["amd64"]
    return [
        *_variables(n),
        "cd /var/lib/vz/template/iso",
        f"U={ubuntu['base_url']}",
        f"KEY={ubuntu['signing_key']}",
        f"IMG={image['file']}",
        "wget $U/$IMG $U/SHA256SUMS $U/SHA256SUMS.gpg",
        'curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x$KEY" '
        "| gpg --dearmor > ubuntu-cloud.gpg",
        'gpg --show-keys --with-colons ubuntu-cloud.gpg | grep -q "^fpr:::::::::$KEY:" '
        "&& echo key-ok",
        "gpgv --keyring ./ubuntu-cloud.gpg SHA256SUMS.gpg SHA256SUMS",
        "sha256sum -c --ignore-missing SHA256SUMS",
        f'echo "{image["sha256"]}  $IMG" | sha256sum -c',
        f"qm create {vmid} --name shipshape-base --pool {_q(n.pool)} --memory 8192 "
        f'--cores 4 --cpu host --net0 "virtio,bridge={n.bridge}" --scsihw virtio-scsi-pci '
        f'--scsi0 "{n.disk}:0,import-from=$PWD/$IMG" --boot order=scsi0 --serial0 socket '
        "--vga serial0 --agent enabled=1",
        f"qm template {vmid}",
    ]


# --- what the token can do ----------------------------------------------------------------


def effective(perms: Mapping[str, Mapping[str, Any]], path: str) -> set[str]:
    """Privileges at path, from GET /access/permissions ({path: {privilege:
    propagate}}): every privilege listed at the path itself, and those of its
    parents that propagate."""
    out = set((perms.get(path) or {}).keys())
    parts = path.strip("/").split("/")
    for i in range(len(parts)):
        prefix = "/" + "/".join(parts[:i])
        out.update(priv for priv, propagate in (perms.get(prefix) or {}).items() if propagate)
    return out


def missing_grants(perms, n: Names) -> list[tuple[str, str, list[str]]]:
    """(path, role, privileges lacking) for each path the token needs."""
    out = []
    for path, role in ((f"/pool/{n.pool}", "ShipShapeVM"),
                       (f"/storage/{n.seeds}", "ShipShapeSeeds"),
                       (f"/storage/{n.disk}", "ShipShapeDisks"),
                       (f"/sdn/zones/localnetwork/{n.bridge}", "PVESDNUser")):
        lacking = [p for p in ROLES[role] if p not in effective(perms, path)]
        if lacking:
            out.append((path, role, lacking))
    return out


def bridges_from(perms) -> list[str]:
    found = []
    for path, privs in (perms or {}).items():
        match = re.match(r"^/sdn/zones/localnetwork/([^/]+)$", path)
        if match and "SDN.Use" in (privs or {}):  # the value is "propagate", not "granted"
            found.append(match.group(1))
    return sorted(found)


# --- the questions ------------------------------------------------------------------------


def _pick(app, what: str, options: list[str], preferred: str | None = None) -> str:
    """One of options; Enter takes the preferred (or first) one."""
    default = preferred if preferred in options else options[0]
    if len(options) == 1:
        app.say(f"{what}: {default}")
        return default
    app.say(f"{what}: " + ", ".join(options))
    while True:
        answer = _ask(app, f"{what} ({default}): ", default)
        if answer in options:
            return answer
        app.note(f"Please answer one of: {', '.join(options)}.")


def _print_commands(app, title: str, lines: list[str]) -> None:
    app.say(f"\n{title}\n")
    for line in lines:
        app.say(f"  {line}")
    app.say("")


def _content(entry: Mapping[str, Any]) -> set[str]:
    return set(str(entry.get("content", "")).split(","))


@dataclass
class Result:
    saved: bool
    message: str = ""


def run(app, store, inv, *, versions: Mapping[str, Any]) -> Result:
    """The whole Proxmox part of setup. Saves [drivers.proxmox] only when every
    required piece is there; otherwise prints the root commands that add it."""
    old = inv.driver_config("proxmox") or {}
    app.say("FleetLedger needs three things from you: the Proxmox address, an API token id\n"
            "and its secret. It finds the rest through the API and asks you to confirm.\n")

    host, port, api_url = parse_address(
        _ask(app, "Proxmox address (host or host:port)" +
             (f" ({old['api_url']})" if old.get("api_url") else "") + ": ",
             old.get("api_url", "")))

    fingerprint = _pin(app, host, port, api_url, old)

    app.say("\nThe API token. If you have none yet, press Enter: setup prints the commands\n"
            "that make one (run them as root on the Proxmox host), then stops.")
    token_id = _ask(app, "Token id, e.g. shipshape@pve!build" +
                    (f" ({old['token_id']})" if old.get("token_id") else "") + ": ",
                    old.get("token_id", ""))
    if not token_id:
        _print_commands(app, "On the Proxmox host, as root (step 1 of "
                        "docs/runbooks/proxmox-setup.md):", user_and_token_commands(Names()))
        app.say("Copy the token secret it prints (shown once), then run ship-fleet setup "
                "again.")
        return Result(False, "no token yet")
    if not TOKEN_ID_RE.match(token_id):
        raise FleetError(f"'{token_id}' is not a token id (user@realm!name).")

    secret, reused, store_kind = _token_secret(app, token_id)
    config: dict[str, Any] = {"api_url": api_url, "token_id": token_id,
                              "tls_fingerprint": fingerprint}
    api = _api(app, config, lambda: secret)
    try:
        version = api.get("/version") or {}
    except proxmox_api.ProxmoxError as exc:
        if exc.status == 401:
            raise FleetError("Proxmox refused the token (401): check the token id and secret. "
                             "Nothing was saved.") from None
        raise
    app.say(f"\nThe token works: Proxmox VE {version.get('version', '?')}, "
            "through the pinned certificate.")
    item = secret_store.proxmox_item(token_id)
    secret_path = paths.config_dir() / SECRET_FILE
    if store_kind == "keyring":
        if not reused:
            secret_store.store_in_keyring(item, secret)
        config.update(token_store="keyring", keyring_item=item)
        app.say(f"The secret is in your OS keyring ({secret_store.SERVICE} / {item}).")
    else:
        if not reused:
            paths.private_dir(secret_path.parent)
            paths.write_private(secret_path, (secret + "\n").encode("utf-8"))
        config["token_file"] = _tilde(secret_path)
        app.say(f"The secret is in {secret_path} (0600).")

    return _discover(app, store, api, config, old, versions)


def _pin(app, host: str, port: int, api_url: str, old: Mapping[str, Any]) -> str:
    fetch = getattr(app, "cert_fingerprint", None) or proxmox_api.server_fingerprint
    fingerprint = fetch(host, port)
    if old.get("api_url") == api_url and old.get("tls_fingerprint") == fingerprint:
        app.say(f"Certificate: {fingerprint}\n(the one already pinned)")
        return fingerprint
    app.say(f"\nThe server's certificate (SHA-256):\n  {fingerprint}\n"
            "Check it on the Proxmox host before trusting it (as root):\n"
            "  openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256\n"
            "(pveproxy-ssl.pem instead, if you installed your own certificate), or in the\n"
            "web interface: the node > System > Certificates.")
    if _ask(app, "Type yes if it matches, to pin it: ").lower() != "yes":
        raise FleetError("Not pinned, so nothing was sent to it and nothing was saved.")
    return fingerprint


def _token_secret(app, token_id: str) -> tuple[str, bool, str]:
    """(secret, reused, store): a secret already stored for this token is offered
    first (it is checked with the API before anything is saved, never shown)."""
    item = secret_store.proxmox_item(token_id)
    existing = secret_store.existing_in_keyring(item)
    if existing is not None and _plausible_secret(existing) and _choose(
            app, f"A secret for this token is already in your keyring ({secret_store.SERVICE}"
            f" / {item}). Use it (checked with Proxmox first, never shown)",
            ("yes", "no"), "yes") == "yes":
        return existing, True, "keyring"
    path = paths.config_dir() / SECRET_FILE
    if path.exists():
        try:
            on_disk = proxmox_api.read_token(str(path))
        except FleetError:
            on_disk = None
        if on_disk and _choose(app, f"A token secret is already in {_tilde(path)}. Use it "
                               "(checked with Proxmox first, never shown)",
                               ("yes", "no"), "yes") == "yes":
            return on_disk, True, "file"
    secret = _secret(app, "Token secret (not shown): ")
    if not _plausible_secret(secret) or any(c.isspace() for c in secret):
        raise FleetError("That does not look like a Proxmox token secret.")
    return secret, False, _choose_store(app)


def _choose_store(app) -> str:
    usable, why = secret_store.keyring_usable()
    app.say("\nWhere to keep the token secret:\n"
            "  keyring  your OS keyring (GNOME Keyring or KWallet; Keychain on macOS):\n"
            "           encrypted at rest, unlocked with your login. While it is unlocked,\n"
            "           any program running as you can read it, agents on this computer\n"
            "           included.\n"
            "  file     a file only you can read (0600). Readable by any program running\n"
            "           as you, and by anyone who gets a copy of your disk.\n")
    if not usable:
        app.say(f"The keyring cannot be used here ({why}); using the file.")
        return "file"
    return _choose(app, "Store", ("keyring", "file"), "keyring")


def _api(app, config: Mapping[str, Any], secret: Callable[[], str]):
    factory = getattr(app, "proxmox_api", None)
    if factory is not None:
        return factory(config, secret=secret)
    return proxmox_api.ProxmoxAPI(config, secret=secret)


def _get(api, path: str, **params: Any) -> Any:
    """GET, with a refusal (403) read as "the token cannot see it"."""
    try:
        return api.get(path, **params)
    except proxmox_api.ProxmoxError as exc:
        if exc.status == 403:
            return None
        raise


def _discover(app, store, api, config: dict[str, Any], old: Mapping[str, Any],
              versions: Mapping[str, Any]) -> Result:
    n = Names(token_id=config["token_id"])
    todo: list[tuple[str, list[str]]] = []  # (what is missing, root commands)

    # Node.
    nodes = sorted(str(e["node"]) for e in (_get(api, "/nodes") or [])
                   if e.get("node") and e.get("status", "online") == "online")
    if not nodes:
        raise FleetError("The token sees no online Proxmox node.",
                         hint="Check the node is up, and that the token is the one you made.")
    node = _pick(app, "Node", nodes, old.get("node"))

    # Pool.
    pools = sorted(str(e["poolid"]) for e in (_get(api, "/pools") or []) if e.get("poolid"))
    if pools:
        n.pool = _pick(app, "Pool for Ships", pools, old.get("pool") or "shipshape")
    else:
        todo.append(("a pool the token can use", [
            f'pveum pool add {_q(n.pool)} --comment "ShipShape Ships"   # if it does not exist',
            *_grant(f"/pool/{n.pool}", "ShipShapeVM", n)]))

    # Storages.
    storages = [e for e in (_get(api, f"/nodes/{node}/storage") or [])
                if e.get("storage") and e.get("enabled", 1) and e.get("active", 1)]
    disks = sorted(e["storage"] for e in storages if "images" in _content(e))
    seeds = sorted(e["storage"] for e in storages if "iso" in _content(e))
    uploads = sorted(e["storage"] for e in storages if "import" in _content(e))
    if disks:
        n.disk = _pick(app, "Storage for VM disks", disks, old.get("disk_storage"))
    else:
        todo.append(("a VM disk storage the token can use (pvesm status lists them)",
                     [*_variables(Names(bridge="x")),
                      *_grant("/storage/$DISK_STORAGE", "ShipShapeDisks", n)]))
    # Seeds hold each new Ship's host key until it enrols, and the token may delete
    # there: only a storage that holds nothing but ISOs will do.
    seed_options = [s for s in seeds if s != n.disk and _content(
        next(e for e in storages if e["storage"] == s)) - {""} == {"iso"}]
    if seed_options:
        preferred = old.get("seed_storage") or next(
            (s for s in seed_options if "seed" in s), None)
        n.seeds = _pick(app, "Storage for seed ISOs", seed_options, preferred)
    else:
        todo.append(("a storage only for seed ISOs (it holds each new Ship's host key until "
                     "it enrols, so keep it apart)", seed_storage_commands(n)))
    image_storage = None
    if uploads:
        options = ["none", *uploads]
        choice = _pick(app, "Storage for baked image uploads (optional)", options,
                       old.get("image_storage") or uploads[0])
        image_storage = None if choice == "none" else choice

    # Bridge: the token may use the bridges it has SDN.Use on.
    perms = _get(api, "/access/permissions") or {}
    bridges = bridges_from(perms)
    if bridges:
        n.bridge = _pick(app, "Network bridge", bridges, old.get("bridge"))
    else:
        todo.append(("a network bridge the token may use",
                     [*_variables(Names(disk="x")),
                      *_grant("/sdn/zones/localnetwork/$BRIDGE", "PVESDNUser", n)]))

    # The rights on what was found.
    if not todo:
        for path, role, lacking in missing_grants(perms, n):
            todo.append((f"rights on {path} ({', '.join(lacking)})", _grant(path, role, n)))

    # Template.
    template = None
    if not todo:
        resources = _get(api, "/cluster/resources", type="vm") or []
        templates = sorted((int(e["vmid"]), str(e.get("name", ""))) for e in resources
                           if e.get("template") and e.get("node") == node and "vmid" in e)
        if templates:
            labels = [f"{vmid} ({name})" if name else str(vmid) for vmid, name in templates]
            preferred = next((label for (vmid, name), label in zip(templates, labels, strict=True)
                              if vmid == old.get("template_vmid") or name == "shipshape-base"),
                             None)
            template = int(_pick(app, "Template VM", labels, preferred).split()[0])
        else:
            todo.append(("a template VM (from Ubuntu's signed cloud image, at the pinned build)",
                         template_commands(n, versions)))

    if todo:
        app.say("\nStill missing on the Proxmox host:")
        for what, _ in todo:
            app.say(f"  - {what}")
        _print_commands(app, "Run these as root on the Proxmox host:",
                        [line for _, lines in todo for line in lines])
        app.say("Then run ship-fleet setup again: it picks up from here (the token is kept).")
        return Result(False, "missing pieces on the host")

    if not set(SNAPSHOT_PRIVS) <= effective(perms, f"/pool/{n.pool}"):
        app.say("\nOptional: snapshots need two more rights (ship-fleet snapshot); on the host:\n"
                f'  pveum role modify ShipShapeVM --append 1 --privs "{" ".join(SNAPSHOT_PRIVS)}"')

    vmid_range = _vmid_range(app, old.get("vmid_range"))
    config.update(node=node, pool=n.pool, template_vmid=template, seed_storage=n.seeds,
                  disk_storage=n.disk, bridge=n.bridge)
    if image_storage:
        config["image_storage"] = image_storage
    if vmid_range:
        config["vmid_range"] = vmid_range
    ordered = {k: config[k] for k in (
        "api_url", "node", "pool", "template_vmid", "seed_storage", "disk_storage", "bridge",
        "image_storage", "token_id", "token_store", "keyring_item", "token_file",
        "tls_fingerprint", "vmid_range") if k in config}
    with store.edit() as ed:
        ed.set_driver("proxmox", ordered)
        ed.set_default("driver", "proxmox")
    app.say("\nSaved [drivers.proxmox] in ships.toml; new Ships are made on Proxmox.")
    return Result(True)


def _vmid_range(app, old) -> list[int] | None:
    shown = f"{old[0]}-{old[1]}" if old else "any free one"
    while True:
        answer = _ask(app, f"VM ids FleetLedger may use, e.g. 9100-9199 ({shown}): ", "")
        if not answer:
            return list(old) if old else None
        match = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", answer)
        if match and 100 <= int(match.group(1)) <= int(match.group(2)):
            return [int(match.group(1)), int(match.group(2))]
        app.note("Give two numbers, first-last, both 100 or more.")
