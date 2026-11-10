"""Test harness for the Ship track: a sandbox Ship in a temporary folder.

`ship` builds a fake root (SHIPSHAPE_ROOT) with ship.toml, the agent's home, a fake
/proc and a bin folder whose herdr, claude, gh, gcloud, apt-get, npm, ... are
ship/tests/fakes/fake.py. PATH holds only that folder plus the system folders, and
the fakes shadow every command ship-ctl runs, so no test reaches a real tool.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHIP = REPO / "ship"
FAKE = Path(__file__).resolve().parent / "fakes" / "fake.py"
FAKE_COMMANDS = [
    "example-cli",
    "herdr",
    "hc",
    "claude",
    "gcloud",
    "gh",
    "git",
    "useradd",
    "findmnt",
    "lsblk",
    "growpart",
    "resize2fs",
    "npm",
    "dpkg-query",
    "apt-get",
    "cloud-init",
    "systemctl",
    "journalctl",
    "visudo",
    "sshd",
    "vnstat",
    "nft",
    "ip",
    "timedatectl",
    "logger",
    "getent",
]
ZONES = ("UTC", "Etc/UTC", "Europe/Berlin", "America/New_York")  # tzdata in the sandbox
BOOT_ID = "0b6f7e0e-0000-4000-8000-000000000001"
LEDGER = "/home/agent/.local/state/shipshape/modes.json"

BASIC_TOML = """\
schema = 1

[ship]
name = "ship-01"
id = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
tags = ["work"]

[claude]
allow_bypass = true

[[autostart]]
name = "main"
cwd = "~/work/main"
prompt = "Get oriented."

[[autostart]]
name = "grinder"
cwd = "~/work/grinder"
mode = "bypassPermissions"
"""


def make_bundle(dest: Path, catalogue: bool = False) -> Path:
    """A tooling bundle laid out as on a Ship: ship/ contents plus versions.toml.

    With `catalogue`, the real recipes, profiles and skills come along; otherwise
    tools/ and profiles/ start empty for a test to fill.
    """
    parts = ["lib", "bin", "schemas", "files", "signins"]
    if catalogue:
        parts += ["tools", "profiles", "skills"]
    for part in parts:
        shutil.copytree(
            SHIP / part, dest / part, ignore=shutil.ignore_patterns("__pycache__", "node_modules")
        )
    for part in ("tools", "profiles", "provision"):
        (dest / part).mkdir(exist_ok=True)
    shutil.copy(REPO / "versions.toml", dest / "versions.toml")
    return dest


@dataclass
class Ship:
    root: Path
    bundle: Path
    fake_dir: Path
    env: dict

    # --- scenario -----------------------------------------------------------------
    def scenario(self) -> dict:
        return json.loads((self.fake_dir / "scenario.json").read_text())

    def set(self, **kwargs) -> None:
        s = self.scenario()
        s.update(kwargs)
        (self.fake_dir / "scenario.json").write_text(json.dumps(s))

    def homes(self, command: str) -> list[tuple[list[str], str | None]]:
        """(argv, HOME) for each call of `command`: whose environment it ran in."""
        path = self.fake_dir / "homes.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [(r["argv"], r["home"]) for r in rows if r["argv"][0] == command]

    def calls(self, command: str | None = None) -> list[list[str]]:
        path = self.fake_dir / "calls.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [r for r in rows if command is None or r[0] == command]

    # --- what happens to agents -----------------------------------------------------
    def agent(self, name: str) -> dict:
        return next(a for a in self.scenario()["herdr"]["agents"] if a.get("name") == name)

    def crash(self, name: str) -> None:
        """claude dies in its pane (killed, out of memory): no SessionEnd, and the
        pane stays, back at its shell."""
        s = self.scenario()
        s["herdr"]["agents"] = [a for a in s["herdr"]["agents"] if a.get("name") != name]
        self.set(herdr=s["herdr"])

    def exit(self, name: str, reason: str = "prompt_input_exit") -> None:
        """Someone exits the agent (/exit): Claude runs the SessionEnd hook first."""
        ag = self.agent(name)
        self.crash(name)
        self.end_session(ag["agent_session"]["value"], reason)

    def end_session(self, session: str, reason: str) -> None:
        path = self.p(LEDGER)
        doc = json.loads(path.read_text()) if path.exists() else {
            "schema": "shipshape.modes/1", "sessions": {}}
        entry = doc["sessions"].setdefault(session, {"updated_at": "2026-01-01T00:00:00Z"})
        from datetime import UTC, datetime

        entry["ended"] = {"reason": reason,
                          "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
        path.write_text(json.dumps(doc))

    def reboot(self, uptime: float = 40.0) -> None:
        """A new boot: herdr's server starts again and restores every pane under its
        old id, a fresh shell in its folder, with no agent in it (ShipShape turns
        herdr's own resume off)."""
        s = self.scenario()
        h = s["herdr"]
        known = {p["pane_id"] for p in h.get("panes", [])}
        h.setdefault("panes", []).extend(
            {"pane_id": a["pane_id"], "workspace_id": a.get("workspace_id"), "cwd": a.get("cwd")}
            for a in h["agents"] if a.get("pane_id") and a["pane_id"] not in known)
        h["agents"] = []
        h["exited"] = []
        self.set(herdr=h)
        boot = self.p("/proc/sys/kernel/random/boot_id")
        n = int(boot.read_text().strip()[-4:], 16) + 1
        boot.write_text(BOOT_ID[:-4] + f"{n:04x}\n")
        self.p("/proc/uptime").write_text(f"{uptime} 1.0\n")
        run = self.p("/run")
        if run.exists():
            shutil.rmtree(run)  # /run is cleared at boot

    def record(self) -> dict:
        return self.json("/var/lib/shipshape/agents.json")

    # --- herdr-core (ship.toml runtime = "core") ---------------------------------------
    def use_core(self) -> None:
        """Make this Ship a herdr-core Ship (ship.toml runtime = "core")."""
        text = self.p("/etc/shipshape/ship.toml").read_text()
        self.write_toml(text.replace("schema = 1\n", 'schema = 1\nruntime = "core"\n', 1))
        s = self.scenario()
        s.setdefault("hc", {"server": True, "terminals": []})
        self.set(hc=s["hc"])

    def terminals(self, name: str | None = None) -> list[dict]:
        terms = self.scenario().get("hc", {}).get("terminals", [])
        return [t for t in terms if name is None or t.get("name") == name]

    def terminal(self, name: str) -> dict:
        """The live terminal by that name (else its last one)."""
        terms = self.terminals(name)
        return next((t for t in terms if t["state"] == "live"), terms[-1])

    def _core_change(self, change) -> None:
        s = self.scenario()
        for t in s["hc"]["terminals"]:
            change(t)
        self.set(hc=s["hc"])

    def core_status(self, name: str, state: str | None, kind: str | None = None,
                    msg: str | None = None) -> None:
        """Claude reports a state (OSC 7501); None: it has not reported yet."""
        status = None if state is None else {"app": "claude-code", "state": state,
                                             "kind": kind, **({"msg": msg} if msg else {})}

        def change(t):
            if t.get("name") == name and t["state"] == "live":
                t["status"] = status
        self._core_change(change)

    def core_clear(self, name: str, session: str, mode: str) -> None:
        """/clear in the agent's terminal: its session ends (clear) and a new one
        starts there, in its terminal (the mode-ledger hook records the address),
        then the person switches it to mode."""
        from datetime import UTC, datetime

        s = self.scenario()
        t = next(t for t in s["hc"]["terminals"] if t.get("name") == name and t["state"] == "live")
        old, t["session"] = t["session"], session
        self.set(hc=s["hc"])
        self.end_session(old, "clear")
        path = self.p(LEDGER)
        doc = json.loads(path.read_text())
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc["sessions"][session] = {"mode": mode, "started_at": now, "updated_at": now,
                                    "terminal": t["address"]}
        path.write_text(json.dumps(doc))

    def core_crash(self, name: str, how: str = "signal", recover_in: int = 1) -> None:
        """claude dies in its terminal without a SessionEnd. signal (killed, out of
        memory): the kept terminal loses its program and gets a shell after
        recover_in polls. exit: claude ended with an exit code, which finishes its
        terminal."""
        def change(t):
            if t.get("name") != name or t["state"] != "live":
                return
            if how == "exit":
                t.update(state="finished", exit_code=1, reason="exited", foreground=None,
                         status=None)
            else:
                t.update(foreground=None, status=None, recover_in=recover_in)
        self._core_change(change)

    def core_holder_lost(self, recover_in: int = 1) -> None:
        """The one holder process dies: every kept terminal loses its program at once."""
        def change(t):
            if t["state"] == "live":
                t.update(foreground=None, status=None, recover_in=recover_in)
        self._core_change(change)

    def core_reboot(self, uptime: float = 40.0, recover_in: int = 1) -> None:
        """A new boot: the core starts again; every kept terminal comes back under its
        address and name, without its program, and gets a shell after recover_in
        polls; any other terminal has finished."""
        def change(t):
            if t["state"] != "live":
                return
            if t.get("keep"):
                t.update(foreground=None, status=None, recover_in=recover_in)
            else:
                t.update(state="finished", reason="host_shutdown", foreground=None, status=None)
        self._core_change(change)
        boot = self.p("/proc/sys/kernel/random/boot_id")
        n = int(boot.read_text().strip()[-4:], 16) + 1
        boot.write_text(BOOT_ID[:-4] + f"{n:04x}\n")
        self.p("/proc/uptime").write_text(f"{uptime} 1.0\n")
        run = self.p("/run")
        if run.exists():
            shutil.rmtree(run)

    # --- files ----------------------------------------------------------------------
    def p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def write_toml(self, text: str) -> None:
        self.p("/etc/shipshape/ship.toml").write_text(text)

    def json(self, path: str):
        return json.loads(self.p(path).read_text())

    # --- running ship-ctl ---------------------------------------------------------
    def ctl(self, *args: str, input: str | None = None) -> tuple[int, dict | None, str]:
        proc = subprocess.run(
            [sys.executable, "-I", str(self.bundle / "bin/ship-ctl"), *args],
            env=self.env,
            input=input,
            capture_output=True,
            text=True,
            timeout=60,
        )
        doc = None
        if "--json" in args:
            doc = json.loads(proc.stdout)
        return proc.returncode, doc, proc.stdout + proc.stderr


@pytest.fixture
def ship(tmp_path: Path) -> Ship:
    root = tmp_path / "root"
    bundle = make_bundle(tmp_path / "bundle")
    fake_dir = tmp_path / "fake"
    bindir = tmp_path / "bin"
    for d in (
        fake_dir,
        bindir,
        root / "etc/shipshape",
        root / "home/agent",
        root / "proc/sys/kernel/random",
    ):
        d.mkdir(parents=True)
    for name in FAKE_COMMANDS:
        (bindir / name).symlink_to(FAKE)
    (fake_dir / "scenario.json").write_text(
        json.dumps(
            {
                "herdr": {"server": True, "agents": []},
                "claude": {"logged_in": True, "email": "work@example.com"},
                "gcloud": {"account": "work@example.com", "adc": True},
                "gh": {"login": "example-user"},
            }
        )
    )
    (root / "etc/shipshape/ship.toml").write_text(BASIC_TOML)
    for zone in ZONES:
        zone_file = root / "usr/share/zoneinfo" / zone
        zone_file.parent.mkdir(parents=True, exist_ok=True)
        zone_file.write_bytes(b"TZif2")
    (root / "proc/uptime").write_text("4000.5 100.0\n")
    (root / "proc/loadavg").write_text("0.40 0.30 0.20 1/100 999\n")
    (root / "proc/meminfo").write_text("MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n")
    (root / "proc/sys/kernel/random/boot_id").write_text(BOOT_ID + "\n")
    state = root / "var/lib/shipshape"
    state.mkdir(parents=True)
    (state / "provision.json").write_text(
        json.dumps({"result": "ok", "last_run": "2026-01-01T00:00:00Z"})
    )
    (root / "home/agent/.local/state/shipshape").mkdir(parents=True)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "SHIPSHAPE_ROOT": str(root),
        "SHIPSHAPE_BUNDLE": str(bundle),
        "FAKE_DIR": str(fake_dir),
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
    }
    return Ship(root, bundle, fake_dir, env)


def validator(name: str):
    from jsonschema import Draft202012Validator

    return Draft202012Validator(
        json.loads((REPO / "contracts" / f"{name}.schema.json").read_text())
    )


def assert_valid(name: str, doc: dict) -> None:
    errors = sorted(validator(name).iter_errors(doc), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("HERDR_", "SHIPSHAPE_")):
            monkeypatch.delenv(key, raising=False)
