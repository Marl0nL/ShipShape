"""Locate the ShipShape repo root, select the active instance, and resolve the
per-instance paths + container names the control plane manages.

Multiple independent instances run on one host, each a separate compose project.
Per-instance data lives under ``<root>/instances/<name>/`` (auth/, state/, spool/,
egress allow-list, squid.conf, shipshape.toml, firstmate-data). Selection order:
``--instance`` flag  >  ``$SHIPSHAPE_INSTANCE``  >  ``"default"``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_INSTANCE = "default"
# Safe for use as a container-name / compose-project / image-tag prefix.
_INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def resolve_instance(explicit: str | None = None) -> str:
    name = (explicit or os.environ.get("SHIPSHAPE_INSTANCE") or DEFAULT_INSTANCE).strip()
    if not _INSTANCE_RE.match(name):
        raise SystemExit(
            f"invalid instance name {name!r}: use 1-31 chars of [a-z0-9-], starting "
            "with a letter or digit."
        )
    return name


def find_root() -> Path:
    """Resolve the ShipShape repo root.

    Order: $SHIPSHAPE_ROOT, then walk up from CWD looking for a directory that
    contains both docker-compose.yml and egress/. Falls back to the root recorded
    by install.sh so `shipshape` runs from anywhere.
    """
    env = os.environ.get("SHIPSHAPE_ROOT")
    if env:
        root = Path(env).expanduser().resolve()
        if (root / "docker-compose.yml").is_file():
            return root
        raise SystemExit(f"SHIPSHAPE_ROOT={root} has no docker-compose.yml")

    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / "docker-compose.yml").is_file() and (d / "egress").is_dir():
            return d

    conf = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "shipshape" / "root"
    if conf.is_file():
        root = Path(conf.read_text().strip()).expanduser()
        if (root / "docker-compose.yml").is_file():
            return root

    raise SystemExit(
        "Could not locate the ShipShape root. Re-run ./install.sh, cd into the repo, "
        "or set SHIPSHAPE_ROOT=/path/to/ShipShape."
    )


class Paths:
    """Per-instance filesystem layout. `root` is the repo; everything else lives under
    `root/instances/<instance>/`. Tracked templates (`*.example`, squid.conf.example)
    stay at the repo root and seed a new instance on first use."""

    def __init__(self, root: Path, instance: str = DEFAULT_INSTANCE):
        self.root = root
        self.instance = instance
        self.instance_dir = root / "instances" / instance
        self.auth = self.instance_dir / "auth"
        self.state = self.instance_dir / "state"
        self.spool = self.instance_dir / "spool"
        self.allowlist = self.instance_dir / "egress" / "allowed_domains.txt"
        self.squid_conf = self.instance_dir / "squid.conf"
        self.toml = self.instance_dir / "shipshape.toml"
        self.firstmate_data = self.instance_dir / "firstmate-data"
        self.pending = self.state / "pending.json"
        self.commands = self.state / "commands.json"

    # --- tracked templates at the repo root (shared defaults; git-committed) ---
    @property
    def allowlist_template(self) -> Path:
        return self.root / "egress" / "allowed_domains.example.txt"

    @property
    def toml_template(self) -> Path:
        return self.root / "shipshape.toml.example"

    @property
    def squid_template(self) -> Path:
        return self.root / "squid.conf.example"

    def exists(self) -> bool:
        return self.instance_dir.is_dir()

    def ensure(self) -> None:
        """Create the instance's dirs and seed its config from the tracked templates on
        first run. Never overwrites an existing live file."""
        self.state.mkdir(parents=True, exist_ok=True)
        (self.spool / "req").mkdir(parents=True, exist_ok=True)
        (self.spool / "resp").mkdir(parents=True, exist_ok=True)
        self.auth.mkdir(parents=True, exist_ok=True)
        (self.instance_dir / "egress").mkdir(parents=True, exist_ok=True)
        self.firstmate_data.mkdir(parents=True, exist_ok=True)
        for live, template in (
            (self.allowlist, self.allowlist_template),
            (self.toml, self.toml_template),
            (self.squid_conf, self.squid_template),
        ):
            if not live.exists() and template.exists():
                try:
                    live.write_text(template.read_text())
                except OSError:
                    pass

    @classmethod
    def discover(cls, instance: str | None = None) -> "Paths":
        return cls(find_root(), resolve_instance(instance))


@dataclass
class Config:
    """Per-instance control-plane settings (from the instance's shipshape.toml; env
    vars win). Container names + the compose project are DERIVED from the instance name
    so two instances never collide on host-global container names."""

    instance: str = DEFAULT_INSTANCE
    project: str = "shipshape-default"
    agent_sa: str = ""
    command_cwd: str = ""  # where approved agent commands run (default: repo root)
    agent_container: str = "default-agent-sandbox"
    proxy_container: str = "default-egress-proxy"
    broker_container: str = "default-control-plane"
    adb_container: str = "default-adb-relay"

    @classmethod
    def load(cls, paths: Paths) -> "Config":
        doc: dict = {}
        if paths.toml.is_file():
            import tomllib

            try:
                with paths.toml.open("rb") as f:  # tomllib requires UTF-8 bytes
                    doc = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError):
                doc = {}
        gcp = doc.get("gcp", {})
        cmds = doc.get("commands", {})
        inst = paths.instance
        return cls(
            instance=inst,
            project=f"shipshape-{inst}",
            agent_sa=os.environ.get("SHIPSHAPE_AGENT_SA", gcp.get("agent_sa", "")),
            command_cwd=os.environ.get("SHIPSHAPE_COMMAND_CWD", cmds.get("cwd", "")),
            agent_container=f"{inst}-agent-sandbox",
            proxy_container=f"{inst}-egress-proxy",
            broker_container=f"{inst}-control-plane",
            adb_container=f"{inst}-adb-relay",
        )


# --- process-global active instance context ---------------------------------
# Each `shipshape` process serves exactly ONE instance, resolved once at startup
# (main() / the TUI). docker_ops + egress read this to target the right instance's
# containers without threading Config through every call site.
_active_paths: Paths | None = None
_active_config: Config | None = None


def set_active(paths: Paths, cfg: Config) -> None:
    global _active_paths, _active_config
    _active_paths, _active_config = paths, cfg


def active_paths() -> Paths:
    if _active_paths is None:
        raise RuntimeError("no active ShipShape instance (call config.set_active first)")
    return _active_paths


def active_config() -> Config:
    if _active_config is None:
        raise RuntimeError("no active ShipShape instance (call config.set_active first)")
    return _active_config
