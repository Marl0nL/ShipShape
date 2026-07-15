"""One-time migration of a pre-multi-instance ShipShape install into instances/default/.

The old layout kept per-install data at the repo root (auth/, control-plane/state, …).
This moves that data under instances/default/ so the default instance behaves like any
other, retags the default image, and stops the old (unprefixed) stack so its old-named
containers don't linger.

Safe + idempotent: no-ops once instances/default/ exists; leaves the tracked templates and
READMEs at the root; only MOVES files that exist and aren't already at the destination;
never deletes a source on failure.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import docker_ops

# (root-relative source, dest-relative-to instances/default) — only the LIVE, per-install
# data; tracked templates (*.example) and auth/README.md stay at the root.
_MOVES = [
    ("auth/gcp-sa.json", "auth/gcp-sa.json"),
    ("auth/gh-token", "auth/gh-token"),
    ("auth/claude-token", "auth/claude-token"),
    ("control-plane/state", "state"),
    ("control-plane/spool", "spool"),
    ("egress/allowed_domains.txt", "egress/allowed_domains.txt"),
    ("shipshape.toml", "shipshape.toml"),
    ("squid.conf", "squid.conf"),
    ("firstmate-data", "firstmate-data"),
]


def _has_old_layout(root: Path) -> bool:
    return any(
        [
            (root / "control-plane" / "state").is_dir(),
            (root / "control-plane" / "spool").is_dir(),
            (root / "shipshape.toml").is_file(),
            (root / "egress" / "allowed_domains.txt").is_file(),
            (root / "firstmate-data").is_dir(),
            (root / "auth" / "gcp-sa.json").exists(),
            (root / "auth" / "gh-token").exists(),
            (root / "auth" / "claude-token").exists(),
        ]
    )


def migrate_if_needed(root: Path) -> str | None:
    """Migrate the root layout into instances/default/ if needed. Returns the dest path
    if a migration ran, else None."""
    dst = root / "instances" / "default"
    if dst.exists():
        return None  # already migrated (or default instance already created)
    if not _has_old_layout(root):
        return None  # fresh install — nothing to migrate

    (dst / "auth").mkdir(parents=True, exist_ok=True)
    (dst / "egress").mkdir(parents=True, exist_ok=True)

    # Stop the OLD (unprefixed / repo-dir project) stack so its old-named containers
    # (agent-sandbox, egress-proxy, …) don't linger alongside the new default-* ones.
    docker_ops.run(
        ["docker", "compose", "-f", str(root / "docker-compose.yml"), "down"], timeout=180
    )

    for src_rel, dst_rel in _MOVES:
        src = root / src_rel
        target = dst / dst_rel
        if src.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(target))
            except OSError:
                pass  # leave the source in place on failure; never destructive

    # Retag the old default images into the per-instance namespace so `up` finds them
    # without a rebuild (best-effort; ignore if docker/images are absent).
    listing = docker_ops.run(
        ["docker", "images", "shipshape-agent", "--format", "{{.Tag}}"], timeout=20
    )
    if listing.ok:
        for tag in {t for t in listing.output.split() if t and t != "<none>"}:
            docker_ops.run(
                ["docker", "tag", f"shipshape-agent:{tag}", f"shipshape-agent-default:{tag}"],
                timeout=30,
            )

    return str(dst)
