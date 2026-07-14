"""Locate the ShipShape repo root and the paths the control plane manages."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def find_root() -> Path:
    """Resolve the ShipShape root.

    Order: $SHIPSHAPE_ROOT, then walk up from CWD looking for a directory that
    contains both docker-compose.yml and egress/. Raises with a clear message
    if neither works (the control plane is pipx-installed, so CWD may be
    anywhere).
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

    # Fall back to the root recorded by install.sh, so `shipshape` runs from anywhere.
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
    def __init__(self, root: Path):
        self.root = root
        self.allowlist = root / "egress" / "allowed_domains.txt"
        self.auth = root / "auth"
        self.state = root / "control-plane" / "state"
        self.spool = root / "control-plane" / "spool"
        self.pending = self.state / "pending.json"
        self.commands = self.state / "commands.json"

    def ensure(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.spool / "req").mkdir(parents=True, exist_ok=True)
        (self.spool / "resp").mkdir(parents=True, exist_ok=True)
        # Seed local config from the tracked *.example templates on first run. The live
        # files are gitignored, so personal entries (approved domains, your SA email)
        # never land in the repo or get inherited by other installs.
        for live, template in (
            (self.allowlist, self.allowlist.with_name("allowed_domains.example.txt")),
            (self.root / "shipshape.toml", self.root / "shipshape.toml.example"),
        ):
            if not live.exists() and template.exists():
                try:
                    live.write_text(template.read_text())
                except OSError:
                    pass

    @classmethod
    def discover(cls) -> "Paths":
        return cls(find_root())


@dataclass
class Config:
    """Control-plane settings from shipshape.toml (env vars win)."""

    agent_sa: str = ""
    agent_container: str = "agent-sandbox"
    command_cwd: str = ""  # where approved agent commands run (default: repo root)

    @classmethod
    def load(cls, root: Path) -> "Config":
        doc: dict = {}
        toml = root / "shipshape.toml"
        if toml.is_file():
            import tomllib

            try:
                with toml.open("rb") as f:  # tomllib requires UTF-8 bytes
                    doc = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError):
                doc = {}
        gcp = doc.get("gcp", {})
        cmds = doc.get("commands", {})
        return cls(
            agent_sa=os.environ.get("SHIPSHAPE_AGENT_SA", gcp.get("agent_sa", "")),
            agent_container=os.environ.get(
                "SHIPSHAPE_AGENT_CONTAINER", gcp.get("agent_container", "agent-sandbox")
            ),
            command_cwd=os.environ.get("SHIPSHAPE_COMMAND_CWD", cmds.get("cwd", "")),
        )
