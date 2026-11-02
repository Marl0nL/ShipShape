# Network visibility

Ships have normal internet access. ShipShape does not filter, proxy or block
traffic. Instead, each Ship records which programs reached which domains,
roughly how much they moved, and which destinations are new. You can then
review what agents did afterwards. Like the old proxy log, it records domains,
not URLs; there is no TLS inspection.

## On each Ship

`ship-netwatch.service` runs as root and only observes. It is installed by
`ship-provision`, together with Ubuntu's `libbpf-tools` and `vnstat`. Its
capabilities are bounded to `CAP_BPF CAP_PERFMON` (the eBPF tools) and
`CAP_SYS_PTRACE CAP_DAC_READ_SEARCH` (so `ss -p` can name other users'
processes), with no home folders and a read-only system.

| Source | What it gives |
|---|---|
| `tcpconnect-libbpf` (eBPF) | every new outbound TCP connection, with the program (process name) that made it |
| `tcplife-libbpf` (eBPF) | bytes sent and received per connection, when it closes |
| `resolvectl monitor --json=short` | DNS answers, so each connection is shown by the name it was looked up by, not just an address |
| `vnstat` | bytes per network interface, from kernel counters (everything, including traffic nothing else can attribute) |

These are Ubuntu's precompiled (CO-RE) builds of the BCC project's tools; they
need the kernel's BTF, which Ubuntu kernels have, and compile nothing on the
Ship. The plan named the Python `bpfcc-tools`. On Ubuntu 26.04 those cannot
compile their programs against kernel 7.0's headers, so they are not used.

If the eBPF tools cannot run, the collector falls back to polling `ss` every 5
seconds. Short connections can then be missed, and there are no byte counts.
The summary says which source is in use.

The collector keeps a rolling store in `/var/lib/shipshape/net/` (root only).
For each destination it holds first seen, last seen, ports, addresses, and per
hour the connections, bytes and programs, for 7 days. When each destination was
first seen is kept for 90 days, so a domain contacted again after a quiet week
is not reported as new. The store is capped at 2000 destinations; the least
recently used are dropped first, and the summary counts them. The journal gets
one line per destination seen for the first time, and an hourly digest
(`ship-ctl logs system`).

`ship-ctl net --json` prints the summary (`contracts/net-summary.schema.json`).
The status document's `net` section has the last 24 hours: traffic, counts, and
destinations first seen in that window. If the collector is installed but not
running, the Ship raises `net-collector-down`.

### What it cannot see

It is visibility, not control, and an agent that wants to hide can:

- resolve names its own way (DNS over HTTPS, a hard-coded resolver, bare
  addresses). Those connections show as addresses (`kind: address`);
- use UDP, such as QUIC. The collector records TCP connections only; `vnstat`
  totals still include all traffic;
- with sudo (off by default), stop the collector.

Names and program names come from the Ship, where an agent may have chosen
them. The collector keeps only well-formed host names and replaces any other
character in a program name with `_`. `ship-ctl` checks them again, and
FleetLedger checks every summary against the contract before using it. Show
them as text, never as markup.

## In FleetLedger

```sh
ship-fleet net                       # every Ship: collector, traffic, destinations, new
ship-fleet net ship-01 [--week]      # top destinations (24 h or 7 days), programs, traffic
ship-fleet net --new [--tag work]    # destinations first contacted in the last 24 hours
ship-fleet net --who example.com     # which Ships reached it, or any subdomain
ship-fleet status ship-01            # includes a one-line network summary
```

Each command fetches fresh summaries over SSH; `--offline` uses the last ones.
FleetLedger keeps the last summary per Ship and a first-seen ledger in
`~/.local/share/fleetledger/net/` (0600). It records when each Ship first
contacted each destination, and which Ship in the fleet contacted it first. So
it can flag both "new for this Ship" and "new to the fleet".

### For the web UI (the Network tab)

`fleetledger/fleetledger/net.py` returns plain JSON-able dicts:

| Function | Returns |
|---|---|
| `refresh(ssh, inventory, ships)` | `{ship: Fetched(summary, problem, new_to_fleet)}`; fetches, validates, caches, updates the ledger; never raises for one Ship |
| `ship_view(inventory, ship, summary, now=None)` | `{ship, tags, available, generated_at, collector, totals, top_24h, top_7d, programs, new, truncated}`; each destination row is a net-summary destination plus `new` and `new_to_fleet` booleans |
| `new_destinations(inventory, tag=None, now=None)` | rows `{ship, name, kind, scope, first_seen, new_to_fleet, connections_24h, programs}`, newest first |
| `who_talked(inventory, domain, tag=None)` | rows `{ship, name, first_seen, last_seen, connections_24h, connections_7d, programs}` for the domain and its subdomains |
| `cached_summary(ship_name)` | the last valid summary, or None |

`totals.hourly` gives up to 7 days of per-hour traffic for a chart. A
destination's `tx_bytes_*` and `rx_bytes_*` are null when bytes are unknown
(the `ss` fallback).
