"""Find Ships announcing themselves on the LAN, and fetch a host key to adopt.

Announcements (`_shipshape._tcp`, contracts/ship-runtime.md) are hints only:
they say where a Ship might be, never whether to trust it. Trust comes from
comparing the host key's fingerprint with the VM console (`ship-fleet adopt`).
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

from fleetledger.errors import ConfigError, FleetError

SERVICE_TYPE = "_shipshape._tcp.local."


@dataclass(frozen=True)
class Announcement:
    instance: str
    host: str
    port: int
    addresses: tuple[str, ...] = ()
    txt: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.txt.get("name") or self.instance

    @property
    def ship_id(self) -> str | None:
        return self.txt.get("id")


def browse(timeout: float = 3.0) -> list[Announcement]:
    """Listen for Ship announcements for a few seconds (zeroconf)."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError as exc:  # pragma: no cover - a declared dependency
        raise ConfigError("ship-fleet scan needs the zeroconf package.") from exc

    found: dict[str, Announcement] = {}

    class Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=2000)
            if info is None:
                return
            txt = {
                k.decode(errors="replace"): (v or b"").decode(errors="replace")
                for k, v in (info.properties or {}).items()
            }
            found[name] = Announcement(
                instance=name.removesuffix("." + type_),
                host=(info.server or "").rstrip("."),
                port=info.port or 22,
                addresses=tuple(info.parsed_addresses()),
                txt=txt,
            )

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            self.add_service(zc, type_, name)

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            found.pop(name, None)

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICE_TYPE, Listener())
        time.sleep(timeout)
    finally:
        zc.close()
    return sorted(found.values(), key=lambda a: a.name)


def keyscan(host: str, timeout: int = 10) -> str:
    """The ed25519 host key a host presents: '<type> <base64>'. Unverified."""
    try:
        proc = subprocess.run(
            ["ssh-keyscan", "-t", "ed25519", "-T", str(timeout), "--", host],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except FileNotFoundError as exc:
        raise FleetError("ssh-keyscan not found; install OpenSSH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise FleetError(f"{host} did not answer within {timeout}s.") from exc
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#") and parts[1] == "ssh-ed25519":
            return f"{parts[1]} {parts[2]}"
    raise FleetError(f"{host} presented no ed25519 host key (is it a Ship, and is it up?).")
