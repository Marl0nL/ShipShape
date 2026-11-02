"""Login concierge (W9): pattern tests in a real terminal, account checks, audit."""

import json
import stat
import sys
from pathlib import Path

import pytest
from fleetledger.concierge import audit, engine, providers
from fleetledger.concierge.engine import Flow
from fleetledger.concierge.redact import clean, redact

from fleetledger import concierge

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_cli.py"
FIX = HERE / "fixtures" / "concierge"
CLAUDE_CODE = "EXAMPLE-CODE-0123456789abcdef#example-state"
GCLOUD_CODE = "4/0EXAMPLE-verification-code"


class Prompter:
    def __init__(self, code=None, live=False):
        self.code = code
        self.live = live
        self.shown = []
        self.ticks = 0
        self.offers = []

    def show_link(self, url, code, expires_in):
        self.shown.append((url, code, expires_in))

    def ask_code(self):
        return self.code

    def waiting(self, remaining):
        self.ticks += 1

    def offer_live(self, reason, seen):
        self.offers.append((reason, seen))
        return self.live


def play(fixture, monkeypatch, tmp_path):
    log = tmp_path / "typed.log"
    monkeypatch.setenv("FAKE_CLI_INPUT_LOG", str(log))
    return engine.spawn([sys.executable, str(FAKE), str(FIX / fixture)]), log


def provider(name):
    return providers.load_all()[name]


def test_providers_load():
    loaded = providers.load_all()
    assert set(loaded) == {"claude", "gcloud", "gcloud-adc", "github"}
    assert loaded["gcloud"].services == ["gcloud", "gcloud_adc"]
    assert loaded["github"].mode == "device-code"
    assert [p.id for p in providers.resolve("all")] == ["claude", "gcloud", "github"]
    with pytest.raises(Exception, match="Unknown sign-in service: agy"):
        providers.resolve(["agy"])


def test_provider_schema_rejects_bad_files(tmp_path):
    bad = tmp_path / "x.toml"
    bad.write_text('id = "x"\nlabel = "X"\nmode = "paste-back"\nrun = "x"\ntimeout = 60\n'
                   '[match]\nurl = "u"\nsuccess = "s"\n[[check]]\nservice = "x"\nrun = "x"\n'
                   '[logout]\nrun = "x"\n')
    with pytest.raises(Exception, match="prompt"):
        providers.parse(bad)


def test_claude_paste_back(monkeypatch, tmp_path):
    child, typed = play("claude.txt", monkeypatch, tmp_path)
    prompter = Prompter(code=f"  {CLAUDE_CODE}\n")
    result = Flow(provider("claude"), child, prompter).run()
    assert result.outcome == "completed", result
    (url, code, expires_in) = prompter.shown[0]
    assert url.startswith("https://claude.ai/oauth/authorize?code=true&client_id=")
    assert url.endswith("state=example-state")  # whole link, no escape codes
    assert code is None and 590 < expires_in <= 600
    assert typed.read_text() == CLAUDE_CODE + "\n"  # typed into the terminal, trimmed
    assert CLAUDE_CODE not in result.detail


def test_gcloud_paste_back(monkeypatch, tmp_path):
    child, typed = play("gcloud.txt", monkeypatch, tmp_path)
    prompter = Prompter(code=GCLOUD_CODE)
    result = Flow(provider("gcloud"), child, prompter).run()
    assert result.outcome == "completed"
    assert prompter.shown[0][0].startswith("https://accounts.google.com/o/oauth2/auth?")
    assert prompter.shown[0][0].endswith("prompt=consent")
    assert typed.read_text() == GCLOUD_CODE + "\n"


def test_github_device_code(monkeypatch, tmp_path):
    child, typed = play("github.txt", monkeypatch, tmp_path)
    prompter = Prompter()
    result = Flow(provider("github"), child, prompter).run()
    assert result.outcome == "completed"
    assert prompter.shown == [
        ("https://github.com/login/device", "ABCD-1234", pytest.approx(900, abs=5))
    ]
    assert typed.read_text() == "\n"  # only Enter was pressed for "Press Enter"
    assert prompter.ticks > 0  # the countdown ran while waiting


def test_wrong_code_fails_without_leaking_it(monkeypatch, tmp_path):
    child, _ = play("claude-bad-code.txt", monkeypatch, tmp_path)
    result = Flow(provider("claude"), child, Prompter(code="WRONG-0123456789abcdefghijkl")).run()
    assert result.outcome == "failed"
    assert "WRONG" not in result.detail and "[code]" in result.detail
    assert "state=example-state" not in result.detail  # link tokens redacted too


def test_cancel_types_nothing(monkeypatch, tmp_path):
    child, typed = play("claude.txt", monkeypatch, tmp_path)
    result = Flow(provider("claude"), child, Prompter(code="")).run()
    assert result.outcome == "cancelled"
    assert not typed.exists()


def test_changed_wording_offers_live_terminal(monkeypatch, tmp_path):
    child, _ = play("changed-wording.txt", monkeypatch, tmp_path)
    prompter = Prompter(live=False)
    result = Flow(provider("claude"), child, prompter, pattern_timeout=1).run()
    assert result.outcome == "failed" and "No sign-in link" in result.detail
    assert "new page" in prompter.offers[0][1]


def test_live_terminal_takeover(monkeypatch, tmp_path):
    child, _ = play("changed-wording.txt", monkeypatch, tmp_path)
    taken = []
    monkeypatch.setattr(child, "interact", lambda: taken.append(True))
    result = Flow(provider("claude"), child, Prompter(live=True), pattern_timeout=1).run()
    assert result.outcome == "manual" and taken == [True]


def test_device_code_expires(monkeypatch, tmp_path):
    child, _ = play("github-expired.txt", monkeypatch, tmp_path)
    p = provider("github")
    short = providers.Provider(**{**p.__dict__, "timeout": 2})
    result = Flow(short, child, Prompter()).run()
    assert result.outcome == "timeout"


def test_clean_and_redact():
    raw = "\x1b[1mhi\x1b[0m\r\n\x1b]8;;https://x.example/a?b=c\x1b\\link\x1b]8;;\x1b\\\r"
    assert clean(raw) == "hi\nlink\n"
    text = "code ABCD see https://x.example/p?token=abc and tok_0123456789abcdefghijklmnop"
    assert redact(text, ["ABCD"]) == "code [code] see https://x.example/p?[…] and [redacted]"


# --- login(): checks against the tag, audit ---------------------------------


def ship_side(answers):
    """FakeSSH handler: answers by the agent command run (bash -c '<cmd>')."""

    def handler(ship, cmd, inp):
        if cmd == ["true"]:
            return 0, b"", ""
        command = cmd[-1]
        for key, value in answers.items():
            if key in command:
                return value
        raise AssertionError(command)

    return handler


class RecordingSSH:
    def __init__(self, inner, store):
        from fleetledger.ssh import SSH

        self.inner = inner
        self.real = SSH(store.load())
        self.argvs = []

    def run(self, ship, command, **kw):
        return self.inner.run(ship, command, **kw)

    def argv(self, ship, host, command, **kw):
        argv = self.real.argv(ship, host, command, **kw)
        self.argvs.append(argv)
        return argv


def fake_spawner(fixture, monkeypatch, tmp_path):
    def spawner(argv):
        child, _ = play(fixture, monkeypatch, tmp_path)
        return child

    return spawner


def test_login_ok_and_audited(store, fake_ssh, monkeypatch, tmp_path, home):
    inv = store.load()
    ssh = RecordingSSH(fake_ssh(ship_side({
        "claude auth status --json": (0, json.dumps({"loggedIn": True, "email": "work@example.com",
                                                      "subscriptionType": "max"}), ""),
    })), store)
    result = concierge.login(ssh, inv, inv.ship("ship-01"), provider("claude"),
                             Prompter(code=CLAUDE_CODE),
                             spawner=fake_spawner("claude.txt", monkeypatch, tmp_path))
    assert result.outcome == "ok" and result.account == "work@example.com"
    argv = ssh.argvs[0]
    assert "-tt" in argv and argv[-1] == "sudo -n -iu agent -- bash -c 'claude auth login'"
    assert all(CLAUDE_CODE not in a for a in argv)
    entries = audit.read()
    assert entries == [{**entries[0], "ship": "ship-01", "action": "login", "service": "claude",
                        "outcome": "ok", "account": "work@example.com"}]
    log = home / "data" / "audit.log"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert CLAUDE_CODE not in log.read_text() and "oauth" not in log.read_text()


def test_login_wrong_account_is_flagged(store, fake_ssh, monkeypatch, tmp_path):
    inv = store.load()
    ssh = RecordingSSH(fake_ssh(ship_side({
        "claude auth status --json": (0, json.dumps({"loggedIn": True,
                                                      "email": "you@example.com"}), ""),
    })), store)
    result = concierge.login(ssh, inv, inv.ship("ship-01"), provider("claude"),
                             Prompter(code=CLAUDE_CODE),
                             spawner=fake_spawner("claude.txt", monkeypatch, tmp_path))
    assert result.outcome == "wrong-account"
    check = result.checks[0]
    assert check.account == "you@example.com" and check.expected == ("work@example.com",)
    assert audit.read()[-1]["outcome"] == "wrong-account"


def test_gcloud_checks_both_stores_and_runs_after(store, fake_ssh, monkeypatch, tmp_path):
    inv = store.load()
    calls = []
    answers = {
        "gcloud auth list": (0, b"work@example.com\n", ""),
        "print-access-token": (1, b"", "no ADC"),
    }

    def handler(ship, cmd, inp):
        calls.append(cmd[-1])
        return ship_side(answers)(ship, cmd, inp)

    ssh = RecordingSSH(fake_ssh(handler), store)
    result = concierge.login(ssh, inv, inv.ship("ship-01"), provider("gcloud"),
                             Prompter(code=GCLOUD_CODE),
                             spawner=fake_spawner("gcloud.txt", monkeypatch, tmp_path))
    assert [c.service for c in result.checks] == ["gcloud", "gcloud_adc"]
    assert result.outcome == "failed"  # ADC did not take
    assert any(">/dev/null" in c for c in calls)


def test_github_after_command(store, fake_ssh, monkeypatch, tmp_path):
    inv = store.load()
    seen = []

    def handler(ship, cmd, inp):
        seen.append(cmd[-1])
        if cmd == ["true"]:
            return 0, b"", ""
        if "setup-git" in cmd[-1]:
            return 0, b"", ""
        return 0, b"example-work\n", ""

    ssh = RecordingSSH(fake_ssh(handler), store)
    result = concierge.login(ssh, inv, inv.ship("ship-01"), provider("github"), Prompter(),
                             spawner=fake_spawner("github.txt", monkeypatch, tmp_path))
    assert result.outcome == "ok" and result.account == "example-work"
    assert seen[1] == "gh auth setup-git --hostname github.com"


def test_failed_flow_skips_checks(store, fake_ssh, monkeypatch, tmp_path):
    inv = store.load()
    ssh = RecordingSSH(fake_ssh(ship_side({})), store)
    result = concierge.login(ssh, inv, inv.ship("ship-01"), provider("claude"),
                             Prompter(code="WRONG"),
                             spawner=fake_spawner("claude-bad-code.txt", monkeypatch, tmp_path))
    assert result.outcome == "failed" and result.checks == []
    assert audit.read()[-1] == {**audit.read()[-1], "outcome": "failed", "account": None}


def test_logout(store, fake_ssh):
    inv = store.load()
    ssh = fake_ssh(ship_side({"gh auth logout --hostname github.com": (0, b"", "")}))
    assert concierge.logout(ssh, inv.ship("ship-01"), provider("github"))
    assert audit.read()[-1]["action"] == "logout"
