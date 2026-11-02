"""Apply an allow-list edit and hot-reload Squid, rolling back on failure so a bad
rule set can never silently take egress down. Shared by the CLI, the TUI, and the
provisioner (kept here to avoid an import cycle through cli.py)."""

from __future__ import annotations

import shutil

from . import docker_ops
from .allowlist import Allowlist
from .config import Paths


def set_bandwidth(paths: Paths, mbps: int) -> docker_ops.Result:
    """Rewrite Squid's delay-pool cap (MB/s) in squid.conf. squid.conf is a single-file
    mount, so a RUNNING proxy needs a force-recreate to pick it up — we do that when it's
    up; otherwise the change applies on next boot."""
    conf = paths.squid_conf
    try:
        lines = conf.read_text().splitlines()
    except OSError as e:
        return docker_ops.Result(False, str(e))
    rate = max(1, int(mbps)) * 1048576  # MB/s -> bytes/s
    out, hit = [], False
    for ln in lines:
        if ln.strip().startswith("delay_parameters 1"):
            out.append(f"delay_parameters 1 {rate}/{rate}")
            hit = True
        else:
            out.append(ln)
    if not hit:
        return docker_ops.Result(False, "no `delay_parameters 1` line in squid.conf")
    conf.write_text("\n".join(out) + "\n")
    if docker_ops.container_running():
        r = docker_ops.compose(["up", "-d", "--force-recreate", "egress-proxy"], timeout=120)
        return docker_ops.Result(
            r.ok, f"bandwidth → {mbps} MB/s; egress-proxy recreated" if r.ok else r.output
        )
    return docker_ops.Result(True, f"bandwidth → {mbps} MB/s (applies on next boot)")


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
