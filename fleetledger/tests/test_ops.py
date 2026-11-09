"""Day-2 operations (ops.py) against a simulated Ship.

The simulated Ship keeps its own clock, uptime, bundle and agents: a reboot takes
it down for a while, it comes back with a fresh uptime, and herdr resumes its
agents a little later (or not, when a test says so). Uploads run the real upload
script with bash into a temporary folder, and converge reads the bundle that
actually arrived, so the checksum sidecar and paths are tested for real.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fleetledger.errors import FleetError
from fleetledger.seed.bundle import Bundle
from fleetledger.ssh import HostKeyMismatch, Result, Unreachable

from fleetledger import cli, inhibitors, ops, status

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parents[1] / "contracts" / "examples"
RUNNING = json.loads((EXAMPLES / "ship-status" / "running.json").read_text())
EPOCH = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
OLD_COMMIT = "0123456789ab"
NEW_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
VERSION = {
    "schema": "shipshape.ship-ctl-result/1", "command": "version", "ok": True, "changed": [],
    "messages": [], "error": None,
    "data": {"tooling": "0.1.0", "commit": OLD_COMMIT, "contracts": {
        "ship-status": 1, "ship-toml": 1, "ship-ctl-result": 1, "bundle": 1, "seed": 1,
        "runtime": 1}},
}


def make_bundle(commit=NEW_COMMIT, tooling="0.2.0", dirty=False) -> Bundle:
    manifest = {"schema": "shipshape.bundle/1", "tooling_version": tooling, "commit": commit,
                "dirty": dirty, "created_at": "2026-01-02T00:00:00Z"}
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w") as tar:
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo("bundle.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return Bundle(raw.getvalue(), manifest)


def ctl_result(command, ok=True, data=None, error=None, changed=()):
    return {"schema": "shipshape.ship-ctl-result/1", "command": command, "ok": ok,
            "changed": list(changed), "messages": [], "data": data or {}, "error": error}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class SimShip:
    """One simulated Ship."""

    def __init__(self, clock, incoming: Path, name="ship-01"):
        self.clock = clock
        self.incoming = incoming
        self.name = name
        self.booted = clock.t - 86400
        self.down_until = None
        self.boot_delay = 40  # seconds from the reboot command until it answers again
        self.resume_delay = 30  # seconds after boot until herdr resumed its agents
        self.agents = {"main": "auto", "builder": "bypassPermissions"}
        self.after_reboot = None  # agents after resume, if different
        self.never_back = False
        self.bundle_commit = OLD_COMMIT
        self.tooling = "0.1.0"
        self.herdr_changes = False  # does the next converge replace herdr?
        self.herdr_pending = False
        self.converge_error = None
        self.report_commit = None  # lie about the bundle after converge
        self.accounts = {}  # service -> account reported in auth
        self.forgotten = 0  # shared SSH connections closed by FleetLedger
        self.time_format = "%Y-%m-%dT%H:%M:%SZ"  # the contract also allows fractions
        self.refuse_reboot = None  # stderr of a refused reboot command
        self.refuse_forced = None  # ... of a refused forced one (--check-inhibitors=no)
        self.blockers = []  # what the inhibitor probe finds (inhibitors.PROBE's items)
        self.lock_released = False  # the lock goes away once the probe has looked
        self.probe_fails = False
        self.probes = 0
        self.after_reboot_id = None  # someone else answers at its name after the reboot
        self.after_reboot_host_key = False  # ... or answers with a different host key
        self.impostor = False
        self.calls = []
        self.allow_bypass = True
        self.working = set()  # agents herdr reports as working
        self.ctl_version = 1  # 0: tooling from before `ship-ctl agent`
        self.recorded = False  # tooling with the agent record (status agent_record)
        self.relaunching = {}  # recorded agents not running now; they come back at boot
        self.herdr_resume = True  # also report herdr.last_resume (older FleetLedgers read it)
        self.converged = []
        self.reboots = []

    # --- state ------------------------------------------------------------
    def up(self):
        if self.down_until is None:
            return True
        if self.never_back or self.clock.t < self.down_until:
            return False
        self.booted = self.down_until
        self.down_until = None
        self.herdr_pending = False
        self.impostor = bool(self.after_reboot_id or self.after_reboot_host_key)
        if self.after_reboot is not None:
            self.agents = dict(self.after_reboot)
        else:
            self.agents.update(self.relaunching)
        self.relaunching = {}
        return True

    def status(self, ship):
        doc = copy.deepcopy(RUNNING)
        now = self.clock.t
        uptime = int(now - self.booted)
        doc["generated_at"] = datetime.fromtimestamp(EPOCH + now, UTC).strftime(
            self.time_format)
        doc["ship"].update(name=ship.name, id=ship.id, tags=list(ship.tags or ()),
                           tooling=self.tooling, bundle_commit=self.bundle_commit)
        if self.impostor and self.after_reboot_id:
            doc["ship"]["id"] = self.after_reboot_id
        doc["host"]["uptime_s"] = uptime
        resumed = uptime >= self.resume_delay
        agents = self.agents if resumed else {}
        doc["herdr"]["agents"] = [
            {"name": n, "kind": "claude", "state": "idle", "mode": m} for n, m in agents.items()
        ]
        at = datetime.fromtimestamp(EPOCH + self.booted + self.resume_delay, UTC).strftime(
            self.time_format)
        doc["herdr"]["last_resume"] = None if not resumed or not self.herdr_resume else {
            "at": at, "method": "api-fallback" if self.recorded else "hidden-client",
            "resumed": len(agents), "failed": 0}
        if self.recorded:
            stamp = "2026-01-01T00:00:00Z"
            doc["agent_record"] = {"runtime": "herdr", "resume": None if not resumed else {
                "at": at, "method": "ship-ctl", "resumed": len(agents), "failed": 0},
                "agents": [{"name": n, "cwd": f"/home/agent/work/{n}", "mode": m,
                            "autostart": False, "state": state, "relaunch_failures": 0,
                            "created_at": stamp, "updated_at": stamp}
                           for n, m, state in [
                               *((n, m, "running" if resumed else "starting")
                                 for n, m in self.agents.items()),
                               *((n, m, "starting") for n, m in self.relaunching.items())]]}
        for service, account in self.accounts.items():
            doc["auth"][service]["account"] = account
        doc["attention"] = []
        if self.herdr_pending:
            doc["attention"].append({"code": "herdr-restart-pending",
                                     "message": "herdr was updated; reboot to switch."})
        doc["state"] = "running" if agents else "ready"
        return doc

    # --- commands -----------------------------------------------------------
    def __call__(self, ship, command, input):
        self.calls.append(list(command))
        if not self.up():
            raise Unreachable(f"Cannot reach {ship.name}: connection refused")
        if self.impostor and self.after_reboot_host_key:
            raise HostKeyMismatch(f"{ship.name} answered with a host key other than the pinned one")
        if command[:2] == ["bash", "-c"]:
            local = [self.incoming.as_posix() if a == ops.INCOMING else a for a in command]
            proc = subprocess.run(local, input=input or b"", capture_output=True)
            return proc.returncode, proc.stdout, proc.stderr.decode()
        if command == ["python3", "-I", "-"]:  # inhibitors.PROBE, as fleet
            assert input == inhibitors.PROBE.encode()
            self.probes += 1
            if self.probe_fails:
                return 3, "", "busctl: not found\n"
            found = json.dumps({"blockers": self.blockers})
            if self.lock_released:
                self.refuse_reboot, self.blockers = None, []
            return 0, found, ""
        if command[:4] == ["sudo", "-n", "systemctl", "reboot"]:
            forced = command[4:] == ["--check-inhibitors=no"]
            assert forced or command[4:] == [], command
            refused = self.refuse_forced if forced else self.refuse_reboot
            if refused:
                return 1, "", refused
            self.reboots.append(list(ship.names))
            self.down_until = self.clock.t + self.boot_delay
            raise Unreachable(f"Cannot reach {ship.name}: connection closed by remote host")
        assert command[:3] == ["sudo", "-n", "/usr/local/bin/ship-ctl"], command
        verb = command[3]
        if verb == "version":
            return 0, json.dumps(VERSION), ""
        if verb == "status":
            return 0, json.dumps(self.status(ship)), ""
        if verb == "converge":
            return self.converge(command[command.index("--bundle") + 1])
        if verb == "agent":
            return self.agent(command[4:-1])
        if verb == "logs":
            return 0, json.dumps(ctl_result("logs", data={"lines": [f"{ship.name} line"]})), ""
        return 0, json.dumps(ctl_result(verb, changed=["/etc/shipshape/ship.toml"])), ""

    def agent(self, argv):
        """`ship-ctl agent restart`, as the Ship does it: the ledger (and so status)
        says the new mode afterwards."""
        if not self.ctl_version:
            return 2, json.dumps(ctl_result("status", ok=False, error={
                "code": "usage", "message": "argument COMMAND: invalid choice: 'agent'"})), ""
        assert argv[0] == "restart"
        mode = argv[argv.index("--mode") + 1] if "--mode" in argv else None
        names = [a for a in argv[1:] if not a.startswith("--") and a != mode]
        names = list(self.agents) if "--all" in argv else names
        if mode == "bypassPermissions" and not self.allow_bypass:
            return 1, json.dumps(ctl_result("agent", ok=False, error={
                "code": "invalid-input",
                "message": "bypassPermissions is not allowed on this Ship"})), ""
        restarted, skipped = [], []
        for name in names:
            if name in self.working and "--force" not in argv:
                skipped.append({"name": name, "reason": "it is working; not interrupted"})
                continue
            new = mode or "auto"
            restarted.append({"name": name, "mode": new, "previous_mode": self.agents[name],
                              "conversation": "fresh" if "--fresh" in argv else "continued",
                              "was": "idle"})
            self.agents[name] = new
        data = {"restarted": restarted, "skipped": skipped}
        doc = ctl_result("agent", data=data, changed=[r["name"] for r in restarted])
        doc["messages"] = [f"{r['name']} restarted in {r['mode']}." for r in restarted]
        if skipped:
            doc.update(ok=False, error={"code": "agent-busy", "message": "1 agent(s) are busy"})
            return 1, json.dumps(doc), ""
        return 0, json.dumps(doc), ""

    def converge(self, path):
        assert path.startswith(ops.INCOMING + "/")
        local = self.incoming / path.rsplit("/", 1)[1]
        data = local.read_bytes()
        sidecar = local.with_name(local.name + ".sha256").read_text().split()[0]
        assert sidecar == hashlib.sha256(data).hexdigest()
        assert local.stat().st_mode & 0o077 == 0
        if self.converge_error:
            return 1, json.dumps(ctl_result("converge", ok=False, error={
                "code": "provision-failed", "message": self.converge_error})), ""
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            manifest = json.load(tar.extractfile("bundle.json"))
        self.converged.append(manifest["commit"])
        old = f"{self.tooling}-{self.bundle_commit}"
        self.tooling = manifest["tooling_version"]
        self.bundle_commit = self.report_commit or manifest["commit"][:12]
        if self.herdr_changes:
            self.herdr_pending = True
        return 0, json.dumps(ctl_result("converge", data={
            "from": old, "to": f"{self.tooling}-{manifest['commit'][:12]}"})), ""


class SimSSH:
    def __init__(self, sims):
        self.sims = sims

    def forget(self, ship):
        self.sims[ship.name].forgotten += 1

    def run(self, ship, command, *, input=None, timeout=60):
        sim = self.sims[ship.name]
        code, out, err = sim(ship, list(command), input)
        if isinstance(out, str):
            out = out.encode()
        return Result(code, out, err, ship.names[0])


@pytest.fixture
def world(store, fake_repo, tmp_path):
    clock = Clock()
    incoming = tmp_path / "incoming"
    sims = {name: SimShip(clock, incoming / name, name) for name in ("ship-01", "ship-02")}
    sims["ship-02"].accounts = {"claude": "you@example.com", "github": "example-user"}
    fleet = ops.Fleet(store=store, ssh_factory=lambda inv: SimSSH(sims),
                      repo_factory=lambda: fake_repo, sleep=clock.sleep, clock=clock)

    class World:
        pass

    w = World()
    w.clock, w.sims, w.fleet, w.store, w.incoming = clock, sims, fleet, store, incoming
    w.ship = lambda name: store.load().ship(name)
    return w


def events():
    seen = []
    return seen, lambda ship, text: seen.append((ship, text))


# --- select -----------------------------------------------------------------


def test_a_failed_command_is_described_by_its_last_line():
    assert ops._last_line("warming up\nstill going\nno space left on device\n") == \
        "no space left on device"
    assert ops._last_line("") == "no output"
    assert ops._last_line("  \n") == "no output"


def test_select_by_names_tag_or_all(store):
    inv = store.load()
    assert [s.name for s in ops.select(inv, ["ship-02"])] == ["ship-02"]
    assert [s.name for s in ops.select(inv, tag="work")] == ["ship-01"]
    assert [s.name for s in ops.select(inv, ["all"])] == ["ship-01", "ship-02"]
    assert [s.name for s in ops.select(inv, everything=True)] == ["ship-01", "ship-02"]
    assert [s.name for s in ops.select(inv, ["ship-02", "ship-02"])] == ["ship-02"]


@pytest.mark.parametrize("names,tag,everything", [
    ([], None, False), (["ship-01"], "work", False), (["all", "ship-01"], None, False),
    ([], "work", True),
])
def test_select_needs_exactly_one_way(store, names, tag, everything):
    with pytest.raises(FleetError):
        ops.select(store.load(), names, tag, everything)


def test_select_unknown_tag_or_name(store):
    with pytest.raises(FleetError):
        ops.select(store.load(), tag="nope")
    with pytest.raises(FleetError):
        ops.select(store.load(), ["ship-09"])



def test_every_attention_code_in_the_contract_has_a_severity():
    """A code added to the contract without a severity would sort as medium by default."""
    schema = json.loads((HERE.parents[1] / "contracts" / "ship-status.schema.json").read_text())
    text = schema["properties"]["attention"]["items"]["properties"]["code"]["description"]
    codes = set(re.findall(r"\b[a-z]+(?:-[a-z]+)+\b", text)) - {"as-is"}
    assert "herdr-restart-pending" in codes and "tags-differ" in codes
    assert sorted(codes - set(ops.SEVERITY)) == []

# --- reboot -------------------------------------------------------------------


def test_reboot_waits_and_checks_agents_resumed_in_their_modes(world):
    seen, on_event = events()
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")], on_event=on_event)
    assert outcome.ok, outcome
    assert "2/2 agents resumed in their modes" in outcome.summary
    assert outcome.data["agents_after"] == {"main": "auto", "builder": "bypassPermissions"}
    assert world.sims["ship-01"].reboots == [["ship-01.local"]]
    assert world.sims["ship-01"].forgotten == 1  # no polling over a dead connection
    assert ("ship-01", "rebooting") in seen


def test_reboot_expects_every_agent_the_record_keeps_running(world):
    """helper crashed just before the reboot and the Ship's record is starting it
    again: it must come back too, in the mode the record keeps for it. And the
    resume is the record's (herdr.last_resume is only for older FleetLedgers)."""
    sim = world.sims["ship-01"]
    sim.recorded, sim.relaunching, sim.herdr_resume = True, {"helper": "plan"}, False
    start = world.clock.t
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")],
                           times=ops.RebootTimes(back=600, resume=600, poll=10))
    assert outcome.ok, outcome
    assert world.clock.t - start < 200  # it saw the record's resume, not the full window
    assert outcome.data["agents_before"] == {"main": "auto", "builder": "bypassPermissions",
                                             "helper": "plan"}
    assert "3/3 agents resumed in their modes" in outcome.summary


def test_reboot_flags_a_recorded_agent_that_did_not_come_back(world):
    sim = world.sims["ship-01"]
    sim.recorded, sim.relaunching = True, {"helper": "plan"}
    sim.after_reboot = {"main": "auto", "builder": "bypassPermissions"}
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")],
                           times=ops.RebootTimes(back=600, resume=60, poll=10))
    assert not outcome.ok
    assert outcome.details == ["agent helper did not come back"]


def test_expected_modes_come_from_the_record_and_what_runs():
    doc = copy.deepcopy(RUNNING)
    doc["herdr"]["agents"] = [
        {"name": "main", "kind": "claude", "state": "idle", "mode": "plan"},
        {"name": "other", "kind": "codex", "state": "idle", "mode": None},
    ]
    stamp = "2026-01-01T00:00:00Z"
    doc["agent_record"] = {"runtime": "herdr", "resume": None, "agents": [
        {"name": n, "cwd": "/w", "mode": m, "autostart": False, "state": st,
         "relaunch_failures": 0, "created_at": stamp, "updated_at": stamp}
        for n, m, st in [("main", "auto", "running"), ("crashed", "dontAsk", "starting"),
                         ("gone", "auto", "stopped"), ("broken", "auto", "failed")]]}
    # main runs in plan now (changed in Claude since the record last synced): plan.
    assert ops.expected_modes(doc) == {"main": "plan", "other": None, "crashed": "dontAsk"}
    del doc["agent_record"]
    assert ops.expected_modes(doc) == {"main": "plan", "other": None}


def test_reboot_flags_an_agent_that_lost_bypass(world):
    sim = world.sims["ship-01"]
    sim.after_reboot = {"main": "auto", "builder": "auto"}
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")])
    assert not outcome.ok
    assert outcome.details == ["agent builder came back in auto, was bypassPermissions"]


def test_reboot_flags_a_missing_agent_after_the_resume_window(world):
    world.sims["ship-01"].after_reboot = {"main": "auto"}
    start = world.clock.t
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")],
                           times=ops.RebootTimes(back=600, resume=120, poll=10))
    assert not outcome.ok
    assert outcome.details == ["agent builder did not come back"]
    assert world.clock.t - start >= 120  # it waited for the resume window


def test_reboot_gives_up_when_the_ship_does_not_come_back(world):
    world.sims["ship-01"].never_back = True
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")],
                           times=ops.RebootTimes(back=120, resume=60, poll=10))
    assert not outcome.ok
    assert "did not come back within 120 s" in outcome.summary


def test_reboot_does_not_mistake_the_old_boot_for_the_new_one(world):
    """While the Ship is still shutting down it may answer with its old uptime."""
    sim = world.sims["ship-01"]
    sim.boot_delay = 60
    original = sim.up

    def slow_up():
        if sim.down_until is not None and world.clock.t < sim.down_until - 35:
            return True  # still answering from the old boot for 25 s
        return original()

    sim.up = slow_up
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")])
    assert outcome.ok
    assert outcome.data["back_after_s"] >= 60


def test_reboot_is_sent_over_one_name_only(world, store):
    """A connection dropped by the reboot is never retried on the Ship's other names."""
    sim = world.sims["ship-02"]
    sim.agents = {}
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-02")])
    assert outcome.ok
    assert sim.reboots == [["ship-02.local"]]  # not both of its names
    assert "no agents were running" in outcome.summary


def test_reboot_accepts_fractional_second_timestamps(world):
    """The status contract allows fractional seconds in generated_at and last_resume."""
    sim = world.sims["ship-01"]
    sim.time_format = "%Y-%m-%dT%H:%M:%S.%fZ"
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")])
    assert outcome.ok, outcome
    assert "2/2 agents resumed in their modes" in outcome.summary


@pytest.mark.parametrize("value,expected", [
    ("2026-01-01T00:00:00Z", EPOCH),
    ("2026-01-01T00:00:00.5Z", EPOCH + 0.5),
    ("2026-01-01T00:00:00.123456789Z", EPOCH + 0.123456789),
])
def test_parse_ts_reads_the_contract_format(value, expected):
    assert ops._parse_ts(value) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("value", [
    "2026-01-01T00:00:00",  # no zone: would silently be read as local time
    "2026-01-01T00:00:00+10:00", "2026-13-01T00:00:00Z", "", None,
])
def test_parse_ts_rejects_other_formats_as_a_fleet_error(value):
    with pytest.raises(FleetError, match="unreadable time"):
        ops._parse_ts(value)


def test_an_unreadable_time_fails_only_that_ship(world, monkeypatch):
    """Not a traceback for the whole batch, mid-reboot."""
    real = ops._resumed_this_boot

    def picky(doc):
        if doc["ship"]["name"] == "ship-02":
            doc = copy.deepcopy(doc)
            doc["generated_at"] = "not a time"
        return real(doc)

    monkeypatch.setattr(ops, "_resumed_this_boot", picky)
    outcomes = ops.reboot(world.fleet, [world.ship("ship-01"), world.ship("ship-02")])
    assert [o.ok for o in outcomes] == [True, False]
    assert "unreadable time" in outcomes[1].summary


def test_a_refused_reboot_command_fails_at_once(world):
    """sudo denied or an inhibitor refusing: fail now, do not wait 600 s for a reboot."""
    sim = world.sims["ship-01"]
    sim.refuse_reboot = "sudo: a password is required\n"
    start = world.clock.t
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")])
    assert not outcome.ok
    assert outcome.summary == "ship-01: the reboot command failed: sudo: a password is required"
    assert world.clock.t == start  # no waiting at all
    assert sim.reboots == []


def test_reboot_without_wait_reports_a_refused_command(world):
    world.sims["ship-01"].refuse_reboot = "Operation inhibited\n"
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")], wait=False)
    assert not outcome.ok and outcome.summary.startswith("reboot refused:")


# What logind (systemd 258 and later) says when a block lock refuses a root reboot.
DENIED = "Call to Reboot failed: Operation denied due to active block inhibitor\n"
# APT installing packages for `sudo apt upgrade` in fleet's interactive SSH login.
APT_IN_SESSION = {
    "who": "APT", "why": "APT is installing or removing packages", "mode": "block",
    "user": "root", "pid": 4242,
    "process": {"pid": 4242, "comm": "apt", "user": "root", "tty": "pts/0",
                "session": {"id": "7", "user": "fleet", "service": "sshd", "tty": None,
                            "state": "active"},
                "unit": None, "unit_user": None},
    "still_running": [],
}


def test_a_reboot_refused_by_a_shutdown_lock_names_it_and_the_next_step(world):
    """The refusal seen with an `apt upgrade` left running in an open SSH login: the
    outcome says what holds the lock and where, and how to get past it, at once."""
    sim = world.sims["ship-01"]
    sim.refuse_reboot, sim.blockers = DENIED, [APT_IN_SESSION]
    seen, on_event = events()
    start = world.clock.t
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")], on_event=on_event)
    assert not outcome.ok
    assert outcome.summary == (
        'reboot refused: a login session is blocking it: apt (PID 4242, as root) in '
        "fleet's SSH login session on pts/0 holds a shutdown lock: \"APT is installing or "
        'removing packages".')
    assert outcome.details[0] == ("Let it finish, or close fleet's session on the Ship (log "
                                  "out of it), then reboot again.")
    assert "ship-fleet reboot ship-01 --force" in outcome.details[1]
    assert "herdr still saves every agent first" in outcome.details[1]
    assert "sudo dpkg --configure -a" in outcome.details[2]
    assert outcome.data["refused"]["said"] == DENIED.strip()
    assert outcome.data["refused"]["blockers"][0]["process"]["session"]["user"] == "fleet"
    assert ("ship-01", "reboot refused: something holds a shutdown lock") in seen
    assert sim.reboots == [] and sim.probes == 1
    assert world.clock.t == start  # no waiting for a reboot that is not happening


def test_a_lock_released_while_it_was_looked_up_is_tried_once_more(world):
    sim = world.sims["ship-01"]
    sim.refuse_reboot, sim.lock_released = DENIED, True
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")])
    assert outcome.ok, outcome
    assert len(sim.reboots) == 1 and sim.probes == 1


def test_a_lock_that_cannot_be_looked_up_still_gets_the_next_step(world):
    sim = world.sims["ship-01"]
    sim.refuse_reboot, sim.probe_fails = DENIED, True
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")])
    assert not outcome.ok
    assert outcome.summary.startswith(
        "reboot refused: Call to Reboot failed: Operation denied due to active block "
        "inhibitor. Something on the Ship holds a shutdown lock")
    assert "ship-fleet reboot ship-01 --force" in outcome.details[-1]
    assert outcome.data["refused"]["blockers"] is None


def test_a_refused_sudo_is_not_mistaken_for_a_lock(world):
    sim = world.sims["ship-01"]
    sim.refuse_reboot = "sudo: a password is required\n"
    ops.reboot(world.fleet, [world.ship("ship-01")])
    assert sim.probes == 0


def test_force_overrides_the_lock_and_still_waits_for_the_agents(world):
    """--force: systemctl reboot --check-inhibitors=no (delay locks such as herdr's
    still get their time), then the same checks as any reboot."""
    sim = world.sims["ship-01"]
    sim.refuse_reboot, sim.blockers = DENIED, [APT_IN_SESSION]
    seen, on_event = events()
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")], on_event=on_event,
                           force=True)
    assert outcome.ok, outcome
    assert ["sudo", "-n", "systemctl", "reboot", "--check-inhibitors=no"] in sim.calls
    assert ["sudo", "-n", "systemctl", "reboot"] not in sim.calls
    assert outcome.data["forced"] is True and "2/2 agents resumed" in outcome.summary
    assert ("ship-01", "rebooting, overriding any shutdown lock") in seen
    assert sim.probes == 0


@pytest.mark.parametrize("said", ["Call to Reboot failed: Access denied",
                                  "Call to Reboot failed: Operation denied due to active "
                                  "block inhibitor"])
def test_a_refused_forced_reboot_is_reported_as_it_is(world, said):
    """Forcing was the way past a lock already: no lookup, no second try."""
    sim = world.sims["ship-01"]
    sim.refuse_forced, sim.blockers = said + "\n", [APT_IN_SESSION]
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")], force=True)
    assert not outcome.ok
    assert outcome.summary == f"ship-01: the reboot command failed: {said}"
    assert sim.probes == 0


def test_update_then_a_refused_reboot_says_both_and_how_to_finish(world):
    """The update is done; the reboot that switches herdr is what is left to do."""
    sim = world.sims["ship-01"]
    sim.herdr_changes = True
    sim.refuse_reboot, sim.blockers = DENIED, [APT_IN_SESSION]
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle())
    assert not outcome.ok
    assert outcome.summary.startswith("updated to ")
    assert "; reboot refused: a login session is blocking it: apt (PID 4242" in \
        outcome.summary
    assert outcome.data["rebooted"] is False and "refused" in outcome.data["reboot"]
    assert any("ship-fleet reboot ship-01 --force" in d for d in outcome.details)
    assert sim.converged and sim.reboots == []


def test_reboot_stops_when_another_machine_answers_at_the_name(world):
    """A status from a different Ship id after the reboot is not this Ship coming back."""
    sim = world.sims["ship-01"]
    sim.after_reboot_id = "00000000-0000-4000-8000-000000000099"
    start = world.clock.t
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")])
    assert not outcome.ok
    assert "00000000-0000-4000-8000-000000000099" in outcome.summary
    assert "ignoring its status" in outcome.summary
    assert world.clock.t - start < 600  # stopped at once, did not wait out the budget


def test_reboot_stops_when_the_ship_returns_with_another_host_key(world):
    sim = world.sims["ship-01"]
    sim.after_reboot_host_key = True
    start = world.clock.t
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")])
    assert not outcome.ok
    assert "host key other than the pinned one" in outcome.summary
    assert world.clock.t - start < 600


def test_reboot_without_wait(world):
    [outcome] = ops.reboot(world.fleet, [world.ship("ship-01")], wait=False)
    assert outcome.ok and outcome.summary == "reboot started"
    assert world.sims["ship-01"].reboots


def test_reboot_of_an_unreachable_ship_fails_only_that_ship(world):
    world.sims["ship-02"].down_until = world.clock.t + 10_000
    outcomes = ops.reboot(world.fleet, [world.ship("ship-01"), world.ship("ship-02")])
    assert [o.ok for o in outcomes] == [True, False]
    assert "Cannot reach ship-02" in outcomes[1].summary
    assert world.sims["ship-02"].reboots == []


# --- update ---------------------------------------------------------------------


def test_update_uploads_converges_and_cleans_up(world):
    sim = world.sims["ship-01"]
    seen, on_event = events()
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(),
                           on_event=on_event)
    assert outcome.ok, outcome
    assert outcome.summary == f"updated to 0.2.0-{NEW_COMMIT[:12]}"
    assert sim.converged == [NEW_COMMIT]
    assert sim.reboots == []  # herdr did not change: no reboot
    assert outcome.data["rebooted"] is False
    assert list((world.incoming / "ship-01").iterdir()) == []  # upload and sidecar removed
    assert any("uploading the bundle" in text for _, text in seen)


def test_update_reboots_when_herdr_changed_and_checks_agents(world):
    sim = world.sims["ship-01"]
    sim.herdr_changes = True
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle())
    assert outcome.ok, outcome
    assert sim.reboots and outcome.data["rebooted"] is True
    assert "2/2 agents resumed in their modes" in outcome.summary
    assert outcome.data["reboot"]["agents_before"] == {"main": "auto",
                                                        "builder": "bypassPermissions"}


def test_update_reports_a_mode_lost_across_the_herdr_reboot(world):
    sim = world.sims["ship-01"]
    sim.herdr_changes = True
    sim.after_reboot = {"main": "auto", "builder": "default"}
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle())
    assert not outcome.ok
    assert "agent builder came back in default, was bypassPermissions" in outcome.details


def test_update_never_reboots_with_never(world):
    sim = world.sims["ship-01"]
    sim.herdr_changes = True
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(),
                           reboot_policy="never")
    assert outcome.ok and sim.reboots == []
    assert any("next reboot" in d for d in outcome.details)


def test_update_always_reboots_with_always(world):
    sim = world.sims["ship-01"]
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(),
                           reboot_policy="always")
    assert outcome.ok and len(sim.reboots) == 1


def test_update_skips_a_ship_already_on_the_bundle(world):
    sim = world.sims["ship-01"]
    sim.bundle_commit, sim.tooling = NEW_COMMIT[:12], "0.2.0"
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle())
    assert outcome.ok and outcome.summary.startswith("already on")
    assert sim.converged == []
    [forced] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(),
                          force=True)
    assert forced.ok and sim.converged == [NEW_COMMIT]


def test_update_always_converges_a_dirty_bundle(world):
    sim = world.sims["ship-01"]
    sim.bundle_commit, sim.tooling = NEW_COMMIT[:12], "0.2.0"
    seen, on_event = events()
    ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(dirty=True),
               on_event=on_event)
    assert sim.converged == [NEW_COMMIT]
    assert any("uncommitted" in text for _, text in seen)


def test_update_up_to_date_ship_with_herdr_pending_still_reboots(world):
    sim = world.sims["ship-01"]
    sim.bundle_commit, sim.tooling, sim.herdr_pending = NEW_COMMIT[:12], "0.2.0", True
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle())
    assert outcome.ok and sim.converged == [] and len(sim.reboots) == 1


def test_update_converge_failure_is_reported_and_upload_removed(world):
    sim = world.sims["ship-01"]
    sim.converge_error = "apt failed"
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle())
    assert not outcome.ok
    assert "provision-failed" in outcome.summary and "apt failed" in outcome.summary
    assert list((world.incoming / "ship-01").iterdir()) == []
    assert sim.reboots == []


def test_update_catches_a_ship_reporting_the_wrong_bundle(world):
    world.sims["ship-01"].report_commit = "badbadbadbad"
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle())
    assert not outcome.ok
    assert "reports bundle badbadbadbad" in outcome.summary


def test_update_refuses_a_provisioning_ship(world, monkeypatch):
    sim = world.sims["ship-01"]
    original = sim.status

    def provisioning(ship):
        doc = original(ship)
        doc["state"] = "provisioning"
        return doc

    sim.status = provisioning
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle())
    assert not outcome.ok and "still provisioning" in outcome.summary
    assert sim.converged == []


def test_update_rejects_an_unknown_reboot_policy(world):
    with pytest.raises(FleetError):
        ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(),
                   reboot_policy="sometimes")


def test_upload_never_puts_the_bundle_in_argv(world):
    bundle = make_bundle()
    ops.update(world.fleet, [world.ship("ship-01")], bundle=bundle)
    upload = next(c for c in world.sims["ship-01"].calls if c[:2] == ["bash", "-c"])
    assert upload[2] == ops.UPLOAD_SCRIPT
    assert upload[3:] == ["upload", ops.INCOMING, f"bundle-{bundle.sha256[:16]}.tar.gz",
                          bundle.sha256]


def test_update_many_ships_one_failing(world):
    world.sims["ship-02"].down_until = world.clock.t + 10_000
    outcomes = ops.update(world.fleet, [world.ship("ship-01"), world.ship("ship-02")],
                          bundle=make_bundle())
    assert [o.ship for o in outcomes] == ["ship-01", "ship-02"]
    assert [o.ok for o in outcomes] == [True, False]


# --- settings, tags, tools, logs ------------------------------------------------


def test_parse_assignments_new_keys():
    assert ops.parse_assignments([
        "lan_guard=true", "lan_guard_allow=192.0.2.10,198.51.100.0/24",
        "auto_reboot=true", "reboot_time=03:30", "timezone=America/Argentina/Buenos_Aires",
    ]) == ["network.lan_guard=true", "network.lan_guard_allow=192.0.2.10,198.51.100.0/24",
           "updates.auto_reboot=true", "updates.reboot_time=03:30",
           "ship.timezone=America/Argentina/Buenos_Aires"]
    assert ops.parse_assignments(["lan_guard_allow="]) == ["network.lan_guard_allow="]


@pytest.mark.parametrize("bad", [
    "reboot_time=4am", "reboot_time=24:00", "lan_guard_allow=router.example.com",
    "auto_reboot=yes", "session_prefix=-bad", "nonsense=1", "lan_guard",
    "timezone=../../etc/shadow", "timezone=europe/berlin", "timezone=", "timezone=UTC+10:00",
])
def test_parse_assignments_rejects(bad):
    with pytest.raises(FleetError):
        ops.parse_assignments([bad])


def test_set_settings_on_many_ships_records_claude_keys(world):
    outcomes = ops.set_settings(world.fleet, [world.ship("ship-01"), world.ship("ship-02")],
                                ["update_channel=stable", "lan_guard=true"])
    assert all(o.ok for o in outcomes)
    for name in ("ship-01", "ship-02"):
        call = next(c for c in world.sims[name].calls if c[3:4] == ["set"])
        assert call[4:] == ["claude.update_channel=stable", "network.lan_guard=true", "--json"]
        assert world.store.load().ship(name).claude["update_channel"] == "stable"


def test_set_default_mode_says_running_agents_keep_theirs(world):
    [outcome] = ops.set_settings(world.fleet, [world.ship("ship-01")],
                                 ["default_mode=acceptEdits"])
    assert outcome.ok
    assert any("Running agents keep the mode they are in" in d
               and "ship-fleet agent restart ship-01 --all" in d for d in outcome.details)
    [outcome] = ops.set_settings(world.fleet, [world.ship("ship-01")], ["lan_guard=true"])
    assert not any("Running agents" in d for d in outcome.details)


# --- agent restart ----------------------------------------------------------------


def test_restart_agent_then_reboot_expects_the_new_mode(world):
    sim = world.sims["ship-01"]
    outcome = ops.restart_agents(world.fleet, world.ship("ship-01"), ["main"],
                                 mode="bypassPermissions")
    assert outcome.ok, outcome
    assert outcome.summary == "restarted main in bypassPermissions"
    call = next(c for c in sim.calls if c[3:4] == ["agent"])
    assert call[3:] == ["agent", "restart", "main", "--mode", "bypassPermissions", "--json"]
    # The reboot check reads the modes from status, which now says bypass for main.
    [rebooted] = ops.reboot(world.fleet, [world.ship("ship-01")])
    assert rebooted.ok, rebooted
    assert rebooted.data["agents_before"] == {"main": "bypassPermissions",
                                              "builder": "bypassPermissions"}
    # Had it come back in its old mode, the check would say so.
    sim.after_reboot = {"main": "auto", "builder": "bypassPermissions"}
    [rebooted] = ops.reboot(world.fleet, [world.ship("ship-01")])
    assert not rebooted.ok
    assert "agent main came back in auto, was bypassPermissions" in rebooted.details


def test_restart_agent_passes_every_option(world):
    sim = world.sims["ship-01"]
    ops.restart_agents(world.fleet, world.ship("ship-01"), everyone=True, fresh=True,
                       force=True)
    call = next(c for c in sim.calls if c[3:4] == ["agent"])
    assert call[3:] == ["agent", "restart", "--all", "--fresh", "--force", "--json"]


def test_restart_agent_reports_the_bypass_refusal(world):
    world.sims["ship-01"].allow_bypass = False
    [outcome] = ops.for_each(world.fleet, [world.ship("ship-01")], lambda s, ev: (
        ops.restart_agents(world.fleet, s, ["main"], mode="bypassPermissions")))
    assert not outcome.ok
    assert "bypassPermissions is not allowed on this Ship" in outcome.summary
    assert world.sims["ship-01"].agents["main"] == "auto"


def test_restart_agent_keeps_what_the_ship_did_when_one_is_busy(world):
    world.sims["ship-01"].working = {"builder"}
    outcome = ops.restart_agents(world.fleet, world.ship("ship-01"), everyone=True,
                                 mode="plan")
    assert not outcome.ok
    assert outcome.summary.startswith("restarted main in plan; ")
    assert "builder: it is working; not interrupted" in outcome.details
    assert outcome.data["skipped"] == [{"name": "builder",
                                        "reason": "it is working; not interrupted"}]


@pytest.mark.parametrize("kwargs,why", [
    ({}, "Which agents"),
    ({"agents": ["main"], "everyone": True}, "Which agents"),
    ({"agents": ["Main!"]}, "not an agent name"),
    ({"agents": ["main"], "mode": "yolo"}, "Unknown mode"),
])
def test_restart_agent_checks_its_arguments_first(world, kwargs, why):
    with pytest.raises(FleetError, match=why):
        ops.restart_agents(world.fleet, world.ship("ship-01"), **kwargs)
    assert not world.sims["ship-01"].calls


def test_restart_agent_on_older_tooling_says_to_update(world):
    world.sims["ship-01"].ctl_version = 0
    with pytest.raises(FleetError) as exc:
        ops.restart_agents(world.fleet, world.ship("ship-01"), ["main"])
    assert "cannot restart agents yet" in exc.value.message
    assert exc.value.hint == "Update it first: ship-fleet update ship-01"


def test_set_tags_changes_inventory_and_ship(world):
    [outcome] = ops.set_tags(world.fleet, [world.ship("ship-01")], ["+lab", "-work"])
    assert outcome.ok and outcome.data["tags"] == ["lab"]
    assert world.store.load().ship("ship-01").tags == ("lab",)
    call = next(c for c in world.sims["ship-01"].calls if c[3:4] == ["set"])
    assert call[4] == "ship.tags=lab"


def test_set_tags_local_does_not_touch_the_ship(world):
    [outcome] = ops.set_tags(world.fleet, [world.ship("ship-01")], ["+lab"], local=True)
    assert outcome.ok and world.sims["ship-01"].calls == []


def test_set_tags_rejects_a_bad_tag_before_any_change(world):
    with pytest.raises(FleetError):
        ops.set_tags(world.fleet, [world.ship("ship-01")], ["+Bad Tag"])
    assert world.store.load().ship("ship-01").tags == ("work",)


def test_set_tags_reports_a_ship_it_could_not_reach(world):
    world.sims["ship-01"].down_until = world.clock.t + 10_000
    [outcome] = ops.set_tags(world.fleet, [world.ship("ship-01")], ["+lab"])
    assert not outcome.ok and "inventory" in outcome.summary
    assert world.store.load().ship("ship-01").tags == ("lab", "work")


def test_change_tools_on_many_ships(world):
    outcomes = ops.change_tools(world.fleet, [world.ship("ship-01"), world.ship("ship-02")],
                                "add", ["tasks-axi"])
    assert all(o.ok for o in outcomes)
    assert "tasks-axi" in world.store.load().ship("ship-02").tools


def test_change_tools_rejects_unknown_ids_before_touching_ships(world):
    with pytest.raises(FleetError):
        ops.change_tools(world.fleet, [world.ship("ship-01")], "add", ["no-such-tool"])
    assert world.sims["ship-01"].calls == []


def test_logs_and_fleet_logs(world):
    assert ops.logs(world.fleet, world.ship("ship-01"), "herdr", 5) == ["ship-01 line"]
    outcomes = ops.fleet_logs(world.fleet, [world.ship("ship-01"), world.ship("ship-02")],
                              "status", 5)
    assert [o.data["lines"] for o in outcomes] == [["ship-01 line"], ["ship-02 line"]]
    with pytest.raises(FleetError):
        ops.logs(world.fleet, world.ship("ship-01"), "secrets", 5)
    with pytest.raises(FleetError):
        ops.logs(world.fleet, world.ship("ship-01"), "herdr", 0)


# --- attention ------------------------------------------------------------------


def test_attention_orders_by_severity_then_ship(store):
    inv = store.load()
    one, two = inv.ship("ship-01"), inv.ship("ship-02")
    views = [
        status.ShipView(one, status=RUNNING, attention=[
            {"code": "behind-tooling", "message": "old tooling", "subject": "tooling"},
            {"code": "agent-waiting", "message": "main waits", "subject": "main"},
        ]),
        status.ShipView(two, problem="unreachable", message="Cannot reach ship-02"),
    ]
    items = ops.attention(views)
    assert [(i.ship, i.code, i.severity) for i in items] == [
        ("ship-02", "unreachable", "high"),
        ("ship-01", "agent-waiting", "medium"),
        ("ship-01", "behind-tooling", "low"),
    ]
    assert items[1].as_dict()["subject"] == "main"


def test_attention_shows_unknown_codes_as_medium(store):
    ship = store.load().ship("ship-01")
    view = status.ShipView(ship, status=RUNNING,
                           attention=[{"code": "brand-new", "message": "something"}])
    [item] = ops.attention([view])
    assert item.code == "brand-new" and item.severity == "medium" and item.subject is None


# --- the CLI on top -------------------------------------------------------------


@pytest.fixture
def app(world, fake_repo):
    out, err = io.StringIO(), io.StringIO()
    tty = {"value": False}
    answers = []
    a = cli.App(out=out, err=err, store_factory=lambda: world.store,
                repo_factory=lambda: fake_repo, ssh_factory=world.fleet.ssh_factory,
                sleep=world.clock.sleep, clock=world.clock,
                stdin_isatty=lambda: tty["value"],
                confirm=lambda prompt: answers.pop(0))

    def run(*argv):
        out.seek(0), out.truncate(), err.seek(0), err.truncate()
        code = cli.main(list(argv), a)
        return code, out.getvalue(), err.getvalue()

    a.run, a.tty, a.answers = run, tty, answers
    return a


@pytest.fixture
def packed(monkeypatch):
    bundle = make_bundle()
    monkeypatch.setattr(ops.bundle_mod, "pack", lambda repo: bundle)
    return bundle


def test_cli_update_one_ship(app, world, packed):
    code, out, err = app.run("update", "ship-01")
    assert code == 0, err
    assert f"ship-01: updated to 0.2.0-{NEW_COMMIT[:12]}" in out
    assert "ship-01: uploading the bundle" in err


def test_cli_update_many_needs_yes_when_not_a_terminal(app, world, packed):
    code, out, err = app.run("update", "all")
    assert code == 1 and "--yes" in err
    assert world.sims["ship-01"].converged == []
    code, out, err = app.run("update", "all", "--yes")
    assert code == 0, err
    assert "2 of 2 Ships OK." in out


def test_cli_update_many_asks_on_a_terminal(app, world, packed):
    app.tty["value"] = True
    app.answers.append("no")
    code, _, err = app.run("update", "--tag", "work")
    assert code == 0  # one Ship: no question asked
    app.answers.append("no")
    code, _, err = app.run("update", "all", "--force")
    assert code == 1 and "Nothing done" in err


def test_cli_update_never_does_not_ask(app, world, packed):
    code, out, _ = app.run("update", "all", "--reboot", "never", "--json")
    assert code == 0
    assert [o["ship"] for o in json.loads(out)] == ["ship-01", "ship-02"]


def test_cli_reboot_reports_lost_mode_with_exit_1(app, world):
    world.sims["ship-01"].after_reboot = {"main": "auto", "builder": "auto"}
    code, out, _ = app.run("reboot", "ship-01")
    assert code == 1
    assert "ship-01: FAILED:" in out
    assert "agent builder came back in auto, was bypassPermissions" in out


def test_cli_reboot_tag_with_yes(app, world):
    code, out, _ = app.run("reboot", "--tag", "personal", "--yes", "--no-wait")
    assert code == 0 and "ship-02: reboot started" in out


def test_cli_agent_restart(app, world):
    code, out, err = app.run("agent", "restart", "ship-01", "main", "--mode", "plan")
    assert code == 0, err
    assert "ship-01: restarted main in plan" in out
    assert "restarting main in plan" in err
    world.sims["ship-01"].working = {"main"}
    code, out, _ = app.run("agent", "restart", "ship-01", "--all", "--json")
    assert code == 1
    assert json.loads(out)[0]["data"]["skipped"][0]["name"] == "main"
    code, _, err = app.run("agent", "restart", "ship-01", "main", "--all")
    assert code == 1 and "not both" in err


def test_cli_reboot_refused_by_a_lock_says_what_holds_it_then_force_reboots(app, world):
    sim = world.sims["ship-01"]
    sim.refuse_reboot, sim.blockers = DENIED, [APT_IN_SESSION]
    code, out, _ = app.run("reboot", "ship-01")
    assert code == 1 and sim.reboots == []
    assert ("ship-01: FAILED: reboot refused: a login session is blocking it: apt (PID 4242, "
            "as root) in fleet's SSH login session on pts/0") in out
    assert "  To reboot anyway: ship-fleet reboot ship-01 --force" in out
    code, out, _ = app.run("reboot", "ship-01", "--force")
    assert code == 0, out
    assert sim.reboots and sim.calls[-1] != ["sudo", "-n", "systemctl", "reboot"]
    assert "2/2 agents resumed in their modes" in out


def test_cli_force_reboot_of_many_asks_as_a_force_reboot(app, world):
    code, _, err = app.run("reboot", "all", "--force")
    assert code == 1 and "This would force-reboot 2 Ships (ship-01, ship-02)" in err
    app.tty["value"] = True
    app.answers.append("no")
    code, _, err = app.run("reboot", "all", "--force")
    assert code == 1 and "Nothing done" in err
    assert world.sims["ship-01"].reboots == []


def test_cli_set_by_tag_and_all(app, world):
    code, out, err = app.run("set", "--tag", "personal", "auto_reboot=true")
    assert code == 0, err
    assert "ship-02:" in out and "ship-01" not in out
    code, out, err = app.run("set", "all", "reboot_time=02:15")
    assert code == 0 and "2 of 2 Ships OK." in out
    code, _, err = app.run("set", "ship-01", "reboot_time=late")
    assert code == 1 and "HH:MM" in err
    assert not any(c[3:4] == ["set"] and "updates.reboot_time=late" in c
                   for c in world.sims["ship-01"].calls)


def test_cli_tools_by_tag(app, world):
    code, out, err = app.run("tools", "add", "--tag", "work", "tasks-axi")
    assert code == 0, err
    assert "ship-01: tools" in out and "tasks-axi" in out


def test_cli_logs_many_prefixes_lines(app, world):
    code, out, _ = app.run("logs", "all", "status", "--lines", "3")
    assert code == 0
    assert out.splitlines() == ["ship-01: ship-01 line", "ship-02: ship-02 line"]
    code, out, _ = app.run("logs", "ship-01", "herdr")
    assert out.splitlines() == ["ship-01 line"]
    code, _, err = app.run("logs", "ship-01", "nope")
    assert code == 1 and "Unknown log source" in err


def test_cli_attention_table_json_and_watch(app, world):
    world.sims["ship-02"].down_until = world.clock.t + 10_000
    world.sims["ship-01"].herdr_pending = True
    code, out, _ = app.run("attention")
    assert code == 0
    lines = out.splitlines()
    assert lines[1].split()[:3] == ["high", "ship-02", "unreachable"]
    assert "herdr-restart-pending" in out
    code, out, _ = app.run("attention", "--json", "--tag", "work")
    assert [i["code"] for i in json.loads(out)] == ["herdr-restart-pending"]

    polls = {"n": 0}

    def sleep(seconds):
        polls["n"] += 1
        world.clock.sleep(seconds)
        if polls["n"] == 1:
            world.sims["ship-01"].herdr_pending = False
            world.sims["ship-01"].agents = {"main": "auto"}
        if polls["n"] == 2:
            raise KeyboardInterrupt

    app.sleep = sleep
    code, out, _ = app.run("attention", "--watch", "5")
    assert code == 130
    printed = out.splitlines()
    # First round prints both items; the second prints nothing new.
    assert len(printed) == 2
    assert any("unreachable" in line or "Cannot reach" in line for line in printed)


def test_cli_attention_nothing(app, world):
    code, out, _ = app.run("attention", "--tag", "personal")
    assert code == 0 and out.strip() == "Nothing needs attention."


@pytest.mark.parametrize("argv,expected", [
    (["tools", "add", "--tag", "work", "a", "b"], ["tools", "--tag", "work", "add", "a", "b"]),
    (["logs", "--tag=work", "herdr"], ["logs", "--tag=work", "herdr"]),
    (["set", "ship-01", "x=1"], ["set", "ship-01", "x=1"]),
    (["ssh", "ship-01", "--", "--tag", "x"], ["ssh", "ship-01", "--", "--tag", "x"]),
    (["login", "--tag", "work", "claude"], ["login", "--tag", "work", "claude"]),
])
def test_tag_option_is_hoisted_for_batch_commands(argv, expected):
    assert cli._hoist_tag_option(argv) == expected


# --- update never rolls a Ship back ------------------------------------------------------


@pytest.fixture
def history(fake_repo):
    """This checkout in git: one, then two (HEAD); side branches off one."""
    root = fake_repo.root

    def git(*args):
        return subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c",
                               "user.email=test@example.com", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "one")
    one = git("rev-parse", "HEAD")
    git("checkout", "-qb", "side")
    (root / "side.txt").write_text("side\n")
    git("add", "-A")
    git("commit", "-qm", "side")
    side = git("rev-parse", "HEAD")
    git("checkout", "-q", "-")
    (root / "two.txt").write_text("two\n")
    git("add", "-A")
    git("commit", "-qm", "two")
    return one, git("rev-parse", "HEAD"), side


def test_update_never_rolls_a_ship_back_to_an_older_commit(world, history):
    one, two, _ = history
    sim = world.sims["ship-01"]
    sim.bundle_commit = two[:12]  # another computer's newer checkout updated it
    seen, on_event = events()
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(commit=one),
                           on_event=on_event)
    assert not outcome.ok, outcome
    assert outcome.summary == (f"not updated: it runs ShipShape {two[:12]}, newer than this "
                               f"checkout's {one[:12]} (or on another branch)")
    said = " ".join(outcome.details)
    assert "git pull" in said and "ship-fleet update ship-01 --force" in said
    assert "Fleet actions" in said  # the web UI's way too
    assert sim.converged == [] and not list((world.incoming / "ship-01").glob("*"))
    assert ("ship-01", "not updated: that would roll it back") in seen

    seen, on_event = events()
    [forced] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(commit=one),
                          force=True, on_event=on_event)
    assert forced.ok and sim.converged == [one]  # on purpose, and said so
    assert forced.summary == f"rolled back to 0.2.0-{one[:12]}"
    assert forced.details[0].startswith("Rolled back on purpose (--force): it runs ShipShape")
    assert forced.data["rolled_back"].startswith(f"it runs ShipShape {two[:12]}")
    assert any(text.startswith("rolling back on purpose (--force)") for _, text in seen)


def test_update_brings_a_ship_forward(world, history):
    one, two, _ = history
    sim = world.sims["ship-01"]
    sim.bundle_commit = one[:12]
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(commit=two))
    assert outcome.ok and sim.converged == [two]


@pytest.mark.parametrize("where,says", [("side", "or on another branch"),
                                        ("elsewhere", "a commit this checkout does not have")])
def test_a_ship_on_another_branch_or_an_unknown_commit_is_left_alone(world, history, where,
                                                                     says):
    one, two, side = history
    sim = world.sims["ship-01"]
    sim.bundle_commit = side[:12] if where == "side" else "abcdef012345"
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(commit=two))
    assert not outcome.ok and says in outcome.summary and sim.converged == []


def test_a_ship_on_a_commit_this_checkout_lacks_may_be_a_merged_branch(world, history):
    one, two, _ = history
    world.sims["ship-01"].bundle_commit = "abcdef012345"
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(commit=two))
    said = " ".join(outcome.details)
    assert "since merged or rebased, this is the move forward" in said and "--force" in said


def test_a_shallow_clone_says_it_cannot_tell(world, history, fake_repo, tmp_path):
    from fleetledger.repo import Repo

    one, two, _ = history
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{fake_repo.root}",
                    str(shallow)], check=True, capture_output=True)
    world.fleet.repo_factory = lambda: Repo(shallow)
    sim = world.sims["ship-01"]
    sim.bundle_commit = one[:12]  # older: but past the clone's depth
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle(commit=two))
    assert not outcome.ok and "this shallow clone cannot see" in outcome.summary
    assert "git fetch --unshallow" in outcome.details[0] and sim.converged == []


def test_commits_reach_git_only_as_hex(history, fake_repo):
    for odd in ("--help", "-C/tmp", "HEAD", "main", "ABCDEF0123456", ""):
        assert fake_repo.has_commit(odd) is False, odd
        assert fake_repo.is_ancestor(odd, "HEAD") is None, odd
    assert fake_repo.has_commit(history[0][:12])


def test_a_ship_already_on_this_commit_is_not_a_roll_back(world, history):
    one, two, _ = history
    sim = world.sims["ship-01"]
    sim.bundle_commit, sim.tooling = two[:12], "0.2.0"
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")],
                           bundle=make_bundle(commit=two, dirty=True))
    assert outcome.ok and sim.converged == [two]  # a dirty bundle converges, as before


def test_a_checkout_outside_git_cannot_tell_and_updates_as_before(world):
    sim = world.sims["ship-01"]
    sim.bundle_commit = "abcdef012345"
    [outcome] = ops.update(world.fleet, [world.ship("ship-01")], bundle=make_bundle())
    assert outcome.ok and sim.converged == [NEW_COMMIT]


def test_cli_update_says_why_it_left_a_ship_and_force_rolls_it_back(app, world, history,
                                                                   monkeypatch):
    one, two, _ = history
    world.sims["ship-01"].bundle_commit = two[:12]
    monkeypatch.setattr(ops.bundle_mod, "pack", lambda repo: make_bundle(commit=one))
    code, out, err = app.run("update", "ship-01")
    assert code == 1 and f"newer than this checkout's {one[:12]}" in out + err
    assert world.sims["ship-01"].converged == []
    code, out, err = app.run("update", "ship-01", "--force")
    assert code == 0, out + err
    assert world.sims["ship-01"].converged == [one]
