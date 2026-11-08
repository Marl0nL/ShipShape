"""A demo fleet for the web UI: fake Ships in a throwaway FleetLedger home.

    .venv/bin/python fleetledger/tests/demo_ui.py [--port 8790]

Makes a private FleetLedger home in a new temporary folder (config, data, keys),
writes an inventory of six made-up Ships, and serves `ship-fleet ui` against
Ships that are simulated in this process: nothing is reached over the network,
and your real ~/.config/fleetledger, ~/.ssh and keyring are never read. The
history store is seeded with a week of made-up samples so the dashboard's charts
have something to draw, and with a few finished operations for the Activity page.
Use it for screenshots and for working on the UI.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EXAMPLES = ROOT / "contracts" / "examples"
sys.path.insert(0, str(ROOT / "fleetledger"))

from fleetledger.drivers.base import (  # noqa: E402 - after the path is set
    Driver,
    Outcome,
    PowerState,
    Snapshot,
    check_snapshot_name,
)

# Ships on the in-memory demo hypervisor (DemoDriver, as "proxmox"); the rest are manual.
ON_DEMO_DRIVER = {"ship-01", "ship-02", "ship-03", "ship-04"}
DEMO_FINGERPRINT = ":".join(["AB", "CD", "EF", "01", "23", "45", "67", "89"] * 4)  # made up

SHIPS = {
    # name: (tags, on the overlay)
    "ship-01": (["work"], True),
    "ship-02": (["work"], True),
    "ship-03": (["personal"], True),
    "ship-04": (["lab"], True),
    "ship-05": (["lab"], True),
    "ship-06": (["work"], False),
}
DOWN = {"ship-05"}
# Refuses a plain reboot: apt still running in someone's SSH login. Force reboot gets
# past it, then waits for a fresh boot the demo never makes, like any demo reboot.
LOCKED = {"ship-04": {
    "who": "APT", "why": "APT is installing or removing packages", "mode": "block",
    "user": "root", "pid": 4242,
    "process": {"pid": 4242, "comm": "apt", "user": "root", "tty": "pts/0",
                "session": {"id": "7", "user": "sam", "service": "sshd", "tty": None,
                            "state": "active"}},
    "still_running": []}}
LEFT: set[str] = set()  # Ships taken off the overlay in this demo run
MODES_NOW: dict[tuple[str, str], str] = {}  # (Ship, agent) -> mode after a demo restart
CLAUDE_SET: dict[str, dict] = {}  # Ship -> claude settings changed in this demo run
INBOX: dict[str, dict[str, tuple[int, float]]] = {  # Ship -> file name -> (size, when)
    "ship-01": {"design-notes.md": (18_342, time.time() - 3 * 3600),
                "sample-data.csv": (2_411_520, time.time() - 26 * 3600)},
}
DEMO_CA: dict[str, str] = {}  # the demo CA's fingerprints, once home() has made it

INVENTORY = """\
schema = 1

[fleet]
admin_key = "{keys}/fleet_admin"
agent_keys = ["{keys}/you.pub"]

[defaults]
driver = "manual"
claude = {{ remote_control = "on", default_mode = "auto" }}

[tag.work]
claude = "work@example.com"
github = "example-work"

[tag.personal]
claude = "you@example.com"
github = "example-user"

[overlay]
provider = "tailscale"
tags = ["tag:ship"]

# Never contacted: DemoDriver stands in for this Proxmox (see make_driver).
[drivers.proxmox]
api_url = "https://pve.example.invalid:8006/api2/json"
node = "pve"
pool = "shipshape"
template_vmid = 9000
seed_storage = "shipshape-seeds"
disk_storage = "local-lvm"
bridge = "vmbr0"
token_id = "shipshape@pve!demo"
token_file = "{keys}/proxmox-token"  # never written: nothing reads it in the demo
tls_fingerprint = "{fingerprint}"
vmid_range = [9100, 9199]
"""

SHIP_ENTRY = """
[ship.{name}]
id = "{id}"
tags = {tags}
driver = "{driver}"
names = [{names}]
host_key = "{host_key}"
enrolled = "2026-01-01T00:00:00Z"
{overlay}"""


class DemoDriver(Driver):
    """A hypervisor in memory, for the demo: each Ship's VM and its snapshots live in
    this process, and every call succeeds at once. It stands in for the "proxmox"
    driver (make_driver), so anything the UI does through a driver (snapshots here;
    making, destroying, starting and stopping a Ship) works in the demo without a
    hypervisor. Nothing is contacted."""

    id = "proxmox"

    def __init__(self) -> None:
        self.vms: dict[str, dict] = {}  # ship name -> {"state", "vmid", "snapshots"}

    def _vm(self, ship) -> dict:
        return self.vms.setdefault(ship.name, {"state": "running",
                                               "vmid": 9100 + len(self.vms),
                                               "snapshots": []})

    def _done(self, text: str, **ref) -> Outcome:
        return Outcome(True, [f"{text} (demo hypervisor)."], driver_ref=ref)

    # --- the VM ---------------------------------------------------------------------
    def create(self, ship, seed, size, image=None) -> Outcome:
        vm = self._vm(ship)
        vm["state"] = "running"
        return self._done(f"Made {ship.name}'s VM", vmid=vm["vmid"])

    def start(self, ship) -> Outcome:
        self._vm(ship)["state"] = "running"
        DOWN.discard(ship.name)  # it answers again
        return self._done(f"Started {ship.name}")

    def stop(self, ship) -> Outcome:
        self._vm(ship)["state"] = "stopped"
        DOWN.add(ship.name)  # and stops answering, as a stopped VM does
        return self._done(f"Stopped {ship.name}")

    def reboot(self, ship) -> Outcome:
        self._vm(ship)["state"] = "running"
        return self._done(f"Rebooted {ship.name}")

    def destroy(self, ship) -> Outcome:
        self.vms.pop(ship.name, None)
        return self._done(f"Destroyed {ship.name}'s VM")

    def status(self, ship) -> PowerState:
        vm = self.vms.get(ship.name)
        return PowerState(vm["state"] if vm else "unknown")

    def finish_enrolment(self, ship) -> Outcome:
        return self._done(f"Detached {ship.name}'s seed")

    # --- baked images ----------------------------------------------------------------
    def publish_image(self, image) -> Outcome:
        self.templates = getattr(self, "templates", 9180) + 1
        return self._done(f"Made image {image.id} into template {self.templates}",
                          template_vmid=self.templates)

    def remove_image(self, image) -> Outcome:
        return self._done(f"Removed image {image.id}'s template")

    # --- snapshots -------------------------------------------------------------------
    def snapshots(self, ship) -> list:
        return list(self._vm(ship)["snapshots"])

    def snapshot(self, ship, name, description="") -> Outcome:
        check_snapshot_name(name)
        taken = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._vm(ship)["snapshots"].append(Snapshot(name, taken, description))
        return self._done(f"Took snapshot {name} of {ship.name}")

    def rollback(self, ship, name) -> Outcome:
        check_snapshot_name(name)
        return self._done(f"Rolled {ship.name} back to {name}")

    def delete_snapshot(self, ship, name) -> Outcome:
        check_snapshot_name(name)
        vm = self._vm(ship)
        vm["snapshots"] = [s for s in vm["snapshots"] if s.name != name]
        return self._done(f"Deleted snapshot {name} of {ship.name}")


def make_driver():
    """The demo's driver_factory: DemoDriver for "proxmox", the real one otherwise."""
    from fleetledger.drivers import get_driver

    demo = DemoDriver()
    now = datetime.now(UTC)
    for name, days, note in (("ship-01", 6, "before the herdr upgrade"),
                             ("ship-01", 1, "known good"), ("ship-02", 3, "")):
        taken = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        vm = demo.vms.setdefault(name, {"state": "running", "vmid": 9100 + len(demo.vms),
                                        "snapshots": []})
        vm["snapshots"].append(Snapshot(f"snap-{taken[:10].replace('-', '')}", taken, note))

    def factory(inventory, driver_id, **kw):  # kw: what `new` passes (ubuntu_release)
        return demo if driver_id == "proxmox" else get_driver(inventory, driver_id, **kw)

    return factory


def demo_images(base: Path) -> str:
    """Two made-up baked images: one published to the demo hypervisor, one only on
    this computer (a small stand-in file, not a real qcow2)."""
    import hashlib

    folder = base / "data" / "images"
    folder.mkdir(parents=True, mode=0o700)
    local = "26.04-20261104-4e5f6a7b"
    qcow = folder / f"{local}.qcow2"
    qcow.write_bytes(b"demo image")
    common = ('ubuntu = {{ release = "26.04", build = "{build}" }}\ntooling = "0.1.0"\n'
              'commit = "0123456789abcdef0123456789abcdef01234567"\n')
    return (f'\n[image."26.04-20261102-0a1b2c3d"]\ncreated = "2026-11-02T10:00:00Z"\n'
            f'sha256 = "{"ab" * 32}"\n' + common.format(build="20261016")
            + 'recipes = ["chrome-devtools-axi"]\n'
            'published = { proxmox = { template_vmid = 9181 } }\n'
            f'\n[image."{local}"]\ncreated = "2026-11-04T10:00:00Z"\n'
            f'sha256 = "{hashlib.sha256(qcow.read_bytes()).hexdigest()}"\nfile = "{qcow}"\n'
            + common.format(build="20261030") + 'recipes = ["chrome-devtools-axi", "gh-axi"]\n')


def ship_id(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def home(base: Path) -> None:
    for name in ("config", "data", "run", "keys"):
        (base / name).mkdir(mode=0o700)
    os.environ["FLEETLEDGER_CONFIG_DIR"] = str(base / "config")
    os.environ["FLEETLEDGER_DATA_DIR"] = str(base / "data")
    os.environ["FLEETLEDGER_RUNTIME_DIR"] = str(base / "run")
    # Your home and ssh-agent too: "Let this computer use ssh" writes an SSH config and
    # herdr's saved machines under HOME, so in the demo that is the throwaway home.
    (base / "home").mkdir(mode=0o700)
    os.environ["HOME"] = str(base / "home")
    os.environ.pop("SSH_AUTH_SOCK", None)
    for key in ("fleet_admin", "you", *(f"host-{name}" for name in SHIPS)):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", key,
                        "-f", str(base / "keys" / key)], check=True)
    text = INVENTORY.format(keys=base / "keys", fingerprint=DEMO_FINGERPRINT)
    for i, (name, (tags, joined)) in enumerate(SHIPS.items(), start=1):
        overlay = f'overlay_node = "nDemo{i}"\n' if joined else ""
        driver = "proxmox" if name in ON_DEMO_DRIVER else "manual"
        host_key = " ".join((base / "keys" / f"host-{name}.pub").read_text().split()[:2])
        # A joined Ship's overlay name comes first, as `overlay join` records it.
        names = ([f"{name}.example-tailnet.ts.net"] if joined else []) + [
            f"{name}.example.invalid"]
        text += SHIP_ENTRY.format(name=name, id=ship_id(i), tags=json.dumps(tags),
                                  host_key=host_key, overlay=overlay, driver=driver,
                                  names=", ".join(json.dumps(n) for n in names))
        if name == "ship-06":  # it never uses Google Cloud: shown as off
            text += 'signins = ["claude", "github"]\n'
    text += demo_images(base)
    path = base / "config" / "ships.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    demo_ca(base, path)


def demo_ca(base: Path, path: Path) -> None:
    """A made-up certificate authority (keys without a passphrase, in the demo home)
    and one trusted machine, so Settings ▸ Access shows certificates and machines."""
    from fleetledger.store import TomlFileStore

    from fleetledger import ca

    for key in ("host_ca", "user_ca", "laptop"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"demo-{key}",
                        "-f", str(base / "keys" / key)], check=True)
    public = {k: (base / "keys" / f"{k}.pub").read_text().strip()
              for k in ("host_ca", "user_ca", "laptop")}
    DEMO_CA.update(host=ca.fingerprint(public["host_ca"]),
                   user=ca.fingerprint(public["user_ca"]))
    soon = _stamp(datetime.now(UTC) + timedelta(hours=10))
    with TomlFileStore(path).edit() as ed:
        ed.set_ca({"host_key": str(base / "keys" / "host_ca"),
                   "user_key": str(base / "keys" / "user_ca")})
        ed.set_machine("laptop", {"public_key": public["laptop"],
                                  "added": "2026-01-01T00:00:00Z", "scope": ["tag:work"],
                                  "logins": ["agent"], "last_valid_before": soon})


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def status_doc(name: str, tags: list[str], index: int, now: float) -> dict:
    """A plausible status document for one demo Ship, wobbling with time."""
    base = json.loads((EXAMPLES / "ship-status" / "running-overlay.json").read_text())
    doc = copy.deepcopy(base)
    stamp = _stamp(datetime.fromtimestamp(now, UTC))
    doc["generated_at"] = stamp
    doc["ship"].update(name=name, id=ship_id(index), tags=tags)
    for entry in doc["auth"].values():
        entry["checked_at"] = stamp
    wave = math.sin(now / 5400 + index) + 0.5 * math.sin(now / 900 + 2 * index)
    host = doc["host"]
    host.update(cpus=4, load1=round(4 * max(1, min(100, 30 + 8 * index + 18 * wave)) / 100, 2),
                uptime_s=86400 * index + 3600,
                mem_used_pct=round(max(5, min(100, 35 + 7 * index + 6 * wave)), 1),
                disk_used_pct=float([22, 91, 47, 63, 30, 12][index - 1]),
                updates_pending=[3, 0, 12, 0, 0, 0][index - 1],
                reboot_required=index == 4)
    agents = doc["herdr"]["agents"]
    doc["attention"] = []
    if DEMO_CA:  # certified by the demo CA; ship-04's runs out soon
        days = 5 if name == "ship-04" else 60
        until = datetime.fromtimestamp(now, UTC) + timedelta(days=days)
        doc["ssh"] = {"host_certificate": {
            "serial": 100 + index, "key_id": f"{name} host",
            "principals": [name, f"{name}.example.invalid"],
            "valid_after": _stamp(until - timedelta(days=90)), "valid_before": _stamp(until),
            "ca_fingerprint": DEMO_CA["host"]},
            "user_ca": [DEMO_CA["user"]], "agent_principals": ["agent:all"],
            "revoked_keys_sha256": None}
    if name == "ship-01":
        doc["attention"].append({"code": "agent-waiting", "subject": "main",
                                 "message": "Agent 'main' is waiting for you."})
    if name == "ship-02":
        agents[0]["state"] = "working"
        agents.append({"name": "tests", "kind": "claude", "state": "working", "mode": "auto",
                       "pane": "w3:p1", "workspace": "w3"})
    if name == "ship-03":
        doc["claude"]["remote_control"] = "off"
        doc["auth"]["claude"].update(state="expired", account="you@example.com")
        doc["auth"]["github"].update(state="missing", account=None)
        doc["attention"] += [
            {"code": "signin-expired", "subject": "claude",
             "message": "Claude's sign-in has expired."},
            {"code": "signin-missing", "subject": "github", "message": "Not signed in to GitHub."},
        ]
        for a in agents:
            a["state"] = "idle"
    if name == "ship-04":
        soon = datetime.fromtimestamp(now, UTC) + timedelta(days=2)
        doc["auth"]["gcloud"].update(state="expiring", expires_at=_stamp(soon))
        doc["attention"].append({"code": "signin-expiring", "subject": "gcloud",
                                 "message": "The Google Cloud sign-in expires soon."})
        doc["herdr"]["agents"] = agents[:1]
        agents[0]["state"] = "idle"
        doc["attention"] += [
            {"code": "reboot-required", "message": "The OS asks for a reboot."},
            {"code": "host-cert-expiring", "subject": "ssh",
             "message": "The SSH host certificate expires in 5 days."},
        ]
    if name == "ship-06" or name in LEFT:
        doc["overlay"] = None
    else:  # each Ship its own overlay name and address
        doc["overlay"].update(name=f"{name}.example-tailnet.ts.net", node_id=f"nDemo{index}",
                              addresses=[f"192.0.2.{10 + index}"])
    if name == "ship-06":
        doc["herdr"]["agents"] = []
        doc["state"] = "ready"
    for a in doc["herdr"]["agents"]:
        a["mode"] = MODES_NOW.get((name, a.get("name")), a["mode"])
    if doc["herdr"]["agents"] and doc["state"] != "running":
        doc["state"] = "running"
    adc = doc["auth"]["gcloud_adc"]
    adc.update(state="ok", account=doc["auth"]["gcloud"]["account"])
    if "personal" in tags:  # signed in as the accounts its tag expects
        for service, entry in doc["auth"].items():
            if entry["account"]:
                entry["account"] = "example-user" if service == "github" else "you@example.com"
    doc["net"] = {"window_h": 24, "rx_mb": 120.0 * index, "tx_mb": 14.0 * index,
                  "new_destinations": [], "collector": "running", "source": "ebpf",
                  "destinations": 10 + index, "connections": 200 * index}
    doc["tools"] = {"gh-axi": {"state": "ok", "version": "1.4.0"},  # catalogue tools
                    "tasks-axi": {"state": "ok", "version": "0.9.2"}}
    if name != "ship-06":  # its settings, as newer tooling reports them; ship-06 is older
        settings = json.loads((EXAMPLES / "ship-status" / "running-settings.json")
                              .read_text())["settings"]
        settings["ship"]["tags"] = list(tags)
        settings["claude"]["session_prefix"] = name
        settings["network"]["ssh_listen"] = doc["overlay"]["ssh_listen"] if doc.get(
            "overlay") else "any"
        doc["settings"] = settings
    if doc.get("settings") and CLAUDE_SET.get(name):  # set in this demo run
        doc["settings"]["claude"].update(CLAUDE_SET[name])
        doc["claude"].update(CLAUDE_SET[name])
    return doc


def demo_browse(timeout: float) -> list:
    """What `ship-fleet scan` hears in the demo: no network, two announcements."""
    from fleetledger.scan import Announcement

    return [Announcement("ship-01", "ship-01.example.invalid", 22, ("192.0.2.11",),
                         {"name": "ship-01", "id": ship_id(1)}),
            Announcement("ship-08", "ship-08.example.invalid", 22, ("192.0.2.18",),
                         {"name": "ship-08"})]


_DEMO_KEYS: dict[str, str] = {}


def demo_keyscan(host: str) -> str:
    """A made-up host key per host, the same each time (no ssh-keyscan, no network)."""
    from fleetledger.seed import hostkey

    if host not in _DEMO_KEYS:
        _DEMO_KEYS[host] = " ".join(hostkey.generate(host.split(".")[0]).public.split()[:2])
    return _DEMO_KEYS[host]


def make_ssh(inventory):
    from fleetledger.ssh import SSH, Result, Unreachable

    names = list(SHIPS)

    class DemoSSH(SSH):
        def run(self, ship, command, *, input=None, timeout=60):
            if ship.name in DOWN or ship.name not in SHIPS:  # a new one: no VM in the demo
                raise Unreachable(f"Cannot reach {ship.name} (demo: switched off).")
            if command[:4] == ["sudo", "-n", "systemctl", "reboot"] and ship.name in LOCKED \
                    and "--check-inhibitors=no" not in command:
                return Result(1, b"", "Call to Reboot failed: Operation denied due to active "
                              "block inhibitor\n", ship.names[0])
            if command == ["python3", "-I", "-"]:  # fleetledger.inhibitors.PROBE
                found = [LOCKED[ship.name]] if ship.name in LOCKED else []
                return Result(0, json.dumps({"blockers": found}).encode(), "", ship.names[0])
            verb = command[3] if command[:3] == ["sudo", "-n", "/usr/local/bin/ship-ctl"] else ""
            if verb == "status":
                tags, _joined = SHIPS[ship.name]
                doc = status_doc(ship.name, tags, names.index(ship.name) + 1, time.time())
                return Result(0, json.dumps(doc).encode(), "", ship.names[0])
            if verb == "logs":  # a few made-up lines of the asked source
                answer = ctl_result("logs")
                answer["data"] = {"lines": [f"{ship.name} {command[4]}: demo line {i}"
                                            for i in range(1, 6)]}
                return Result(0, json.dumps(answer).encode(), "", ship.names[0])
            if verb == "overlay":  # status and leave, as ship-ctl overlay answers
                if command[4:5] == ["leave"]:
                    LEFT.add(ship.name)
                tags, _joined = SHIPS[ship.name]
                doc = status_doc(ship.name, tags, names.index(ship.name) + 1, time.time())
                answer = ctl_result("overlay")
                answer["data"] = {"overlay": doc["overlay"]}
                return Result(0, json.dumps(answer).encode(), "", ship.names[0])
            if verb == "agent":  # restart: the agents come back in the mode asked for
                return Result(*demo_restart(ship.name, command[5:-1]), ship.names[0])
            if verb == "set":  # the Claude Code settings, so Settings shows a change
                changed = CLAUDE_SET.setdefault(ship.name, {})
                for pair in command[4:-1]:
                    key, _, value = pair.partition("=")
                    if key in ("claude.default_mode", "claude.allow_bypass"):
                        field = key.split(".")[1]
                        changed[field] = value == "true" if field == "allow_bypass" else value
                answer = ctl_result("set")
                answer["changed"] = ["/etc/shipshape/ship.toml"]
                answer["messages"] = ["Saved and applied."]
                return Result(0, json.dumps(answer).encode(), "", ship.names[0])
            if verb == "inbox":  # list and remove, as ship-ctl inbox answers them
                return Result(0, json.dumps(demo_inbox(ship.name, command[4:-1])).encode(),
                              "", ship.names[0])
            if verb in ("version", "ssh-trust"):  # so certificate jobs succeed
                return Result(0, json.dumps(ctl_result(verb)).encode(), "", ship.names[0])
            return Result(0, b"{}", "", ship.names[0])

        def send(self, ship, command, source, *, on_progress=None, timeout=600):
            """`ship-ctl inbox put`: read the file in pieces, slowly, so the card
            shows its progress; keep only its name and size."""
            if ship.name in DOWN or ship.name not in SHIPS:
                raise Unreachable(f"Cannot reach {ship.name} (demo: switched off).")
            argv = command[4:-1]
            name = argv[argv.index("--name") + 1]
            size = int(argv[argv.index("--size") + 1])
            sent = 0
            while chunk := source.read(max(size // 20, 64 * 1024)):
                sent += len(chunk)
                time.sleep(0.15)
                if on_progress:
                    on_progress(sent)
            from fleetledger.inbox import numbered

            box = INBOX.setdefault(ship.name, {})
            stored, n = name, 1
            while stored in box:
                n += 1
                stored = numbered(name, n)
            box[stored] = (size, time.time())
            answer = ctl_result("inbox")
            answer["data"] = {"name": stored, "given": name, "size": size,
                              "path": f"/home/agent/inbox/{stored}", "renamed": stored != name}
            return Result(0, json.dumps(answer).encode(), "", ship.names[0])

        def probe_certificate(self, ship, known_hosts, timeout=30):
            return None  # as if the Ship presented its new certificate

    return DemoSSH(inventory)


def demo_restart(name: str, argv: list[str]) -> tuple[int, bytes, str]:
    """`ship-ctl agent restart` on a demo Ship: as the Ship answers it, without herdr."""
    tags, _joined = SHIPS[name]
    doc = status_doc(name, tags, list(SHIPS).index(name) + 1, time.time())
    settings = doc.get("settings") or {}
    claude = settings.get("claude") or doc["claude"]
    own = {e["name"]: e.get("mode") for e in settings.get("autostart") or []}
    mode = argv[argv.index("--mode") + 1] if "--mode" in argv else None
    agents = {a["name"]: a for a in doc["herdr"]["agents"] if a.get("name")}
    names = list(agents) if "--all" in argv else [
        a for a in argv if not a.startswith("--") and a != mode]
    answer = ctl_result("agent")
    restarted, skipped = [], []
    for agent in names:
        new = mode or own.get(agent) or claude["default_mode"]
        if new == "bypassPermissions" and not claude["allow_bypass"]:
            answer.update(ok=False, error={"code": "invalid-input", "message": (
                "bypassPermissions is not allowed on this Ship (claude.allow_bypass is "
                "false); allow it first, then restart the agent")})
            return 1, json.dumps(answer).encode(), ""
        if agents[agent]["state"] in ("working", "unknown") and "--force" not in argv:
            skipped.append({"name": agent, "reason": "it is working; not interrupted"})
            continue
        restarted.append({"name": agent, "mode": new, "previous_mode": agents[agent]["mode"],
                          "conversation": "fresh" if "--fresh" in argv else "continued",
                          "was": agents[agent]["state"]})
        MODES_NOW[(name, agent)] = new
    answer["data"] = {"restarted": restarted, "skipped": skipped}
    answer["changed"] = [r["name"] for r in restarted]
    answer["messages"] = [f"{r['name']} restarted in {r['mode']}, continuing its conversation."
                          for r in restarted]
    if skipped:
        answer["messages"] += [f"{s['name']} is working: left alone. Try again when it is "
                               "idle, or force it (which interrupts its turn)." for s in skipped]
        answer.update(ok=False, error={"code": "agent-busy", "message": (
            f"{len(skipped)} agent(s) are busy and were not restarted (force to interrupt "
            "them)")})
        return 1, json.dumps(answer).encode(), ""
    return 0, json.dumps(answer).encode(), ""


def demo_inbox(name: str, argv: list[str]) -> dict:
    """`ship-ctl inbox list|remove|notify` on a demo Ship."""
    box = INBOX.setdefault(name, {})
    answer = ctl_result("inbox")
    if argv[:1] == ["remove"]:
        gone = [n for n in argv[1:] if box.pop(n, None) is not None]
        answer["data"] = {"removed": gone, "missing": [n for n in argv[1:] if n not in gone],
                          "refused": []}
    elif argv[:1] == ["notify"]:
        answer["data"] = {"count": int(argv[-1]), "sent": ["main"], "skipped": []}
    else:
        files = sorted(box.items(), key=lambda kv: -kv[1][1])
        answer["data"] = {
            "path": "/home/agent/inbox", "exists": True, "others": 0, "truncated": False,
            "max_bytes": 256 * 1024 * 1024, "free_bytes": 41_200_000_000,
            "files": [{"name": n, "size": size,
                       "modified": datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
                      for n, (size, at) in files]}
    return answer


def ctl_result(verb: str) -> dict:
    """ship-ctl's answer (the ship-ctl-result contract) to version and ssh-trust."""
    data = {"tooling": "0.1.0", "commit": "0123456789ab",
            "contracts": {"ship-status": 1, "ship-toml": 1, "ship-ctl-result": 1,
                          "bundle": 1, "seed": 1, "runtime": 1, "ssh-trust": 1,
                          "inbox": 1}}
    return {"schema": "shipshape.ship-ctl-result/1", "command": verb, "ok": True,
            "changed": ["/etc/ssh/sshd_config.d/shipshape.conf"] if verb == "ssh-trust" else [],
            "messages": [], "data": data if verb == "version" else {}, "error": None}


def seed_net() -> None:
    """Cached 7-day network summaries, as if just refreshed."""
    from fleetledger import net, paths

    folder = paths.private_dir(net.net_dir())
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    for i, name in enumerate(SHIPS, start=1):
        if name in DOWN:
            continue
        doc = json.loads((EXAMPLES / "net-summary" / "running.json").read_text())
        doc["generated_at"] = _stamp(now)
        doc["collector"]["updated_at"] = _stamp(now)
        for d in doc["destinations"]:
            d["first_seen"] = _stamp(now - timedelta(days=3))
            d["last_seen"] = _stamp(now - timedelta(minutes=5))
        hourly = []
        for h in range(7 * 24, 0, -1):
            hour = now - timedelta(hours=h)
            day = 0.6 + 0.4 * math.sin((hour.hour - 6) / 24 * 2 * math.pi)
            hourly.append({"hour": _stamp(hour), "rx_mb": round(i * 6 * day + (h % 5), 1),
                           "tx_mb": round(i * 0.8 * day + (h % 3) * 0.2, 1)})
        doc["totals"]["hourly"] = hourly
        paths.write_private(folder / f"{name}.json", json.dumps(doc).encode())


def seed_history() -> None:
    """A week of samples, every 10 minutes, with a gap where nobody was looking."""
    try:
        from fleetledger import history
    except ImportError:  # a checkout from before the history store
        return
    store = history.History()
    now = time.time()
    names = list(SHIPS)
    rows = []
    for step in range(7 * 24 * 6, 0, -1):
        at = now - step * 600
        if 2 * 86400 < step * 600 < 2.4 * 86400:
            continue  # the UI was closed: no polls, no samples
        for i, name in enumerate(names, start=1):
            if name in DOWN and step < 30:
                rows.append(history.Sample(name, at, up=False))
                continue
            wave = math.sin(at / 5400 + i) + 0.5 * math.sin(at / 900 + 2 * i)
            rows.append(history.Sample(
                name, at, up=True,
                cpu=max(1.0, min(100.0, 30 + 8 * i + 18 * wave)),
                mem=max(5.0, min(100.0, 35 + 7 * i + 6 * wave)),
                disk=[22, 91, 47, 63, 30, 12][i - 1] - step * 0.0004 * i,
                agents=[2, 3, 2, 1, 2, 0][i - 1], working=[1, 3, 0, 0, 1, 0][i - 1],
                waiting=1 if name == "ship-01" and step < 12 else 0))
    store.add(rows)


def seed_operations() -> None:
    """A few days of finished operations for the Activity page (one dismissed, one
    failed, one by hand)."""
    try:
        from fleetledger.history import Operation, Operations
    except ImportError:  # a checkout from before the operations history
        return
    now = time.time()
    store = Operations()
    made = [
        ("update", "Update", ["ship-01", "ship-02"], 3 * 86400, 140, "ok", None,
         ["ship-01: converged, no reboot needed", "ship-02: converged, no reboot needed"], True),
        ("reboot", "Reboot", ["ship-03"], 2 * 86400 + 4000, 75, "ok", None,
         ["Rebooting ship-03.", "ship-03 is back: 2 agents in their modes."], False),
        ("snapshot", "Take snapshot before-upgrade", ["ship-04"], 2 * 86400, 12, "by-hand",
         None, ["The manual driver cannot snapshot: take it on your hypervisor."], False),
        ("update", "Update", ["ship-05"], 86400 + 600, 31, "failed",
         "ship-05 did not answer over SSH.", ["Connecting to ship-05...", "timed out"], False),
        ("tools", "Add tools", ["ship-02"], 7200, 48, "ok", None,
         ["Installing ripgrep.", "ship-02: tools changed"], True),
    ]
    for i, (kind, title, ships, ago, took, outcome, error, log, dismissed) in enumerate(made, 1):
        store.save(Operation(id=i, kind=kind, title=title, ships=tuple(ships),
                             started=now - ago, ended=now - ago + took, outcome=outcome,
                             error=error, log=tuple(log), dismissed=dismissed))


def demo_spawner(argv: list[str]):
    """The concierge's terminal in the demo: a recorded sign-in session played back
    (tests/fake_cli.py), instead of ssh to a Ship. A paste-back sign-in waits for its
    code, so the Sign-ins page has something waiting for you; any code is refused."""
    from fleetledger.concierge import engine

    command = " ".join(argv)
    fixture = ("gcloud" if "gcloud" in command else
               "claude" if "claude" in command else None)
    if fixture is None:  # GitHub's device flow would finish at once: wait instead
        script = Path(tempfile.mkdtemp(prefix="fleetledger-demo-")) / "wait.txt"
        script.write_text("> Open this URL to continue in your web browser: "
                          "https://github.com/login/device\n"
                          "> ! First copy your one-time code: ABCD-1234\n~ 900\n! 1\n")
        path = script
    else:
        path = HERE / "fixtures" / "concierge" / f"{fixture}.txt"
    return engine.spawn([sys.executable, str(HERE / "fake_cli.py"), str(path)])


def seed_remote(port: int):
    """--remote: the remote web UI as on, at a made-up address, with two paired devices
    (one paired 6 hours ago, so it needs pairing again for danger actions; the other on
    the trusted machine "laptop"). Nothing is served through a real Tailscale: the Serve
    check is skipped."""
    import secrets

    from fleetledger.web import devices, remote

    setting = remote.Remote("https://fleetledger.example-tailnet.ts.net", port,
                            ("you@example.com",))
    remote.save(setting)
    now = time.time()
    for name, how, ago, machine in (("phone", "laptop", 6 * 3600, None),
                                    ("laptop", devices.FROM_LINK, 1800, "laptop")):
        devices.Devices(wall=lambda ago=ago: now - ago).add(
            secrets.token_urlsafe(32), name=name, login="you@example.com",
            origin=setting.origin, how=how, machine=machine)
    return setting


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--home", help="use this (empty) folder as the FleetLedger home")
    parser.add_argument("--remote", action="store_true",
                        help="show the remote web UI as on (Settings, Devices), with made-up "
                             "paired devices; never through a real Tailscale")
    parser.add_argument("--service", action="store_true",
                        help="run as the service does (ship-fleet ui --service): no link "
                             "printed (ship-fleet ui link --local, with this home's "
                             "FLEETLEDGER_RUNTIME_DIR, gives one), and a stop lets running "
                             "jobs finish first")
    args = parser.parse_args()
    base = Path(args.home) if args.home else Path(tempfile.mkdtemp(prefix="fleetledger-demo-"))
    home(base)
    from fleetledger.repo import Repo
    from fleetledger.store import TomlFileStore
    from fleetledger.web import server

    from fleetledger import cli

    store = TomlFileStore(base / "config" / "ships.toml")
    seed_net()
    seed_history()
    seed_operations()
    app = cli.App(store_factory=lambda: store, repo_factory=lambda: Repo(ROOT),
                  ssh_factory=make_ssh, sleep=time.sleep, driver_factory=make_driver(),
                  stdin_isatty=lambda: False, browse=demo_browse, keyscan=demo_keyscan,
                  spawner=demo_spawner)
    from fleetledger import ca

    app.signer = ca.Signer(agent_keys=lambda: set())  # the demo CA's keys; never ssh-agent
    print(f"Demo FleetLedger home: {base}", flush=True)
    extra = {"remote": seed_remote(args.port)} if args.remote else {}
    if args.service:
        extra["service"] = True
    return server.serve(app, port=args.port, open_browser=False, interval=10, **extra)


if __name__ == "__main__":
    raise SystemExit(main())
