#!/usr/bin/env python3
"""ship-netwatch: see where this Ship connects, without getting in the way.

Runs as root (ship-netwatch.service) and only observes; nothing is blocked and
nothing sits in the traffic path. It folds three streams into a rolling store,
/var/lib/shipshape/net/state.json, which `ship-ctl net` summarises:

- new outbound TCP connections, with the program that made them:
  `tcpconnect-libbpf -t` (eBPF, from Ubuntu's libbpf-tools: precompiled, so
  nothing is compiled on the Ship). If it cannot run, `ss` is polled every 5
  seconds instead (short connections can then be missed);
- bytes per connection when it closes: `tcplife-libbpf -w` (eBPF only);
- DNS answers: `resolvectl monitor --json=short`, so each connection's address
  is shown as the name it was looked up by.

Per destination it keeps first seen, last seen, ports, addresses, and per hour
the connections, bytes and programs, for 7 days; when each destination was
first seen is kept for 90 days, so "new" stays meaningful.

Every name and program comes from the Ship, where an agent may have chosen it,
so only well-formed host names are kept and any odd character in a program
name becomes "_". The service runs with ProtectHome=yes: it never touches a
home folder. Logs go to the journal: each destination seen for the first time,
and an hourly digest.

Python standard library only.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = "shipshape.netwatch-state/1"
STATE_DIR = Path(os.environ.get("SHIPSHAPE_NET_DIR", "/var/lib/shipshape/net"))
RETENTION = timedelta(days=7)
FIRST_SEEN_RETENTION = timedelta(days=90)
MAX_DESTINATIONS = 2000
MAX_FIRST_SEEN = 20000
MAX_DNS = 20000
DNS_TTL = timedelta(hours=24)
SAVE_EVERY = 60.0
POLL_EVERY = 5.0
MAX_PER_DEST = 16  # ports, addresses, programs kept per destination

HOSTNAME = re.compile(
    r"^(?=.{1,253}$)([a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*$"
)
PROGRAM_BAD = re.compile(r"[^A-Za-z0-9_.+:@-]")


# --- small helpers --------------------------------------------------------------
def utc(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_key(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H")


def clean_name(name: object) -> str | None:
    """A DNS name as kept: lower case, no trailing dot, well formed; else None."""
    if not isinstance(name, str):
        return None
    name = name.strip().rstrip(".").lower()
    return name if HOSTNAME.match(name) else None


def clean_program(comm: object) -> str:
    if not isinstance(comm, str) or not comm.strip():
        return "?"
    return PROGRAM_BAD.sub("_", comm.strip())[:32]


def clean_address(text: str) -> str | None:
    """Canonical form (net-summary contract): no zone index, mapped IPv6 as IPv4."""
    try:
        addr = ipaddress.ip_address(text.strip().strip("[]").split("%")[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


SHARED = ipaddress.ip_network("100.64.0.0/10")


def scope_of(address: str) -> str:
    addr = ipaddress.ip_address(address)
    if addr.is_loopback or addr.is_unspecified:
        return "local"
    if addr.is_private or addr.is_link_local or (addr.version == 4 and addr in SHARED):
        return "lan"
    return "internet"


# --- the store -------------------------------------------------------------------
class Store:
    """Destinations with hourly buckets; pruned to RETENTION, capped in size."""

    def __init__(self, doc: dict | None = None, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        doc = doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA else {}
        self.started_at = doc.get("started_at") or utc(now)
        self.updated_at = doc.get("updated_at")
        self.source = doc.get("source")
        self.note = doc.get("note")
        self.dropped = int(doc.get("dropped", 0))
        self.destinations: dict[str, dict] = dict(doc.get("destinations", {}))
        self.first_seen: dict[str, str] = dict(doc.get("first_seen", {}))
        self.lock = threading.Lock()
        self.new_since_digest: list[str] = []
        self.connections_since_digest = 0

    def to_doc(self) -> dict:
        return {
            "schema": SCHEMA,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "note": self.note,
            "dropped": self.dropped,
            "destinations": self.destinations,
            "first_seen": self.first_seen,
        }

    def _dest(self, name: str, kind: str, scope: str, now: datetime) -> tuple[dict, bool]:
        dest = self.destinations.get(name)
        if dest is not None:
            return dest, False
        if len(self.destinations) >= MAX_DESTINATIONS:
            oldest = min(self.destinations, key=lambda n: self.destinations[n]["last_seen"])
            del self.destinations[oldest]
            self.dropped += 1
        first = self.first_seen.get(name)
        new = first is None
        if new:
            first = utc(now)
            self.first_seen[name] = first
        dest = {
            "kind": kind,
            "scope": scope,
            "first_seen": first,
            "last_seen": utc(now),
            "ports": [],
            "addresses": [],
            "hours": {},
        }
        self.destinations[name] = dest
        return dest, new

    def record_connect(
        self, name: str | None, address: str, port: int, program: str, now: datetime
    ) -> bool:
        """One outbound connection. Returns True if the destination is new to this Ship."""
        with self.lock:
            kind = "domain" if name else "address"
            dest_name = name or address
            dest, new = self._dest(dest_name, kind, scope_of(address), now)
            dest["last_seen"] = utc(now)
            if port not in dest["ports"] and len(dest["ports"]) < MAX_PER_DEST:
                dest["ports"].append(port)
            if address not in dest["addresses"] and len(dest["addresses"]) < MAX_PER_DEST:
                dest["addresses"].append(address)
            bucket = dest["hours"].setdefault(
                hour_key(now), {"c": 0, "tx": None, "rx": None, "p": {}}
            )
            bucket["c"] += 1
            programs = bucket["p"]
            if program in programs or len(programs) < MAX_PER_DEST:
                programs[program] = programs.get(program, 0) + 1
            self.updated_at = utc(now)
            self.connections_since_digest += 1
            if new:
                self.new_since_digest.append(dest_name)
            return new

    def record_bytes(self, name: str, tx: int, rx: int, now: datetime) -> None:
        with self.lock:
            dest = self.destinations.get(name)
            if dest is None:
                return
            bucket = dest["hours"].setdefault(
                hour_key(now), {"c": 0, "tx": None, "rx": None, "p": {}}
            )
            bucket["tx"] = (bucket["tx"] or 0) + max(0, tx)
            bucket["rx"] = (bucket["rx"] or 0) + max(0, rx)

    def prune(self, now: datetime) -> None:
        with self.lock:
            cutoff = hour_key(now - RETENTION)
            for name in list(self.destinations):
                hours = self.destinations[name]["hours"]
                for key in [k for k in hours if k < cutoff]:
                    del hours[key]
                if not hours:
                    del self.destinations[name]
            seen_cutoff = utc(now - FIRST_SEEN_RETENTION)
            for name in [n for n, t in self.first_seen.items() if t < seen_cutoff]:
                del self.first_seen[name]
            if len(self.first_seen) > MAX_FIRST_SEEN:
                keep = sorted(self.first_seen.items(), key=lambda kv: kv[1])[-MAX_FIRST_SEEN:]
                self.first_seen = dict(keep)

    def save(self, folder: Path = STATE_DIR) -> None:
        with self.lock:
            data = json.dumps(self.to_doc(), separators=(",", ":")).encode("utf-8")
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = folder / f".state.json.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, folder / "state.json")

    def digest(self) -> str | None:
        with self.lock:
            if not self.connections_since_digest:
                return None
            line = (
                f"digest: {self.connections_since_digest} connection(s), "
                f"{len(self.new_since_digest)} new destination(s)"
            )
            self.connections_since_digest = 0
            self.new_since_digest = []
            return line


def load_store(folder: Path = STATE_DIR) -> Store:
    try:
        return Store(json.loads((folder / "state.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return Store()


# --- DNS answers -----------------------------------------------------------------
class Names:
    """Address -> the name it was looked up by (the question, not a CNAME target)."""

    def __init__(self) -> None:
        self.by_address: dict[str, tuple[str, datetime]] = {}
        self.lock = threading.Lock()

    def add(self, address: str, name: str, now: datetime) -> None:
        with self.lock:
            if len(self.by_address) >= MAX_DNS:
                cutoff = now - DNS_TTL
                self.by_address = {a: v for a, v in self.by_address.items() if v[1] > cutoff}
                if len(self.by_address) >= MAX_DNS:
                    self.by_address.clear()
            self.by_address[address] = (name, now)

    def lookup(self, address: str, now: datetime) -> str | None:
        with self.lock:
            hit = self.by_address.get(address)
        if hit and now - hit[1] <= DNS_TTL:
            return hit[0]
        return None


def _addresses_in(node: object) -> Iterable[str]:
    """Addresses in a resolved answer record: a list of bytes, or a string."""
    if (
        isinstance(node, list)
        and len(node) in (4, 16)
        and all(isinstance(b, int) and 0 <= b <= 255 for b in node)
    ):
        try:
            yield str(ipaddress.ip_address(bytes(node)))
        except ValueError:
            return
    elif isinstance(node, str):
        addr = clean_address(node)
        if addr:
            yield addr


def parse_dns_event(line: str) -> list[tuple[str, str]]:
    """(address, name) pairs from one `resolvectl monitor --json=short` line."""
    try:
        doc = json.loads(line)
    except ValueError:
        return []
    if not isinstance(doc, dict):
        return []
    questions = doc.get("question") or []
    name = None
    for q in questions if isinstance(questions, list) else []:
        key = q.get("key", q) if isinstance(q, dict) else {}
        name = clean_name(key.get("name") if isinstance(key, dict) else None)
        if name:
            break
    pairs = []
    answers = doc.get("answer") or []
    for ans in answers if isinstance(answers, list) else []:
        rr = ans.get("rr", ans) if isinstance(ans, dict) else {}
        if not isinstance(rr, dict):
            continue
        rr_name = None
        key = rr.get("key")
        if isinstance(key, dict):
            rr_name = clean_name(key.get("name"))
        for field in ("address", "addr"):
            for addr in _addresses_in(rr.get(field)):
                pairs.append((addr, name or rr_name))
    return [(a, n) for a, n in pairs if n]


# --- connection sources ----------------------------------------------------------
def parse_tcpconnect(line: str) -> tuple[int, str, str, int] | None:
    """(pid, program, address, port) from `tcpconnect-libbpf -t` output.

    Columns: TIME(s) PID COMM IP SADDR DADDR DPORT. COMM may contain spaces, so
    the line is read from both ends.
    """
    parts = line.split()
    if len(parts) < 7 or not parts[1].isdigit() or not parts[-1].isdigit():
        return None
    try:
        float(parts[0])
    except ValueError:
        return None
    address = clean_address(parts[-2])
    if address is None or parts[-4] not in ("4", "6"):
        return None
    program = clean_program(" ".join(parts[2:-4]))
    return int(parts[1]), program, address, int(parts[-1])


def parse_tcplife(line: str) -> tuple[int, str, str, int, str, int, int, int] | None:
    """(pid, program, laddr, lport, raddr, rport, tx_bytes, rx_bytes) from tcplife.

    Columns of `tcplife-libbpf -w`: PID COMM LADDR LPORT RADDR RPORT TX_KB RX_KB MS,
    with TX_KB and RX_KB as decimals (1.88).
    """
    parts = line.split()
    if len(parts) < 9 or not parts[0].isdigit():
        return None
    try:
        tx_kb, rx_kb = float(parts[-3]), float(parts[-2])
        lport, rport = int(parts[-6]), int(parts[-4])
        float(parts[-1])
    except ValueError:
        return None
    laddr, raddr = clean_address(parts[-7]), clean_address(parts[-5])
    if laddr is None or raddr is None or tx_kb < 0 or rx_kb < 0:
        return None
    program = clean_program(" ".join(parts[1:-7]))
    return (int(parts[0]), program, laddr, lport, raddr, rport, round(tx_kb * 1024),
            round(rx_kb * 1024))


SS_USERS = re.compile(r'\("((?:[^"\\]|\\.)*)",pid=(\d+)')


def parse_ss(text: str) -> list[tuple[str, int, str, int, str]]:
    """(local, lport, peer, pport, program) of established connections in `ss -tnpH`.

    Without a state filter each line starts with the state (ESTAB, SYN-SENT...);
    with one (`state established`) ss leaves that column out, so a line starting
    with a number is already established and everything sits one column left.
    """
    out = []
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0].isdigit():
            parts = ["ESTAB", *parts]
        if len(parts) < 5 or parts[0] != "ESTAB":
            continue
        local, peer = parts[3], parts[4]
        if ":" not in local or ":" not in peer:
            continue
        laddr, _, lport = local.rpartition(":")
        paddr, _, pport = peer.rpartition(":")
        laddr, paddr = clean_address(laddr), clean_address(paddr)
        if not (laddr and paddr and lport.isdigit() and pport.isdigit()):
            continue
        m = SS_USERS.search(line)
        program = clean_program(m.group(1)) if m else "?"
        out.append((laddr, int(lport), paddr, int(pport), program))
    return out


def listening_ports(text: str) -> set[int]:
    ports = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and ":" in parts[3]:
            port = parts[3].rpartition(":")[2]
            if port.isdigit():
                ports.add(int(port))
    return ports


# --- the collector -----------------------------------------------------------------
Clock = Callable[[], datetime]


class Collector:
    """Turns tool output into store records.

    The DNS answer for a connection can reach us a moment after the connection
    itself (they come from different tools), so a connection whose address has
    no name yet waits up to NAME_WAIT for one before it is recorded as a bare
    address. Bytes that arrive while it waits are kept with it.
    """

    NAME_WAIT = timedelta(seconds=3)

    def __init__(self, store: Store, names: Names, *, clock: Clock | None = None,
                 log: Callable[[str], None] | None = None) -> None:
        self.store = store
        self.names = names
        self.clock = clock or (lambda: datetime.now(UTC))
        self.log = log or (lambda m: print(m, flush=True))
        self.lock = threading.Lock()
        # (address, port) -> destination name, for attributing tcplife bytes
        self.recent: dict[tuple[str, int], tuple[str, datetime]] = {}
        # connections waiting for a name: [seen, address, port, program, tx, rx]
        self.pending: list[list] = []

    def on_dns(self, line: str) -> None:
        now = self.clock()
        named = set()
        for address, name in parse_dns_event(line):
            self.names.add(address, name, now)
            named.add(address)
        if named:
            with self.lock:
                ready = [p for p in self.pending if p[1] in named]
                self.pending = [p for p in self.pending if p[1] not in named]
            for p in ready:
                self._record(*p[1:], now=now)

    def _record(self, address: str, port: int, program: str, tx: int = 0, rx: int = 0,
                *, now: datetime) -> None:
        name = self.names.lookup(address, now)
        dest = name or address
        if self.store.record_connect(name, address, port, program, now):
            self.log(f"new destination: {dest} ({address} port {port}, {program})")
        if tx or rx:
            self.store.record_bytes(dest, tx, rx, now)
        with self.lock:
            self.recent[(address, port)] = (dest, now)
            if len(self.recent) > 4 * MAX_DESTINATIONS:
                cutoff = now - timedelta(hours=1)
                self.recent = {k: v for k, v in self.recent.items() if v[1] > cutoff}

    def on_connect(self, address: str, port: int, program: str) -> None:
        if scope_of(address) == "local":
            return
        now = self.clock()
        if self.names.lookup(address, now) is None:
            with self.lock:
                if len(self.pending) < 10000:
                    self.pending.append([now, address, port, program, 0, 0])
                    return
        self._record(address, port, program, now=now)

    def flush(self) -> None:
        """Record connections that waited long enough for a name, as addresses."""
        now = self.clock()
        with self.lock:
            due = [p for p in self.pending if now - p[0] >= self.NAME_WAIT]
            self.pending = [p for p in self.pending if now - p[0] < self.NAME_WAIT]
        for p in due:
            self._record(*p[1:], now=now)

    def on_tcpconnect(self, line: str) -> None:
        parsed = parse_tcpconnect(line)
        if parsed:
            _, program, address, port = parsed
            self.on_connect(address, port, program)

    def on_tcplife(self, line: str) -> None:
        parsed = parse_tcplife(line)
        if not parsed:
            return
        _, _, _, _, raddr, rport, tx, rx = parsed
        # Only connections this Ship opened: their (address, port) was seen by
        # tcpconnect. Sessions into the Ship (SSH) have the client's port there.
        with self.lock:
            hit = self.recent.get((raddr, rport))
            if hit is None:
                for p in self.pending:
                    if p[1] == raddr and p[2] == rport:
                        p[4] += tx
                        p[5] += rx
                        return
        if hit:
            self.store.record_bytes(hit[0], tx, rx, self.clock())


class SSPoller:
    """Fallback without eBPF: new established outbound connections, every few seconds."""

    def __init__(self, collector: Collector, run: Callable[[list[str]], str]) -> None:
        self.collector = collector
        self.run = run
        self.seen: set[tuple[str, int, str, int]] = set()

    def poll(self) -> None:
        listening = listening_ports(self.run(["ss", "-tlnH"]))
        current = set()
        for laddr, lport, paddr, pport, program in parse_ss(self.run(["ss", "-tnpH"])):
            if lport in listening:
                continue  # someone connected to us
            key = (laddr, lport, paddr, pport)
            current.add(key)
            if key not in self.seen:
                self.collector.on_connect(paddr, pport, program)
        self.seen = current


# --- running ---------------------------------------------------------------------------
# C programs: stdbuf makes them write each line as it happens into the pipe.
EBPF_TOOLS = {
    "tcpconnect": ["stdbuf", "-oL", "tcpconnect-libbpf", "-t"],
    "tcplife": ["stdbuf", "-oL", "tcplife-libbpf", "-w"],
}
DNS_MONITOR = ["resolvectl", "monitor", "--json=short"]


def _stream(
    argv: list[str],
    handle: Callable[[str], None],
    stop: threading.Event,
    ready: threading.Event | None = None,
) -> subprocess.Popen | None:
    try:
        # Line by line as it happens: stdbuf for the C tools (EBPF_TOOLS), and
        # unbuffered for anything in Python.
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env
        )
    except OSError:
        return None

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if ready is not None:
                ready.set()
            if stop.is_set():
                break
            try:
                handle(line)
            except Exception as exc:  # one odd line must never stop the collector
                print(f"skipped a line from {argv[0]}: {type(exc).__name__}", flush=True)

    threading.Thread(target=pump, daemon=True).start()
    return proc


def _ss(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def main() -> int:
    if os.geteuid() != 0:
        print("ship-netwatch: run as root", file=sys.stderr)
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    store = load_store()
    store.prune(datetime.now(UTC))
    names = Names()
    collector = Collector(store, names)
    procs = []
    dns = _stream(DNS_MONITOR, collector.on_dns, stop)
    if dns is None:
        print("resolvectl monitor is not available; destinations stay as addresses", flush=True)
    else:
        procs.append(dns)

    # eBPF first. tcpconnect prints a header line once it is attached; if it has
    # not within 30 s, or exits, poll ss instead (at once when it exits).
    ready = threading.Event()
    tcpconnect = _stream(EBPF_TOOLS["tcpconnect"], collector.on_tcpconnect, stop, ready)
    poller = None
    if tcpconnect is not None:
        for _ in range(30):
            if ready.wait(1) or tcpconnect.poll() is not None:
                break
    if tcpconnect is not None and ready.is_set() and tcpconnect.poll() is None:
        procs.append(tcpconnect)
        life = _stream(EBPF_TOOLS["tcplife"], collector.on_tcplife, stop)
        if life is not None:
            procs.append(life)
        store.source, store.note = "ebpf", None
        print("recording with eBPF (tcpconnect-libbpf, tcplife-libbpf)", flush=True)
    else:
        if tcpconnect is not None and tcpconnect.poll() is None:
            tcpconnect.terminate()
        poller = SSPoller(collector, _ss)
        store.source = "ss"
        store.note = (
            "eBPF tools unavailable; polling connections every 5 s (short "
            "connections can be missed; no byte counts)."
        )
        print("eBPF tools unavailable; polling ss every 5 s", flush=True)

    last_save = last_digest = time.monotonic()
    try:
        while not stop.is_set():
            if poller is not None:
                poller.poll()
            collector.flush()
            for proc in procs:
                if proc.poll() is not None:
                    print(
                        f"{proc.args[0]} exited ({proc.returncode}); restarting the service",
                        flush=True,
                    )
                    stop.set()
            now = time.monotonic()
            if now - last_save >= SAVE_EVERY:
                store.prune(datetime.now(UTC))
                store.save()
                last_save = now
            if now - last_digest >= 3600:
                line = store.digest()
                if line:
                    print(line, flush=True)
                last_digest = now
            stop.wait(1.0 if poller is None else POLL_EVERY)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        collector.clock = lambda: datetime.now(UTC) + Collector.NAME_WAIT
        collector.flush()
        store.save()
    # A child that died makes the service restart (Restart=on-failure).
    return 1 if any(p.returncode not in (None, 0, -15) for p in procs) else 0


if __name__ == "__main__":
    sys.exit(main())
