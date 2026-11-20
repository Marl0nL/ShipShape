"""The disruptive verbs that ask before they act: stop, logout, overlay leave, signins
turning one off, resize --restart and set ssh_listen=overlay (agent restart --force and
--fresh: test_ops.py). Each asks for yes in a terminal and names the Ship; where nobody
can be asked it does nothing unless --yes, as snapshot rollback does; what is not
disruptive asks nothing."""

from __future__ import annotations

import pytest
from fleetledger.drivers import Outcome
from test_cli import env, with_overlay  # noqa: F401  (a fixture: the CLI's store, fake driver and Ship)


def at_terminal(env, answer):  # noqa: F811
    """A person at a terminal who types answer; the questions asked are kept."""
    asked = []
    env.app.stdin_isatty = lambda: True
    env.app.confirm = lambda prompt: asked.append(prompt) or answer
    return asked


def nobody_asked(env):  # noqa: F811
    env.app.confirm = lambda prompt: pytest.fail(f"asked {prompt!r}")


def ship_calls(env, name="ship-01"):  # noqa: F811
    return [c for n, c in env.ship.calls if n == name]


# --- stop ---------------------------------------------------------------------


def test_stop_asks_in_a_terminal(env):  # noqa: F811
    assert env.run("new", "ship-07", "--driver", "proxmox", "--no-wait")[0] == 0
    asked = at_terminal(env, "no")
    code, _, err = env.run("stop", "ship-07")
    assert code == 1 and "Not stopped: ship-07 keeps running." in err
    assert "ship-07's VM" in asked[0] and "ship-fleet start ship-07" in asked[0]
    assert asked[0].endswith("Type yes to go on: ")
    assert not getattr(env.driver, "power", [])
    asked = at_terminal(env, "YES")  # a word: any case
    assert env.run("stop", "ship-07")[0] == 0 and len(asked) == 1
    assert env.driver.power == [("stop", "ship-07")]


def test_stop_without_a_terminal_needs_yes(env):  # noqa: F811
    assert env.run("new", "ship-07", "--driver", "proxmox", "--no-wait")[0] == 0
    nobody_asked(env)
    code, _, err = env.run("stop", "ship-07")
    assert code == 1 and "Nothing done." in err and "--yes" in err and "ship-07" in err
    assert not getattr(env.driver, "power", [])
    assert env.run("stop", "ship-07", "--yes")[0] == 0
    assert env.driver.power == [("stop", "ship-07")]


def test_start_asks_nothing(env):  # noqa: F811
    assert env.run("new", "ship-07", "--driver", "proxmox", "--no-wait")[0] == 0
    nobody_asked(env)
    assert env.run("start", "ship-07")[0] == 0


def test_an_unknown_ship_is_refused_before_anything_is_asked(env):  # noqa: F811
    at_terminal(env, "yes")
    nobody_asked(env)
    code, _, err = env.run("stop", "ship-99")
    assert code != 0 and "ship-99" in err


# --- logout ---------------------------------------------------------------------


def test_logout_asks_and_names_the_service(env):  # noqa: F811
    asked = at_terminal(env, "")
    code, _, err = env.run("logout", "ship-01", "github")
    assert code == 1 and "Not signed out" in err
    assert "Signing ship-01 out of GitHub" in asked[0]
    assert not any("gh auth logout" in c[-1] for c in ship_calls(env))
    at_terminal(env, "yes")
    code, out, _ = env.run("logout", "ship-01", "github")
    assert code == 0 and "signed out of GitHub" in out


def test_logout_without_a_terminal_needs_yes(env):  # noqa: F811
    nobody_asked(env)
    code, _, err = env.run("logout", "ship-01", "all")
    assert code == 1 and "--yes" in err
    assert not any("logout" in c[-1] or "revoke" in c[-1] for c in ship_calls(env))


def test_logout_refuses_an_unknown_service_before_asking(env):  # noqa: F811
    at_terminal(env, "yes")
    nobody_asked(env)
    code, _, err = env.run("logout", "ship-01", "agy")
    assert code != 0 and "Unknown sign-in service" in err


# --- signins: turning one off --------------------------------------------------


def test_turning_a_sign_in_off_asks(env):  # noqa: F811
    asked = at_terminal(env, "no")
    code, _, err = env.run("signins", "ship-01", "--off", "gcloud")
    assert code == 1 and "Nothing changed: ship-01 still uses Google Cloud." in err
    assert "Turning Google Cloud off on ship-01 signs ship-01 out" in asked[0]
    assert env.store.load().ship("ship-01").signins is None  # all, as before
    assert not any("gcloud auth revoke" in c[-1] for c in ship_calls(env))
    at_terminal(env, "yes")
    assert env.run("signins", "ship-01", "--off", "gcloud")[0] == 0
    assert env.store.load().ship("ship-01").signins == ("claude", "github")


def test_an_only_that_drops_one_in_use_asks_and_names_each(env):  # noqa: F811
    nobody_asked(env)
    code, _, err = env.run("signins", "ship-01", "--only", "claude")
    assert code == 1 and "--yes" in err and "Google Cloud, GitHub" in err
    assert env.store.load().ship("ship-01").signins is None
    asked = at_terminal(env, "yes")
    assert env.run("signins", "ship-01", "--only", "claude")[0] == 0
    assert "Google Cloud, GitHub off on ship-01" in asked[0] and "them" in asked[0]
    assert env.store.load().ship("ship-01").signins == ("claude",)


def test_turning_on_or_keeping_every_one_asks_nothing(env):  # noqa: F811
    assert env.run("signins", "ship-01", "--off", "gcloud", "--yes")[0] == 0
    nobody_asked(env)
    assert env.run("signins", "ship-01", "--on", "gcloud")[0] == 0
    assert env.run("signins", "ship-01", "--only", "claude,gcloud,github")[0] == 0
    assert env.run("signins", "ship-01", "--off", "gcloud", "--yes")[0] == 0
    assert env.run("signins", "ship-01", "--off", "gcloud")[0] == 0  # already off


# --- overlay leave ---------------------------------------------------------------


def test_overlay_leave_asks(env):  # noqa: F811
    with_overlay(env)
    env.run("overlay", "join", "ship-01")
    names = env.store.load().ship("ship-01").names
    asked = at_terminal(env, "no")
    code, _, err = env.run("overlay", "leave", "ship-01")
    assert code == 1 and "Nothing left: ship-01 stays on the overlay." in err
    assert asked[0].startswith("ship-01 leaves the overlay: from then on only the LAN")
    assert env.store.load().ship("ship-01").names == names
    assert not any(c[3:5] == ["overlay", "leave"] for c in ship_calls(env))
    env.app.stdin_isatty = lambda: False
    nobody_asked(env)
    code, _, err = env.run("overlay", "leave", "ship-01")
    assert code == 1 and "--yes" in err
    assert env.run("overlay", "leave", "ship-01", "--yes")[0] == 0
    assert env.store.load().ship("ship-01").names == ("ship-01.local",)


def test_overlay_leave_on_several_ships_asks_once(env):  # noqa: F811
    with_overlay(env)
    asked = at_terminal(env, "no")
    assert env.run("overlay", "leave", "ship-01", "ship-02")[0] == 1
    assert len(asked) == 1 and asked[0].startswith("ship-01, ship-02 leave the overlay")
    assert "reaches them" in asked[0]


# --- resize --restart ---------------------------------------------------------------


def resizable(env):  # noqa: F811
    sized, rebooted = [], []
    env.driver.resize = lambda ship, size: sized.append(size) or Outcome(done=True)
    env.driver.reboot = lambda ship: rebooted.append(ship.name) or Outcome(done=True)
    assert env.run("new", "ship-16", "--driver", "proxmox")[0] == 0
    return sized, rebooted


def test_resize_restart_asks(env):  # noqa: F811
    sized, rebooted = resizable(env)
    asked = at_terminal(env, "no")
    code, _, err = env.run("resize", "ship-16", "--cores", "8", "--restart")
    assert code == 1 and "Not resized" in err and "Without --restart" in err
    assert "restarts its VM through the hypervisor now" in asked[0] and "ship-16" in asked[0]
    assert sized == [] and rebooted == []
    assert env.store.load().ship("ship-16").size.get("cores") != 8
    at_terminal(env, "yes")
    assert env.run("resize", "ship-16", "--cores", "8", "--restart")[0] == 0
    assert sized == [{"cores": 8}] and rebooted == ["ship-16"]


def test_resize_restart_without_a_terminal_needs_yes(env):  # noqa: F811
    sized, rebooted = resizable(env)
    nobody_asked(env)
    code, _, err = env.run("resize", "ship-16", "--memory-mb", "16384", "--restart")
    assert code == 1 and "--yes" in err and sized == [] and rebooted == []


def test_resize_without_restart_and_a_bad_size_ask_nothing(env):  # noqa: F811
    sized, rebooted = resizable(env)
    at_terminal(env, "yes")
    nobody_asked(env)
    assert env.run("resize", "ship-16", "--cores", "8")[0] == 0
    assert env.run("resize", "ship-16", "--cores", "0", "--restart")[0] == 1
    assert env.run("resize", "ship-16", "--restart")[0] == 1
    assert env.run("resize", "ship-01", "--disk-gb", "80", "--record-only", "--restart")[0] == 0
    assert sized == [{"cores": 8}] and rebooted == []


# --- set ssh_listen=overlay -----------------------------------------------------------


def test_set_ssh_listen_overlay_asks(env):  # noqa: F811
    asked = at_terminal(env, "no")
    code, _, err = env.run("set", "ship-01", "ssh_listen=overlay")
    assert code == 1 and "Nothing changed on ship-01." in err
    assert "ssh_listen=overlay makes ship-01 refuse SSH logins from the LAN" in asked[0]
    assert not any("network.ssh_listen=overlay" in c for c in ship_calls(env))
    at_terminal(env, "yes")
    assert env.run("set", "ship-01", "network.ssh_listen=overlay")[0] == 0  # full name too
    assert any("network.ssh_listen=overlay" in c for c in ship_calls(env))


def test_set_ssh_listen_overlay_without_a_terminal_needs_yes(env):  # noqa: F811
    nobody_asked(env)
    code, _, err = env.run("set", "--tag", "work", "lan_guard=true", "ssh_listen=overlay")
    assert code == 1 and "--yes" in err and "ship-01" in err
    assert not env.ship.calls


def test_set_asks_nothing_for_anything_else(env):  # noqa: F811
    nobody_asked(env)
    assert env.run("set", "ship-01", "ssh_listen=any", "lan_guard=true")[0] == 0
    assert env.run("set", "ship-01", "ssh_listen=lan")[0] != 0  # a bad value: refused first
