import io
import subprocess

import pytest
from fleetledger.concierge.terminal import TerminalPrompter, hyperlink, received
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
    assert "Open this URL" in text and "\nhttps://github.com/login/device\n" in text
    assert "Enter the code ABCD-1234" in text and "about 15 minutes" in text
    p.waiting(30)  # not a terminal: no countdown noise
    assert stream.getvalue() == text
    assert p.ask_code() == " CODE " and "hidden" in asked[0]
    assert stream.getvalue() == text + "  Received 4 characters.\n"  # never the code


def test_prompter_cancel_and_no_live_without_terminal(monkeypatch):
    def interrupted(prompt):
        raise KeyboardInterrupt

    stream = io.StringIO()
    p = TerminalPrompter("ship-01", "Claude Code", stream=stream, read_secret=interrupted)
    assert p.ask_code() is None
    monkeypatch.setattr("sys.stdin", io.StringIO())
    assert p.offer_live("No sign-in link appeared.", "some [redacted] output") is False
    assert "some [redacted] output" in stream.getvalue()


class Tty(io.StringIO):
    def isatty(self):
        return True


URL = "https://accounts.example.com/o/oauth2/auth?client_id=example&state=example-state"


def test_the_link_is_a_hyperlink_on_a_line_of_its_own_in_a_terminal():
    stream = Tty()
    TerminalPrompter("ship-01", "Claude Code", stream=stream).show_link(URL, None, 600)
    lines = stream.getvalue().splitlines()
    at = lines.index(f"\x1b]8;;{URL}\x1b\\{URL}\x1b]8;;\x1b\\")
    assert "Open this URL" in lines[at - 1]
    assert "paste it below" in lines[at + 1]


def test_the_link_is_plain_text_when_not_a_terminal():
    stream = io.StringIO()
    TerminalPrompter("ship-01", "Claude Code", stream=stream).show_link(URL, None, 600)
    assert URL in stream.getvalue().splitlines() and "\x1b" not in stream.getvalue()


@pytest.mark.parametrize("url", ["https://example.com/a b", "https://example.com/\x07x",
                                 "https://example.com/caf\u00e9"])
def test_only_a_plain_url_becomes_a_hyperlink(url):
    assert hyperlink(url) == url


def test_a_one_time_code_is_shown_as_it_is_pasted_and_confirmed():
    stream = io.StringIO()
    asked = []
    p = TerminalPrompter("ship-01", "Claude Code", stream=stream,
                         read_secret=lambda prompt: pytest.fail("read without echo"),
                         read_code=lambda prompt: asked.append(prompt) or " 4/0EXAMPLE-code ")
    assert p.ask_one_time_code() == "4/0EXAMPLE-code"
    out = stream.getvalue()
    assert "Paste the code here and press Enter" in out and asked == ["  > "]
    assert "  Received 4/0EXAMPLE-code (15 characters).\n" in out


def test_a_long_code_is_confirmed_by_its_ends():
    code = "EXAMPLE-CODE-0123456789abcdef0123456789abcdef#example-state"
    assert received(code) == f"  Received EXAMPLE-CODE-012…xample-state ({len(code)} characters)."
    stream = io.StringIO()
    p = TerminalPrompter("ship-01", "Claude Code", stream=stream,
                         read_code=lambda prompt: "\x1b[200~ABCD-1234\x1b[201~")
    assert p.ask_one_time_code() == "ABCD-1234"  # bracketed-paste marks are dropped
    assert "Received ABCD-1234 (9 characters)" in stream.getvalue()


def test_the_first_ctrl_c_at_the_paste_prompt_asks_for_a_second():
    answers = iter([KeyboardInterrupt, "EXAMPLE-CODE"])

    def read(prompt):
        answer = next(answers)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        return answer

    stream = io.StringIO()
    p = TerminalPrompter("ship-01", "Claude Code", stream=stream, read_code=read)
    assert p.ask_one_time_code() == "EXAMPLE-CODE"
    assert "Press Ctrl+C again to cancel" in stream.getvalue()

    def interrupted(prompt):
        raise KeyboardInterrupt

    twice = TerminalPrompter("ship-01", "Claude Code", stream=io.StringIO(),
                             read_code=interrupted)
    assert twice.ask_one_time_code() is None  # the second one cancels
    eof = TerminalPrompter("ship-01", "Claude Code", stream=io.StringIO(),
                           read_code=lambda prompt: (_ for _ in ()).throw(EOFError))
    assert eof.ask_one_time_code() is None  # Ctrl+D cancels at once


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
