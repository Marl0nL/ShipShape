"""The LAN guard: refuse new outgoing connections to private ranges (contracts/settings.md).

One nftables table, `inet shipshape_lan_guard`, with one output chain. Nothing
else in the Ship's firewall is touched. Replies to connections the Ship accepted
(FleetLedger's SSH, attaching with herdr) are always let through, as are loopback,
DHCP, ICMPv6, the default gateways and DNS servers the Ship uses right now, and
every `network.lan_guard_allow` entry.

`ship-ctl apply` writes the rules to /etc/shipshape/lan-guard.nft, loads them and
enables ship-lan-guard.service, which runs `ship-ctl lan-guard` at every boot to
find the gateways and DNS servers again and reload the table.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

from .context import Context
from .errors import CtlError
from .fsutil import remove, write_atomic
from .shiptoml import ShipToml

TABLE = "shipshape_lan_guard"
UNIT = "ship-lan-guard.service"
PRIVATE_V4 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
PRIVATE_V6 = ("fc00::/7", "fe80::/10")
RESOLV_CONF = "/run/systemd/resolve/resolv.conf"  # the upstream servers, not the stub


def rules_path(ctx: Context) -> Path:
    return ctx.paths.p("/etc/shipshape/lan-guard.nft")


Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _network(text: str) -> Network | None:
    try:
        return ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        return None


def gateways(ctx: Context) -> tuple[list[str], list[str]]:
    """Default gateways from `ip -j route show default` (v4 and v6)."""
    found: list[str] = []
    errors: list[str] = []
    for family in ("-4", "-6"):
        r = ctx.runner.run(["ip", family, "-j", "route", "show", "default"], timeout=5)
        if not r.ok:
            errors.append(f"ip {family} route: exit {r.rc}")
            continue
        try:
            routes = json.loads(r.out or "[]")
        except ValueError:
            errors.append(f"ip {family} route: not JSON")
            continue
        for route in routes if isinstance(routes, list) else []:
            gateway = route.get("gateway") if isinstance(route, dict) else None
            if isinstance(gateway, str):
                found.append(gateway)
    return found, errors


def dns_servers(ctx: Context) -> list[str]:
    """The DNS servers systemd-resolved forwards to."""
    try:
        text = ctx.paths.p(RESOLV_CONF).read_text(encoding="utf-8")
    except OSError:
        return []
    servers = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            servers.append(parts[1].split("%")[0])
    return servers


def allowed(ship: ShipToml, discovered: list[str]) -> tuple[list[Network], list[str]]:
    """Every address the guard lets through, parsed; entries that do not parse are
    reported and left out, so nothing but a clean address ever reaches nft."""
    nets: list[Network] = []
    messages: list[str] = []
    for entry in [*discovered, *ship.lan_guard_allow]:
        net = _network(entry)
        if net is None:
            messages.append(f"lan guard: ignoring {entry[:60]!r}, not an address or CIDR")
        elif net not in nets:
            nets.append(net)
    return nets, messages


def _text(net: Network) -> str:
    return str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net)


def render(nets: list[Network]) -> str:
    v4 = [_text(n) for n in nets if n.version == 4]
    v6 = [_text(n) for n in nets if n.version == 6]

    def nft_set(name: str, kind: str, items: list[str] | tuple[str, ...]) -> list[str]:
        lines = [f"  set {name} {{", f"    type {kind}", "    flags interval"]
        if items:
            lines.append(f"    elements = {{ {', '.join(items)} }}")
        return [*lines, "  }"]

    body = [
        "# Written by ship-ctl from /etc/shipshape/ship.toml (network.lan_guard).",
        "# Loading this file replaces the whole table in one transaction.",
        f"table inet {TABLE}",
        f"delete table inet {TABLE}",
        f"table inet {TABLE} {{",
        *nft_set("allow4", "ipv4_addr", v4),
        *nft_set("allow6", "ipv6_addr", v6),
        *nft_set("private4", "ipv4_addr", PRIVATE_V4),
        *nft_set("private6", "ipv6_addr", PRIVATE_V6),
        "  chain output {",
        "    type filter hook output priority filter; policy accept;",
        '    oifname "lo" accept',
        "    ct state established,related accept",
        "    udp dport { 67, 547 } accept",
        "    meta l4proto ipv6-icmp accept",
        "    ip daddr @allow4 accept",
        "    ip6 daddr @allow6 accept",
        "    ip daddr @private4 counter reject",
        "    ip6 daddr @private6 counter reject",
        "  }",
        "}",
    ]
    return "\n".join(body) + "\n"


def table_present(ctx: Context) -> bool:
    return ctx.runner.run(["nft", "list", "table", "inet", TABLE], timeout=10).ok


def unit_enabled(ctx: Context) -> bool:
    return ctx.runner.run(["systemctl", "is-enabled", "--quiet", UNIT], timeout=10).ok


def _run(ctx: Context, argv: list[str], what: str) -> None:
    r = ctx.runner.run(argv, timeout=30)
    if not r.ok:
        detail = (r.err.strip().splitlines() or [f"exit {r.rc}"])[-1]
        raise CtlError("internal", f"lan guard: {what} failed: {detail[:300]}")


def sync(ctx: Context, ship: ShipToml, dry_run: bool = False,
         reload: bool = False) -> tuple[list[str], list[str]]:
    """Make the table, the rules file and the boot unit match ship.toml.

    Returns (changed, messages). `reload` loads the rules even if nothing changed
    (the boot unit uses it, since the gateway or DNS servers may differ).
    """
    path = rules_path(ctx)
    changed: list[str] = []
    messages: list[str] = []
    if not ship.lan_guard:
        if remove(path, dry_run=dry_run):
            changed.append(ctx.paths.agent_path(path))
        if table_present(ctx):
            changed.append(f"nftables table inet {TABLE} (removed)")
            if not dry_run:
                _run(ctx, ["nft", "delete", "table", "inet", TABLE], "removing the table")
        if unit_enabled(ctx):
            changed.append(f"{UNIT} (disabled)")
            if not dry_run:
                _run(ctx, ["systemctl", "disable", "--quiet", UNIT], f"disabling {UNIT}")
        return changed, messages

    found, errors = gateways(ctx)
    messages += [f"lan guard: {e}" for e in errors]
    discovered = found + dns_servers(ctx)
    if not found:
        messages.append("lan guard: no default gateway found; only lan_guard_allow and DNS "
                        "are let through to private addresses")
    nets, notes = allowed(ship, discovered)
    messages += notes
    text = render(nets)
    wrote = write_atomic(path, text, 0o644, dry_run=dry_run)
    if wrote:
        changed.append(ctx.paths.agent_path(path))
    if wrote or reload or not table_present(ctx):
        if wrote or not reload:
            changed.append(f"nftables table inet {TABLE} (loaded)")
        if not dry_run:
            _run(ctx, ["nft", "-c", "-f", str(path)], "checking the rules")
            _run(ctx, ["nft", "-f", str(path)], "loading the rules")
    if not unit_enabled(ctx):
        changed.append(f"{UNIT} (enabled)")
        if not dry_run:
            _run(ctx, ["systemctl", "enable", "--quiet", UNIT], f"enabling {UNIT}")
    return changed, messages
