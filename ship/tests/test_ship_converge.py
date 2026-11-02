"""ship-ctl converge: unpack a bundle, switch releases, roll back on failure (contracts/seed.md)."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest
from ship_harness import assert_valid, make_bundle

PROVISION_OK = '#!/bin/sh\necho converge >> "$SHIPSHAPE_ROOT/provision-ran"\n'
PROVISION_FAIL = "#!/bin/sh\nexit 3\n"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def pack(
    tmp: Path,
    *,
    tooling="0.2.0",
    commit=COMMIT,
    provision=PROVISION_OK,
    extra: dict | None = None,
    drop: str | None = None,
) -> Path:
    src = make_bundle(tmp / f"src-{tooling}-{commit[:4]}")
    (src / "provision/ship-provision").write_text(provision)
    (src / "provision/ship-provision").chmod(0o755)
    (src / "bundle.json").write_text(
        json.dumps(
            {
                "schema": "shipshape.bundle/1",
                "tooling_version": tooling,
                "commit": commit,
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
    )
    if drop:
        (src / drop).unlink()
    out = tmp / f"bundle-{tooling}-{commit[:4]}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for path in sorted(src.rglob("*")):
            if "__pycache__" in path.parts:
                continue
            tar.add(path, arcname=str(path.relative_to(src)), recursive=False)
        for name, data in (extra or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out


def current(ship) -> str:
    return Path(ship.p("/opt/shipshape/current").readlink()).name


def test_converge_switches_and_runs_steps(ship, tmp_path):
    bundle = pack(tmp_path)
    rc, doc, out = ship.ctl("converge", "--bundle", str(bundle), "--json")
    assert rc == 0, out
    assert_valid("ship-ctl-result", doc)
    assert doc["data"] == {"from": None, "to": "0.2.0-0123456789ab"}
    assert current(ship) == "0.2.0-0123456789ab"
    assert ship.p("/provision-ran").read_text() == "converge\n"
    assert ship.p("/etc/claude-code/managed-settings.json").exists()  # apply ran
    stored = ship.p("/var/lib/shipshape/bundle.tar.gz")
    assert stored.read_bytes() == bundle.read_bytes()
    assert oct(stored.stat().st_mode & 0o777) == "0o600"

    # Converging again to the same release is fine.
    rc, doc, _ = ship.ctl("converge", "--bundle", str(bundle), "--json")
    assert rc == 0 and doc["data"]["from"] == "0.2.0-0123456789ab"


def test_converge_rolls_back_when_provision_fails(ship, tmp_path):
    ship.ctl("converge", "--bundle", str(pack(tmp_path)), "--json")
    bad = pack(tmp_path, tooling="0.3.0", commit="f" * 40, provision=PROVISION_FAIL)
    rc, doc, _ = ship.ctl("converge", "--bundle", str(bad), "--json")
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "provision-failed"
    assert current(ship) == "0.2.0-0123456789ab"


def test_converge_same_release_rollback_restores_old_files(ship, tmp_path):
    ship.ctl("converge", "--bundle", str(pack(tmp_path)), "--json")
    marker = ship.p("/opt/shipshape/releases/0.2.0-0123456789ab/marker")
    marker.write_text("old")
    (tmp_path / "again").mkdir()
    bad = pack(tmp_path / "again", provision=PROVISION_FAIL)
    rc, _, _ = ship.ctl("converge", "--bundle", str(bad), "--json")
    assert rc == 1
    assert marker.read_text() == "old"
    assert current(ship) == "0.2.0-0123456789ab"


def test_converge_checks_sidecar_checksum(ship, tmp_path):
    bundle = pack(tmp_path)
    sidecar = bundle.with_name(bundle.name + ".sha256")
    sidecar.write_text("0" * 64 + "  bundle.tar.gz\n")
    rc, doc, _ = ship.ctl("converge", "--bundle", str(bundle), "--json")
    assert rc == 1 and doc["error"]["code"] == "bundle-checksum"
    sidecar.write_text(hashlib.sha256(bundle.read_bytes()).hexdigest() + "  bundle.tar.gz\n")
    rc, doc, _ = ship.ctl("converge", "--bundle", str(bundle), "--json")
    assert rc == 0


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        ({"extra": {"../escape": b"x"}}, "unsafe path"),
        ({"extra": {"/abs": b"x"}}, "unsafe path"),
        ({"drop": "bin/ship-ctl"}, "missing bin/ship-ctl"),
        ({"commit": "not-a-commit"}, "bundle.json"),
    ],
)
def test_converge_rejects_bad_bundles(ship, tmp_path, kwargs, needle):
    rc, doc, _ = ship.ctl("converge", "--bundle", str(pack(tmp_path, **kwargs)), "--json")
    assert rc == 1
    assert doc["error"]["code"] == "invalid-bundle"
    assert needle in doc["error"]["message"]
    assert not ship.p("/opt/shipshape/current").exists()
    leftovers = [p.name for p in ship.p("/opt/shipshape/releases").iterdir()]
    assert leftovers == []


def test_converge_rejects_links(ship, tmp_path):
    bundle = pack(tmp_path)
    linked = tmp_path / "linked.tar.gz"
    shutil.copy(bundle, linked)
    with tarfile.open(bundle, "r:gz") as src, tarfile.open(linked, "w:gz") as dst:
        for m in src.getmembers():
            dst.addfile(m, src.extractfile(m) if m.isfile() else None)
        link = tarfile.TarInfo("files/evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/shadow"
        dst.addfile(link)
    rc, doc, _ = ship.ctl("converge", "--bundle", str(linked), "--json")
    assert rc == 1 and "links" in doc["error"]["message"]
