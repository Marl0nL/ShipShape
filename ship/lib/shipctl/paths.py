"""Where things live on a Ship (contracts/ship-runtime.md).

Every absolute path goes through `Paths.p`, so tests can point the whole tool at a
temporary folder with SHIPSHAPE_ROOT. With a root other than "/", ship-ctl runs in
sandbox mode: no root check, no switching to the agent user, no chown.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

AGENT = "agent"


@dataclass(frozen=True)
class Paths:
    root: Path
    bundle: Path

    @classmethod
    def from_env(cls) -> Paths:
        root = Path(os.environ.get("SHIPSHAPE_ROOT") or "/")
        default_bundle = Path(__file__).resolve().parents[2]  # <bundle>/lib/shipctl/
        bundle = Path(os.environ.get("SHIPSHAPE_BUNDLE") or default_bundle)
        return cls(root=root, bundle=bundle)

    @property
    def sandbox(self) -> bool:
        return self.root != Path("/")

    def p(self, path: str) -> Path:
        """Map an absolute path on the Ship into this root."""
        return self.root / path.lstrip("/")

    # --- settings and state (root-owned) ----------------------------------
    @property
    def ship_toml(self) -> Path:
        return self.p("/etc/shipshape/ship.toml")

    @property
    def managed_settings(self) -> Path:
        return self.p("/etc/claude-code/managed-settings.json")

    @property
    def sudoers_agent(self) -> Path:
        return self.p("/etc/sudoers.d/91-shipshape-agent")

    @property
    def opt(self) -> Path:
        return self.p("/opt/shipshape")

    @property
    def releases(self) -> Path:
        return self.opt / "releases"

    @property
    def current(self) -> Path:
        return self.opt / "current"

    @property
    def state(self) -> Path:
        return self.p("/var/lib/shipshape")

    @property
    def run(self) -> Path:
        return self.p("/run/shipshape")

    @property
    def logs(self) -> Path:
        return self.p("/var/log/shipshape")

    @property
    def status_cache(self) -> Path:
        return self.run / "status.json"

    # --- the installed copy of the mode-ledger hook --------------------------
    LEDGER_HOOK = "/opt/shipshape/hooks/mode-ledger"

    # --- the release this ship-ctl came from ------------------------------
    @property
    def versions_toml(self) -> Path:
        here = self.bundle / "versions.toml"
        # In a repo checkout versions.toml sits next to ship/, not inside it.
        return here if here.exists() else self.bundle.parent / "versions.toml"

    @property
    def bundle_json(self) -> Path:
        return self.bundle / "bundle.json"

    def schema(self, name: str) -> Path:
        return self.bundle / "schemas" / f"{name}.schema.json"

    @property
    def recipes(self) -> Path:
        return self.bundle / "tools"

    @property
    def profiles(self) -> Path:
        return self.bundle / "profiles"

    # --- the agent user's home --------------------------------------------
    @property
    def agent_home(self) -> Path:
        return self.p("/home/agent")

    @property
    def agent_state(self) -> Path:
        return self.agent_home / ".local/state/shipshape"

    @property
    def mode_ledger(self) -> Path:
        return self.agent_state / "modes.json"

    @property
    def last_resume(self) -> Path:
        """Written by ship-resume, on tooling before the agent record (read only)."""
        return self.agent_state / "last-resume.json"

    @property
    def agent_env_file(self) -> Path:
        return self.agent_home / ".config/shipshape/env"

    @property
    def proc(self) -> Path:
        fake = self.p("/proc")
        return fake if self.sandbox and fake.is_dir() else Path("/proc")

    def home_rel(self, path: Path) -> str:
        """`path` relative to the agent's home, for HomeFS."""
        return str(path.relative_to(self.agent_home))

    def agent_path(self, path: Path) -> str:
        """The path as the agent sees it on the Ship (strip the sandbox root)."""
        if not self.sandbox:
            return str(path)
        return "/" + str(path.relative_to(self.root))
