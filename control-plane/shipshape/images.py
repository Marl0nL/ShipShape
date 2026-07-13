"""Snapshot & relaunch tagged agent images.

Flow: build/provision the base once, install what you need at runtime, then
`snapshot <tag>` (docker commit) to save a reusable image `shipshape-agent:<tag>`.
The active tag is persisted; the stack boots whichever tag is active (via the
SHIPSHAPE_AGENT_IMAGE compose var).
"""

from __future__ import annotations

from pathlib import Path

from . import docker_ops
from .config import Paths

DEFAULT = "shipshape-agent:base"
PREFIX = "shipshape-agent"


def _qualify(tag: str) -> str:
    return tag if ":" in tag else f"{PREFIX}:{tag}"


def _active_file(paths: Paths) -> Path:
    return paths.state / "active_image"


def active(paths: Paths) -> str:
    f = _active_file(paths)
    if f.exists():
        v = f.read_text().strip()
        if v:
            return v
    return DEFAULT


def set_active(paths: Paths, tag: str) -> str:
    paths.state.mkdir(parents=True, exist_ok=True)
    q = _qualify(tag)
    _active_file(paths).write_text(q + "\n")
    return q


def snapshot(tag: str, container: str = "agent-sandbox") -> docker_ops.Result:
    """Commit the running container's current state to shipshape-agent:<tag>."""
    return docker_ops.run(["docker", "commit", container, _qualify(tag)], timeout=180)


def snapshots() -> list[dict]:
    r = docker_ops.run(
        ["docker", "images", PREFIX, "--format", "{{.Tag}}|{{.Size}}|{{.CreatedSince}}"],
        timeout=20,
    )
    out: list[dict] = []
    if r.ok:
        for line in r.output.splitlines():
            parts = line.split("|")
            if len(parts) >= 3 and parts[0] and parts[0] != "<none>":
                out.append({"tag": parts[0], "size": parts[1], "created": parts[2]})
    return out
