"""Extract denied domains from Squid's access log.

The `agent_audit` logformat (squid.conf) is whitespace-delimited:

    %ts.%03tu %6tr %>a %Ss/%03>Hs %<st %rm %ru %[un %Sh/%<a %mt
      0        1    2   3          4     5   6   7    8         9

A denial has field[3] starting with "TCP_DENIED". We only surface denials on
ports 80/443 — other TCP_DENIED entries come from the port-policy guards
(deny !Safe_ports / deny CONNECT !SSL_ports) and approving a *domain* would not
help them, so they would just be noise in the queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass
class Denied:
    host: str
    method: str


@dataclass
class Access:
    host: str
    method: str
    result: str  # e.g. TCP_DENIED/403, TCP_TUNNEL/200
    allowed: bool


def _host_port(target: str) -> tuple[str | None, str | None]:
    if "://" in target:
        u = urlsplit(target)
        try:
            port = u.port  # raises ValueError on a malformed/out-of-range port
        except ValueError:
            return (None, None)
        default = "443" if u.scheme == "https" else "80"
        return (u.hostname, str(port) if port else default)
    if ":" in target:  # CONNECT host:443
        host, _, port = target.rpartition(":")
        return (host.strip("[]") or None, port)  # strip IPv6 brackets
    return (target or None, None)


def parse_line(line: str) -> Denied | None:
    parts = line.split()
    if len(parts) < 7:
        return None
    if not parts[3].startswith("TCP_DENIED"):
        return None
    method, target = parts[5], parts[6]
    host, port = _host_port(target)
    if not host:
        return None
    if port not in (None, "80", "443"):
        return None
    return Denied(host=host, method=method)


def parse_access(line: str) -> Access | None:
    """Parse ANY agent_audit access line (allowed or denied) for the event feed.
    field[3] is `%Ss/%03>Hs` (e.g. TCP_TUNNEL/200); its '/' distinguishes real
    access lines from cache.log noise on the same stream."""
    parts = line.split()
    if len(parts) < 7:
        return None
    result = parts[3]
    if "/" not in result:
        return None
    host, _ = _host_port(parts[6])
    if not host:
        return None
    return Access(host=host, method=parts[5], result=result, allowed=not result.startswith("TCP_DENIED"))
