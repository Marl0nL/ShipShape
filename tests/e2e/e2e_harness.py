"""End-to-end harness (W11): a throwaway FleetLedger driving real Ships.

Everything goes through the ship-fleet command, run as a separate process the
way a person runs it, against this checkout's ship/ and fleetledger/. The only
thing taken from your config is the [drivers.proxmox] section (and optional
[tag.e2e] expected accounts) of the file SHIPSHAPE_E2E names. Keys are
throwaway, made here; your own keys and inventory are never touched.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "fleetledger"))

from fleetledger.drivers.proxmox import ProxmoxDriver, seed_volume  # noqa: E402
from fleetledger.drivers.proxmox_api import ProxmoxError  # noqa: E402
from fleetledger.inventory import Ship  # noqa: E402
from fleetledger.ssh import SSH  # noqa: E402
from fleetledger.store import TomlFileStore, new_inventory  # noqa: E402


def e2e_config() -> tuple[dict | None, str]:
    """(config, why not) from SHIPSHAPE_E2E: a file with [drivers.proxmox]."""
    path = os.environ.get("SHIPSHAPE_E2E")
    if not path:
        return None, "set SHIPSHAPE_E2E to run"
    file = Path(path).expanduser()
    if not file.is_file():
        return None, f"SHIPSHAPE_E2E: {file} does not exist"
    try:
        config = tomllib.loads(file.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"SHIPSHAPE_E2E: cannot read {file}: {exc}"
    if "proxmox" not in config.get("drivers", {}):
        return None, f"SHIPSHAPE_E2E: {file} has no [drivers.proxmox] section"
    return config, ""


def keygen(path: Path, comment: str) -> Path:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        check=True,
    )
    return path


def log(message: str) -> None:
    print(f"[e2e {time.strftime('%H:%M:%S')}] {message}", flush=True)


class Fleet:
    def __init__(self, workdir: Path, config: dict) -> None:
        self.workdir = workdir
        self.proxmox = config["drivers"]["proxmox"]
        keys = workdir / "keys"
        keys.mkdir(mode=0o700)
        self.admin_key = keygen(keys / "fleet_admin", "e2e-fleet-admin")
        self.agent_key = keygen(keys / "agent", "e2e-agent")
        # A short runtime folder, so SSH connection sharing is on as in real use.
        self.runtime = Path(tempfile.mkdtemp(prefix="fl-e2e-", dir="/tmp"))
        self.env = {
            **os.environ,
            "FLEETLEDGER_CONFIG_DIR": str(workdir / "config"),
            "FLEETLEDGER_DATA_DIR": str(workdir / "data"),
            "FLEETLEDGER_RUNTIME_DIR": str(self.runtime),
            "PYTHONPATH": str(ROOT / "fleetledger"),
            "SHIPSHAPE_REPO": str(ROOT),
        }
        for key in ("FLEETLEDGER_CONFIG_DIR", "FLEETLEDGER_DATA_DIR", "FLEETLEDGER_RUNTIME_DIR"):
            os.environ[key] = self.env[key]  # for the in-process helpers below
        self.store = TomlFileStore(workdir / "config" / "ships.toml")
        doc = new_inventory(str(self.admin_key), [str(self.agent_key) + ".pub"])
        doc["defaults"]["driver"] = "proxmox"
        doc["drivers"] = {"proxmox": self.proxmox}
        if "tag" in config:
            doc["tag"] = config["tag"]
        self.store.create(doc)
        self.driver = ProxmoxDriver(self.proxmox)
        self.attempted: list[str] = []  # Ship names `new` was asked to create

    def close(self) -> None:
        for sock in self.runtime.glob("cm-*"):
            subprocess.run(["ssh", "-O", "exit", "-o", f"ControlPath={sock}", "x"],
                           capture_output=True)
        shutil.rmtree(self.runtime, ignore_errors=True)

    # --- ship-fleet ---------------------------------------------------------
    def cli(self, *args: str, timeout: float = 600, check: bool = True,
            interactive: bool = False) -> subprocess.CompletedProcess:
        if args[:1] == ("new",):
            self.attempted.append(args[1])
        argv = [sys.executable, "-m", "fleetledger.cli", *args]
        log("ship-fleet " + " ".join(args))
        if interactive:
            proc = subprocess.run(argv, env=self.env, timeout=timeout)
        else:
            proc = subprocess.run(argv, env=self.env, capture_output=True, text=True,
                                  timeout=timeout)
            if proc.stdout.strip():
                print(proc.stdout.rstrip(), flush=True)
            if proc.stderr.strip():
                print(proc.stderr.rstrip(), file=sys.stderr, flush=True)
        if check and proc.returncode != 0:
            raise AssertionError(f"ship-fleet {' '.join(args)} exited {proc.returncode}")
        return proc

    def status(self, name: str) -> dict:
        proc = self.cli("status", name, "--json", check=False, timeout=120)
        return json.loads(proc.stdout)

    def ship(self, name: str) -> Ship:
        return self.store.load().ship(name)

    def ssh(self, name: str, *command: str, timeout: float = 120):
        return SSH(self.store.load()).run(self.ship(name), list(command), timeout=timeout)

    # --- waiting --------------------------------------------------------------
    def wait_for(self, what: str, check: Callable[[], object], timeout: float,
                 interval: float = 15) -> object:
        deadline = time.monotonic() + timeout
        while True:
            try:
                value = check()
            except Exception as exc:  # noqa: BLE001 - a Ship mid-boot fails in many ways
                value = None
                last = repr(exc)
            else:
                last = repr(value)[:200]
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {what} (last: {last})")
            time.sleep(interval)

    def ssh_input(self, name: str, text: str, *command: str, timeout: float = 120):
        return SSH(self.store.load()).run(
            self.ship(name), list(command), input=text.encode(), timeout=timeout
        )

    def boot_id(self, name: str) -> str:
        return self.ssh(name, "cat", "/proc/sys/kernel/random/boot_id").text.strip()

    def reboot(self, name: str, timeout: float = 600) -> None:
        """Reboot as FleetLedger does (systemctl reboot, never forced) and wait."""
        before = self.boot_id(name)
        log(f"rebooting {name}")
        try:
            self.ssh(name, "sudo", "-n", "systemctl", "reboot", timeout=30)
        except Exception:  # noqa: BLE001 - the connection may drop as it goes down
            pass
        self.close_connections()
        self.wait_for(f"{name} to come back", lambda: self.boot_id(name) != before, timeout)

    def close_connections(self) -> None:
        for sock in self.runtime.glob("cm-*"):
            subprocess.run(["ssh", "-O", "exit", "-o", f"ControlPath={sock}", "x"],
                           capture_output=True)

    # --- clean-up -------------------------------------------------------------
    def destroy(self, name: str) -> None:
        inv = self.store.load()
        if not inv.has_ship(name):
            return
        ship = inv.ship(name)
        if "vmid" in ship.driver_ref:
            log(f"destroying VM {ship.driver_ref['vmid']}")
            self.driver.destroy(ship)
        with self.store.edit() as ed:
            ed.remove_ship(name)

    def clean_up(self, vmids_before: set[int], seeds_before: set[str]) -> None:
        """Destroy everything this run made, recorded in the inventory or not.

        If `new` died before recording a VMID (killed on a timeout, say), the
        VM is found by its name among the pool members that were not there
        before; leftover seed ISOs are found by the Ship's name.
        """
        for ship in self.store.load().ships():
            self.destroy(ship.name)
        members = self.driver.api.get(f"/pools/{self.proxmox['pool']}").get("members", [])
        for member in members:
            if member.get("vmid") not in vmids_before and member.get("name") in self.attempted:
                log(f"destroying orphan VM {member['vmid']} ({member['name']})")
                self.driver._destroy_vm(int(member["vmid"]), member["name"])
        for volume in self.seed_volumes() - seeds_before:
            if any(volume.split("/", 1)[-1].startswith(f"{n}-") for n in self.attempted):
                log(f"deleting orphan seed {volume}")
                self.driver._delete_volume(volume)

    def fresh_vmid(self) -> int:
        """A free VMID picked at random from vmid_range, so consecutive runs do
        not reuse the number a previous run's VM just gave up."""
        first, last = self.proxmox.get("vmid_range", [9100, 9199])
        candidates = list(range(first, last + 1))
        random.shuffle(candidates)
        for vmid in candidates:
            try:
                self.driver.api.get("/cluster/nextid", vmid=vmid)
            except ProxmoxError as exc:
                if exc.status == 400:  # in use
                    continue
                raise
            return vmid
        raise AssertionError(f"no free VMID in {first}-{last}")

    def pool_vmids(self) -> set[int]:
        members = self.driver.api.get(f"/pools/{self.proxmox['pool']}").get("members", [])
        return {m.get("vmid") for m in members}

    def seed_volumes(self) -> set[str]:
        node, storage = self.proxmox["node"], self.proxmox["seed_storage"]
        return {c["volid"] for c in self.driver.api.get(f"/nodes/{node}/storage/{storage}/content")}

    def seed_volume(self, instance_id: str) -> str:
        return seed_volume(self.proxmox["seed_storage"], instance_id)
