"""herdr-core never moves backwards (runtime.core_copies, newest_core; ship-core-start;
status.core_attention).

A Ship can have two herdr-core copies: the pinned one ship-provision installs
(/usr/local/bin/herdr-core, `hc` beside it; in the sandbox the fake behind
root/usr/local/bin/hc, saying scenario hc.version) and a newer one Herdr's own
machine update puts in the agent's ~/.local/bin (`ship.home_core(version)`: the fake
as a plain file there, saying that version). ShipShape runs, and starts at boot,
the newest; the pinned one on a tie. Status says plainly which one runs.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

HOME_CORE = "/home/agent/.local/bin/herdr-core"


@pytest.fixture
def core(ship):
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.use_core()
    return ship


def _core_calls(ship, copy: str) -> list[list[str]]:
    """ship-ctl's calls to one copy, leaving out the `--version` each run asks."""
    name = "hc" if copy == "pinned" else "herdr-core"
    return [c for c in ship.calls(name) if c[1:] != ["--version"]]


def _never_killed(ship) -> None:
    assert not ship.scenario()["hc"].get("forbidden")
    for name in ("hc", "herdr-core"):
        assert not [c for c in ship.calls(name) if c[1:2] == ["kill"]
                    or c[1:3] == ["server", "stop"]]


def _set(ship, **values) -> None:
    s = ship.scenario()
    s["hc"].update(values)
    ship.set(hc=s["hc"])


def _serving(ship, pid: int, exe) -> None:
    """A running `herdr-core serve` whose executable is exe (as /proc shows it)."""
    proc = ship.p(f"/proc/{pid}")
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"herdr-core\0serve\0")
    (proc / "exe").symlink_to(exe)


def _attention(doc, code: str) -> list[dict]:
    return [a for a in doc["attention"] if a["code"] == code]


# --- what ship-ctl runs -------------------------------------------------------------------
def test_ship_ctl_runs_a_newer_copy_herdr_installed(core):
    """Herdr's own update put 0.1.22 in ~/.local/bin; the pin is 0.1.19. Every hc call
    ship-ctl makes (any of which would start a stopped core) runs the newer copy."""
    ship = core
    ship.home_core("0.1.22")
    ship.ctl("apply", "--json")
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert {t["name"] for t in ship.terminals() if t["state"] == "live"} == {"main", "grinder"}
    assert _core_calls(ship, "home") and not _core_calls(ship, "pinned")
    assert ship.status()["herdr"]["version"] == "0.1.22"
    _never_killed(ship)


@pytest.mark.parametrize("home", ["0.1.18", "0.1.19"])
def test_an_older_or_equal_copy_in_the_home_is_not_used(core, home):
    ship = core
    ship.home_core(home)
    ship.ctl("apply", "--json")
    ship.ctl("refresh")
    assert _core_calls(ship, "pinned") and not _core_calls(ship, "home")
    assert ship.status()["herdr"]["version"] == "0.1.19"


def test_a_newer_pin_wins_over_an_older_copy_herdr_installed(core):
    """The pin moved past Herdr's copy (0.1.23 pinned, 0.1.22 in ~/.local/bin)."""
    ship = core
    ship.home_core("0.1.22")
    _set(ship, version="0.1.23")
    ship.ctl("refresh")
    assert _core_calls(ship, "pinned") and not _core_calls(ship, "home")


@pytest.mark.parametrize("how", ["symlink", "group-writable", "not-executable", "bad-version",
                                 "folder-link"])
def test_a_copy_in_the_home_that_is_not_herdrs_install_is_never_run(core, tmp_path, how):
    """Only a plain file the agent alone can change, that says `hc X.Y.Z`, counts."""
    ship = core
    path = ship.home_core("0.1.22")
    if how == "symlink":
        real = tmp_path / "elsewhere"
        real.write_bytes(path.read_bytes())
        real.chmod(0o755)
        path.unlink()
        path.symlink_to(real)
    elif how == "group-writable":
        path.chmod(0o775)
    elif how == "not-executable":
        path.chmod(0o644)
    elif how == "bad-version":
        _set(ship, home_version="0.1.22 (latest)")
    else:  # ~/.local/bin is a link to another folder
        bin_dir = path.parent
        moved = tmp_path / "moved-bin"
        bin_dir.rename(moved)
        bin_dir.symlink_to(moved)
    ship.ctl("refresh")
    assert not ship.calls("herdr-core") or how == "bad-version"  # asked its version only
    assert _core_calls(ship, "pinned") and not _core_calls(ship, "home")


# --- at boot (ship-core.service) ---------------------------------------------------------
def _boot_start(ship) -> subprocess.CompletedProcess:
    """ship-core-start as ship-core.service runs it, with no core running yet."""
    _set(ship, server=False)
    return subprocess.run([sys.executable, "-I", str(ship.bundle / "bin/ship-core-start")],
                          env=ship.env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize(("home", "started"), [
    (None, "pinned"), ("0.1.18", "pinned"), ("0.1.19", "pinned"), ("0.1.22", "home"),
])
def test_the_boot_starts_the_newest_copy(core, home, started):
    """A reboot never takes the core back to the pin when Herdr installed a newer one."""
    ship = core
    if home:
        ship.home_core(home)
    r = _boot_start(ship)
    assert r.returncode == 0, r.stderr
    h = ship.scenario()["hc"]
    assert h["server"] and h["started_from"] == started
    assert h["running_version"] == (home if started == "home" else "0.1.19")
    assert ("~/.local/bin" in r.stderr) == (started == "home")


def test_the_boot_starts_the_pinned_copy_when_the_home_one_is_not_herdrs(core):
    ship = core
    ship.home_core("0.1.22").chmod(0o777)
    r = _boot_start(ship)
    assert r.returncode == 0, r.stderr
    assert ship.scenario()["hc"]["started_from"] == "pinned"


def test_the_boot_falls_back_to_the_pinned_copy(core):
    """When choosing fails (here: the bundle's lib is broken), the pinned core starts."""
    ship = core
    ship.home_core("0.1.22")
    broken = ship.bundle / "lib/shipctl/runtime.py"
    broken.write_text("raise ImportError('broken on purpose')\n")
    r = _boot_start(ship)
    assert r.returncode == 0, r.stderr
    assert "choosing a herdr-core failed" in r.stderr
    assert ship.scenario()["hc"]["started_from"] == "pinned"


# --- what status says ---------------------------------------------------------------------
def test_status_says_plainly_when_herdrs_newer_copy_runs(core):
    """The Ship this was found on: Herdr's machine update restarted the core from
    ~/.local/bin. It is newer than the pin: kept, no reboot offered, said plainly."""
    ship = core
    home = ship.home_core("0.1.22")
    _serving(ship, 4400, home)
    doc = ship.status()
    assert not _attention(doc, "core-restart-pending")
    [item] = _attention(doc, "core-unpinned")
    assert item["subject"] == "herdr-core"
    assert "0.1.22" in item["message"] and "~/.local/bin" in item["message"]
    assert "pinned 0.1.19" in item["message"] and "keeps it" in item["message"]
    assert "until the next reboot" not in item["message"]


def test_status_offers_the_reboot_when_the_running_core_is_older_than_a_copy(core):
    """The pinned core runs (a reboot before this release started it) while Herdr's
    newer copy is installed: the next start runs the newer one, so a reboot moves it
    forward."""
    ship = core
    pinned = ship.p("/usr/local/bin/herdr-core")
    pinned.write_text("pinned herdr-core")
    ship.home_core("0.1.22")
    _serving(ship, 4401, pinned)
    doc = ship.status()
    [item] = _attention(doc, "core-restart-pending")
    assert "0.1.22 is installed" in item["message"] and "older 0.1.19" in item["message"]
    assert not _attention(doc, "core-unpinned")


def test_status_offers_the_reboot_when_the_pin_moved_past_herdrs_copy(core):
    ship = core
    ship.p("/usr/local/bin/herdr-core").write_text("pinned herdr-core")
    home = ship.home_core("0.1.22")
    _set(ship, version="0.1.23")
    _serving(ship, 4402, home)
    [item] = _attention(ship.status(), "core-restart-pending")
    assert "0.1.23 is installed" in item["message"]


def test_status_says_nothing_when_herdrs_copy_is_the_pinned_version(core):
    ship = core
    ship.p("/usr/local/bin/herdr-core").write_text("pinned herdr-core")
    _serving(ship, 4403, ship.home_core("0.1.19"))
    doc = ship.status()
    assert not _attention(doc, "core-unpinned") and not _attention(doc, "core-restart-pending")


def test_status_never_offers_a_reboot_back_to_an_older_core(core, tmp_path):
    """The running core's file is gone (replaced), and it says it is newer than every
    copy installed: a restart would move it back, so no reboot is offered."""
    ship = core
    ship.p("/usr/local/bin/herdr-core").write_text("pinned herdr-core")
    gone = tmp_path / "replaced-core"
    gone.write_text("a core no longer installed")
    _set(ship, running_version="0.1.22")
    _serving(ship, 4404, gone)
    doc = ship.status()
    assert not _attention(doc, "core-restart-pending")
    [item] = _attention(doc, "core-unpinned")
    assert "no installed copy is as new" in item["message"]
    assert "would start 0.1.19" in item["message"]


def test_the_home_copy_is_asked_its_version_as_the_agent(core):
    """ship-ctl (root on a Ship) runs a copy in the agent's home only as the agent."""
    ship = core
    ship.home_core("0.1.22")
    ship.status()
    homes = [line for line in (ship.fake_dir / "homes.jsonl").read_text().splitlines()
             if '"herdr-core"' in line]
    assert homes and all(str(ship.p("/home/agent")) in line for line in homes)
    assert os.stat(ship.p(HOME_CORE)).st_mode & 0o777 == 0o755


def test_a_pre_release_copy_counts_by_its_numbers(core):
    """`hc 0.2.0-rc1` is 0.2.0, as herdr-core compares its own versions."""
    ship = core
    ship.home_core("0.2.0-rc1")
    ship.ctl("refresh")
    assert _core_calls(ship, "home") and not _core_calls(ship, "pinned")


def test_status_asks_the_core_when_its_copy_does_not_say_its_version(core):
    """Herdr's copy runs but did not answer `--version` this time (busy, or a format
    ship-ctl does not read): the running core's own word decides, never silence."""
    ship = core
    ship.p("/usr/local/bin/herdr-core").write_text("pinned herdr-core")
    home = ship.home_core("0.1.22")
    _set(ship, home_version="0.1.22 (busy)", running_version="0.1.22")
    _serving(ship, 4405, home)
    [item] = _attention(ship.status(), "core-unpinned")
    assert "0.1.22" in item["message"]


def test_the_boot_starts_the_pinned_copy_when_the_newer_one_does_not_run(core):
    """The chosen copy says its version, then cannot be started (gone a moment later):
    a core at all comes first, so the pinned one starts."""
    ship = core
    home = ship.home_core("0.1.22")
    script = ship.bundle / "bin/ship-core-start"
    text = script.read_text().replace(
        "        os.execv(command, [command, *start])\n",
        "        os.unlink(command)\n        os.execv(command, [command, *start])\n")
    script.write_text(text)
    r = _boot_start(ship)
    assert r.returncode == 0, r.stderr
    assert "did not start" in r.stderr and not home.exists()
    assert ship.scenario()["hc"]["started_from"] == "pinned"
