"""Two agents in one folder after a reboot (contracts/ship-ctl.md, "ship-ctl autostart",
`logs resume`, attention agents-share-folder and autostart-folder-taken).

The case: a Ship's [[autostart]] agent `main` (in ~/work) was exited on purpose, so the
agent record keeps it `stopped`, and the person runs their own Claude, `lead`, by hand
in the same folder. At the next boot the record resumes `lead`; autostart used to start
`main` afresh beside it, two agents at work on the same files. Now autostart leaves
`main` unstarted while `lead` works there (folder-taken), says so in the resume log, and
attention names both. With the folder free, `main` starts afresh at a boot as before.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ship_harness import LEDGER

WORK = "/home/agent/work"
TOML = """\
schema = 1

[ship]
name = "ship-01"
id = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
tags = ["personal"]

[claude]
default_mode = "auto"

[[autostart]]
name = "main"
cwd = "~/work"
"""
NO_AUTOSTART = TOML.split("[[autostart]]")[0]
RESUMED_LEAD = "ship-ctl: agent record: lead started again, continuing its session."


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hand_start(ship, name: str, address: str, sid: str, cwd: str = WORK) -> None:
    """A Claude started by hand in its own kept terminal: no ShipShape labels; its
    session is known only from the mode-ledger hook."""
    ship.p(cwd).mkdir(parents=True, exist_ok=True)
    s = ship.scenario()
    s["hc"]["terminals"].append({
        "address": address, "name": name, "argv": ["claude"], "labels": {}, "cwd": cwd,
        "state": "live", "keep": True, "created_ms": 1, "screen": "exact", "cols": 80,
        "rows": 24, "status": {"app": "claude-code", "state": "idle"}, "turn": 0,
        "exit_code": None, "reason": None, "session": sid, "claude_args": [],
        "foreground": {"pid": 77, "name": "claude", "argv": ["claude"], "cwd": cwd}})
    s["hc"]["next"] = max(s["hc"].get("next", 1), int(address[1:]) + 1)  # its own address
    ship.set(hc=s["hc"])
    path = ship.p(LEDGER)
    doc = json.loads(path.read_text()) if path.exists() else {
        "schema": "shipshape.modes/1", "sessions": {}}
    doc["sessions"][sid] = {"mode": "auto", "started_at": now(), "updated_at": now(),
                            "terminal": address}
    path.write_text(json.dumps(doc))


def refresh(ship) -> list[str]:
    """One status refresh: the lines it logs (as ship-status.service's journal has them)."""
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    return [line for line in out.splitlines() if line.startswith("ship-ctl: ")]


def resume_log(ship, lines: list[str]) -> list[str]:
    """`ship-ctl logs resume` over a journal holding exactly these refresh lines."""
    ship.set(journal=[f"2026-01-01T00:00:{i:02d}+0000 ship-01 ship-ctl[100]: {line}"
                      for i, line in enumerate(lines)])
    rc, doc, _ = ship.ctl("logs", "resume", "--json")
    assert rc == 0
    return [line.split("]: ", 1)[1] for line in doc["data"]["lines"]]


def live(ship) -> dict[str, dict]:
    """{name: terminal} for the live terminals Claude runs in."""
    return {t["name"]: t for t in ship.terminals()
            if t["state"] == "live" and (t.get("foreground") or {}).get("name") == "claude"}


def shared(doc: dict) -> list[dict]:
    return [a for a in doc["attention"] if a["code"] == "agents-share-folder"]


def setup(ship, toml: str = TOML, exit_main: bool = True) -> None:
    """First boot (autostart starts main), main exited on purpose, then lead started
    by hand in the same folder and picked up by the record."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.write_toml(toml)
    ship.ctl("apply", "--json")
    refresh(ship)
    if exit_main and "[[autostart]]" in toml:
        ship.exit("main")
        refresh(ship)
    hand_start(ship, "lead", "t2", "sess-lead")
    assert "ship-ctl: agent record: picked up lead (/home/agent/work)" in refresh(ship)


TAKEN = ("ship-ctl: autostart: main not started: 'lead' already works in its folder "
         "(folder-taken)")


def taken(doc: dict) -> list[dict]:
    return [a for a in doc["attention"] if a["code"] == "autostart-folder-taken"]


def test_after_a_reboot_only_lead_comes_back_and_main_waits_for_its_folder(ship):
    setup(ship)
    rec = ship.record()["agents"]
    assert (rec["main"]["want"], rec["main"]["origin"]) == ("stopped", "autostart")
    assert (rec["lead"]["want"], rec["lead"]["origin"]) == ("running", "adopted")
    # Already before the reboot: main would not come back while lead works there.
    assert [a["subject"] for a in taken(ship.status())] == ["main"]
    ship.core_reboot(uptime=35)
    r1 = refresh(ship)
    assert RESUMED_LEAD in r1 and TAKEN in r1
    assert not any("autostart: started" in x for x in r1)
    assert set(live(ship)) == {"lead"}
    assert live(ship)["lead"]["claude_args"][-2:] == ["--resume", "sess-lead"]
    assert len(ship.terminals("main")) == 1  # only the one it was exited in, at first boot
    # The resume log shows the resume and why main did not start.
    assert resume_log(ship, r1) == [TAKEN, RESUMED_LEAD]
    doc = ship.status()
    assert [a["name"] for a in doc["herdr"]["agents"]] == ["lead"]
    assert doc["agent_record"]["resume"]["resumed"] == 1
    assert ship.record()["agents"]["main"]["want"] == "stopped"
    assert shared(doc) == []
    [item] = taken(doc)
    assert item["subject"] == "main"
    assert ("Autostart agent 'main' is not started while 'lead' works in its folder "
            "/home/agent/work") in item["message"]
    assert "autostart.remove=main" in item["message"]
    # Later refreshes start nothing either.
    assert not any("main" in x for x in refresh(ship))
    assert set(live(ship)) == {"lead"}


def test_resume_at_the_first_refresh_and_autostart_at_the_next(ship):
    """Claude's sign-in check does not answer at the first refresh after the boot: the
    record resumes lead then; autostart waits for the sign-in, and at the next refresh
    finds lead running in main's folder."""
    setup(ship)
    ship.core_reboot(uptime=35)
    ship.env["SHIPSHAPE_PROBE_TIMEOUT"] = "1"
    ship.set(claude={**ship.scenario()["claude"], "hang": True})
    r1 = refresh(ship)
    assert RESUMED_LEAD in r1 and not any("autostart" in x for x in r1)
    ship.set(claude={**ship.scenario()["claude"], "hang": False})
    r2 = refresh(ship)
    assert TAKEN in r2 and not any("autostart: started" in x for x in r2)
    assert resume_log(ship, r1 + r2) == [RESUMED_LEAD, TAKEN]
    assert set(live(ship)) == {"lead"}
    assert len(taken(ship.status())) == 1


def test_dns_late_resume_and_autostart_in_one_refresh(ship):
    """DNS answers only at the second refresh: both wait, then one refresh resumes lead
    and, after it, leaves main unstarted."""
    setup(ship)
    ship.core_reboot(uptime=35)
    ship.set(dns=False)
    assert "ship-ctl: agent record: resume waits for DNS" in refresh(ship)
    assert set(live(ship)) == set()
    ship.set(dns=True)
    r2 = refresh(ship)
    assert RESUMED_LEAD in r2 and TAKEN in r2
    assert set(live(ship)) == {"lead"}


def test_ship_ctl_autostart_says_the_folder_is_taken(ship):
    setup(ship)
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 0, doc
    assert doc["data"] == {"started": [], "skipped": [
        {"name": "main", "reason": "folder-taken", "agent": "lead", "agent_running": True}]}
    assert doc["messages"] == ["main not started: 'lead' already works in /home/agent/work "
                               "(folder-taken)."]


def test_once_lead_has_left_main_starts_afresh_at_the_next_boot(ship):
    """An [[autostart]] agent exited on purpose still comes back afresh at a boot when
    its folder is free."""
    setup(ship)
    ship.exit("lead")
    assert ("ship-ctl: agent record: lead stopped (it was exited (prompt_input_exit)); "
            "not started again.") in refresh(ship)
    assert taken(ship.status()) == []
    ship.core_reboot(uptime=35)
    assert "ship-ctl: autostart: started main" in refresh(ship)
    assert set(live(ship)) == {"main"}


def test_without_another_agent_in_its_folder_main_starts_afresh(ship):
    """Counterfactual: main exited, nobody else in ~/work: autostart starts it afresh at
    the next boot, as it always has, and nothing is shared."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.write_toml(TOML)
    ship.ctl("apply", "--json")
    refresh(ship)
    ship.exit("main")
    refresh(ship)
    ship.core_reboot(uptime=35)
    r1 = refresh(ship)
    assert "ship-ctl: autostart: started main" in r1
    assert not any("started again" in x for x in r1)
    assert set(live(ship)) == {"main"}
    assert shared(ship.status()) == []


def test_main_never_exited_is_resumed_not_started_afresh(ship):
    """Counterfactual: main still ran at the reboot: the record resumes both, nothing
    starts afresh. Two agents in one folder all the same, so attention says so."""
    setup(ship, exit_main=False)
    ship.core_reboot(uptime=35)
    r1 = refresh(ship)
    assert RESUMED_LEAD in r1
    assert "ship-ctl: agent record: main started again, continuing its session." in r1
    assert not any("autostart: started" in x for x in r1)
    doc = ship.status()
    assert doc["agent_record"]["resume"]["resumed"] == 2
    assert len(shared(doc)) == 1


def test_without_the_autostart_entry_only_lead_comes_back(ship):
    setup(ship, toml=NO_AUTOSTART)
    ship.core_reboot(uptime=35)
    assert RESUMED_LEAD in refresh(ship)
    refresh(ship)
    assert set(live(ship)) == {"lead"}
    assert shared(ship.status()) == []


def test_two_autostart_entries_for_one_folder_are_meant(ship):
    """Two [[autostart]] entries configured for the same folder are the owner's own
    choice: no advice. A third Claude started there by hand is."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.write_toml(TOML + '\n[[autostart]]\nname = "second"\ncwd = "~/work"\n')
    ship.ctl("apply", "--json")
    refresh(ship)
    assert set(live(ship)) == {"main", "second"}
    assert shared(ship.status()) == []
    hand_start(ship, "lead", "t9", "sess-lead")
    refresh(ship)
    [item] = shared(ship.status())
    assert "3 Claude agents work in /home/agent/work" in item["message"]


def test_two_hand_started_agents_in_one_folder(ship):
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.write_toml(NO_AUTOSTART)
    ship.ctl("apply", "--json")
    hand_start(ship, "one", "t1", "sess-one", cwd=f"{WORK}/app")
    hand_start(ship, "two", "t2", "sess-two", cwd=f"{WORK}/app")
    hand_start(ship, "three", "t3", "sess-three", cwd=f"{WORK}/other")
    refresh(ship)
    [item] = shared(ship.status())
    assert item["subject"] == f"{WORK}/app"
    assert "'one', 'two'" in item["message"] and "three" not in item["message"]
    assert "lose its entry" not in item["message"]  # neither is an [[autostart]] agent


def test_the_resume_log_keeps_autostart_and_record_lines_only(ship):
    lines = resume_log(ship, ["ship-ctl: state: ready",
                              "ship-ctl: autostart: started main",
                              "ship-ctl: signed in: github; finishing gh",
                              "ship-ctl: agent record: lead started again, continuing its "
                              "session.",
                              "ship-ctl: autostart: the herdr server is not answering"])
    assert lines == ["ship-ctl: autostart: started main", RESUMED_LEAD,
                     "ship-ctl: autostart: the herdr server is not answering"]


def test_main_starts_once_the_record_gives_up_on_lead(ship):
    """lead's session no longer loads: it dies at each start. While the record still
    tries it, it takes the folder; once it gives up on it (agent-relaunch-failed), the
    folder is free and main starts afresh in that boot, at the next refresh."""
    setup(ship)
    ship.core_reboot(uptime=35)
    s = ship.scenario()
    s["hc"]["lost_sessions"] = ["sess-lead"]
    ship.set(hc=s["hc"])
    lines: list[str] = []
    for _ in range(6):
        lines += refresh(ship)
        if "main" in live(ship):
            break
    assert any("lead stopped again soon after it started" in x
               or "lead did not start again" in x for x in lines)
    assert not any("folder-taken" in x for x in lines)  # lead never ran: no final skip
    assert "ship-ctl: autostart: started main" in lines
    assert set(live(ship)) == {"main"}
    doc = ship.status()
    assert "agent-relaunch-failed" in {a["code"] for a in doc["attention"]}
    assert taken(doc) == []
