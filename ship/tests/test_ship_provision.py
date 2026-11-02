"""ship-provision (W4), offline: --root sandbox runs, --dry-run, the pinned-key check,
the files it installs, and ship-resume."""

from __future__ import annotations

import configparser
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SHIP = Path(__file__).resolve().parents[1]
PROVISION = SHIP / "provision" / "ship-provision"
FILES = SHIP / "files"


def provision(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **(env or {})}
    return subprocess.run(
        [str(PROVISION), *args], env=full, capture_output=True, text=True, timeout=60
    )


def mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.lstat().st_mode))


def test_first_boot_in_sandbox_is_idempotent(tmp_path):
    first = provision("--first-boot", "--root", str(tmp_path))
    assert first.returncode == 0, first.stdout + first.stderr
    assert "done: 0 change(s)" not in first.stdout
    second = provision("--first-boot", "--root", str(tmp_path))
    assert second.returncode == 0
    assert second.stdout.rstrip().endswith("done: 0 change(s)"), second.stdout
    assert "changed:" not in second.stdout

    root = tmp_path
    assert (root / "opt/shipshape/shim/claude").read_bytes() == (
        FILES / "shim/claude.py"
    ).read_bytes()
    assert mode(root / "opt/shipshape/shim/claude") == "0o755"
    assert mode(root / "opt/shipshape/hooks/mode-ledger") == "0o755"
    assert os.readlink(root / "usr/local/bin/ship-ctl") == "/opt/shipshape/current/bin/ship-ctl"
    axi = os.readlink(root / "usr/local/bin/ship-axi")
    assert axi == "/opt/shipshape/current/axi/bin/ship-axi.js"
    assert (root / "etc/ssh/sshd_config.d/10-shipshape.conf").exists()
    assert (root / "etc/systemd/logind.conf.d/50-shipshape.conf").exists()
    assert (root / "etc/systemd/journald.conf.d/50-shipshape.conf").exists()
    assert (root / "etc/systemd/user/herdr.service").exists()
    assert (root / "etc/systemd/system/ship-status.timer").exists()
    assert mode(root / "home/agent/.claude") == "0o700"
    assert mode(root / "home/agent/.local/state/shipshape") == "0o700"
    assert (root / "home/agent/.npmrc").read_text() == "prefix=/home/agent/.local\n"
    record = json.loads((root / "var/lib/shipshape/provision.json").read_text())
    assert record["result"] == "ok" and record["mode"] == "first-boot"
    assert mode(root / "var/lib/shipshape/provision.json") == "0o644"


def test_apt_sources_from_versions(tmp_path):
    assert provision("--converge", "--root", str(tmp_path)).returncode == 0
    sources = tmp_path / "etc/apt/sources.list.d"
    assert sorted(p.name for p in sources.iterdir()) == [
        "shipshape-github-cli.sources",
        "shipshape-google-chrome.sources",
        "shipshape-google-cloud.sources",
        "shipshape-nodesource.sources",
    ]
    chrome = (sources / "shipshape-google-chrome.sources").read_text()
    assert "URIs: https://dl.google.com/linux/chrome/deb\n" in chrome
    assert "Architectures: amd64\n" in chrome
    assert "Signed-By: /etc/apt/keyrings/shipshape-google-chrome.gpg\n" in chrome
    node = (sources / "shipshape-nodesource.sources").read_text()
    assert "Suites: nodistro\n" in node and "Architectures" not in node


def test_dry_run_writes_nothing(tmp_path):
    r = provision("--first-boot", "--dry-run", "--root", str(tmp_path))
    assert r.returncode == 0
    assert "would change: /etc/ssh/sshd_config.d/10-shipshape.conf" in r.stdout
    assert list(tmp_path.iterdir()) == []


def test_dry_run_spots_a_drifted_file(tmp_path):
    provision("--converge", "--root", str(tmp_path))
    conf = tmp_path / "etc/ssh/sshd_config.d/10-shipshape.conf"
    conf.write_text("PasswordAuthentication yes\n")
    r = provision("--converge", "--dry-run", "--root", str(tmp_path))
    assert "would change: /etc/ssh/sshd_config.d/10-shipshape.conf" in r.stdout
    assert "1 change(s) would be made" in r.stdout
    assert conf.read_text() == "PasswordAuthentication yes\n"
    provision("--converge", "--root", str(tmp_path))
    assert conf.read_bytes() == (FILES / "ssh/10-shipshape.conf").read_bytes()


@pytest.mark.parametrize("args", [[], ["--first-boot", "--bogus"], ["--root"]])
def test_bad_arguments(args):
    assert provision(*args).returncode == 2


def test_help():
    r = provision("--help")
    assert r.returncode == 0 and "--first-boot" in r.stdout and "set -e" not in r.stdout


def test_not_root_without_sandbox():
    if os.geteuid() == 0:
        pytest.skip("running as root")
    r = provision("--converge")
    assert r.returncode == 1 and "run as root" in r.stderr


# --- the pinned-key check ------------------------------------------------------------
def _keyring(home: Path, uids: list[str]) -> tuple[Path, list[str]]:
    env = {"GNUPGHOME": str(home), "PATH": "/usr/bin:/bin"}
    home.mkdir(mode=0o700)
    fprs = []
    for uid in uids:
        subprocess.run(
            [
                "gpg",
                "--batch",
                "--passphrase",
                "",
                "--quick-gen-key",
                uid,
                "ed25519",
                "sign",
                "never",
            ],
            env=env,
            check=True,
            capture_output=True,
        )
    out = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--list-keys"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    take = False
    for line in out.splitlines():
        parts = line.split(":")
        if parts[0] == "pub":
            take = True
        elif parts[0] == "fpr" and take:
            fprs.append(parts[9])
            take = False
    ring = home / "ring.gpg"
    ring.write_bytes(
        subprocess.run(
            ["gpg", "--batch", "--export"], env=env, check=True, capture_output=True
        ).stdout
    )
    return ring, fprs


@pytest.mark.skipif(shutil.which("gpg") is None, reason="needs gpg")
def test_keyring_must_hold_only_pinned_keys(tmp_path):
    ring, fprs = _keyring(tmp_path / "gpg", ["one@example.com", "two@example.com"])

    def ok(allowed: list[str]) -> bool:
        script = f'source "{PROVISION}"; keyring_ok "$1" "$2"'
        r = subprocess.run(
            ["bash", "-c", script, "x", str(ring), "\n".join(allowed)],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0

    assert ok(fprs)
    assert not ok(fprs[:1])  # an extra, unpinned key is refused
    assert not ok(["0" * 40])
    ring.write_bytes(b"")
    assert not ok(fprs)  # no key at all


# --- installed files ---------------------------------------------------------------------
def unit(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(path)
    return cp


def test_user_units():
    herdr = unit(FILES / "systemd/user/herdr.service")
    assert herdr["Unit"]["ConditionUser"] == "agent"
    assert herdr["Service"]["ExecStart"] == "/usr/local/bin/herdr server"
    text = (FILES / "systemd/user/herdr.service").read_text()
    assert "Environment=PATH=/opt/shipshape/shim:%h/.local/bin:" in text  # shim first
    assert herdr["Install"]["WantedBy"] == "default.target"
    resume = unit(FILES / "systemd/user/ship-resume.service")
    assert resume["Unit"]["After"] == "herdr.service"
    assert resume["Service"]["Type"] == "oneshot"
    assert "ship_resume.py" in resume["Service"]["ExecStart"]


def test_status_timer():
    service = unit(FILES / "systemd/system/ship-status.service")
    assert service["Service"]["ExecStart"] == "/usr/local/bin/ship-ctl refresh"
    timer = unit(FILES / "systemd/system/ship-status.timer")
    assert timer["Timer"]["OnUnitActiveSec"] == "30s"


def test_sshd_drop_in_matches_contract():
    text = (FILES / "ssh/10-shipshape.conf").read_text()
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert lines == [
        "HostKey /etc/ssh/ssh_host_ed25519_key",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitRootLogin no",
        "AllowUsers fleet agent",
        "AllowAgentForwarding no",
    ]


def test_profile_snippet(tmp_path):
    home = tmp_path / "home"
    (home / ".config/shipshape").mkdir(parents=True)
    (home / ".config/shipshape/env").write_text("EXAMPLE_PATH='/x/y z'\n")
    script = (
        f'. "{FILES / "profile.d/shipshape.sh"}"; echo "$PATH|$npm_config_prefix|$EXAMPLE_PATH"'
    )
    fake_id = tmp_path / "bin"
    fake_id.mkdir()
    (fake_id / "id").write_text("#!/bin/sh\necho agent\n")
    (fake_id / "id").chmod(0o755)
    r = subprocess.run(
        ["sh", "-c", script],
        env={"HOME": str(home), "PATH": f"{fake_id}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    path, prefix, example = r.stdout.strip().split("|")
    assert path.startswith(f"{home}/.local/bin:")
    assert prefix == f"{home}/.local" and example == "/x/y z"


# --- ship-resume ---------------------------------------------------------------------------
def load_resume():
    spec = importlib.util.spec_from_file_location("ship_resume", FILES / "libexec/ship_resume.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_ship_resume_attaches_waits_and_records(tmp_path, monkeypatch):
    mod = load_resume()
    calls = []
    lists = iter(
        [None]
        + [[{"pane_id": "w1:p1", "agent": "claude", "agent_status": "unknown"}]] * 2
        + [[{"pane_id": "w1:p1", "agent": "claude", "agent_status": "idle"}]] * 10
    )

    def fake_run(argv, timeout=15):
        calls.append(argv)
        rc = 1 if argv[-3:] == ["has-session", "-t", "resume"] else 0
        return subprocess.CompletedProcess(argv, rc, "", "")

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "agent_list", lambda: next(lists))
    monkeypatch.setattr(mod, "dns_ok", lambda: True)
    monkeypatch.setattr(mod, "boot_id", lambda: "boot-1")
    monkeypatch.setenv("HOME", str(tmp_path))
    clock = FakeClock()
    assert mod.main(clock=clock, sleep=clock.sleep) == 0
    new_session = next(c for c in calls if "new-session" in c)
    assert new_session[:3] == ["tmux", "-L", "ship-resume"] and new_session[-1] == "herdr"
    assert ["tmux", "-L", "ship-resume", "kill-server"] == calls[-1]
    rec = json.loads((tmp_path / ".local/state/shipshape/last-resume.json").read_text())
    assert rec["method"] == "hidden-client" and rec["resumed"] == 1 and rec["failed"] == 0
    assert rec["boot_id"] == "boot-1" and rec["at"].endswith("Z")


def test_ship_resume_gives_up_without_herdr(tmp_path, monkeypatch):
    mod = load_resume()
    monkeypatch.setattr(mod, "agent_list", lambda: None)
    monkeypatch.setattr(mod, "dns_ok", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path))
    clock = FakeClock()
    assert mod.main(clock=clock, sleep=clock.sleep) == 1
    assert not (tmp_path / ".local/state/shipshape/last-resume.json").exists()


def test_resume_record_feeds_status(tmp_path):
    """The record ship-resume writes is what ship-ctl reports as herdr.last_resume."""
    sys.path.insert(0, str(SHIP / "lib"))
    from shipctl.context import Context
    from shipctl.paths import Paths
    from shipctl.status import last_resume

    home = tmp_path / "home/agent/.local/state/shipshape"
    home.mkdir(parents=True)
    (home / "last-resume.json").write_text(
        json.dumps(
            {
                "at": "2026-01-01T00:00:00Z",
                "method": "hidden-client",
                "resumed": 2,
                "failed": 0,
                "boot_id": "b",
            }
        )
    )
    ctx = Context(Paths(tmp_path, SHIP))
    assert last_resume(ctx) == {
        "at": "2026-01-01T00:00:00Z",
        "method": "hidden-client",
        "resumed": 2,
        "failed": 0,
    }


def test_agent_home_symlinks_are_replaced_not_followed(tmp_path):
    """On a Ship these steps run as the agent; even so, a planted link is never followed."""
    root = tmp_path / "root"
    home = root / "home/agent"
    home.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o750)
    outside.chmod(0o750)
    victim = tmp_path / "victim"
    victim.write_text("ROOT-ONLY\n")
    (home / ".local").symlink_to(outside)
    (home / ".npmrc").symlink_to(victim)
    r = provision("--converge", "--root", str(root))
    assert r.returncode == 0, r.stdout + r.stderr
    assert victim.read_text() == "ROOT-ONLY\n"
    assert mode(outside) == "0o750" and list(outside.iterdir()) == []
    assert not (home / ".local").is_symlink() and (home / ".local/state/shipshape").is_dir()
    assert not (home / ".npmrc").is_symlink()
    assert (home / ".npmrc").read_text() == "prefix=/home/agent/.local\n"


# --- review follow-ups ------------------------------------------------------------------
def test_claude_folder_symlink_and_home_mode(tmp_path):
    root = tmp_path / "root"
    home = root / "home/agent"
    home.mkdir(parents=True, mode=0o750)
    home.chmod(0o750)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    (home / ".claude").symlink_to(outside)
    r = provision("--converge", "--root", str(root))
    assert r.returncode == 0, r.stdout + r.stderr
    assert mode(outside) == "0o755"  # not re-moded through the link
    assert not (home / ".claude").is_symlink() and mode(home / ".claude") == "0o700"
    assert mode(home) == "0o750"  # the home folder keeps its own mode


def test_mode_drift_is_repaired(tmp_path):
    provision("--converge", "--root", str(tmp_path))
    shim = tmp_path / "opt/shipshape/shim/claude"
    shim.chmod(0o700)
    r = provision("--converge", "--root", str(tmp_path))
    assert "changed: /opt/shipshape/shim/claude" in r.stdout
    assert r.stdout.rstrip().endswith("done: 1 change(s)")
    assert mode(shim) == "0o755"


def test_a_failed_run_records_failed(tmp_path):
    (tmp_path / "opt").mkdir()
    (tmp_path / "opt/shipshape").write_text("not a folder")
    r = provision("--converge", "--root", str(tmp_path))
    assert r.returncode != 0
    record = json.loads((tmp_path / "var/lib/shipshape/provision.json").read_text())
    assert record["result"] == "failed"


def test_units_are_enabled(tmp_path):
    if shutil.which("systemctl") is None:
        pytest.skip("needs systemctl")
    provision("--first-boot", "--root", str(tmp_path))
    assert (tmp_path / "etc/systemd/system/timers.target.wants/ship-status.timer").is_symlink()
    for unit in ("herdr.service", "ship-resume.service"):
        assert (tmp_path / "etc/systemd/user/default.target.wants" / unit).is_symlink()


def test_root_slash_is_refused(tmp_path):
    for value in ("/", "//", f"{tmp_path}/../../../../../../.."):
        r = provision("--converge", "--root", value)
        assert r.returncode == 1 and "--root / would change this machine" in r.stderr
    r = provision("--converge", env={"SHIPSHAPE_ROOT": "/"})
    assert r.returncode == 1


@pytest.mark.skipif(shutil.which("gpg") is None, reason="needs gpg")
def test_keyring_match_is_exact(tmp_path):
    ring, fprs = _keyring(tmp_path / "gpg", ["one@example.com"])
    script = f'source "{PROVISION}"; keyring_ok "$1" "$2"'
    for allowed in (fprs[0][1:], "X" + fprs[0], fprs[0] + "0", fprs[0].lower()):
        r = subprocess.run(["bash", "-c", script, "x", str(ring), allowed], capture_output=True)
        assert r.returncode != 0, allowed


def test_resume_service_waits_for_herdr():
    resume = unit(FILES / "systemd/user/ship-resume.service")
    assert resume["Unit"]["Requires"] == "herdr.service"
    assert resume["Unit"]["ConditionUser"] == "agent"


def test_profile_snippet_ignores_other_users(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "id").write_text("#!/bin/sh\necho fleet\n")
    (bindir / "id").chmod(0o755)
    path = f"{bindir}:/usr/bin:/bin"
    script = f'. "{FILES / "profile.d/shipshape.sh"}"; echo "$PATH|${{npm_config_prefix:-}}"'
    r = subprocess.run(
        ["sh", "-c", script],
        env={"HOME": str(tmp_path), "PATH": path},
        capture_output=True,
        text=True,
    )
    assert r.stdout.strip() == f"{path}|"


def test_ship_resume_waits_for_launches_and_counts_failures(tmp_path, monkeypatch):
    mod = load_resume()
    pending = [
        {"pane_id": "w1:p1", "agent": "claude", "agent_status": "idle", "launch_pending": True}
    ]
    final = [
        {"pane_id": "w1:p1", "agent": "claude", "agent_status": "idle"},
        {"pane_id": "w2:p1", "agent": "claude", "agent_status": "unknown"},
    ]
    lists = iter([pending] * 5 + [final] * 100)
    monkeypatch.setattr(
        mod,
        "run",
        lambda argv, timeout=15: subprocess.CompletedProcess(
            argv, 1 if "has-session" in argv else 0, "", ""
        ),
    )
    monkeypatch.setattr(mod, "agent_list", lambda: next(lists))
    dns = iter([False, False, True] + [True] * 100)
    monkeypatch.setattr(mod, "dns_ok", lambda: next(dns))
    monkeypatch.setattr(mod, "boot_id", lambda: "b")
    monkeypatch.setenv("HOME", str(tmp_path))
    clock = FakeClock()
    assert mod.main(clock=clock, sleep=clock.sleep) == 0
    rec = json.loads((tmp_path / ".local/state/shipshape/last-resume.json").read_text())
    assert rec["resumed"] == 1 and rec["failed"] == 1
    assert clock.t >= 2  # waited for DNS


# --- install-claude.sh against a fake release server ----------------------------------------
FAKE_CURL = """#!/bin/sh
# Serve https:// URLs from $FIXTURE: the path after the host.
out=""
url=""
while [ $# -gt 0 ]; do
  case $1 in
    -o) out=$2; shift ;;
    https://*) url=$1 ;;
  esac
  shift
done
file="$FIXTURE/${url#https://*/}"
[ -f "$file" ] || exit 22
if [ -n "$out" ]; then cp "$file" "$out"; else cat "$file"; fi
"""

DUMMY_CLAUDE = """#!/bin/sh
if [ "$1" = install ]; then
  mkdir -p "$HOME/.local/bin"
  printf '#!/bin/sh\\necho "%s (Claude Code)"\\n' "$2" > "$HOME/.local/bin/claude"
  chmod +x "$HOME/.local/bin/claude"
fi
"""


@pytest.fixture
def release(tmp_path):
    for tool in ("gpg", "gpgv", "jq", "sha256sum"):
        if shutil.which(tool) is None:
            pytest.skip(f"needs {tool}")
    fixture = tmp_path / "fixture"
    gnupg = tmp_path / "gpg"
    ring, fprs = _keyring(gnupg, ["release@example.com"])
    env = {"GNUPGHOME": str(gnupg), "PATH": "/usr/bin:/bin"}
    (fixture / "keys").mkdir(parents=True)
    (fixture / "keys/claude-code.asc").write_bytes(
        subprocess.run(
            ["gpg", "--batch", "--armor", "--export"], env=env, check=True, capture_output=True
        ).stdout
    )
    rel = fixture / "rel"
    (rel / "2.1.316/linux-x64").mkdir(parents=True)
    (rel / "latest").write_text("2.1.316\n")
    binary = rel / "2.1.316/linux-x64/claude"
    binary.write_text(DUMMY_CLAUDE)
    import hashlib

    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    manifest = rel / "2.1.316/manifest.json"
    manifest.write_text(
        json.dumps({"version": "2.1.316", "platforms": {"linux-x64": {"checksum": digest}}})
    )
    subprocess.run(
        ["gpg", "--batch", "--yes", "--detach-sign", "-o", str(manifest) + ".sig", str(manifest)],
        env=env,
        check=True,
        capture_output=True,
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(FAKE_CURL)
    (bindir / "curl").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()

    def install(fingerprint=fprs[0], minimum="2.1.284"):
        return subprocess.run(
            [
                "bash",
                str(SHIP / "provision/install-claude.sh"),
                "https://downloads.example/rel",
                "latest",
                fingerprint,
                minimum,
                "linux-x64",
            ],
            env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "FIXTURE": str(fixture)},
            capture_output=True,
            text=True,
            timeout=60,
        )

    return install, home, manifest


def test_install_claude_verifies_and_installs(release):
    install, home, _ = release
    r = install()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("2.1.316 (Claude Code)")
    assert (home / ".local/bin/claude").exists()


def test_install_claude_refuses_old_version(release):
    install, home, _ = release
    r = install(minimum="9.0.0")
    assert r.returncode == 1 and "below the minimum" in r.stderr
    assert not (home / ".local/bin/claude").exists()


def test_install_claude_refuses_unpinned_key(release):
    install, home, _ = release
    r = install(fingerprint="0" * 40)
    assert r.returncode == 1 and "not the pinned key" in r.stderr


def test_install_claude_refuses_tampered_manifest(release):
    install, home, manifest = release
    manifest.write_text(manifest.read_text().replace("2.1.316", "2.1.317", 1))
    r = install()
    assert r.returncode == 1 and "signature check failed" in r.stderr
    assert not (home / ".local/bin/claude").exists()
