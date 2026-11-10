"""Phase 4 bake, offline: `ship-provision --bake` in a sandbox, and ship/image/bake
with fake virt-customize and qemu-img on PATH (contracts/image.md)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest
from ship_harness import provision

SHIP = Path(__file__).resolve().parents[1]
BAKE = SHIP / "image" / "bake"
CLEAN = SHIP / "image" / "image-clean.sh"


# --- ship-provision --bake ----------------------------------------------------------


def test_bake_does_only_the_shared_system_layer(tmp_path):
    r = provision("--bake", "--root", str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "bake done" in r.stdout
    sources = sorted(p.name for p in (tmp_path / "etc/apt/sources.list.d").iterdir())
    assert "shipshape-google-chrome.sources" in sources
    # Nothing per-Ship: no users' files, no ShipShape files, no units, no settings.
    for absent in ("home", "opt/shipshape", "etc/ssh", "usr/local/bin/ship-ctl",
                   "etc/claude-code", "etc/profile.d/shipshape.sh"):
        assert not (tmp_path / absent).exists(), absent
    # The one unit change every Ship shares: tmp.mount masked, so a Ship made from the
    # image has /tmp on the disk from its first boot.
    units = sorted(str(p.relative_to(tmp_path)) for p in (tmp_path / "etc/systemd").rglob("*"))
    assert units == ["etc/systemd/system", "etc/systemd/system/tmp.mount"]
    assert os.readlink(tmp_path / "etc/systemd/system/tmp.mount") == "/dev/null"
    assert (tmp_path / "etc/tmpfiles.d/tmp.conf").exists()
    # And no provisioning record: first boot must not look done before it ran.
    assert not (tmp_path / "var/lib/shipshape/provision.json").exists()
    assert "systemctl start qemu-guest-agent" not in r.stdout


def test_bake_installs_the_apt_parts_of_bake_recipes(tmp_path):
    r = provision("--bake", "--recipes", "chrome-devtools-axi", "--root", str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipped (--root): apt packages google-chrome-stable" in r.stdout


@pytest.mark.parametrize("args, message", [
    (["--bake", "--recipes", "tasks-axi"], "not marked bake = true"),
    (["--bake", "--recipes", "no-such-tool"], "no recipe no-such-tool"),
    (["--bake", "--recipes", "../../etc/passwd"], "bad recipe id"),
    (["--converge", "--recipes", "chrome-devtools-axi"], "--recipes goes with --bake only"),
])
def test_bake_recipes_are_checked(tmp_path, args, message):
    r = provision(*args, "--root", str(tmp_path))
    assert r.returncode != 0 and message in r.stderr


# --- ship/image/bake ------------------------------------------------------------------

FAKE_QEMU_IMG = """#!/usr/bin/env bash
echo "qemu-img $*" >> "$FAKE_LOG"
case $1 in
  convert) shift; while [[ $1 == -* ]]; do [[ $1 == -O ]] && shift; shift; done
           cp "$1" "$2"; printf 'converted' >> "$2" ;;
  resize) ;;
esac
"""

FAKE_VIRT_CUSTOMIZE = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$FAKE_DIR/virt-customize.args"
while [ $# -gt 0 ]; do
  if [ "$1" = --copy-in ]; then cp "${2%%:*}" "$FAKE_DIR/"; fi
  shift
done
exit "${FAKE_VC_EXIT:-0}"
"""


@pytest.fixture
def bake_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("qemu-img", FAKE_QEMU_IMG), ("virt-customize", FAKE_VIRT_CUSTOMIZE)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    base = tmp_path / "base.img"
    base.write_bytes(b"cloud image")
    bundle = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        data = b"#!/usr/bin/env bash\necho provision\n"
        info = tarfile.TarInfo("provision/ship-provision")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    manifest = {
        "schema": "shipshape.image/1", "id": "26.04-20261102-3f9a1c2e",
        "created": "2026-11-02T10:00:00Z",
        "ubuntu": {"release": "26.04", "build": "20261016", "file": "base.img",
                   "sha256": hashlib.sha256(b"cloud image").hexdigest()},
        "versions_sha256": "0" * 64, "tooling": "0.1.0", "commit": "unknown", "dirty": False,
        "recipes": ["chrome-devtools-axi"], "packages": [], "format": "qcow2",
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "LANG": "C.UTF-8", "TMPDIR": str(tmp_path),
           "FAKE_LOG": str(tmp_path / "fake.log"), "FAKE_DIR": str(tmp_path)}

    class Env:
        pass

    e = Env()
    e.tmp, e.base, e.bundle, e.manifest, e.env = tmp_path, base, bundle, manifest, env
    e.out = tmp_path / "out" / "image.qcow2"
    e.out.parent.mkdir()

    def run(*extra, env=None):
        argv = [str(BAKE), "--base", str(base), "--bundle", str(bundle),
                "--manifest", str(tmp_path / "manifest.json"), "--out", str(e.out), *extra]
        return subprocess.run(argv, env={**e.env, **(env or {})}, capture_output=True,
                              text=True, timeout=60)

    e.run = run
    return e


def work_dirs(e) -> list[str]:
    return [p.name for p in e.tmp.iterdir() if p.name.startswith("shipshape-bake.")]


def test_bake_with_virt_customize(bake_env):
    e = bake_env
    r = e.run()
    assert r.returncode == 0, r.stderr
    assert e.out.read_bytes().startswith(b"cloud image")  # a copy, compressed at the end
    assert e.base.read_bytes() == b"cloud image"  # the base is never modified
    assert list(e.out.parent.iterdir()) == [e.out]  # no .part left beside it
    log = (e.tmp / "fake.log").read_text()
    assert "qemu-img resize -q" in log and "+6G" in log
    assert "convert -q -c -O qcow2" in log  # compressed output
    args = (e.tmp / "virt-customize.args").read_text().splitlines()
    assert args[:2] == ["-a", args[1]] and args[1].endswith("disk.qcow2")
    assert "--network" in args and "--no-logfile" in args
    run_commands = [args[i + 1] for i, a in enumerate(args) if a == "--run-command"]
    assert any("inside.sh /var/tmp/shipshape-bake 'chrome-devtools-axi'" in c
               for c in run_commands)
    # Clean-up comes after provisioning, and only the bake's folder goes after it.
    assert run_commands[-2:] == ["bash /var/tmp/shipshape-bake/image-clean.sh",
                                 "rm -rf /var/tmp/shipshape-bake"]
    assert any("inside.sh" in c for c in run_commands[:-2])
    # The payload copied in: the bundle, the manifest as image.json, the clean-up.
    with tarfile.open(e.tmp / "payload.tar.gz") as tar:
        names = set(tar.getnames())
        image_json = json.load(tar.extractfile("./image.json"))
        inside = tar.extractfile("./inside.sh").read().decode()
    assert {"./bundle/provision/ship-provision", "./image-clean.sh", "./inside.sh"} <= names
    assert image_json == e.manifest
    assert "ship-provision\" --bake" in inside and "/etc/shipshape/image.json" in inside
    assert "image-clean.sh" not in inside  # each backend runs it last, separately
    assert work_dirs(e) == []


def test_bake_refuses_a_base_that_is_not_the_verified_one(bake_env):
    e = bake_env
    e.base.write_bytes(b"something else")
    r = e.run()
    assert r.returncode != 0 and "the manifest says" in r.stderr
    assert not e.out.exists() and not (e.tmp / "virt-customize.args").exists()


def test_bake_refuses_a_manifest_carrying_its_own_hash(bake_env):
    e = bake_env
    (e.tmp / "manifest.json").write_text(json.dumps({**e.manifest, "sha256": "1" * 64}))
    r = e.run()
    assert r.returncode != 0 and "must not carry sha256" in r.stderr


def test_failed_bake_leaves_no_output_and_no_work_dir(bake_env):
    e = bake_env
    r = e.run(env={"FAKE_VC_EXIT": "1"})
    assert r.returncode != 0
    assert not e.out.exists() and work_dirs(e) == []


def test_bake_never_overwrites(bake_env):
    e = bake_env
    e.out.write_bytes(b"older image")
    r = e.run()
    assert r.returncode != 0 and "already exists" in r.stderr
    assert e.out.read_bytes() == b"older image"


@pytest.mark.parametrize("extra, message", [
    (["--backend", "docker"], "virt-customize or qemu"),
    (["--grow", "lots"], "--grow"),
])
def test_bake_options_are_checked(bake_env, extra, message):
    r = bake_env.run(*extra)
    assert r.returncode != 0 and message in r.stderr


def test_bake_needs_virt_customize_or_says_so(bake_env):
    e = bake_env
    os.unlink(e.tmp / "bin" / "virt-customize")
    r = e.run(env={"PATH": f"{e.tmp / 'bin'}:/usr/bin:/bin"})
    if Path("/usr/bin/virt-customize").exists():
        pytest.skip("a real virt-customize is installed here")
    assert r.returncode != 0 and "--backend qemu" in r.stderr


def test_qemu_backend_seed_bakes_cleans_after_cloud_init_and_powers_off():
    """The throwaway seed: no users, no passwords. It bakes; the clean-up runs from a unit
    in /run ordered after cloud-init's last stage, so cloud-init writes nothing after it;
    then a clean poweroff. Any failure powers off at once."""
    text = BAKE.read_text()
    start = text.index("#cloud-config")
    user_data = text[start:text.index("USERDATA\n", start)]
    assert "users: []" in user_data and "ssh_pwauth: false" in user_data
    unit = user_data[user_data.index("[Unit]"):user_data.index("runcmd:")]
    assert "After=cloud-final.service" in unit and "image-clean.sh" in unit
    assert "/run/systemd/system/shipshape-bake-finish.service" in user_data
    assert "journalctl --relinquish-var" in unit and "systemctl poweroff" in unit
    runcmd = user_data[user_data.index("runcmd:"):]
    assert "inside.sh" in runcmd and "image-clean" not in runcmd
    assert "start --no-block shipshape-bake-finish.service" in runcmd
    assert "SHIPSHAPE-BAKE-FAILED" in runcmd and "poweroff --force" in runcmd
    assert "--bind 127.0.0.1" in text  # the payload is served on loopback only


def test_image_clean_removes_what_makes_ships_alike():
    text = CLEAN.read_text()
    for needed in ("rm -f /etc/ssh/ssh_host_*", "truncate -s 0 /etc/machine-id",
                   "cloud-init clean --logs", "rm -rf /var/lib/cloud",
                   "/var/lib/shipshape/provision.json", "apt-get clean",
                   "echo localhost >/etc/hostname"):
        assert needed in text, needed
