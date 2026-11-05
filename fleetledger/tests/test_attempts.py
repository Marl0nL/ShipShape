"""A failed `ship-fleet new` is saved (attempts.py) so it can be edited and tried
again: without secrets, bounded, cleared once a Ship of that name is made, and never
retried into a second VM of the same name."""

from __future__ import annotations

import json
import os
import stat

import pytest
from fleetledger.drivers.base import LeftBehind, Outcome
from fleetledger.drivers.proxmox import ProxmoxDriver
from fleetledger.errors import FleetError
from fleetledger.web import newship
from test_cli import FakeDriver, env  # noqa: F401  (env: a fixture)
from test_drivers import PX, SEED, SIZE, FailingAPI, FakeAPI, mutating, ship

from fleetledger import attempts, cli


class LeavingDriver(FakeDriver):
    """create() fails and could not remove its VM; remove_left can be told to fail."""

    def __init__(self):
        super().__init__()
        self.leave = True
        self.remove_fails = False
        self.removed = []

    def create(self, ship, seed, size, image=None):
        if self.leave:
            raise LeftBehind("Proxmox POST failed: 500 storage full. FleetLedger could "
                             "not remove VM 9101 afterwards.",
                             [{"kind": "vm", "vmid": 9101, "name": ship.name}])
        return super().create(ship, seed, size, image)

    def remove_left(self, name, left):
        if self.remove_fails:
            raise FleetError("Proxmox DELETE failed: 500 VM is locked (clone)")
        self.removed.append((name, left))
        return [f"Deleted VM {left[0]['vmid']} ({name})."]


def saved_file():
    return attempts.attempts_path()


# --- saving the attempt ------------------------------------------------------------


def test_a_failed_new_is_saved_with_its_options_and_nothing_secret(env, home):  # noqa: F811
    env.driver.fail = True
    with pytest.raises(RuntimeError):  # not a FleetError: it still ends the command
        env.run("new", "ship-05", "--driver", "proxmox", "--cores", "6", "--memory-mb",
                "12288", "--disk-gb", "60", "--tag", "work", "--tag", "lab", "--signins",
                "claude,github", "--autostart", "lead:~/work/x:plan", "--repo",
                "someone/agent-setup", "--host", "ship-05.example.net", "--agent-sudo",
                "--autostart-prompt", "use ghp_abcdefghijklmnopqrstuvwxyz0123456789 to log in",
                "--timezone", "Europe/Berlin", "--no-overlay")
    path = saved_file()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    attempt = attempts.get("ship-05")
    assert attempt.stage == "create" and attempt.driver == "proxmox"
    assert attempt.error == "Internal error (RuntimeError)."
    assert attempt.options == {
        "name": "ship-05", "tag": ["work", "lab"], "cores": 6, "memory_mb": 12288,
        "disk_gb": 60, "signins": "claude,github", "autostart": ["lead:~/work/x:plan"],
        "repo": "someone/agent-setup", "no_overlay": True, "driver": "proxmox",
        "host": ["ship-05.example.net"], "agent_sudo": True, "timezone": "Europe/Berlin"}
    assert attempt.dropped == ["autostart_prompt"]  # it held something token-like
    assert "ghp_" not in path.read_text()
    # Exactly the options it was given, as a command that parses.
    args = cli.build_parser().parse_args(attempt.argv())
    assert attempts.options_of(args)[0] == attempt.options


def test_the_cli_prints_the_exact_command_to_try_again(env, home):  # noqa: F811
    def broken(inv, driver_id, **kw):
        raise FleetError("No Proxmox token on this computer.")

    env.app.driver_factory = broken
    code, _, err = env.run("new", "ship-05", "--cores", "3", "--tag", "lab")
    assert code == 1 and "No Proxmox token" in err
    assert "To try again (edit what you need): ship-fleet new ship-05 --tag lab --cores 3" in err
    assert not env.store.load().has_ship("ship-05")  # the name is not left claimed
    assert attempts.get("ship-05").stage == "create"


def test_a_prompt_with_nothing_secret_is_kept(env, home):  # noqa: F811
    env.driver.fail = True
    with pytest.raises(RuntimeError):
        env.run("new", "ship-05", "--driver", "proxmox", "--autostart-prompt", "Get going.")
    attempt = attempts.get("ship-05")
    assert attempt.options["autostart_prompt"] == "Get going." and attempt.dropped == []


def test_a_bad_or_taken_name_saves_nothing(env, home):  # noqa: F811
    assert env.run("new", "Ship_5")[0] == 1
    assert env.run("new", "ship-01")[0] == 1  # in the inventory: that is its Ship's name
    assert attempts.load() == []


def test_a_new_that_works_drops_the_attempt(env, home):  # noqa: F811
    env.driver.fail = True
    with pytest.raises(RuntimeError):
        env.run("new", "ship-05", "--driver", "proxmox")
    env.driver.fail = False
    assert env.run("new", "ship-05", "--driver", "proxmox")[0] == 0
    assert attempts.get("ship-05") is None


def test_attempts_are_bounded_one_per_name_and_survive_a_restart(env, home, monkeypatch):  # noqa: F811
    clock = iter(range(1000, 2000))
    monkeypatch.setattr(attempts.time, "time", lambda: next(clock))
    env.driver.fail = True
    for n in range(attempts.KEEP + 3):
        with pytest.raises(RuntimeError):
            env.run("new", f"ship-{n + 30}", "--driver", "proxmox")
    with pytest.raises(RuntimeError):
        env.run("new", "ship-50", "--driver", "proxmox", "--cores", "2")  # again
    saved = attempts.load()  # read from the file: what a restarted FleetLedger sees
    assert len(saved) == attempts.KEEP
    assert [a.name for a in saved].count("ship-50") == 1
    assert saved[0].name == "ship-50" and saved[0].options["cores"] == 2
    assert "ship-30" not in {a.name for a in saved}  # the oldest went


def test_a_hand_edited_file_never_brings_in_unknown_options(home):
    path = saved_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"attempts": [
        {"name": "ship-07", "at": 1, "options": {"name": "ship-07", "cores": "8",
                                                  "token": "x", "tag": ["a"]},
         "stage": "weird", "left": [{"kind": "rm -rf"}]},
        {"name": "../x", "options": {"name": "../x"}},
        "junk"]}))
    [only] = attempts.load()
    assert only.options == {"name": "ship-07", "tag": ["a"]}
    assert only.stage == "checks" and only.left == []


# --- what a failed attempt left behind -----------------------------------------------


def test_proxmox_create_says_what_it_could_not_remove():
    api = FailingAPI("Proxmox DELETE failed: 500 VM is locked (clone)", vm_status="stopped",
                     fail_on="/resize")
    driver = ProxmoxDriver(PX, api=api, read_seed=lambda s: b"ISO")
    with pytest.raises(LeftBehind) as exc:
        driver.create(ship(vmid=9102), SEED, SIZE)
    assert exc.value.left_behind == [
        {"kind": "vm", "vmid": 9102, "name": "ship-03"},
        {"kind": "volume", "volume": "shipshape-seeds:iso/ship-03-0a1b2c3d.iso"}]
    assert "/resize failed" in exc.value.message and "VM 9102" in exc.value.message


def test_proxmox_create_removes_a_vm_whose_clone_failed_part_way():
    class CloneFails(FakeAPI):
        def wait_task(self, node, upid, timeout=600):
            if upid == "UPID:pve:2":  # the clone's task
                raise FleetError("Proxmox task failed: clone timed out")

    api = CloneFails(vm_status="stopped")
    with pytest.raises(FleetError, match="clone timed out"):
        ProxmoxDriver(PX, api=api, read_seed=lambda s: b"ISO").create(ship(vmid=9102), SEED,
                                                                        SIZE)
    assert ("DELETE", "/nodes/pve/qemu/9102") in [c[:2] for c in mutating(api)]


def test_proxmox_remove_left_checks_names_and_skips_what_is_gone():
    api = FakeAPI(vm_status="stopped")
    driver = ProxmoxDriver(PX, api=api)
    done = driver.remove_left("ship-03", [
        {"kind": "vm", "vmid": 9102, "name": "ship-03"},
        {"kind": "volume", "volume": "shipshape-seeds:iso/ship-03-0a1b2c3d.iso"}])
    assert done == ["Deleted VM 9102 (ship-03).",
                    "Deleted the seed volume shipshape-seeds:iso/ship-03-0a1b2c3d.iso."]
    gone = ProxmoxDriver(PX, api=FakeAPI(vm_name=None))
    assert gone.remove_left("ship-03", [{"kind": "vm", "vmid": 9102, "name": "ship-03"}]) == [
        "VM 9102 (ship-03) was gone already."]
    with pytest.raises(FleetError, match="not ship-03"):  # its VMID was reused
        ProxmoxDriver(PX, api=FakeAPI(vm_name="prod-db")).remove_left(
            "ship-03", [{"kind": "vm", "vmid": 9102, "name": "ship-03"}])
    with pytest.raises(FleetError, match="not a seed volume"):
        driver.remove_left("ship-03", [{"kind": "volume", "volume": "local:vm-1-disk-0"}])


def test_a_retry_removes_what_the_failed_attempt_left_first(env, home):  # noqa: F811
    leaving = LeavingDriver()
    env.app.driver_factory = lambda inv, d, **kw: leaving
    code, _, err = env.run("new", "ship-05", "--driver", "proxmox")
    assert code == 1 and "Trying again removes first what this attempt left: VM 9101" in err
    assert attempts.get("ship-05").left == [{"kind": "vm", "vmid": 9101, "name": "ship-05"}]
    leaving.leave = False
    code, out, _ = env.run("new", "ship-05", "--driver", "proxmox")
    assert code == 0
    assert leaving.removed == [("ship-05", [{"kind": "vm", "vmid": 9101, "name": "ship-05"}])]
    assert out.index("Deleted VM 9101") < out.index("seed written")  # before the new one
    assert [c[0] for c in leaving.created] == ["ship-05"]
    assert attempts.get("ship-05") is None


def test_a_retry_is_refused_while_what_it_left_cannot_be_removed(env, home):  # noqa: F811
    leaving = LeavingDriver()
    env.app.driver_factory = lambda inv, d, **kw: leaving
    env.run("new", "ship-05", "--driver", "proxmox")
    leaving.leave, leaving.remove_fails = False, True
    code, _, err = env.run("new", "ship-05", "--driver", "proxmox")
    assert code == 1
    assert ("The failed attempt at ship-05 left VM 9101 (ship-05), and FleetLedger cannot "
            "remove it: Proxmox DELETE failed: 500 VM is locked (clone)") in err
    assert "Delete it on the hypervisor by hand" in err
    assert leaving.created == []  # never a second VM of that name
    assert not env.store.load().has_ship("ship-05")
    assert attempts.get("ship-05").left  # still there to remove next time


def test_a_ship_that_was_made_but_did_not_enrol_blocks_its_retry(env, home, monkeypatch):  # noqa: F811
    from fleetledger import commands

    def no_enrol(*a, **kw):
        raise FleetError("ship-05 did not finish provisioning: cloud-init failed.")

    monkeypatch.setattr(commands, "_enrol", no_enrol)
    code, _, err = env.run("new", "ship-05", "--driver", "proxmox")
    assert code == 1 and "ship-05 stays in the inventory as far as it got" in err
    assert attempts.get("ship-05").stage == "enrol"
    assert env.store.load().has_ship("ship-05")  # its VM is real: you choose what to do
    code, _, err = env.run("new", "ship-05", "--driver", "proxmox")
    assert code == 1 and "still in the inventory from the failed attempt" in err
    assert attempts.get("ship-05").stage == "enrol"  # the refusal did not overwrite it
    assert len(env.driver.created) == 1


# --- the form, filled in from an attempt ---------------------------------------------


@pytest.mark.parametrize("argv", [
    ["new", "ship-30"],
    ["new", "ship-30", "--tag", "work", "--tag", "lab", "--cores", "6", "--memory-mb",
     "12288", "--disk-gb", "60", "--signins", "claude,github", "--no-overlay"],
    ["new", "ship-30", "--autostart", "lead:~/work/x:plan", "--repo", "someone/agent-setup",
     "--autostart-prompt", "Get going.", "--host", "ship-30.example.net", "--host",
     "192.0.2.30", "--mode", "plan", "--allow-bypass", "--agent-sudo", "--timezone",
     "Europe/Berlin", "--profile", "orchestrator"],
])
def test_the_form_filled_in_from_an_attempt_makes_the_same_command(env, argv, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TZ", "UTC")
    options, _ = attempts.options_of(cli.build_parser().parse_args(argv))
    inv = env.store.load()
    nf = newship.choices(env.app, inv)
    fields = newship.fields_from(options, nf)
    filled = newship.refill(nf, fields)
    assert filled.name == "ship-30"
    _, again = newship.argv(fields, env.app, inv)
    assert sorted(again) == sorted(argv)


def test_tools_come_back_ticked(env):  # noqa: F811
    inv = env.store.load()
    nf = newship.choices(env.app, inv)
    defaults = [t for t, _, on in nf.tools if on]
    extra = next(t for t, _, on in nf.tools if not on)
    fields = newship.fields_from({"name": "ship-30", "tools": extra}, nf)
    assert set(fields["tool"]) == {*defaults, extra}
    fields = newship.fields_from({"name": "ship-30", "tools": extra, "no_default_tools": True},
                                 nf)
    assert fields["tool"] == [extra]
    _, argv = newship.argv(fields, env.app, inv)
    assert argv[argv.index("--tools") - 1:argv.index("--tools") + 2] == [
        "--no-default-tools", "--tools", extra]


def test_a_saved_attempt_is_listed_until_its_ship_is_enrolled(env, home):  # noqa: F811
    env.driver.fail = True
    with pytest.raises(RuntimeError):
        env.run("new", "ship-05", "--driver", "proxmox")
    assert [a.name for a in attempts.current(env.store.load())] == ["ship-05"]
    attempts.save(attempts.Attempt("ship-01", 5.0, {"name": "ship-01"}, "x"))
    assert [a.name for a in attempts.current(env.store.load())] == ["ship-05"]  # enrolled
    assert attempts.get("ship-01") is None


def test_an_enrolled_ships_attempt_is_kept_while_it_names_a_leftover(env, home):  # noqa: F811
    attempts.save(attempts.Attempt("ship-01", 5.0, {"name": "ship-01"}, "x",
                                   left=[{"kind": "vm", "vmid": 9101, "name": "ship-01"}]))
    [kept] = attempts.current(env.store.load())  # ship-01 is enrolled: adopted, say
    assert kept.name == "ship-01" and kept.left and attempts.get("ship-01") is not None


def test_a_saved_attempt_must_be_under_its_own_name(home):
    path = saved_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"attempts": [
        {"name": "ship-07", "at": 1, "options": {"name": "ship-08"}}]}))
    assert attempts.load() == []


def test_proxmox_cleanup_leaves_alone_a_vmid_that_holds_another_vm():
    class Taken(FakeAPI):
        def wait_task(self, node, upid, timeout=600):
            if upid == "UPID:pve:2":
                raise FleetError("Proxmox task failed: clone failed")

    api = Taken(vm_status="stopped", vm_name="someone-else")
    with pytest.raises(FleetError, match="clone failed") as exc:
        ProxmoxDriver(PX, api=api, read_seed=lambda s: b"ISO").create(ship(vmid=9102), SEED,
                                                                        SIZE)
    assert not isinstance(exc.value, LeftBehind)  # not ours: nothing recorded as left
    assert ("DELETE", "/nodes/pve/qemu/9102") not in [c[:2] for c in mutating(api)]


def test_remove_left_is_refused_by_drivers_that_leave_nothing(env):  # noqa: F811
    from fleetledger.drivers import get_driver

    with pytest.raises(FleetError, match="cannot remove"):
        get_driver(env.store.load(), "manual").remove_left("ship-05", [{"kind": "vm"}])


def test_outcome_messages_still_reach_the_person(env):  # noqa: F811
    """The wrapper keeps `new`'s own output and exit code."""
    code, out, _ = env.run("new", "ship-05", "--driver", "proxmox")
    assert code == 0 and "Started VM 9101." in out
    assert isinstance(Outcome(done=True), Outcome)
