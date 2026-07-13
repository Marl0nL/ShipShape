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


def _host_port(target: str) -> tuple[str | None, str | None]:
    if "://" in target:
        u = urlsplit(target)
        return (u.hostname, str(u.port) if u.port else "80")
    if ":" in target:  # CONNECT host:443
        host, _, port = target.rpartition(":")
        return (host or None, port)
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
