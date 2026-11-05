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
    "herdr",
    "claude",
    "gcloud",
    "gh",
    "git",
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
]
ZONES = ("UTC", "Etc/UTC", "Europe/Berlin", "America/New_York")  # tzdata in the sandbox
BOOT_ID = "0b6f7e0e-0000-4000-8000-000000000001"

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
    parts = ["lib", "bin", "schemas", "files"]
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
    resume = root / "home/agent/.local/state/shipshape"
    resume.mkdir(parents=True)
    (resume / "last-resume.json").write_text(
        json.dumps(
            {
                "at": "2026-01-01T00:00:00Z",
                "method": "hidden-client",
                "resumed": 0,
                "boot_id": BOOT_ID,
            }
        )
    )
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
