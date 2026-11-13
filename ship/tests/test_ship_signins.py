"""Sign-in files on the Ship: the registry (signins.py) and the checks (signincheck.py).

The Ship reads the same sign-in files as FleetLedger (ship/signins/, the contract
contracts/signin.schema.json). The built-ins keep their checks written in ship-ctl;
a toolkit-style file checks its CLI with a command, here the fake example-cli.
"""

from __future__ import annotations

import json
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from ship_harness import REPO, SHIP, assert_valid

sys.path.insert(0, str(SHIP / "lib"))
from shipctl import schema, signincheck, signins, status, tomlw  # noqa: E402
from shipctl.errors import CtlError  # noqa: E402
from shipctl.paths import Paths  # noqa: E402

CONTRACT = REPO / "contracts" / "signin.schema.json"
BUILT_INS = sorted((SHIP / "signins").glob("*.toml"))
TOKEN = "FAKE-TOKEN-MUST-NEVER-APPEAR"


def built_in_registry() -> signins.Registry:
    return signins.build(SHIP / "signins", SHIP / "schemas" / "signin.schema.json")


# --- the built-ins, from the shared files -------------------------------------------
def test_built_ins_as_before():
    reg = built_in_registry()
    assert reg.keys == ("claude", "gcloud", "gcloud_adc", "github")
    assert {k: s.status.builtin for k, s in reg.checks.items()} == {
        "claude": "claude", "gcloud": "gcloud", "gcloud_adc": "adc", "github": "github"}
    assert reg.labels == {"claude": "Claude Code", "gcloud": "gcloud",
                          "gcloud_adc": "Application Default Credentials", "github": "GitHub"}
    assert signins.CORE == {"claude"}  # missing is always an attention item
    assert sorted(reg.flows) == ["claude", "gcloud", "gcloud-adc", "github"]
    assert reg.flows["gcloud"].keys == ("gcloud", "gcloud_adc")
    assert reg.notice_label("gcloud") == "gcloud and Application Default Credentials"
    assert reg.notice_label("gcloud-adc") == "Application Default Credentials"
    assert reg.notice_label("claude") == "Claude Code"
    assert reg.flow("gcloud_adc").id == "gcloud-adc"  # by status key too
    assert set(status.BUILTIN_PROBES) == {"claude", "gcloud", "adc", "github"}


def test_status_document_names_every_key():
    """The status contract requires an entry for each built-in key."""
    doc = json.loads((REPO / "contracts" / "ship-status.schema.json").read_text())
    assert set(doc["properties"]["auth"]["required"]) == set(built_in_registry().keys)


@pytest.mark.parametrize("path", BUILT_INS, ids=lambda p: p.name)
def test_built_in_files_round_trip_the_contract(path: Path):
    """Each file validates (both validators agree), and survives a TOML round trip."""
    spec = json.loads(CONTRACT.read_text())
    doc = tomllib.loads(path.read_text())
    assert not list(Draft202012Validator(spec).iter_errors(doc))
    assert schema.Validator(spec).errors(doc) == []
    again = tomllib.loads(tomlw.dumps(doc))
    assert again == doc and schema.Validator(spec).errors(again) == []
    assert doc["status"]["builtin"] in status.BUILTIN_PROBES


def test_the_bundle_carries_the_files_and_the_schema(ship):
    paths = Paths(root=ship.root, bundle=ship.bundle)
    assert sorted(p.name for p in paths.signins.glob("*.toml")) == [p.name for p in BUILT_INS]
    assert paths.schema("signin").read_bytes() == CONTRACT.read_bytes()


# --- refusals -------------------------------------------------------------------------
GOOD = """\
schema = 1
id = "exampledocs"
label = "Example Docs"
mode = "paste-back"
run = "example-cli auth login"
timeout = 300

[signin]
label = "Example Docs"
order = 10

[match]
url = 'https://auth\\.example\\.com/\\S+'
prompt = 'Paste the code'
success = 'Signed in'

[status]
run = "example-cli auth status --json"
ok_json = "signed_in"
account = "json:account.email"
expires_at = "json:expires_at"
missing_exit = [1]
env = { EXAMPLE_NO_FILE_LOG = "1" }

[secrets]
paths = ["~/.config/example-cli/tokens.json"]
store = "file"

[logout]
run = "example-cli auth logout"
"""


def folder_with(tmp_path: Path, name: str, text: str) -> Path:
    folder = tmp_path / "signins"
    folder.mkdir(exist_ok=True)
    for p in BUILT_INS:
        (folder / p.name).write_text(p.read_text())
    (folder / name).write_text(text)
    return folder


def build(folder: Path) -> signins.Registry:
    return signins.build(folder, CONTRACT)


def test_a_toolkit_file_is_a_new_key(tmp_path):
    reg = build(folder_with(tmp_path, "exampledocs.toml", GOOD))
    assert reg.keys[-1] == "exampledocs"
    st = reg.checks["exampledocs"].status
    assert (st.every, st.expiring_within, st.missing_exit) == (300, 86400, (1,))
    assert st.env == (("EXAMPLE_NO_FILE_LOG", "1"),)


@pytest.mark.parametrize("change,message", [
    (('store = "file"', 'store = "keyring"'), "no unlocked password store"),
    (('account = "json:account.email"', "account = 're:no group'"), "exactly one group"),
    (('expires_at = "json:expires_at"', "expires_at = 're:(a)(b)'"), "exactly one group"),
    (('id = "exampledocs"', 'id = "other"'), "does not match the file name"),
    (("[logout]", "[params]\nsite = 1\n\n[logout]"), "params"),
    (("[status]\n", "[status]\nbuiltin = \"claude\"\n"), "allowed form"),
])
def test_bad_files_are_refused(tmp_path, change, message):
    folder = folder_with(tmp_path, "exampledocs.toml", GOOD.replace(*change))
    with pytest.raises(CtlError, match=message):
        build(folder)


def test_a_status_must_check_one_of_its_flows_keys(tmp_path):
    text = GOOD.replace("[status]", '[[check]]\nservice = "other"\nrun = "true"\n\n[status]')
    with pytest.raises(CtlError, match="none of its"):
        build(folder_with(tmp_path, "exampledocs.toml", text))


def test_every_key_a_flow_signs_in_needs_a_check(tmp_path):
    folder = folder_with(tmp_path, "exampledocs.toml", GOOD)
    (folder / "gcloud-adc.toml").unlink()  # gcloud still signs in gcloud_adc
    with pytest.raises(CtlError, match="no sign-in file checks gcloud_adc"):
        build(folder)


# --- the checks, in the sandbox Ship ---------------------------------------------------
def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_file(ship, text: str = GOOD, name: str = "exampledocs.toml") -> None:
    (ship.bundle / "signins" / name).write_text(text)


def answer(ship, line: str = "auth status --json", **how) -> None:
    s = ship.scenario()
    s.setdefault("example-cli", {})[line] = how
    ship.set(**{"example-cli": s["example-cli"]})


def signed_in(email="you@example.com", expires=None) -> str:
    doc = {"signed_in": True, "account": {"email": email}}
    if expires is not None:
        doc["expires_at"] = expires
    return json.dumps(doc)


def cli_calls(ship) -> list[dict]:
    path = ship.fake_dir / "example-cli.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def status_doc(ship) -> dict:
    rc, doc, out = ship.ctl("status", "--json")
    assert rc == 0, out
    assert_valid("ship-status", doc)
    return doc


def test_an_adapter_check(ship):
    add_file(ship)
    answer(ship, out=signed_in(expires=iso(time.time() + 30 * 86400)))
    doc = status_doc(ship)
    entry = doc["auth"]["exampledocs"]
    assert entry["state"] == "ok" and entry["account"] == "you@example.com"
    assert entry["expires_at"].endswith("Z")
    assert list(doc["auth"]) == ["claude", "gcloud", "gcloud_adc", "github", "exampledocs"]
    assert cli_calls(ship)[0]["env"] == {"EXAMPLE_NO_FILE_LOG": "1"}
    # built-ins unchanged beside it
    assert doc["auth"]["claude"]["account"] == "work@example.com"
    assert doc["auth"]["gcloud_adc"] == {**doc["auth"]["gcloud_adc"], "state": "ok",
                                         "account": None}


@pytest.mark.parametrize("how,state", [
    ({"out": json.dumps({"signed_in": False})}, "missing"),
    ({"rc": 1}, "missing"),
    ({"rc": 3}, "error"),  # not in missing_exit
    ({"out": "not json"}, "error"),
])
def test_adapter_states(ship, how, state):
    add_file(ship)
    answer(ship, **how)
    assert status_doc(ship)["auth"]["exampledocs"]["state"] == state


NATIVE = GOOD.replace('''run = "example-cli auth status --json"
ok_json = "signed_in"
account = "json:account.email"
expires_at = "json:expires_at"
missing_exit = [1]
''', '''run = "example-cli status"
format = "shipshape"
every = "1m"
''')


def native(**fields) -> str:
    return "starting up\n" + json.dumps({"shipshape_signin": 1, **fields})


def test_the_native_status_line(ship):
    add_file(ship, NATIVE)
    answer(ship, "status", out=native(state="ok", account="you@example.com",
                                      detail="read only", expires_at=int(time.time()) + 86400 * 9))
    entry = status_doc(ship)["auth"]["exampledocs"]
    assert entry["state"] == "ok" and entry["account"] == "you@example.com"
    assert entry["detail"] == "read only" and entry["expires_at"].endswith("Z")


@pytest.mark.parametrize("out,state", [
    (native(state="missing", account="ignored@example.com"), "missing"),
    (native(state="weird"), "error"),
    ("no line at all", "error"),
])
def test_native_states(ship, out, state):
    add_file(ship, NATIVE)
    answer(ship, "status", out=out)
    entry = status_doc(ship)["auth"]["exampledocs"]
    assert entry["state"] == state
    if state == "missing":
        assert entry["account"] is None


@pytest.mark.parametrize("ahead,state,code", [
    (3 * 86400, "ok", None),
    (2 * 3600, "expiring", "signin-expiring"),
    (-3600, "expired", "signin-expired"),
])
def test_expiry_states_and_attention(ship, ahead, state, code):
    add_file(ship)
    answer(ship, out=signed_in(expires=iso(time.time() + ahead)))
    doc = status_doc(ship)
    assert doc["auth"]["exampledocs"]["state"] == state
    items = [a for a in doc["attention"] if a.get("subject") == "exampledocs"]
    if code:
        assert items == [{"code": code, "subject": "exampledocs", "message":
                          "The Example Docs sign-in " + ("expires soon." if state == "expiring"
                                                         else "has expired.")}]
    else:
        assert items == []


def test_native_expiry_also_counts(ship):
    add_file(ship, NATIVE)
    answer(ship, "status", out=native(state="ok", expires_at=iso(time.time() + 600)))
    assert status_doc(ship)["auth"]["exampledocs"]["state"] == "expiring"


def test_results_are_kept_for_every(ship):
    add_file(ship)
    answer(ship, out=signed_in())
    first = status_doc(ship)["auth"]["exampledocs"]
    second = status_doc(ship)["auth"]["exampledocs"]
    assert len(cli_calls(ship)) == 1  # the second refresh used the kept result
    assert second == first  # checked_at too: when it was really checked
    kept = ship.p("/run/shipshape/signin-checks.json")
    assert kept.stat().st_mode & 0o777 == 0o600
    # A changed check is a different check: it runs again.
    add_file(ship, GOOD.replace('missing_exit = [1]', 'missing_exit = [1, 4]'))
    status_doc(ship)
    assert len(cli_calls(ship)) == 2


def test_every_0s_checks_on_each_refresh(ship):
    add_file(ship, GOOD.replace("[status]\n", '[status]\nevery = "0s"\n'))
    answer(ship, out=signed_in())
    status_doc(ship)
    status_doc(ship)
    assert len(cli_calls(ship)) == 2


def test_a_slow_check_is_unknown_and_never_holds_up_status(ship):
    ship.env["SHIPSHAPE_PROBE_TIMEOUT"] = "1"
    add_file(ship)
    answer(ship, out=signed_in(), hang=20)
    started = time.monotonic()
    doc = status_doc(ship)
    assert time.monotonic() - started < 15
    assert doc["auth"]["exampledocs"]["state"] == "unknown"
    assert "exampledocs sign-in check: timed out" in doc["errors"]
    assert doc["auth"]["claude"]["state"] == "ok"  # the rest is there
    answer(ship, out=signed_in())  # a timed-out check is not kept: it runs again
    assert status_doc(ship)["auth"]["exampledocs"]["state"] == "ok"


TOKEN_FILE = GOOD.replace('''run = "example-cli auth status --json"
ok_json = "signed_in"
account = "json:account.email"
expires_at = "json:expires_at"
''', '''run = "example-cli print-token"
discard_output = true
''')


def test_discard_output_reads_only_the_exit_code(ship):
    add_file(ship, TOKEN_FILE)
    answer(ship, "print-token", out=TOKEN)
    rc, doc, out = ship.ctl("status", "--json")
    assert doc["auth"]["exampledocs"]["state"] == "ok"
    assert TOKEN not in out
    assert TOKEN not in ship.p("/run/shipshape/signin-checks.json").read_text()
    answer(ship, "print-token", out=TOKEN, rc=1)
    rc, doc, out = ship.ctl("signin", "check", "exampledocs", "--json")
    assert doc["data"]["auth"]["exampledocs"]["state"] == "missing" and TOKEN not in out


def test_output_is_capped(ship):
    """Only the first 64 KiB is read: a huge answer is unreadable, not a memory hog."""
    add_file(ship)
    answer(ship, out=signed_in(), big=200_000)
    assert status_doc(ship)["auth"]["exampledocs"]["state"] == "error"


def test_the_runner_caps_and_drains(tmp_path):
    from shipctl.runner import Runner

    runner = Runner(Paths(root=tmp_path, bundle=tmp_path))
    r = runner.run(["bash", "-c", "head -c 300000 /dev/zero | tr '\\0' x; echo; echo err >&2"],
                   limit=1000, timeout=10)
    assert r.ok and r.out == "x" * 1000 and r.err == ""


def test_parse_time():
    assert signincheck.parse_time("2026-01-02T03:04:05Z") == 1767323045
    assert signincheck.parse_time("2026-01-02T05:04:05+02:00") == 1767323045
    assert signincheck.parse_time("1767323045") == 1767323045
    assert signincheck.parse_time(1767323045.5) == 1767323045.5
    assert signincheck.parse_time("2026-01-02T03:04:05") is None  # no offset
    assert signincheck.parse_time("soon") is None
    assert signincheck.parse_time(True) is None


# --- ship-ctl signin check --------------------------------------------------------------
def test_signin_check_runs_now_and_keeps_the_result(ship):
    add_file(ship)
    answer(ship, out=signed_in())
    status_doc(ship)
    answer(ship, out=signed_in("other@example.com"))
    rc, doc, out = ship.ctl("signin", "check", "exampledocs", "--json")
    assert rc == 0, out
    assert_valid("ship-ctl-result", doc)
    assert doc["command"] == "signin" and doc["data"]["signin"] == "exampledocs"
    assert doc["data"]["auth"]["exampledocs"]["account"] == "other@example.com"
    assert doc["messages"] == ["exampledocs: ok (other@example.com)"]
    # The next status agrees with it, from the kept result.
    assert status_doc(ship)["auth"]["exampledocs"]["account"] == "other@example.com"
    assert len(cli_calls(ship)) == 2


def test_signin_check_covers_every_key_the_flow_signs_in(ship):
    rc, doc, _ = ship.ctl("signin", "check", "gcloud", "--json")
    assert rc == 0
    auth = doc["data"]["auth"]
    assert list(auth) == ["gcloud", "gcloud_adc"]
    assert auth["gcloud"]["account"] == "work@example.com" and auth["gcloud_adc"]["state"] == "ok"
    rc, doc, _ = ship.ctl("signin", "check", "gcloud_adc", "--json")
    assert doc["data"]["signin"] == "gcloud-adc" and list(doc["data"]["auth"]) == ["gcloud_adc"]


def test_signin_check_built_ins_as_status_has_them(ship):
    rc, doc, _ = ship.ctl("signin", "check", "github", "--json")
    entry = doc["data"]["auth"]["github"]
    assert entry["state"] == "ok" and entry["account"] == "example-user"
    assert entry["scopes"] == ["gist", "read:org", "repo"]
    s = ship.scenario()
    s["claude"]["logged_in"] = False
    ship.set(claude=s["claude"])
    rc, doc, _ = ship.ctl("signin", "check", "claude", "--json")
    assert doc["data"]["auth"]["claude"]["state"] == "missing"


def test_signin_check_unknown_is_a_usage_error(ship):
    rc, doc, _ = ship.ctl("signin", "check", "nothing", "--json")
    assert rc == 2 and doc["error"]["code"] == "usage"
    assert "known: claude, gcloud, gcloud-adc, github" in doc["error"]["message"]


def test_version_lists_the_signin_contract(ship):
    rc, doc, _ = ship.ctl("version", "--json")
    assert doc["data"]["contracts"]["signin"] == 1


# --- reacting to a toolkit sign-in -------------------------------------------------------
def test_notify_signin_takes_a_toolkit_id(ship):
    add_file(ship)
    answer(ship, out=signed_in())
    ship.ctl("autostart", "--json")
    rc, doc, out = ship.ctl("notify-signin", "exampledocs", "--json")
    assert rc == 0, out
    assert doc["data"]["account"] == "you@example.com"
    assert sorted(doc["data"]["sent"]) == ["grinder", "main"]
    line = ship.prompts()[-1][1]
    assert line == ("ShipShape: Example Docs is now signed in as you@example.com. "
                    "Re-check it if you need it.")


def test_notify_signin_names_what_gcloud_signs_in(ship):
    ship.ctl("autostart", "--json")
    rc, doc, _ = ship.ctl("notify-signin", "gcloud", "--json")
    line = ship.prompts()[-1][1]
    assert line.startswith("ShipShape: gcloud and Application Default Credentials is now "
                           "signed in as work@example.com.")


def test_an_expiring_sign_in_releases_waiting_tools(ship):
    """A tool that needs a toolkit sign-in finishes once it is signed in, even if it
    ends soon."""
    add_file(ship)
    (ship.bundle / "tools" / "example-tool.toml").write_text(
        'schema = 1\nid = "example-tool"\ndescription = "Uses Example Docs."\nkind = "script"\n'
        'script = "example-tool.sh"\nneeds = ["exampledocs"]\ncheck = "true"\n')
    (ship.bundle / "tools" / "example-tool.sh").write_text("#!/bin/sh\nexit 0\n")
    (ship.bundle / "tools" / "example-tool.sh").chmod(0o755)
    answer(ship, rc=1)
    ship.ctl("tools", "add", "example-tool", "--json")
    assert status_doc(ship)["tools"]["example-tool"]["state"] == "waiting"
    answer(ship, out=signed_in(expires=iso(time.time() + 3600)))
    ship.ctl("signin", "check", "exampledocs", "--json")
    rc, doc, out = ship.ctl("refresh", "--json")
    assert rc == 0, out
    assert status_doc(ship)["tools"]["example-tool"]["state"] == "ok"


# --- review follow-ups ---------------------------------------------------------------------
@pytest.mark.parametrize("out", [
    native(state="ok", expires_at="9999-12-31T23:59:59-05:00"),  # past what can be written
    native(state="ok", expires_at="0001-01-01T00:00:00+05:00"),
    "[" * 60000,  # JSON nested too deep to parse
])
def test_no_output_stops_the_status_document(ship, out):
    add_file(ship, NATIVE if out.startswith("starting") else GOOD)
    answer(ship, "status" if out.startswith("starting") else "auth status --json", out=out)
    entry = status_doc(ship)["auth"]["exampledocs"]
    assert entry["state"] in ("ok", "error")


def test_a_kept_result_still_expires(ship):
    add_file(ship, GOOD.replace("[status]\n", '[status]\nevery = "1h"\nexpiring_within = "1s"\n'))
    answer(ship, out=signed_in(expires=iso(time.time() + 3)))
    assert status_doc(ship)["auth"]["exampledocs"]["state"] == "ok"
    time.sleep(4)
    doc = status_doc(ship)
    assert doc["auth"]["exampledocs"]["state"] == "expired"
    assert len(cli_calls(ship)) == 1  # from the kept result
    assert any(a["code"] == "signin-expired" for a in doc["attention"])


def test_the_runner_keeps_output_a_helper_outlives(tmp_path):
    """A helper left holding stdout: what was printed is kept, and the helper ends."""
    from shipctl.runner import Runner

    runner = Runner(Paths(root=tmp_path, bundle=tmp_path))
    mark = tmp_path / "late"
    started = time.monotonic()
    r = runner.run(["bash", "-c", f"echo hi; (sleep 3; touch {mark}) & exit 0"],
                   limit=1024, timeout=10)
    assert r.ok and r.out == "hi\n" and time.monotonic() - started < 3
    time.sleep(3.5)
    assert not mark.exists()


def test_a_bad_duration_is_refused_not_crashed_on(tmp_path):
    with pytest.raises(CtlError, match="not a duration"):
        signins.seconds("5m\n")
    folder = folder_with(tmp_path, "exampledocs.toml",
                         GOOD.replace("[status]\n", '[status]\nevery = "5m\\n"\n'))
    with pytest.raises(CtlError, match="exampledocs.toml: status: "):
        build(folder)


def test_a_notice_for_a_flow_without_its_own_status(ship):
    """A flow that signs in another file's key (here gcloud_adc) reads that key."""
    text = (SHIP / "signins" / "gcloud-adc.toml").read_text()
    text = text.replace('id = "gcloud-adc"', 'id = "adc-again"')
    text = text[:text.index("# The Ship's check")] + text[text.index("# Where the sign-in"):]
    add_file(ship, text, "adc-again.toml")
    ship.ctl("autostart", "--json")
    rc, doc, out = ship.ctl("notify-signin", "adc-again", "--json")
    assert rc == 0, out
    assert sorted(doc["data"]["sent"]) == ["grinder", "main"]
