"""Apply an allow-list edit and hot-reload Squid, rolling back on failure so a bad
rule set can never silently take egress down. Shared by the CLI, the TUI, and the
provisioner (kept here to avoid an import cycle through cli.py)."""

from __future__ import annotations

import shutil

from . import docker_ops
from .allowlist import Allowlist


def apply(al: Allowlist) -> docker_ops.Result:
    al.save()
    if not docker_ops.container_running():
        return docker_ops.Result(True, "saved (egress-proxy not running; will apply on next start)")
    r = docker_ops.reconfigure()
    if not r.ok and al.path is not None:
        bak = al.path.with_name(al.path.name + ".bak")
        if bak.exists():
            shutil.copy2(bak, al.path)
            docker_ops.reconfigure()
        return docker_ops.Result(False, f"reconfigure failed — rolled back:\n{r.output}")
    return r
