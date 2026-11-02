"""Runtime component provisioning — the backing logic for the init/provision wizard.

Dev stacks are NOT baked into the image; they are installed on demand. A component
maps to an install command (a PATH command in the agent image, e.g. install-flutter)
plus the egress domains it needs. Provisioning enables those domains in the
allow-list and runs the installer in the container.

Domain safety: provisioning only records domains it *newly added*, so deprovisioning
disables exactly those — baseline/shared domains that were already present are never
touched. Extra components can be defined in shipshape.toml under [components.<name>].
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import docker_ops, egress
from .allowlist import Allowlist
from .config import Config, Paths


@dataclass
class Component:
    name: str
    install: str
    domains: list[str] = field(default_factory=list)
    check: str = ""
    description: str = ""


REGISTRY: dict[str, Component] = {
    "flutter": Component(
        "flutter",
        "install-flutter",
        # storage.googleapis.com is covered by the baseline .googleapis.com allow.
        ["dl.google.com", "pub.dev", ".gstatic.com"],
        "flutter --version",
        "Flutter / Dart SDK",
    ),
    "adb": Component(
        "adb",
        "install-adb",
        ["dl.google.com"],
        "adb version",
        "Android platform-tools (adb)",
    ),
}


def load_registry(root: Path) -> dict[str, Component]:
    reg = dict(REGISTRY)
    toml = root / "shipshape.toml"
    if toml.is_file():
        import tomllib

        try:
            with toml.open("rb") as f:
                doc = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            doc = {}
        for name, spec in (doc.get("components", {}) or {}).items():
            if isinstance(spec, dict) and spec.get("install"):
                reg[name] = Component(
                    name,
                    spec["install"],
                    list(spec.get("domains", [])),
                    spec.get("check", ""),
                    spec.get("description", ""),
                )
    return reg


def _state_file(paths: Paths) -> Path:
    return paths.state / "provisioned.json"


def _load(paths: Paths) -> dict:
    f = _state_file(paths)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save(paths: Paths, data: dict) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    f = _state_file(paths)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(f)


def provision(
    paths: Paths,
    cfg: Config,
    name: str,
    run=docker_ops.run,
    is_running=docker_ops.container_running,
) -> tuple[bool, str]:
    reg = load_registry(paths.root)
    comp = reg.get(name)
    if not comp:
        return False, f"unknown component '{name}' (known: {', '.join(sorted(reg))})"

    # 1. enable the component's domains; remember only the ones we actually added
    #    (so deprovision never disables a pre-existing baseline domain).
    al = Allowlist.load(paths.allowlist)
    added = [d for d in comp.domains if al.add(d, enabled=True)]
    res = egress.apply(al)
    if not res.ok:
        return False, f"failed to enable domains: {res.output}"
    st = _load(paths)
    st[name] = {"added": added}
    _save(paths, st)

    # 2. run the installer inside the container (if it's up)
    if not is_running(cfg.agent_container):
        return True, (
            f"enabled domains for {name}; start {cfg.agent_container} and re-run "
            f"`shipshape provision {name}` to install"
        )
    r = run(["docker", "exec", cfg.agent_container, "sh", "-lc", comp.install], timeout=1800)
    return r.ok, (r.output or f"installed {name}") if r.ok else f"install failed:\n{r.output}"


def deprovision(paths: Paths, name: str) -> tuple[bool, str]:
    """Disable exactly the domains provisioning added (baseline/shared untouched).
    Does not uninstall the component."""
    reg = load_registry(paths.root)
    if name not in reg:
        return False, f"unknown component '{name}'"
    st = _load(paths)
    added = st.get(name, {}).get("added", [])
    al = Allowlist.load(paths.allowlist)
    for d in added:
        al.set_enabled(d, False)
    res = egress.apply(al)
    if res.ok:
        st.pop(name, None)
        _save(paths, st)
        return True, f"disabled {len(added)} domain(s) added for {name} (not uninstalled)"
    return False, res.output


def statuses(
    paths: Paths,
    cfg: Config,
    probe: bool = False,
    run=docker_ops.run,
    is_running=docker_ops.container_running,
) -> list[dict]:
    reg = load_registry(paths.root)
    enabled = set(Allowlist.load(paths.allowlist).enabled_domains())
    provisioned = _load(paths)
    out = []
    for name, comp in sorted(reg.items()):
        dom_on = all(d in enabled for d in comp.domains) if comp.domains else True
        installed = None
        if probe and comp.check and is_running(cfg.agent_container):
            installed = run(
                ["docker", "exec", cfg.agent_container, "sh", "-lc", comp.check], timeout=30
            ).ok
        out.append(
            {
                "name": name,
                "description": comp.description,
                "domains_enabled": dom_on,
                "provisioned": name in provisioned,
                "installed": installed,
            }
        )
    return out
