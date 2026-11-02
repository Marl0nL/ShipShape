"""`ship-ctl converge --bundle PATH`: switch to a new tooling bundle (contracts/seed.md).

1. Check the bundle: its .sha256 sidecar if present, safe paths only, the required
   entries, and bundle.json against the manifest schema.
2. Unpack it to /opt/shipshape/releases/<tooling>-<commit12> and point `current` at it.
3. Run the NEW release's `ship-provision --converge`, `ship-ctl apply` and
   `ship-ctl tools run`.

If any step fails, `current` goes back to the previous release.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from . import schema
from .context import Context
from .errors import CtlError
from .fsutil import ensure_dir

REQUIRED = ("bundle.json", "versions.toml", "provision/ship-provision", "bin/ship-ctl")
EXECUTABLE = ("provision/ship-provision", "bin/ship-ctl")
MAX_UNPACKED = 200 * 1024 * 1024
KEEP_RELEASES = 3
STEP_TIMEOUT = 3600


def verify_checksum(path: Path) -> None:
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.exists():
        return
    try:
        expected = sidecar.read_text(encoding="utf-8").split()[0].lower()
    except (OSError, IndexError) as exc:
        raise CtlError("bundle-checksum", f"cannot read {sidecar.name}") from exc
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != expected:
        raise CtlError("bundle-checksum", "the bundle does not match its .sha256 file")


def safe_extract(path: Path, dest: Path) -> None:
    try:
        tar = tarfile.open(path, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise CtlError("invalid-bundle", "not a gzip-compressed tar archive") from exc
    with tar:
        total = 0
        members = tar.getmembers()
        for m in members:
            name = PurePosixPath(m.name)
            if name.is_absolute() or ".." in name.parts:
                raise CtlError("invalid-bundle", f"unsafe path in bundle: {m.name[:200]}")
            if not (m.isfile() or m.isdir()):
                raise CtlError(
                    "invalid-bundle", f"links and devices are not allowed: {m.name[:200]}"
                )
            total += m.size
        if total > MAX_UNPACKED:
            raise CtlError("invalid-bundle", "the bundle unpacks to more than 200 MB")
        tar.extractall(dest, members=members, filter="data")


def check_unpacked(ctx: Context, root: Path) -> dict:
    for entry in REQUIRED:
        if not (root / entry).is_file():
            raise CtlError("invalid-bundle", f"missing {entry}")
    for entry in EXECUTABLE:
        if not os.access(root / entry, os.X_OK):
            raise CtlError("invalid-bundle", f"{entry} is not executable")
    try:
        manifest = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CtlError("invalid-bundle", "bundle.json is not valid JSON") from exc
    errors = schema.load(ctx.paths.schema("bundle-manifest")).errors(manifest)
    if errors:
        raise CtlError("invalid-bundle", "bundle.json: " + "; ".join(errors[:3]))
    return manifest


def release_name(manifest: dict) -> str:
    return f"{manifest['tooling_version']}-{manifest['commit'][:12]}"


def point_current(current: Path, target: Path) -> None:
    tmp = current.with_name(f".current-{secrets.token_hex(4)}")
    os.symlink(target, tmp)
    os.replace(tmp, current)


def prune(releases: Path, keep: set[Path]) -> None:
    dirs = sorted(
        (d for d in releases.iterdir() if d.is_dir() and not d.name.startswith(".")),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    for d in dirs[KEEP_RELEASES:]:
        if d.resolve() not in keep:
            shutil.rmtree(d, ignore_errors=True)


def _step(ctx: Context, argv: list[str], release: Path, what: str, code: str) -> None:
    env = dict(os.environ)
    env["SHIPSHAPE_BUNDLE"] = str(release)
    ctx.log(f"converge: {what}")
    r = ctx.runner.run(argv, timeout=STEP_TIMEOUT, env=env)
    if r.err.strip():
        for line in r.err.strip().splitlines()[-20:]:
            ctx.log(f"converge: {what}: {line}")
    if not r.ok:
        raise CtlError(code, f"{what} failed (exit {r.rc})")


def converge(ctx: Context, bundle: str) -> dict:
    path = Path(bundle)
    if not path.is_file():
        raise CtlError("invalid-bundle", f"no bundle at {bundle}")
    verify_checksum(path)

    releases = ctx.paths.releases
    ensure_dir(releases, 0o755)
    current = ctx.paths.current
    previous = Path(os.readlink(current)) if current.is_symlink() else None
    from_name = previous.name if previous else None

    tmp = Path(tempfile.mkdtemp(prefix=".unpack-", dir=releases))
    moved_aside: Path | None = None
    try:
        safe_extract(path, tmp)
        manifest = check_unpacked(ctx, tmp)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    name = release_name(manifest)
    dest = releases / name
    if dest.exists():
        moved_aside = releases / f".old-{name}-{secrets.token_hex(4)}"
        os.rename(dest, moved_aside)
    os.rename(tmp, dest)
    os.chmod(dest, 0o755)
    point_current(current, dest)

    try:
        state = ctx.paths.state
        ensure_dir(state, 0o755)
        if path.resolve() != (state / "bundle.tar.gz").resolve():
            shutil.copyfile(path, state / "bundle.tar.gz")
        os.chmod(state / "bundle.tar.gz", 0o600)
        python = [sys.executable, "-I"]
        _step(
            ctx,
            [str(dest / "provision/ship-provision"), "--converge"],
            dest,
            "ship-provision --converge",
            "provision-failed",
        )
        _step(
            ctx,
            [*python, str(dest / "bin/ship-ctl"), "apply", "--json"],
            dest,
            "ship-ctl apply",
            "provision-failed",
        )
        _step(
            ctx,
            [*python, str(dest / "bin/ship-ctl"), "tools", "run", "--json"],
            dest,
            "ship-ctl tools run",
            "tool-failed",
        )
    except BaseException:
        ctx.log(f"converge: rolling back to {from_name or 'no release'}")
        if moved_aside is not None:
            shutil.rmtree(dest, ignore_errors=True)
            os.rename(moved_aside, dest)
            moved_aside = None
        if previous is not None:
            point_current(current, previous)
        elif current.is_symlink():
            current.unlink()
        raise
    finally:
        if moved_aside is not None:
            shutil.rmtree(moved_aside, ignore_errors=True)

    with contextlib.suppress(OSError):
        prune(releases, {dest.resolve()})
    return {"from": from_name, "to": name}
