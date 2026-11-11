"""ship-provision (W4), offline: --root sandbox runs, --dry-run, the pinned-key check,
and the files it installs."""

from __future__ import annotations

import configparser
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from ship_harness import PROVISION, provision

SHIP = Path(__file__).resolve().parents[1]
FILES = SHIP / "files"


def mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.lstat().st_mode))


def test_guest_agent_starts_right_after_base_packages(tmp_path):
    """The hypervisor reads the address from the guest agent: start it before the slow steps."""
    lines = provision("--first-boot", "--root", str(tmp_path)).stdout.splitlines()
    start = lines.index("ship-provision: skipped (--root): systemctl start qemu-guest-agent")
    assert "apt packages" in lines[start - 1] and "qemu-guest-agent" in lines[start - 1]
    assert "keyrings" in lines[start + 1]  # before the apt repos, herdr, claude, tools


def test_first_boot_in_sandbox_is_idempotent(tmp_path):
    first = provision("--first-boot", "--root", str(tmp_path))
    assert first.returncode == 0, first.stdout + first.stderr
    assert "done: 0 change(s)" not in first.stdout
    second = provision("--first-boot", "--root", str(tmp_path))
    assert second.returncode == 0
    assert second.stdout.rstrip().endswith("done: 0 change(s)"), second.stdout
    assert "changed:" not in second.stdout

    root = tmp_path
    assert not (root / "opt/shipshape/shim").exists()  # ship-ctl resumes agents itself
    assert mode(root / "opt/shipshape/hooks/mode-ledger") == "0o755"
    assert os.readlink(root / "usr/local/bin/ship-ctl") == "/opt/shipshape/current/bin/ship-ctl"
    axi = os.readlink(root / "usr/local/bin/ship-axi")
    assert axi == "/opt/shipshape/current/axi/bin/ship-axi.js"
    assert (root / "etc/ssh/sshd_config.d/10-shipshape.conf").exists()
    assert (root / "etc/systemd/logind.conf.d/50-shipshape.conf").exists()
    assert (root / "etc/systemd/journald.conf.d/50-shipshape.conf").exists()
    assert (root / "etc/systemd/user/herdr.service").exists()
    assert (root / "etc/systemd/system/ship-status.timer").exists()
    # The LAN guard units are installed, but only ship-ctl apply enables them.
    for name in ("ship-lan-guard-early.service", "ship-lan-guard.service"):
        assert (root / "etc/systemd/system" / name).exists()
        assert not list(root.glob(f"etc/systemd/system/*.wants/{name}"))
    assert mode(root / "home/agent/.claude") == "0o700"
    assert mode(root / "home/agent/.local/state/shipshape") == "0o700"
    assert mode(root / "home/agent/inbox") == "0o700"  # the file inbox
    assert mode(root / "home/agent/.local/state/shipshape/incoming") == "0o700"
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
        "shipshape-tailscale.sources",
    ]
    chrome = (sources / "shipshape-google-chrome.sources").read_text()
    assert "URIs: https://dl.google.com/linux/chrome/deb\n" in chrome
    assert "Architectures: amd64\n" in chrome
    assert "Signed-By: /etc/apt/keyrings/shipshape-google-chrome.gpg\n" in chrome
    node = (sources / "shipshape-nodesource.sources").read_text()
    assert "Suites: nodistro\n" in node and "Architectures" not in node


def test_old_gcloud_logs_are_removed(tmp_path):
    """gcloud logs can hold tokens a command printed: converge removes them."""
    assert provision("--converge", "--root", str(tmp_path)).returncode == 0
    logs = tmp_path / "home/agent/.config/gcloud/logs/2026.01.01"
    logs.mkdir(parents=True)
    (logs / "12.00.00.000000.log").write_text("INFO ___FILE_ONLY___ ya29.example\n")
    (tmp_path / "home/agent/.config/gcloud/credentials.db").write_text("kept")
    r = provision("--converge", "--root", str(tmp_path))
    assert "changed: /home/agent/.config/gcloud/logs removed" in r.stdout, r.stdout
    assert not (tmp_path / "home/agent/.config/gcloud/logs").exists()
    assert (tmp_path / "home/agent/.config/gcloud/credentials.db").read_text() == "kept"
    again = provision("--converge", "--root", str(tmp_path))
    assert again.stdout.rstrip().endswith("done: 0 change(s)"), again.stdout


def test_gcloud_logs_link_is_removed_not_followed(tmp_path):
    assert provision("--converge", "--root", str(tmp_path)).returncode == 0
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("kept")
    gcloud = tmp_path / "home/agent/.config/gcloud"
    gcloud.mkdir(parents=True)
    (gcloud / "logs").symlink_to(elsewhere)
    assert provision("--converge", "--root", str(tmp_path)).returncode == 0
    assert not (gcloud / "logs").is_symlink() and not (gcloud / "logs").exists()
    assert (elsewhere / "keep.txt").read_text() == "kept"


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


@pytest.mark.skipif(shutil.which("gpg") is None, reason="needs gpg")
def test_keyring_check_removes_its_gnupg_home(tmp_path):
    """A failing gpg (empty or bad ring) must not leave its temporary home behind."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    script = f'source "{PROVISION}"; keyring_ok "$1" "$2"'
    for name, data in (("empty", b""), ("bad", b"not a keyring\n" * 4)):
        ring = tmp_path / f"{name}.gpg"
        ring.write_bytes(data)
        r = subprocess.run(
            ["bash", "-c", script, "x", str(ring), "0" * 40],
            env={**os.environ, "TMPDIR": str(tmp)},
            capture_output=True,
        )
        assert r.returncode != 0, name
        assert list(tmp.iterdir()) == [], name


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
    assert "Environment=PATH=%h/.local/bin:" in text and "shim" not in text
    assert herdr["Install"]["WantedBy"] == "default.target"
    # herdr's server, and herdr-core's for core Ships: ship-ctl resumes agents from its record.
    assert sorted(p.name for p in (FILES / "systemd/user").iterdir()) == [
        "herdr.service", "ship-core.service"]
    core = unit(FILES / "systemd/user/ship-core.service")
    assert core["Unit"]["ConditionUser"] == "agent"
    assert core["Service"]["ExecStart"] == "/usr/local/bin/hc server start"
    assert core["Install"]["WantedBy"] == "default.target"


def test_status_timer():
    service = unit(FILES / "systemd/system/ship-status.service")
    assert service["Service"]["ExecStart"] == "/usr/local/bin/ship-ctl refresh"
    timer = unit(FILES / "systemd/system/ship-status.timer")
    assert timer["Timer"]["OnUnitActiveSec"] == "30s"


def test_lan_guard_unit():
    service = unit(FILES / "systemd/system/ship-lan-guard.service")
    assert service["Service"]["ExecStart"] == "/usr/local/bin/ship-ctl lan-guard"
    assert service["Service"]["Type"] == "oneshot"
    after = service["Unit"]["After"].split()
    assert "network-online.target" in after and "ship-lan-guard-early.service" in after
    assert service["Install"]["WantedBy"] == "multi-user.target"


def test_lan_guard_early_unit_loads_the_stored_rules_before_the_network():
    """No window at boot where the Ship is on the LAN (and herdr resumes agents)
    without the guard: the stored rules load before network-pre.target, which
    every network interface and user session waits for."""
    sys.path.insert(0, str(SHIP / "lib"))
    from shipctl import languard

    early = unit(FILES / "systemd/system/ship-lan-guard-early.service")
    assert early["Unit"]["DefaultDependencies"] == "no"  # else it would wait for basic.target
    assert "network-pre.target" in early["Unit"]["Before"].split()
    assert "network-pre.target" in early["Unit"]["Wants"].split()
    assert "network-pre.target" not in early["Unit"].get("After", "").split()
    assert early["Service"]["ExecStart"] == f"/usr/sbin/nft -f {languard.RULES}"
    assert early["Service"]["Type"] == "oneshot"
    assert early["Install"]["WantedBy"] == "sysinit.target"
    assert languard.EARLY_UNIT == "ship-lan-guard-early.service"
    assert languard.UNITS == (languard.EARLY_UNIT, languard.UNIT)


def test_nftables_is_a_base_package():
    text = (SHIP / "provision/ship-provision").read_text()
    base = text[text.index("BASE_PACKAGES=("):]
    assert "nftables" in base[: base.index(")")]


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


# --- herdr.last_resume ------------------------------------------------------------------
def test_last_resume_comes_from_the_record_else_ship_resumes_old_one(tmp_path):
    """Until a Ship's first boot on tooling with the agent record, status reports the
    record ship-resume wrote; from then on, the agent record's."""
    sys.path.insert(0, str(SHIP / "lib"))
    from shipctl.context import Context
    from shipctl.paths import Paths
    from shipctl.status import last_resume

    home = tmp_path / "home/agent/.local/state/shipshape"
    home.mkdir(parents=True)
    (home / "last-resume.json").write_text(json.dumps({
        "at": "2026-01-01T00:00:00Z", "method": "hidden-client", "resumed": 2, "failed": 0,
        "boot_id": "b"}))
    ctx = Context(Paths(tmp_path, SHIP))
    assert last_resume(ctx) == {"at": "2026-01-01T00:00:00Z", "method": "hidden-client",
                                "resumed": 2, "failed": 0}
    state = tmp_path / "var/lib/shipshape"
    state.mkdir(parents=True)
    (state / "agents.json").write_text(json.dumps({
        "schema": "shipshape.agent-record/1", "runtime": "herdr", "agents": {},
        "resume": {"boot_id": "c", "at": "2026-01-02T00:00:00Z", "resumed": 3, "failed": 1}}))
    assert last_resume(ctx) == {"at": "2026-01-02T00:00:00Z", "method": "api-fallback",
                                "resumed": 3, "failed": 1}


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
    hook = tmp_path / "opt/shipshape/hooks/mode-ledger"
    hook.chmod(0o700)
    r = provision("--converge", "--root", str(tmp_path))
    assert "changed: /opt/shipshape/hooks/mode-ledger" in r.stdout
    assert r.stdout.rstrip().endswith("done: 1 change(s)")
    assert mode(hook) == "0o755"


def test_converge_removes_the_resume_shim_and_ship_resume(tmp_path):
    """A Ship provisioned before the agent record had the resume shim and the
    hidden-client unit, enabled: a converge takes both away."""
    provision("--converge", "--root", str(tmp_path))
    old = {"opt/shipshape/shim/claude": "#!/usr/bin/python3\n",
           "etc/systemd/user/ship-resume.service": "[Unit]\n"}
    for rel, text in old.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    wants = tmp_path / "etc/systemd/user/default.target.wants/ship-resume.service"
    wants.parent.mkdir(parents=True, exist_ok=True)
    wants.symlink_to("/etc/systemd/user/ship-resume.service")
    r = provision("--converge", "--root", str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    for rel in ("opt/shipshape/shim/claude", "etc/systemd/user/default.target.wants/"
                "ship-resume.service", "etc/systemd/user/ship-resume.service"):
        assert f"changed: removed /{rel}" in r.stdout
        assert not (tmp_path / rel).exists() and not (tmp_path / rel).is_symlink()
    assert not (tmp_path / "opt/shipshape/shim").exists()
    again = provision("--converge", "--root", str(tmp_path))
    assert again.stdout.rstrip().endswith("done: 0 change(s)"), again.stdout


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
    wants = tmp_path / "etc/systemd/user/default.target.wants"
    assert sorted(p.name for p in wants.iterdir()) == ["herdr.service"]


def test_the_agent_launcher_is_installed(tmp_path):
    provision("--converge", "--root", str(tmp_path))
    launcher = tmp_path / "opt/shipshape/bin/agent-launch"
    assert mode(launcher) == "0o755"
    assert launcher.read_bytes() == (FILES / "bin/agent-launch").read_bytes()
    assert (tmp_path / "etc/systemd/user/ship-core.service").exists()


def test_a_core_ship_starts_herdr_core_at_boot_not_herdr(tmp_path):
    """ship.toml runtime = "core": ship-core.service is enabled instead of herdr.service
    (both stay installed), and back again when it says herdr."""
    if shutil.which("systemctl") is None:
        pytest.skip("needs systemctl")
    provision("--first-boot", "--root", str(tmp_path))
    wants = tmp_path / "etc/systemd/user/default.target.wants"
    toml = tmp_path / "etc/shipshape/ship.toml"
    toml.write_text('schema = 1\nruntime = "core"\n\n[ship]\nname = "ship-01"\n'
                    'id = "7c9e6679-7425-40de-944b-e07fc1f90ae7"\n')
    r = provision("--converge", "--root", str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(p.name for p in wants.iterdir()) == ["ship-core.service"]
    assert (tmp_path / "etc/systemd/user/herdr.service").exists()
    again = provision("--converge", "--root", str(tmp_path))
    assert again.stdout.rstrip().endswith("done: 0 change(s)"), again.stdout
    toml.write_text(toml.read_text().replace('runtime = "core"\n', ""))
    provision("--converge", "--root", str(tmp_path))
    assert sorted(p.name for p in wants.iterdir()) == ["herdr.service"]


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


def test_npmrc_fifo_does_not_hang(tmp_path):
    home = tmp_path / "home/agent"
    home.mkdir(parents=True)
    os.mkfifo(home / ".npmrc")
    r = provision("--converge", "--root", str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / ".npmrc").is_file()
    assert (home / ".npmrc").read_text() == "prefix=/home/agent/.local\n"


def _install_core(tmp_path: Path, archive: Path, digest: str) -> subprocess.CompletedProcess:
    """Run install_herdr_core from the sourced script against a --root sandbox."""
    root = tmp_path / "root"
    script = (f'. "{PROVISION}"; ROOT="{root}"; SANDBOX=1; '
              f'install_herdr_core "{archive}" "{digest}"')
    return subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=60)


def _core_archive(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "herdr-core").write_text("#!/bin/sh\necho herdr-core\n")
    (src / "LICENSE").write_text("license\n")
    archive = tmp_path / "herdr-core-linux-x86_64.tar.gz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(src), "herdr-core", "LICENSE"],
                   check=True)
    return archive


def test_herdr_core_installs_the_binary_and_hc_link(tmp_path):
    import hashlib
    archive = _core_archive(tmp_path)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    result = _install_core(tmp_path, archive, digest)
    assert result.returncode == 0, result.stderr
    bin_dir = tmp_path / "root/usr/local/bin"
    assert mode(bin_dir / "herdr-core") == "0o755"
    assert (bin_dir / "herdr-core").read_text().endswith("echo herdr-core\n")
    assert os.readlink(bin_dir / "hc") == "herdr-core"
    assert not (bin_dir / "LICENSE").exists()
    assert (tmp_path / "root/var/lib/shipshape/herdr-core.sha256").read_text().strip() == digest


def test_herdr_core_refuses_a_mismatched_archive(tmp_path):
    archive = _core_archive(tmp_path)
    result = _install_core(tmp_path, archive, "0" * 64)
    assert result.returncode != 0
    assert "herdr-core checksum mismatch" in result.stderr + result.stdout
    assert not (tmp_path / "root/usr/local/bin/herdr-core").exists()
    assert not (tmp_path / "root/usr/local/bin/hc").exists()


def test_herdr_core_pin_is_in_versions():
    import tomllib
    pin = tomllib.loads((SHIP.parent / "versions.toml").read_text())["herdr_core"]
    assert pin["version"] == "0.1.19"
    assert pin["assets"]["x86_64"]["sha256"] == (
        "4adcfb423d52f24c5cc4a1e2cd2fcad1f62d28bb77876ac472c29082a3eaa424")
    assert all(a["url"].startswith("https://releases.herdr.dev/core/0.1.19/")
               and a["url"].endswith(".tar.gz") for a in pin["assets"].values())


# --- /tmp on the disk ---------------------------------------------------------------
TMP_MASK = "etc/systemd/system/tmp.mount"
TMPFILES = "etc/tmpfiles.d/tmp.conf"


def _recorders(tmp_path: Path) -> tuple[dict, Path]:
    """A bin folder whose mount, umount and systemctl record every call (systemctl
    then runs the real one, which --root needs to enable units offline)."""
    bin_dir, log = tmp_path / "fakebin", tmp_path / "calls.log"
    bin_dir.mkdir()
    real = shutil.which("systemctl")
    for name in ("mount", "umount", "systemctl"):
        then = f'exec {real} "$@"' if name == "systemctl" and real else "exit 0"
        (bin_dir / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n{then}\n')
        (bin_dir / name).chmod(0o755)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin"}, log


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_a_new_ship_gets_a_disk_tmp_with_age_cleanup(tmp_path):
    """First boot masks tmp.mount (so /tmp is a folder on the root disk) and owns the
    tmpfiles rule that keeps it 1777 and cleans what nothing used for 10 days."""
    root = tmp_path / "root"
    root.mkdir()
    env, log = _recorders(tmp_path)
    r = provision("--first-boot", "--root", str(root), env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.readlink(root / TMP_MASK) == "/dev/null"
    assert f"changed: /{TMP_MASK} -> /dev/null" in r.stdout
    rule = (root / TMPFILES).read_text()
    assert (root / TMPFILES).read_bytes() == (FILES / "tmpfiles/tmp.conf").read_bytes()
    assert mode(root / TMPFILES) == "0o644"
    lines = [ln.split() for ln in rule.splitlines() if ln and not ln.startswith("#")]
    assert ["q", "/tmp", "1777", "root", "root", "10d"] in lines
    assert ["q", "/var/tmp", "1777", "root", "root", "30d"] in lines  # it replaces systemd's
    assert not any(c.startswith(("mount", "umount")) for c in _calls(log))
    assert not any("tmp.mount" in c for c in _calls(log))
    again = provision("--first-boot", "--root", str(root), env=env)
    assert again.stdout.rstrip().endswith("done: 0 change(s)"), again.stdout


def test_an_existing_ships_update_moves_tmp_to_disk_at_the_next_reboot(tmp_path):
    """A Ship provisioned before this change, /tmp a live tmpfs: its next converge masks
    tmp.mount once, says the reboot applies it, and never unmounts or stops anything."""
    root = tmp_path / "root"
    root.mkdir()
    assert provision("--converge", "--root", str(root)).returncode == 0
    (root / TMP_MASK).unlink()  # as an older release left it
    (root / TMPFILES).unlink()
    (root / "proc/self").mkdir(parents=True)
    (root / "proc/self/mounts").write_text(
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        "tmpfs /tmp tmpfs rw,nosuid,nodev,nr_inodes=1048576,inode64 0 0\n")
    env, log = _recorders(tmp_path)
    r = provision("--converge", "--root", str(root), env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.readlink(root / TMP_MASK) == "/dev/null"
    assert (root / TMPFILES).exists()
    assert r.stdout.count("changed:") == 2, r.stdout
    assert "/tmp stays in memory (tmpfs) until the next reboot, which puts it on the disk" \
        in r.stdout
    assert not any(c.startswith(("mount", "umount")) for c in _calls(log))
    assert not any("tmp.mount" in c for c in _calls(log))
    again = provision("--converge", "--root", str(root), env=env)
    assert again.stdout.rstrip().endswith("done: 0 change(s)"), again.stdout
    assert "tmpfs" not in again.stdout  # said once, when it changed


def test_tmp_mask_replaces_a_unit_file_and_dry_run_only_reports(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert provision("--converge", "--root", str(root)).returncode == 0
    (root / TMP_MASK).unlink()
    (root / TMP_MASK).write_text("[Mount]\nWhat=tmpfs\nWhere=/tmp\nType=tmpfs\n")
    dry = provision("--converge", "--dry-run", "--root", str(root))
    assert f"would change: /{TMP_MASK} -> /dev/null" in dry.stdout
    assert not (root / TMP_MASK).is_symlink()
    provision("--converge", "--root", str(root))
    assert os.readlink(root / TMP_MASK) == "/dev/null"


def test_the_tmpfiles_rule_is_what_systemd_ships():
    """Ours replaces systemd's tmp.conf, so it must keep both of its lines."""
    lines = [ln for ln in (FILES / "tmpfiles/tmp.conf").read_text().splitlines()
             if ln and not ln.startswith("#")]
    assert lines == ["q /tmp 1777 root root 10d", "q /var/tmp 1777 root root 30d"]
