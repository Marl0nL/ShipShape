import io
import subprocess

import pytest
from fleetledger.concierge.terminal import TerminalPrompter
from fleetledger.errors import FleetError

from fleetledger import scan


def test_prompter_shows_link_and_reads_code_hidden(monkeypatch):
    stream = io.StringIO()
    asked = []
    p = TerminalPrompter("ship-01", "GitHub", stream=stream,
                         read_secret=lambda prompt: asked.append(prompt) or " CODE ")
    p.show_link("https://github.com/login/device", "ABCD-1234", 900)
    text = stream.getvalue()
    assert "GitHub sign-in for ship-01" in text
    assert "Open https://github.com/login/device" in text
    assert "Enter the code ABCD-1234" in text and "about 15 minutes" in text
    assert p.ask_code() == " CODE " and "hidden" in asked[0]
    p.waiting(30)  # not a terminal: no countdown noise
    assert stream.getvalue() == text


def test_prompter_cancel_and_no_live_without_terminal(monkeypatch):
    def interrupted(prompt):
        raise KeyboardInterrupt

    stream = io.StringIO()
    p = TerminalPrompter("ship-01", "Claude Code", stream=stream, read_secret=interrupted)
    assert p.ask_code() is None
    monkeypatch.setattr("sys.stdin", io.StringIO())
    assert p.offer_live("No sign-in link appeared.", "some [redacted] output") is False
    assert "some [redacted] output" in stream.getvalue()


def test_keyscan_picks_the_ed25519_key(monkeypatch):
    out = ("# ship-01.local:22 SSH-2.0-OpenSSH_9.6\n"
           "ship-01.local ssh-rsa AAAArsa\n"
           "ship-01.local ssh-ed25519 AAAAed25519\n")
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert scan.keyscan("ship-01.local") == "ssh-ed25519 AAAAed25519"
    assert seen[0][:3] == ["ssh-keyscan", "-t", "ed25519"]
    assert seen[0][-2:] == ["--", "ship-01.local"]
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""))
    with pytest.raises(FleetError, match="no ed25519 host key"):
        scan.keyscan("ship-01.local")


def test_announcement_names():
    a = scan.Announcement("ship-01", "ship-01.local", 22, (), {"name": "ship-01", "id": "x"})
    assert a.name == "ship-01" and a.ship_id == "x"
    assert scan.Announcement("ship-02", "h", 22).name == "ship-02"
