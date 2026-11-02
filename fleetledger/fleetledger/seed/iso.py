"""Seed ISOs: ISO 9660 with Joliet and Rock Ridge, volume label CIDATA.

Built with pycdlib, so no host tools (genisoimage, xorriso) are needed.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path

import pycdlib

from fleetledger.paths import write_private

VOLUME_ID = "CIDATA"

# Rock Ridge / Joliet name -> ISO 9660 level 1 name (8.3, upper case).
ISO_NAMES = {
    "user-data": "/USERDATA.;1",
    "meta-data": "/METADATA.;1",
    "network-config": "/NETWORKC.;1",
}


def build(files: Mapping[str, str]) -> bytes:
    """The ISO image holding the given NoCloud files (name -> text)."""
    unknown = set(files) - set(ISO_NAMES)
    if unknown:
        raise ValueError(f"not a NoCloud seed file: {sorted(unknown)}")
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident=VOLUME_ID)
    try:
        for name in ISO_NAMES:
            if name not in files:
                continue
            data = files[name].encode("utf-8")
            iso.add_fp(
                io.BytesIO(data),
                len(data),
                ISO_NAMES[name],
                rr_name=name,
                joliet_path=f"/{name}",
            )
        out = io.BytesIO()
        iso.write_fp(out)
    finally:
        iso.close()
    return out.getvalue()


def write(files: Mapping[str, str], path: Path) -> None:
    """Write the ISO to path with mode 0600 (it holds a private host key)."""
    write_private(path, build(files))


def read(data: bytes) -> tuple[str, dict[str, str]]:
    """Volume label and files (by Rock Ridge name) of a seed ISO."""
    iso = pycdlib.PyCdlib()
    iso.open_fp(io.BytesIO(data))
    try:
        label = iso.pvd.volume_identifier.decode("ascii").strip()
        files = {}
        for child in iso.list_children(rr_path="/"):
            if child.is_dot() or child.is_dotdot() or child.is_dir():
                continue
            name = child.rock_ridge.name().decode("utf-8")
            out = io.BytesIO()
            iso.get_file_from_iso_fp(out, rr_path=f"/{name}")
            files[name] = out.getvalue().decode("utf-8")
        joliet = {
            child.file_identifier().decode("utf-16_be")
            for child in iso.list_children(joliet_path="/")
            if not (child.is_dot() or child.is_dotdot())
        }
        if joliet != set(files):
            raise ValueError("Joliet and Rock Ridge names disagree")
    finally:
        iso.close()
    return label, files
