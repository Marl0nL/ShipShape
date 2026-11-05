"""ship-fleet start and stop (D5): the VM through its driver; the manual driver's steps."""

from __future__ import annotations

from test_cli import env  # noqa: F401  (a fixture: the CLI's store, fake driver and Ship)


def test_start_and_stop_go_through_the_driver(env):  # noqa: F811
    assert env.run("new", "ship-07", "--driver", "proxmox", "--no-wait")[0] == 0
    code, out, err = env.run("stop", "ship-07")
    assert code == 0, err
    assert "Shut down ship-07." in out and "ship-fleet start ship-07" in err
    code, out, err = env.run("start", "ship-07")
    assert code == 0, err
    assert "Started ship-07." in out and "status ship-07 --wait" in err
    assert env.driver.power == [("stop", "ship-07"), ("start", "ship-07")]


def test_the_manual_driver_prints_the_steps(env):  # noqa: F811
    assert env.store.load().ship("ship-01").driver == "manual"
    code, out, _ = env.run("stop", "ship-01")
    assert code == 0 and "Stop ship-01's VM by hand:" in out
    code, out, _ = env.run("start", "ship-01")
    assert code == 0 and "Start ship-01's VM by hand:" in out
    assert not getattr(env.driver, "power", [])


def test_an_unknown_ship_is_refused(env):  # noqa: F811
    code, _, err = env.run("stop", "ship-99")
    assert code != 0 and "ship-99" in err
    assert not getattr(env.driver, "power", [])
