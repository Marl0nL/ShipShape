"""ship-ctl wake: one line to the Ship's primary agent, once per boot, some minutes
after it (contracts/settings.md, "Waking the primary agent after a boot").

The sandbox Ship's ship.toml has two [[autostart]] agents, main and grinder; with
none marked, main (the first) is the primary. Tests set the uptime to just past the
delay, as when ship-wake.timer fires.
"""

from __future__ import annotations

import pytest
from ship_harness import assert_valid

DROP_IN = "/etc/systemd/system/ship-wake.timer.d/50-shipshape.conf"
WAKE = "/var/lib/shipshape/wake.json"
LINE = ("Your Ship rebooted 10 minutes ago. Make sure you've resumed any background "
        "processes or work that you should be doing, and review whether subagents or "
        "crews may have been interrupted by the reboot too.")


def prompts(ship) -> list[list[str]]:
    return ship.scenario()["herdr"].get("prompts", [])


def uptime(ship, seconds: float) -> None:
    ship.p("/proc/uptime").write_text(f"{seconds} 1.0\n")


def wake(ship) -> dict:
    rc, doc, out = ship.ctl("wake", "--json")
    assert rc == 0, out
    assert_valid("ship-ctl-result", doc)
    assert doc["command"] == "wake"
    return doc["data"]


def wake_lines(ship) -> list[str]:
    return [e["message"] for e in ship.scenario().get("logged", [])
            if e["message"].startswith("wake: ")]


def set_state(ship, name: str, state: str) -> None:
    s = ship.scenario()
    for a in s["herdr"]["agents"]:
        if a.get("name") == name:
            a["agent_status"] = state
    ship.set(herdr=s["herdr"])


@pytest.fixture
def booted(ship):
    """main and grinder started by the first refresh of the boot (which records the
    boot's resume), autostart's first prompts forgotten, 10 minutes into the boot."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    s = ship.scenario()
    assert {a["name"] for a in s["herdr"]["agents"]} == {"main", "grinder"}
    s["herdr"]["prompts"] = []
    ship.set(herdr=s["herdr"])
    uptime(ship, 605)
    return ship


def turn_on(ship, *pairs: str) -> dict:
    rc, doc, out = ship.ctl("set", "wake.after_boot=true", *pairs, "--json")
    assert rc == 0, out
    return doc


# --- off by default ---------------------------------------------------------------------
def test_off_by_default_sends_nothing(booted):
    ship = booted
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    assert "ship-wake.timer" not in ship.scenario().get("enabled_units", [])
    assert not ship.p(DROP_IN).exists()
    assert wake(ship)["outcome"] == "off"
    assert prompts(ship) == []
    assert not ship.p(WAKE).exists()  # turned on later this boot, it can still run


# --- on ---------------------------------------------------------------------------------
def test_on_enables_the_timer_with_the_delay(booted):
    ship = booted
    doc = turn_on(ship)
    assert "ship-wake.timer (enabled)" in doc["changed"]
    assert any("from the next boot" in m for m in doc["messages"])
    assert "ship-wake.timer" in ship.scenario()["enabled_units"]
    assert "OnBootSec=10min" in ship.p(DROP_IN).read_text()
    assert ["systemctl", "daemon-reload"] in ship.calls("systemctl")
    rc, doc, _ = ship.ctl("set", "wake.delay_minutes=5", "--json")
    assert rc == 0
    assert "OnBootSec=5min" in ship.p(DROP_IN).read_text()
    rc, doc, _ = ship.ctl("set", "wake.after_boot=false", "--json")
    assert "ship-wake.timer (disabled)" in doc["changed"]
    assert "ship-wake.timer" not in ship.scenario()["enabled_units"]
    assert not ship.p(DROP_IN).exists()


def test_on_sends_once_to_the_primary(booted):
    ship = booted
    turn_on(ship)
    data = wake(ship)
    assert data == {"outcome": "sent", "agent": "main", "tries": 1, "reason": None}
    assert prompts(ship) == [["main", LINE]]
    assert wake_lines(ship) == ["wake: sent to the primary agent main."]
    assert ship.json(WAKE)["outcome"] == "sent"


def test_a_second_run_in_the_same_boot_sends_nothing(booted):
    """A restart of the unit, a run by hand, or the status refresh never resend."""
    ship = booted
    turn_on(ship)
    wake(ship)
    assert wake(ship)["outcome"] == "already-done"
    ship.ctl("refresh")
    assert wake(ship)["outcome"] == "already-done"
    assert prompts(ship) == [["main", LINE]]


def test_the_next_boot_sends_again(booted):
    ship = booted
    turn_on(ship)
    wake(ship)
    ship.reboot(uptime=40)
    rc, _, out = ship.ctl("refresh")  # the boot's resume
    assert rc == 0, out
    uptime(ship, 610)
    assert wake(ship)["outcome"] == "sent"
    assert [p[0] for p in prompts(ship)] == ["main", "main"]


def test_delay_and_text_are_set_and_rendered(booted):
    ship = booted
    uptime(ship, 125)
    turn_on(ship, "wake.delay_minutes=2", "wake.text=Back up {delay} after a reboot: check.")
    status = ship.ctl("status", "--json")[1]
    assert status["settings"]["wake"] == {"after_boot": True, "delay_minutes": 2,
                                          "text": "Back up {delay} after a reboot: check."}
    assert wake(ship)["outcome"] == "sent"
    assert prompts(ship) == [["main", "Back up 2 minutes after a reboot: check."]]


def test_a_text_with_a_line_break_is_refused(booted):
    ship = booted
    rc, doc, _ = ship.ctl("set", "wake.text=first\nsecond", "--json")
    assert rc == 4 and doc["error"]["code"] == "invalid-ship-toml"
    rc, doc, _ = ship.ctl("set", "wake.delay_minutes=soon", "--json")
    assert rc != 0 and not doc["ok"]


def test_before_the_delay_or_long_after_nothing_is_sent(booted):
    ship = booted
    turn_on(ship)
    uptime(ship, 120)
    assert wake(ship)["outcome"] == "too-early"
    assert not ship.p(WAKE).exists()
    uptime(ship, 4000)  # the timer was enabled or moved late in the boot
    assert wake(ship)["outcome"] == "late"
    assert prompts(ship) == []
    assert wake(ship)["outcome"] == "already-done"


# --- the primary agent ------------------------------------------------------------------
def test_a_marked_primary_gets_it(booted):
    ship = booted
    rc, doc, out = ship.ctl("agent", "primary", "grinder", "--json")
    assert rc == 0, out
    assert_valid("ship-ctl-result", doc)
    assert doc["data"] == {"primary": "grinder", "marked": True}
    assert ship.record()["primary"] == "grinder"
    assert_valid("agent-record", ship.record())
    status = ship.ctl("status", "--json")[1]
    assert_valid("ship-status", status)
    assert status["agent_record"]["primary"] == {"name": "grinder", "marked": True}
    turn_on(ship)
    assert wake(ship)["agent"] == "grinder"
    assert [p[0] for p in prompts(ship)] == ["grinder"]


def test_only_one_primary_and_clear(booted):
    ship = booted
    ship.ctl("agent", "primary", "grinder", "--json")
    ship.ctl("agent", "primary", "main", "--json")
    assert ship.record()["primary"] == "main"
    rc, doc, _ = ship.ctl("agent", "primary", "main", "grinder", "--json")
    assert rc == 2 and doc["error"]["code"] == "usage"
    rc, doc, _ = ship.ctl("agent", "primary", "nobody", "--json")
    assert rc == 1 and doc["error"]["code"] == "invalid-input"
    rc, doc, _ = ship.ctl("agent", "primary", "--clear", "--json")
    assert rc == 0
    assert doc["data"] == {"primary": "main", "marked": False}  # the default
    assert ship.record()["primary"] is None


def test_without_a_primary_it_logs_and_sends_nothing(ship):
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    rc, _, out = ship.ctl("set", "autostart.remove=main", "autostart.remove=grinder",
                          "wake.after_boot=true", "--json")
    assert rc == 0, out
    ship.ctl("refresh")  # this boot's resume: nothing to resume
    uptime(ship, 605)
    data = wake(ship)
    assert data["outcome"] == "no-primary" and data["agent"] is None
    assert prompts(ship) == []
    assert len(wake_lines(ship)) == 1 and "no primary agent" in wake_lines(ship)[0]
    assert ship.ctl("status", "--json")[1]["agent_record"]["primary"] is None


# --- notice rules -----------------------------------------------------------------------
def test_a_blocked_primary_is_retried_then_given_up(booted):
    """Text and Enter could answer its permission prompt: never typed into."""
    ship = booted
    turn_on(ship)
    set_state(ship, "main", "blocked")
    data = wake(ship)
    assert data == {"outcome": "gave-up", "agent": "main", "tries": 6,
                    "reason": "waiting-for-permission"}
    assert prompts(ship) == []
    assert wake_lines(ship) == ["wake: not sent to the primary agent main after 6 tries "
                                "(waiting-for-permission); gave up."]
    set_state(ship, "main", "idle")
    assert wake(ship)["outcome"] == "already-done"  # given up for this boot
    assert prompts(ship) == []


def test_an_agent_in_an_unknown_state_or_not_running_is_not_typed_into(booted):
    ship = booted
    turn_on(ship)
    set_state(ship, "main", "unknown")
    assert wake(ship)["reason"] == "not-ready"
    ship.reboot(uptime=40)
    ship.ctl("refresh")
    ship.crash("main")
    s = ship.scenario()
    s["herdr"]["fail_start"] = ["main"]  # it is not back by the time the wake runs
    ship.set(herdr=s["herdr"])
    uptime(ship, 605)
    data = wake(ship)
    assert data["outcome"] == "gave-up"
    assert data["reason"] == "not-running"
    assert prompts(ship) == []


def test_it_waits_for_this_boots_resume(booted):
    ship = booted
    turn_on(ship, "wake.delay_minutes=1")
    ship.reboot(uptime=65)  # no refresh yet: the resume is not done
    data = wake(ship)
    assert data["outcome"] == "gave-up" and data["reason"] == "resume-pending"
    assert prompts(ship) == []
