"""Per-instance snapshot & relaunch of tagged agent images.

Each instance has its own image namespace, `shipshape-agent-<instance>:<tag>`, so a
company instance and a personal instance keep entirely separate images. Flow: build
the base once, install what you need at runtime, then `snapshot <tag>` (docker commit)
to save a reusable image. The active tag is persisted per instance; the stack boots it
via the SHIPSHAPE_AGENT_IMAGE compose var. `fork` copies another instance's image into
this one's namespace as a convenient starting point.
"""

from __future__ import annotations

from . import config, docker_ops
from .config import Paths


def prefix(paths: Paths) -> str:
    return f"shipshape-agent-{paths.instance}"


def default_image(paths: Paths) -> str:
    return f"{prefix(paths)}:base"


def _qualify(paths: Paths, tag: str) -> str:
    return tag if ":" in tag else f"{prefix(paths)}:{tag}"


def _active_file(paths: Paths):
    return paths.state / "active_image"


def active(paths: Paths) -> str:
    f = _active_file(paths)
    if f.exists():
        v = f.read_text().strip()
        if v:
            return v
    return default_image(paths)


def set_active(paths: Paths, tag: str) -> str:
    paths.state.mkdir(parents=True, exist_ok=True)
    q = _qualify(paths, tag)
    _active_file(paths).write_text(q + "\n")
    return q


def snapshot(paths: Paths, tag: str, container: str | None = None) -> docker_ops.Result:
    """Commit this instance's running container to shipshape-agent-<instance>:<tag>."""
    container = container or config.active_config().agent_container
    return docker_ops.run(["docker", "commit", container, _qualify(paths, tag)], timeout=180)


def delete(paths: Paths, tag: str) -> docker_ops.Result:
    return docker_ops.run(["docker", "rmi", _qualify(paths, tag)], timeout=60)


def rename(paths: Paths, old: str, new: str) -> docker_ops.Result:
    r = docker_ops.run(["docker", "tag", _qualify(paths, old), _qualify(paths, new)], timeout=30)
    if not r.ok:
        return r
    return docker_ops.run(["docker", "rmi", _qualify(paths, old)], timeout=30)


def rebuild(paths: Paths) -> docker_ops.Result:
    """Rebuild this instance's BASE image from the Dockerfile. Always targets
    shipshape-agent-<instance>:base so it never overwrites a saved snapshot."""
    return docker_ops.compose(["build", "agent-sandbox"], image=default_image(paths), timeout=1800)


def fork(paths: Paths, from_instance: str, src_tag: str = "base", dst_tag: str = "base") -> docker_ops.Result:
    """Copy another instance's image into THIS instance's namespace (docker tag), e.g.
    seed `personal` from `company`'s configured base. NOTE: this carries over whatever is
    baked into that image, including any credentials snapshotted into it."""
    src = f"shipshape-agent-{from_instance}:{src_tag}"
    dst = _qualify(paths, dst_tag)
    r = docker_ops.run(["docker", "tag", src, dst], timeout=30)
    return docker_ops.Result(r.ok, f"forked {src} → {dst}" if r.ok else r.output)


def snapshots(paths: Paths) -> list[dict]:
    r = docker_ops.run(
        ["docker", "images", prefix(paths), "--format", "{{.Tag}}|{{.Size}}|{{.CreatedSince}}"],
        timeout=20,
    )
    out: list[dict] = []
    if r.ok:
        for line in r.output.splitlines():
            parts = line.split("|")
            if len(parts) >= 3 and parts[0] and parts[0] != "<none>":
                out.append({"tag": parts[0], "size": parts[1], "created": parts[2]})
    return out
