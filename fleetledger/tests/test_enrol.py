import pytest
from fleetledger.drivers.base import Outcome
from fleetledger.errors import FleetError
from fleetledger.ssh import HostKeyMismatch
from fleetledger.status import ShipView

from fleetledger import enrol as enrol_mod


def views(*states):
    """ShipViews for a sequence of provision results / problems."""
    out = []
    for s in states:
        if s in ("unreachable", "host-key", "identity"):
            out.append(ShipView(None, problem=s, message=s))
        else:
            out.append(ShipView(None, status={"state": "provisioning",
                                              "provision": {"result": s}}))
    return out


@pytest.fixture
def scripted(monkeypatch):
    def install(*states):
        queue = views(*states)
        calls = []

        def fake_fetch(ssh, inventory, ship, tooling=None):
            calls.append(ship.name)
            return queue.pop(0)

        monkeypatch.setattr(enrol_mod, "fetch", fake_fetch)
        return calls

    return install


class Driver:
    def __init__(self, done=True):
        self.done = done
        self.finished = []

    def finish_enrolment(self, ship):
        self.finished.append(ship.name)
        if self.done:
            return Outcome(done=True, messages=["Detached the seed."])
        return Outcome(done=False, instructions=["qm set 9101 --delete ide2"])


def test_waits_through_boot(store, scripted):
    calls = scripted("unreachable", "unreachable", "running", "ok")
    inv = store.load()
    seen = []
    view = enrol_mod.wait_for_provisioning(
        None, inv, inv.ship("ship-01"), sleep=lambda s: None, on_update=seen.append
    )
    assert view.provision_result == "ok" and len(calls) == 4 and len(seen) == 4


def test_host_key_stops_at_once(store, scripted):
    scripted("unreachable", "host-key", "ok")
    inv = store.load()
    with pytest.raises(HostKeyMismatch):
        enrol_mod.wait_for_provisioning(None, inv, inv.ship("ship-01"), sleep=lambda s: None)


def test_identity_problem_stops_at_once(store, scripted):
    calls = scripted("unreachable", "identity", "ok")
    inv = store.load()
    with pytest.raises(FleetError, match="identity"):
        enrol_mod.wait_for_provisioning(None, inv, inv.ship("ship-01"), sleep=lambda s: None)
    assert len(calls) == 2


def test_times_out(store, scripted):
    scripted(*["unreachable"] * 10)
    inv = store.load()
    ticks = iter(range(0, 100000, 100))
    with pytest.raises(enrol_mod.StillStarting, match="did not finish provisioning") as exc:
        enrol_mod.wait_for_provisioning(
            None, inv, inv.ship("ship-01"), timeout=250, interval=100,
            sleep=lambda s: None, clock=lambda: next(ticks),
        )
    # Not called an error: a slow first boot may still get there.
    assert "may still be starting" in exc.value.message
    assert "ship-fleet status ship-01 --wait" in exc.value.hint


def _with_seed(store, home):
    with store.edit() as ed:
        ed.set_field("ship-01", "driver_ref", {"vmid": 9101, "seed": "ship-01-0a1b2c3d"})
    seeds = home / "data" / "seeds"
    seeds.mkdir(mode=0o700)
    copy = seeds / "ship-01-0a1b2c3d.iso"
    copy.write_bytes(b"iso")
    return copy


def test_enrol_records_and_cleans_up(store, scripted, home):
    copy = _with_seed(store, home)
    scripted("running", "ok")
    driver = Driver(done=True)
    result = enrol_mod.enrol(store, None, driver, store.load().ship("ship-01"),
                             sleep=lambda s: None)
    assert driver.finished == ["ship-01"]
    assert not copy.exists()
    ship = store.load().ship("ship-01")
    assert ship.enrolled is not None and ship.driver_ref == {"vmid": 9101}
    assert result.messages[-1] == "ship-01 is enrolled."


def test_enrol_manual_keeps_seed_name(store, scripted, home):
    _with_seed(store, home)
    scripted("ok")
    result = enrol_mod.enrol(store, None, Driver(done=False), store.load().ship("ship-01"),
                             sleep=lambda s: None)
    assert result.instructions == ["qm set 9101 --delete ide2"]
    ship = store.load().ship("ship-01")
    assert ship.enrolled and ship.driver_ref["seed"] == "ship-01-0a1b2c3d"


def test_failed_provisioning_is_not_enrolled(store, scripted, home):
    _with_seed(store, home)
    with store.edit() as ed:
        ed.set_field("ship-01", "enrolled", None)  # being made
    scripted("failed")
    with pytest.raises(FleetError, match="Provisioning failed"):
        enrol_mod.enrol(store, None, Driver(), store.load().ship("ship-01"), sleep=lambda s: None)
    assert store.load().ship("ship-01").enrolled is None
