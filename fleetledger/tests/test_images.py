"""Baked images: base image verification, ids, manifests and the bake call.

The signature checks use real gpg and gpgv with a throwaway key made here; the
bake itself is a fake (ship/tests covers ship/image/bake).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fleetledger.errors import FleetError
from fleetledger.repo import Repo

from fleetledger import images, schemas

pytestmark = pytest.mark.skipif(not shutil.which("gpg") or not shutil.which("gpgv"),
                                reason="needs gpg and gpgv")

IMAGE_BYTES = b"pretend this is a cloud image\n" * 100
IMG_FILE = "ubuntu-26.04-server-cloudimg-amd64.img"


def gpg(home: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["gpg", "--batch", "--homedir", str(home), *args], check=True,
                          capture_output=True, **kw)


HOMES: list[Path] = []


@pytest.fixture(autouse=True)
def stop_gpg_agents():
    """Key generation starts a gpg-agent per throwaway home; stop them afterwards.

    This runs after tmp_path is deleted, and any gpgconf call makes the home's socket
    folder under /run/user again, so remove that folder too."""
    yield
    while HOMES:
        home = str(HOMES.pop())
        for args in (["--kill", "all"], ["--remove-socketdir"]):
            subprocess.run(["gpgconf", "--homedir", home, *args],
                           capture_output=True, check=False)


def make_key(home: Path, name: str) -> tuple[str, bytes]:
    home.mkdir(mode=0o700)
    HOMES.append(home)
    gpg(home, "--passphrase", "", "--quick-gen-key", f"{name} <{name}@example.com>",
        "ed25519", "sign", "never")
    out = gpg(home, "--with-colons", "--list-keys", text=True).stdout
    fpr = next(line.split(":")[9] for line in out.splitlines() if line.startswith("fpr"))
    return fpr, gpg(home, "--armor", "--export", fpr).stdout


@pytest.fixture
def mirror(tmp_path):
    """A fake cloud-images mirror and keyserver, signed by a throwaway key."""
    fpr, armored = make_key(tmp_path / "gnupg", "signer")
    sha = hashlib.sha256(IMAGE_BYTES).hexdigest()
    sums = f"{'1' * 64} *other.img\n{sha} *{IMG_FILE}\n"
    site = tmp_path / "site"
    site.mkdir()
    (site / "SHA256SUMS").write_text(sums)
    gpg(tmp_path / "gnupg", "--detach-sign", "--output", str(site / "SHA256SUMS.gpg"),
        str(site / "SHA256SUMS"))
    (site / IMG_FILE).write_bytes(IMAGE_BYTES)
    (site / "key.asc").write_bytes(armored)

    class Mirror:
        pass

    m = Mirror()
    m.fpr, m.sha, m.site, m.fetched = fpr, sha, site, []

    def fetch(url: str, dest: Path) -> None:
        m.fetched.append(url)
        name = "key.asc" if "keyserver" in url else url.rsplit("/", 1)[1]
        shutil.copy(site / name, dest)

    m.fetch = fetch
    return m


def pin(repo: Repo, *, fpr: str, sha: str) -> Repo:
    text = repo.versions_path.read_text()
    text = re.sub(r'signing_key = "[0-9A-F]{40}"', f'signing_key = "{fpr}"', text)
    text = re.sub(r'(\[ubuntu\.images\.amd64\]\nfile = "[^"]+"\nsha256 = )"[0-9a-f]{64}"',
                  rf'\1"{sha}"', text)
    repo.versions_path.write_text(text)
    return Repo(repo.root)


def test_ensure_base_downloads_and_verifies_once(fake_repo, mirror, tmp_path):
    repo = pin(fake_repo, fpr=mirror.fpr, sha=mirror.sha)
    base = images.ensure_base(repo, fetch=mirror.fetch, cache=tmp_path / "cache")
    assert base.path.read_bytes() == IMAGE_BYTES and base.sha256 == mirror.sha
    assert base.build == repo.versions["ubuntu"]["build"]
    assert len(mirror.fetched) == 4
    assert any(f"0x{mirror.fpr}" in url for url in mirror.fetched)
    images.ensure_base(repo, fetch=mirror.fetch, cache=tmp_path / "cache")
    assert len(mirror.fetched) == 4  # cached and still matching: nothing fetched


def test_ensure_base_refuses_another_key(fake_repo, mirror, tmp_path):
    other, armored = make_key(tmp_path / "gnupg-other", "other")
    (mirror.site / "key.asc").write_bytes(armored)
    repo = pin(fake_repo, fpr=mirror.fpr, sha=mirror.sha)
    with pytest.raises(FleetError, match="not the one pinned"):
        images.ensure_base(repo, fetch=mirror.fetch, cache=tmp_path / "cache")
    assert list((tmp_path / "cache").rglob("*.img")) == []


def test_ensure_base_refuses_a_key_bundle(fake_repo, mirror, tmp_path):
    """The keyserver answer must be exactly the pinned key, not the pinned key plus others."""
    _, armored = make_key(tmp_path / "gnupg-extra", "extra")
    with (mirror.site / "key.asc").open("ab") as fh:
        fh.write(armored)
    repo = pin(fake_repo, fpr=mirror.fpr, sha=mirror.sha)
    with pytest.raises(FleetError, match="not the one pinned"):
        images.ensure_base(repo, fetch=mirror.fetch, cache=tmp_path / "cache")


def test_ensure_base_refuses_a_bad_signature(fake_repo, mirror, tmp_path):
    with (mirror.site / "SHA256SUMS").open("a") as fh:
        fh.write(f"{'2' * 64} *added-after-signing.img\n")
    repo = pin(fake_repo, fpr=mirror.fpr, sha=mirror.sha)
    with pytest.raises(FleetError, match="not signed by the pinned"):
        images.ensure_base(repo, fetch=mirror.fetch, cache=tmp_path / "cache")
    assert not any(IMG_FILE in url for url in mirror.fetched)  # image never downloaded


def test_ensure_base_refuses_a_pin_the_signed_list_disagrees_with(fake_repo, mirror, tmp_path):
    repo = pin(fake_repo, fpr=mirror.fpr, sha="3" * 64)
    with pytest.raises(FleetError, match="versions.toml pins"):
        images.ensure_base(repo, fetch=mirror.fetch, cache=tmp_path / "cache")


def test_ensure_base_refuses_a_tampered_download(fake_repo, mirror, tmp_path):
    repo = pin(fake_repo, fpr=mirror.fpr, sha=mirror.sha)
    (mirror.site / IMG_FILE).write_bytes(IMAGE_BYTES + b"x")
    with pytest.raises(FleetError, match="not the pinned one"):
        images.ensure_base(repo, fetch=mirror.fetch, cache=tmp_path / "cache")
    assert list((tmp_path / "cache").rglob("*.img*")) == []


# --- ids, manifests and the bake call --------------------------------------------


def bakeable(repo: Repo) -> Repo:
    (repo.tools_dir / "chrome-devtools-axi.toml").write_text(
        (Path(__file__).resolve().parents[2] / "ship/tools/chrome-devtools-axi.toml").read_text()
    )
    return repo


@pytest.fixture
def cached_base(home, fake_repo):
    """versions.toml pinned to a base image already in the cache: no downloads."""
    sha = hashlib.sha256(IMAGE_BYTES).hexdigest()
    repo = bakeable(pin(fake_repo, fpr="A" * 40, sha=sha))
    u = repo.versions["ubuntu"]
    folder = home / "data" / "images" / "base" / f"{u['release']}-{u['build']}"
    folder.mkdir(parents=True)
    (folder / IMG_FILE).write_bytes(IMAGE_BYTES)
    return repo


def test_build_runs_the_bake_and_records_a_valid_manifest(cached_base, home):
    seen = {}

    def run(argv, check=False):
        seen["argv"] = argv
        args = dict(zip(argv[1::2], argv[2::2], strict=False))
        seen["manifest"] = json.loads(Path(args["--manifest"]).read_text())
        seen["bundle"] = Path(args["--bundle"]).read_bytes()[:2]
        Path(args["--out"]).write_bytes(b"QCOW2-OUTPUT")
        return subprocess.CompletedProcess(argv, 0)

    built = images.build(cached_base, backend="qemu", run=run, note=lambda m: None,
                         created="2026-11-02T10:00:00Z")
    argv = seen["argv"]
    assert argv[0].endswith("ship/image/bake") and argv[-2:] == ["--backend", "qemu"]
    assert seen["bundle"] == b"\x1f\x8b"  # a gzip tooling bundle
    m = seen["manifest"]
    assert re.fullmatch(r"26\.04-20261102-[0-9a-f]{8}", m["id"]) and m["id"] == built.id
    assert m["recipes"] == ["chrome-devtools-axi"]
    assert "google-chrome-stable" in m["packages"] and "gh" in m["packages"]
    assert "sha256" not in m  # the image cannot carry its own hash
    assert built.path == home / "data" / "images" / f"{built.id}.qcow2"
    assert oct(built.path.stat().st_mode & 0o777) == "0o600"
    side = json.loads(built.path.with_suffix(".json").read_text())
    assert side["sha256"] == hashlib.sha256(b"QCOW2-OUTPUT").hexdigest()
    assert schemas.is_valid("image-manifest", side, cached_base.contracts)
    assert built.entry["sha256"] == side["sha256"] and built.entry["recipes"] == m["recipes"]


def test_same_inputs_same_id_different_inputs_new_id(cached_base):
    base = images.ensure_base(cached_base)
    a = images.inputs_hash(base, cached_base, ["chrome-devtools-axi"])
    assert a == images.inputs_hash(base, cached_base, ["chrome-devtools-axi"])
    assert a != images.inputs_hash(base, cached_base, [])
    provision = cached_base.ship_dir / "provision/ship-provision"
    provision.write_text(provision.read_text() + "# changed\n")
    assert a != images.inputs_hash(base, cached_base, ["chrome-devtools-axi"])


def test_failed_bake_leaves_nothing(cached_base, home):
    def run(argv, check=False):
        out = Path(argv[argv.index("--out") + 1])
        out.write_bytes(b"half")
        return subprocess.CompletedProcess(argv, 3)

    with pytest.raises(FleetError, match="exit 3"):
        images.build(cached_base, run=run, note=lambda m: None)
    assert list((home / "data" / "images").glob("*.qcow2")) == []


def test_only_bake_recipes_can_be_baked(cached_base):
    with pytest.raises(FleetError, match="Not marked bake"):
        images.build(cached_base, recipes=["tasks-axi"], run=None, note=lambda m: None)


def test_local_file_checks_the_recorded_sum(store, tmp_path):
    qcow = tmp_path / "x.qcow2"
    qcow.write_bytes(b"IMG")
    with store.edit() as ed:
        ed.add_image("26.04-20261102-3f9a1c2e", {
            "created": "2026-11-02T10:00:00Z", "sha256": hashlib.sha256(b"IMG").hexdigest(),
            "file": str(qcow), "ubuntu": {"release": "26.04", "build": "20261016"},
            "tooling": "0.1.0", "commit": "unknown"})
    image = images.local_file(store.load(), "26.04-20261102-3f9a1c2e")
    assert image.path == qcow and image.size == 3
    qcow.write_bytes(b"IMG!")
    with pytest.raises(FleetError, match="does not match"):
        images.local_file(store.load(), "26.04-20261102-3f9a1c2e")
    os.unlink(qcow)
    with pytest.raises(FleetError, match="missing"):
        images.local_file(store.load(), "26.04-20261102-3f9a1c2e")
