"""Read, check and write /etc/shipshape/ship.toml (contracts/ship.toml.schema.json)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import schema, tomlw
from .errors import CtlError
from .fsutil import write_atomic
from .paths import Paths

HEADER = (
    "This Ship's settings. Change them with `ship-ctl set`, not by hand.\n"
    "Schema: contracts/ship.toml.schema.json in the ShipShape repo."
)

CLAUDE_DEFAULTS = {
    "remote_control": "on",
    "default_mode": "auto",
    "allow_bypass": False,
    "update_channel": "latest",
}


@dataclass
class ShipToml:
    doc: dict

    @property
    def name(self) -> str:
        return self.doc["ship"]["name"]

    @property
    def id(self) -> str:
        return self.doc["ship"]["id"]

    @property
    def tags(self) -> list[str]:
        return list(self.doc["ship"].get("tags", []))

    @property
    def claude(self) -> dict:
        """Claude settings with defaults filled in (settings.md)."""
        out = dict(CLAUDE_DEFAULTS)
        out.update(self.doc.get("claude", {}))
        out.setdefault("session_prefix", self.name)
        return out

    @property
    def agent_sudo(self) -> bool:
        return bool(self.doc.get("agent", {}).get("sudo", False))

    @property
    def lan_guard(self) -> bool:
        return bool(self.doc.get("network", {}).get("lan_guard", False))

    @property
    def lan_guard_allow(self) -> list[str]:
        return list(self.doc.get("network", {}).get("lan_guard_allow", []))

    @property
    def timezone(self) -> str:
        """IANA zone for the Ship's clock (settings.md, "OS updates"); UTC when absent."""
        return self.doc["ship"].get("timezone", "UTC")

    @property
    def updates(self) -> dict:
        """OS update policy with defaults filled in (settings.md, "OS updates")."""
        out = {"auto_reboot": False, "reboot_time": "04:00"}
        out.update(self.doc.get("updates", {}))
        return out

    @property
    def owner(self) -> dict | None:
        """The owner's login ([owner] name and keys), or None: no owner login."""
        owner = self.doc.get("owner")
        return dict(owner) if owner and owner.get("name") else None

    @property
    def autostart(self) -> list[dict]:
        return list(self.doc.get("autostart", []))

    @property
    def tools(self) -> list[str]:
        return list(self.doc.get("tools", {}).get("install", []))

    @property
    def profiles(self) -> list[str]:
        return list(self.doc.get("tools", {}).get("profiles", []))

    def profile_options(self, profile: str) -> dict[str, str]:
        return dict(self.doc.get("profile_options", {}).get(profile, {}))


def check(doc: object, paths: Paths) -> list[str]:
    """Every reason `doc` is not a valid ship.toml (empty when valid)."""
    errors = schema.load(paths.schema("ship.toml")).errors(doc)
    if not errors and isinstance(doc, dict):
        names = [a["name"] for a in doc.get("autostart", [])]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            errors.append(f"autostart: duplicate agent names: {', '.join(dupes)}")
    return errors


def zone_installed(paths: Paths, zone: str) -> bool:
    """Is `zone` a time zone tzdata has on this Ship? The schema pattern already
    rules out dots and leading slashes, so this stays inside /usr/share/zoneinfo."""
    return paths.p(f"/usr/share/zoneinfo/{zone}").is_file()


def parse(text: str, paths: Paths) -> ShipToml:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CtlError("invalid-ship-toml", f"ship.toml is not valid TOML: {exc}") from exc
    errors = check(doc, paths)
    if errors:
        raise CtlError("invalid-ship-toml", "; ".join(errors[:5]))
    return ShipToml(doc)


def load(paths: Paths) -> ShipToml:
    try:
        text = paths.ship_toml.read_text(encoding="utf-8")
    except OSError as exc:
        raise CtlError("internal", f"cannot read {paths.ship_toml}: {exc.strerror}") from exc
    return parse(text, paths)


def save(doc: dict, path: Path, dry_run: bool = False) -> bool:
    return write_atomic(path, tomlw.dumps(doc, HEADER), 0o644, dry_run=dry_run)
