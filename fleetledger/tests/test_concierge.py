"""Login concierge (W9): pattern tests in a real terminal, account checks, audit."""

import json
import re
import stat
import sys
from pathlib import Path

import pytest
from fleetledger.concierge import audit, engine, providers
from fleetledger.concierge.engine import Flow
from fleetledger.concierge.redact import clean, redact, tail

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


def play(fixture, monkeypatch, tmp_path, columns=None):
    log = tmp_path / "typed.log"
    monkeypatch.setenv("FAKE_CLI_INPUT_LOG", str(log))
    path = fixture if isinstance(fixture, Path) else FIX / fixture
    argv = [sys.executable, str(FAKE), str(path)]
    child = engine.spawn(argv, columns=columns) if columns else engine.spawn(argv)
    return child, log


CLAUDE_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=00000000-0000-4000-8000-"
    "000000000000&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth"
    "%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference&"
    "code_challenge=example-code-challenge&code_challenge_method=S256&state=example-state"
)


class Clock:
    """A clock tests can jump, and that can tick on every read."""

    def __init__(self):
        self.t = 1000.0
        self.step = 0.0

    def __call__(self):
        self.t += self.step
        return self.t


def provider(name):
    return providers.load_all()[name]


def test_github_runs_without_prompts():
    """gh's prompts wait for a terminal's cursor report that never comes."""
    assert provider("github").run.startswith("GH_PROMPT_DISABLED=1 gh auth login ")
    assert provider("github").send == ()


NO_GCLOUD_LOG = "CLOUDSDK_CORE_DISABLE_FILE_LOGGING=1 gcloud "


def test_every_gcloud_command_runs_without_file_logging():
    """gcloud's own debug log stores the sign-in code (phase 0, S5)."""
    seen = 0
    for p in providers.load_all().values():
        for command in [p.run, *p.after, p.logout, *(c.run for c in p.checks)]:
            calls = re.findall(r"(?:\S+=\S+ )*gcloud ", command)
            seen += len(calls)
            assert all(c.endswith(NO_GCLOUD_LOG) for c in calls), (p.id, command)
    assert seen >= 8
    assert provider("gcloud").run.startswith(NO_GCLOUD_LOG + "auth login ")
    assert provider("gcloud-adc").run.startswith(NO_GCLOUD_LOG + "auth application-default login ")


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
    """The session recorded in spike S5 (no echo of the pasted code)."""
    child, typed = play("claude.txt", monkeypatch, tmp_path)
    prompter = Prompter(code=f"  {CLAUDE_CODE}\n")
    result = Flow(provider("claude"), child, prompter).run()
    assert result.outcome == "completed", result
    (url, code, expires_in) = prompter.shown[0]
    assert url == CLAUDE_URL
    assert code is None and 590 < expires_in <= 600
    assert typed.read_text() == CLAUDE_CODE + "\n"  # typed into the terminal, trimmed
    assert CLAUDE_CODE not in result.detail


def test_escape_sequences_and_hyperlinks(monkeypatch, tmp_path):
    child, _ = play("claude-ansi.txt", monkeypatch, tmp_path)
    prompter = Prompter(code=CLAUDE_CODE)
    assert Flow(provider("claude"), child, prompter).run().outcome == "completed"
    url = prompter.shown[0][0]
    assert url.startswith("https://claude.ai/oauth/authorize?code=true&client_id=")
    assert url.endswith("state=example-state") and "\x1b" not in url


@pytest.mark.parametrize("fixture", ["claude-split.txt", "claude-osc-split.txt"])
def test_link_split_across_reads(fixture, monkeypatch, tmp_path):
    """Neither half a link nor half an escape sequence is ever shown."""
    child, _ = play(fixture, monkeypatch, tmp_path)
    prompter = Prompter(code=CLAUDE_CODE)
    assert Flow(provider("claude"), child, prompter).run().outcome == "completed"
    assert [s[0] for s in prompter.shown] == [CLAUDE_URL]


def test_wide_terminal_keeps_long_links_whole(monkeypatch, tmp_path):
    """A tool that wraps at the terminal's width must not break the link."""
    child, _ = play("claude-wrapped.txt", monkeypatch, tmp_path)
    prompter = Prompter(code=CLAUDE_CODE)
    assert Flow(provider("claude"), child, prompter).run().outcome == "completed"
    long_line = (FIX / "claude-wrapped.txt").read_text().splitlines()[1]
    assert prompter.shown[0][0] == long_line.removeprefix(">w ")  # 1300+ characters
    narrow, _ = play("claude-wrapped.txt", monkeypatch, tmp_path, columns=80)
    cut = Prompter(code=CLAUDE_CODE)
    Flow(provider("claude"), narrow, cut).run()
    assert len(cut.shown[0][0]) == 80  # what an 80-column terminal would have given


def test_clean_exit_without_success_line_goes_to_the_checks(monkeypatch, tmp_path):
    child, _ = play("claude-quiet.txt", monkeypatch, tmp_path)
    result = Flow(provider("claude"), child, Prompter(code=CLAUDE_CODE)).run()
    assert result.outcome == "completed" and result.exitstatus == 0


def test_code_arriving_after_the_window_is_not_typed(monkeypatch, tmp_path):
    child, typed = play("claude.txt", monkeypatch, tmp_path)
    clock = Clock()

    class Slow(Prompter):
        def ask_code(self):
            clock.t += 700  # the person took longer than the 600 s window
            return CLAUDE_CODE

    result = Flow(provider("claude"), child, Slow(), clock=clock).run()
    assert result.outcome == "cancelled" and "window closed" in result.detail
    assert not typed.exists()


def test_typed_code_gets_its_verdict_past_the_window(monkeypatch, tmp_path):
    child, typed = play("claude-slow-verdict.txt", monkeypatch, tmp_path)
    clock = Clock()

    class JustInTime(Prompter):
        def ask_code(self):
            clock.t += 595
            clock.step = 5  # the window closes while the tool checks the code
            return CLAUDE_CODE

    result = Flow(provider("claude"), child, JustInTime(), clock=clock).run()
    assert result.outcome == "completed"
    assert typed.read_text() == CLAUDE_CODE + "\n"


def test_fixed_prompts_are_answered_once(monkeypatch, tmp_path):
    toml = tmp_path / "demo.toml"
    toml.write_text(
        'id = "demo"\nlabel = "Demo"\nmode = "device-code"\nrun = "demo"\ntimeout = 60\n'
        "[match]\nurl = 'https://example\\.com/device'\ncode = 'code: (\\S+)'\n"
        "success = 'Done'\n[match.send]\n'Press Enter' = \"\\r\"\n"
        '[[check]]\nservice = "demo"\nrun = "true"\n[logout]\nrun = "true"\n'
    )
    session = tmp_path / "demo.txt"
    session.write_text("> code: ABCD at https://example.com/device now\n>> Press Enter \n<\n"
                       "~ 1\n> Done\n! 0\n")
    child, typed = play(session, monkeypatch, tmp_path)
    sent = []
    real_send = child.send
    monkeypatch.setattr(child, "send", lambda text: sent.append(text) or real_send(text))
    result = Flow(providers.parse(toml), child, Prompter()).run()
    assert result.outcome == "completed" and sent == ["\r"]


def test_gcloud_paste_back(monkeypatch, tmp_path):
    child, typed = play("gcloud.txt", monkeypatch, tmp_path)
    prompter = Prompter(code=GCLOUD_CODE)
    result = Flow(provider("gcloud"), child, prompter).run()
    assert result.outcome == "completed"
    assert prompter.shown[0][0].startswith("https://accounts.google.com/o/oauth2/auth?")
    assert prompter.shown[0][0].endswith("code_challenge_method=S256")
    assert typed.read_text() == GCLOUD_CODE + "\n"


def test_github_device_code(monkeypatch, tmp_path):
    child, typed = play("github.txt", monkeypatch, tmp_path)
    prompter = Prompter()
    result = Flow(provider("github"), child, prompter).run()
    assert result.outcome == "completed"
    assert prompter.shown == [
        ("https://github.com/login/device", "ABCD-1234", pytest.approx(900, abs=5))
    ]
    assert not typed.exists()  # prompts are off: nothing to answer
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


def test_redact_scrubs_accounts_and_ids_after_escapes_are_gone():
    raw = ("\x1b]3008;start=0000;machineid=00000000000000000000000000000000\x1b\\"
           "Logged in as \x1b[1muser@example.com\x1b[0m, org "
           "00000000-0000-0000-0000-000000000000\r\n")
    assert tail(clean(raw)) == "Logged in as [email], org [redacted]"
    for email in ("first.last+tag@mail.corp.example", "x@a.b.test", "u_1@ship-01.mail.invalid"):
        assert redact(f"as {email}.") == "as [email]."


# --- login(): checks against the tag, audit ---------------------------------


NOTIFIED: list[list[str]] = []  # ship-ctl notify-signin calls the fake Ship received


def ship_ctl_side(cmd, notify_data=None):
    """The fake Ship's ship-ctl: version, and notify-signin telling main and grinder."""
    from fleetledger.shipctl import KNOWN_CONTRACTS

    verb = cmd[3]
    base = {"schema": "shipshape.ship-ctl-result/1", "command": verb, "ok": True,
            "changed": [], "messages": [], "error": None}
    if verb == "version":
        return 0, json.dumps({**base, "data": {"tooling": "0.1.0", "commit": "0123456789ab",
                                               "contracts": dict(KNOWN_CONTRACTS)}}), ""
    if verb == "notify-signin":
        NOTIFIED.append(cmd[3:])
        data = notify_data or {"service": cmd[4], "account": "work@example.com",
                               "sent": ["main", "grinder"], "skipped": []}
        return 0, json.dumps({**base, "data": data}), ""
    raise AssertionError(cmd)


def ship_side(answers, notify_data=None):
    """FakeSSH handler: answers by the agent command run (bash -c '<cmd>')."""

    def handler(ship, cmd, inp):
        if cmd == ["true"]:
            return 0, b"", ""
        if cmd[:3] == ["sudo", "-n", "/usr/local/bin/ship-ctl"]:
            return ship_ctl_side(cmd, notify_data)
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


def test_gcloud_login_terminal_has_file_logging_off(store, fake_ssh, monkeypatch, tmp_path):
    inv = store.load()
    ssh = RecordingSSH(fake_ssh(ship_side({
        "gcloud auth list": (0, b"work@example.com\n", ""),
        "print-access-token": (0, b"", ""),
    })), store)
    concierge.login(ssh, inv, inv.ship("ship-01"), provider("gcloud"),
                    Prompter(code=GCLOUD_CODE),
                    spawner=fake_spawner("gcloud.txt", monkeypatch, tmp_path))
    assert ssh.argvs[0][-1] == (
        "sudo -n -iu agent -- bash -c "
        "'CLOUDSDK_CORE_DISABLE_FILE_LOGGING=1 gcloud auth login --no-launch-browser --update-adc'"
    )


def test_account_is_read_without_terminal_escapes(store, fake_ssh):
    """gh prints the login in bold; a login shell may add an OSC 3008 line."""
    inv = store.load()
    out = b"\x1b]3008;start=0000;user=agent\x1b\\\x1b[1mwork-login\x1b[0m\r\n"
    ssh = fake_ssh(ship_side({"gh api user": (0, out, "")}))
    check = provider("github").checks[0]
    result = concierge.run_check(ssh, inv, inv.ship("ship-01"), check)
    assert result.signed_in and result.account == "work-login"


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


def test_signed_out_status_is_a_failure(store, fake_ssh, monkeypatch, tmp_path):
    """claude auth status exits 0 either way; loggedIn decides."""
    inv = store.load()
    ssh = RecordingSSH(fake_ssh(ship_side({
        "claude auth status --json": (0, json.dumps({"loggedIn": False}), ""),
    })), store)
    result = concierge.login(ssh, inv, inv.ship("ship-01"), provider("claude"),
                             Prompter(code=CLAUDE_CODE),
                             spawner=fake_spawner("claude.txt", monkeypatch, tmp_path))
    assert result.outcome == "failed" and result.checks[0].signed_in is False


def test_gcloud_wrong_account(store, fake_ssh, monkeypatch, tmp_path):
    """One wrong check is enough, even when another (ADC) has no account."""
    inv = store.load()
    ssh = RecordingSSH(fake_ssh(ship_side({
        "gcloud auth list": (0, b"you@example.com\n", ""),
        "print-access-token": (0, b"", ""),
    })), store)
    result = concierge.login(ssh, inv, inv.ship("ship-01"), provider("gcloud"),
                             Prompter(code=GCLOUD_CODE),
                             spawner=fake_spawner("gcloud.txt", monkeypatch, tmp_path))
    assert result.outcome == "wrong-account"
    assert [(c.service, c.matches) for c in result.checks] == [
        ("gcloud", False), ("gcloud_adc", None)]


def test_checks_run_after_a_live_takeover(store, fake_ssh, monkeypatch, tmp_path):
    inv = store.load()
    ssh = RecordingSSH(fake_ssh(ship_side({
        "claude auth status --json": (0, json.dumps({"loggedIn": True,
                                                      "email": "work@example.com"}), ""),
    })), store)

    def spawner(argv):
        child, _ = play("changed-wording.txt", monkeypatch, tmp_path)
        monkeypatch.setattr(child, "interact", lambda: None)
        return child

    result = concierge.login(ssh, inv, inv.ship("ship-01"), provider("claude"),
                             Prompter(live=True), spawner=spawner,
                             flow_options={"pattern_timeout": 1})
    assert result.outcome == "ok" and result.checks and result.account == "work@example.com"


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


# --- after a sign-in: tell the Ship's core agents (ship-ctl notify-signin) ------


def claude_login(store, fake_ssh, monkeypatch, tmp_path, email="work@example.com",
                 logged_in=True, notify_data=None):
    NOTIFIED.clear()
    inv = store.load()
    ssh = RecordingSSH(fake_ssh(ship_side({
        "claude auth status --json": (0, json.dumps({"loggedIn": logged_in, "email": email}), ""),
    }, notify_data)), store)
    return concierge.login(ssh, inv, inv.ship("ship-01"), provider("claude"),
                           Prompter(code=CLAUDE_CODE),
                           spawner=fake_spawner("claude.txt", monkeypatch, tmp_path))


def test_a_good_sign_in_tells_the_core_agents(store, fake_ssh, monkeypatch, tmp_path):
    result = claude_login(store, fake_ssh, monkeypatch, tmp_path)
    assert result.outcome == "ok"
    # Only the service name goes to the Ship: it reads the account itself.
    assert NOTIFIED == [["notify-signin", "claude", "--json"]]
    assert "Told main, grinder that Claude Code is signed in." in result.notes


@pytest.mark.parametrize("email,logged_in,outcome", [
    ("you@example.com", True, "wrong-account"), (None, False, "failed")])
def test_no_notice_unless_the_sign_in_checked_out(store, fake_ssh, monkeypatch, tmp_path,
                                                  email, logged_in, outcome):
    result = claude_login(store, fake_ssh, monkeypatch, tmp_path, email, logged_in)
    assert result.outcome == outcome
    assert NOTIFIED == []


def test_agents_not_told_are_listed(store, fake_ssh, monkeypatch, tmp_path):
    data = {"service": "claude", "account": "work@example.com", "sent": ["main"],
            "skipped": [{"name": "grinder", "reason": "waiting-for-permission"}]}
    result = claude_login(store, fake_ssh, monkeypatch, tmp_path, notify_data=data)
    assert result.notes[-2:] == ["Told main that Claude Code is signed in.",
                                 "grinder was not told (waiting-for-permission)."]


def test_a_failed_notice_never_fails_the_sign_in(store, fake_ssh, monkeypatch, tmp_path):
    """An older Ship without notify-signin, say: the sign-in still counts, with a note."""
    from fleetledger import concierge as concierge_mod

    def old_ship(*args, **kwargs):
        raise concierge_mod.FleetError("ship-ctl notify-signin failed (usage): invalid choice")

    monkeypatch.setattr(concierge_mod.ShipCtl, "call", old_ship)
    result = claude_login(store, fake_ssh, monkeypatch, tmp_path)
    assert result.outcome == "ok"
    assert result.notes[-1].startswith("The Ship's agents were not told about this sign-in:")
    assert audit.read()[-1]["outcome"] == "ok"
