"""ship-fleet machine-bundle NAME (machinebundle.py): a trusted machine's own bundle as
JSON (contracts/machine-bundle.schema.json), and the idle limit that goes with it
(ship-fleet trust NAME --allow-again / --idle-limit), its audit lines and attention.

Certificates are made by the real ssh-keygen with throwaway CA keys (caenv, from
test_ca); the clock is moved by replacing ca.utc_now. Nothing reaches a network.
"""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import timedelta

import pytest
from fl_harness import keygen
from fleetledger.concierge import audit
from test_ca import caenv, cakeys  # noqa: F401  (fixtures)
from test_cli import RUNNING, env  # noqa: F401  (env is a fixture)

from fleetledger import ca, handover, machinebundle, ops, paths, schemas, status, trustcmd, vault

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

DAY = timedelta(days=1)


def trust(e, tmp_path, name="laptop", *more):
    keygen(tmp_path / name, name)
    code, out, err = e.run("trust", name, "--key", str(tmp_path / f"{name}.pub"), *more)
    assert code == 0, err


def ask(e, name="laptop"):
    """machine-bundle NAME: (exit code, the document or None, stderr). A refusal must
    leave stdout empty."""
    code, out, err = e.run("machine-bundle", name)
    if code != 0:
        assert out == ""
        return code, None, err
    doc = json.loads(out)
    assert schemas.errors("machine-bundle", doc) == []
    return code, doc, err


def at(monkeypatch, when):
    monkeypatch.setattr(ca, "utc_now", lambda: when)


def field(e, name, key):
    return e.store.load().doc["machine"][name].get(key)


def set_field(e, name, key, value):
    with e.store.edit() as ed:
        ed.set_machine_field(name, key, value)


def audited():
    return [(a["service"], a["outcome"]) for a in audit.read() if a["action"] == "machine-bundle"]


class NoSigning:
    def sign(self, *a, **kw):
        raise AssertionError("signed")


# --- what it returns ------------------------------------------------------------------
def test_a_machine_gets_only_the_ships_its_scope_reaches_with_their_tags_and_runtime(
        caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path, "laptop", "--scope", "tag:work", "--logins", "agent")
    trust(caenv, tmp_path, "desk")  # all
    herdr = copy.deepcopy(RUNNING)
    herdr.setdefault("herdr", {})["runtime"] = "herdr"
    status.write_cache("ship-02", herdr)
    code, doc, err = ask(caenv, "laptop")
    assert code == 0, err
    assert doc["machine"] == "laptop" and doc["scope"] == ["tag:work"]
    assert doc["logins"] == ["agent"] and doc["ssh"] == {"user": "agent"}
    assert [s["name"] for s in doc["ships"]] == ["ship-01"]  # ship-02 is personal
    assert "ship-02" not in json.dumps(doc)
    assert doc["ships"][0]["tags"] == ["work"] and doc["ships"][0]["runtime"] == "core"
    assert doc["known_hosts"] == [f"@cert-authority ship-01,ship-01.local "
                                  f"{caenv.cakeys.host_public}"]
    assert doc["fleetledger"] is None  # FleetLedger records no VM of its own here
    assert doc["idle_limit"] == {"on": True, "days": 30}
    code, desk, _ = ask(caenv, "desk")
    assert [(s["name"], s["tags"], s["runtime"]) for s in desk["ships"]] == [
        ("ship-01", ["work"], "core"), ("ship-02", ["personal"], "herdr")]
    assert desk["ssh"] == {"user": "agent"} and desk["logins"] == ["agent", "owner"]


def test_each_ship_comes_with_its_lan_name_and_its_overlay_name(caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path, "desk")
    with caenv.store.edit() as ed:
        ed.set_field("ship-02", "names", ["ship-02.tailnet.example", "ship-02.local",
                                          "ship-02.example.net"])
        ed.set_field("ship-02", "overlay_node", "node0123")
    caenv.app.resolves = lambda host: host == "ship-02.example.net"
    _, doc, _ = ask(caenv, "desk")
    names = {s["name"]: s["host_names"] for s in doc["ships"]}
    assert names["ship-02"] == {"lan": "ship-02.example.net",
                                "overlay": "ship-02.tailnet.example"}
    assert names["ship-01"] == {"lan": "ship-01.local", "overlay": None}  # resolves nowhere


def test_fleetledgers_own_host_key_comes_with_it(caenv, tmp_path):  # noqa: F811
    from fleetledger import appliance

    trust(caenv, tmp_path)
    key = (tmp_path / "laptop.pub").read_text().split()  # any ed25519 key will do
    appliance.record_self(caenv.store, {"name": "fleetledger", "names": ["fleetledger.local"],
                                        "host_key": f"{key[0]} {key[1]}"})
    _, doc, _ = ask(caenv)
    assert doc["fleetledger"] == {"name": "fleetledger",
                                  "known_hosts": f"fleetledger,fleetledger.local {key[0]} "
                                                 f"{key[1]}"}


# --- the certificate: half its life ---------------------------------------------------
def test_it_signs_only_once_the_certificate_is_past_half_its_life(caenv, tmp_path,  # noqa: F811
                                                                 monkeypatch):
    trust(caenv, tmp_path)
    first = (paths.data_dir() / "trust" / "laptop" / "laptop-cert.pub").read_text().strip()
    serial = field(caenv, "laptop", "last_serial")
    _, doc, _ = ask(caenv)
    assert doc["certificate"]["line"] == first and doc["certificate"]["renewed"] is False
    assert doc["certificate"]["not_renewed"] is None
    until = ca.parse_stamp(doc["certificate"]["valid_before"])
    # Just before half its 7 days are gone: the same certificate, nothing signed.
    at(monkeypatch, until - timedelta(hours=84) - timedelta(minutes=1))
    caenv.app.signer = NoSigning()
    _, doc, _ = ask(caenv)
    assert doc["certificate"]["line"] == first and doc["certificate"]["renewed"] is False
    assert field(caenv, "laptop", "last_serial") == serial
    # Past half: a new one, recorded as `trust` records one, for the same lifetime.
    now = until - timedelta(hours=83)
    at(monkeypatch, now)
    caenv.app.signer = ca.Signer(agent_keys=lambda: set())
    _, doc, _ = ask(caenv)
    cert = doc["certificate"]
    assert cert["renewed"] is True and cert["line"] != first
    info = ca.cert_info(cert["line"])
    assert info.serial == field(caenv, "laptop", "last_serial") != serial
    assert ca.stamp(info.valid_before) == cert["valid_before"]
    assert cert["valid_before"] == ca.stamp(now + timedelta(hours=168))
    assert field(caenv, "laptop", "last_valid_before") == cert["valid_before"]
    assert [i["serial"] for i in field(caenv, "laptop", "issued")] == [serial, info.serial]
    assert sorted(info.principals) == ["agent:all", "owner:all"]
    # Asked again at once: that new one, not another.
    caenv.app.signer = NoSigning()
    _, again, _ = ask(caenv)
    assert again["certificate"]["line"] == cert["line"] and not again["certificate"]["renewed"]
    assert [o for _, o in audited()] == ["returned", "returned", "renewed", "returned"]


def test_a_renewal_checks_again_once_ships_toml_is_locked(
        caenv, tmp_path, monkeypatch):  # noqa: F811
    """Two asks at once: the second finds the first's certificate under the lock and
    signs none, so no issued certificate goes unrecorded."""
    trust(caenv, tmp_path)
    until = ca.parse_stamp(field(caenv, "laptop", "last_valid_before"))
    now = until - timedelta(hours=10)
    at(monkeypatch, now)
    cfg = ca.config(caenv.store.load())
    assert machinebundle._renew(caenv.app, caenv.store, "laptop", now, cfg) is True
    issued = field(caenv, "laptop", "issued")
    caenv.app.signer = NoSigning()
    assert machinebundle._renew(caenv.app, caenv.store, "laptop", now, cfg) is False
    assert field(caenv, "laptop", "issued") == issued


def test_a_certificate_it_does_not_hold_or_that_no_longer_fits_is_replaced(
        caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path, "laptop", "--scope", "tag:work")
    # trust --print-cert signed one FleetLedger did not keep: the kept one is stale.
    code, printed, _ = caenv.run("trust", "laptop", "--print-cert")
    assert code == 0
    _, doc, _ = ask(caenv)
    assert doc["certificate"]["renewed"] is True
    assert doc["certificate"]["line"] != printed.strip()
    # Narrowed to the agent's login (its wider certificate goes into the revocation
    # list; nothing new is signed): the kept one is for more than it may have now, so
    # it is never handed out again.
    kept = doc["certificate"]["line"]
    trustcmd.set_logins(caenv.app, caenv.store, "laptop", ["agent"], lambda line: None)
    assert ca.cert_info(kept).serial == field(caenv, "laptop", "last_serial")
    _, doc, _ = ask(caenv)
    assert doc["certificate"]["line"] != kept
    assert doc["certificate"]["renewed"] is True
    assert ca.cert_info(doc["certificate"]["line"]).principals == ("agent:tag:work",)


# --- the vault locked ------------------------------------------------------------------
def test_with_the_vault_locked_it_signs_nothing_and_says_why(
        caenv, tmp_path, monkeypatch):  # noqa: F811
    trust(caenv, tmp_path)
    first = (paths.data_dir() / "trust" / "laptop" / "laptop-cert.pub").read_text().strip()
    until = ca.parse_stamp(field(caenv, "laptop", "last_valid_before"))
    monkeypatch.setattr(vault, "state", lambda: "locked")
    caenv.app.signer = NoSigning()
    _, doc, _ = ask(caenv)  # not due: the current one, and nothing to say
    assert doc["certificate"]["line"] == first and doc["certificate"]["not_renewed"] is None
    at(monkeypatch, until - DAY)  # due: the current one still, and why it is not new
    _, doc, _ = ask(caenv)
    assert doc["certificate"]["line"] == first and doc["certificate"]["renewed"] is False
    assert doc["certificate"]["not_renewed"]["code"] == "vault-locked"
    assert "ship-fleet unlock" in doc["certificate"]["not_renewed"]["why"]
    assert [s["name"] for s in doc["ships"]] == ["ship-01", "ship-02"]
    at(monkeypatch, until + DAY)  # lapsed: no certificate at all, the rest as ever
    code, doc, _ = ask(caenv)
    assert code == 0 and doc["certificate"]["line"] is None
    assert doc["certificate"]["valid_before"] is None
    assert doc["certificate"]["not_renewed"]["code"] == "vault-locked"
    assert [s["name"] for s in doc["ships"]] == ["ship-01", "ship-02"]
    assert field(caenv, "laptop", "last_refreshed") == ca.stamp(until + DAY)
    assert audited()[-1] == ("laptop", "returned, not renewed: vault-locked")
    # Every other change still refuses while locked: allowing a machine again included.
    code, _, err = caenv.run("trust", "laptop", "--allow-again")
    assert code == 1 and "locked" in err


def test_a_really_locked_vault_signs_nothing_either(caenv, tmp_path, monkeypatch):  # noqa: F811
    trust(caenv, tmp_path)
    monkeypatch.setattr(vault, "SCRYPT_N", 2 ** 10)  # as test_vault's fast fixture
    monkeypatch.setattr(vault, "in_ram", lambda path: True)
    vault.create("correct horse battery staple")
    vault.lock()
    assert vault.state() == "locked"
    caenv.app.signer = NoSigning()
    at(monkeypatch, ca.parse_stamp(field(caenv, "laptop", "last_valid_before")) - DAY)
    code, doc, err = ask(caenv)
    assert code == 0, err
    assert doc["certificate"]["not_renewed"]["code"] == "vault-locked"
    assert doc["certificate"]["line"]


def test_a_ca_key_that_needs_a_passphrase_is_never_asked_for(
        caenv, tmp_path, monkeypatch):  # noqa: F811
    """No terminal, stdin closed: ssh-keygen fails at once instead of prompting."""
    trust(caenv, tmp_path)
    subprocess.run(["ssh-keygen", "-q", "-p", "-N", "secret-passphrase", "-P", "", "-f",
                    str(caenv.cakeys.user_key)], check=True, capture_output=True)
    caenv.app.signer = ca.Signer(runner=machinebundle.no_prompt_run, agent_keys=lambda: set())
    at(monkeypatch, ca.parse_stamp(field(caenv, "laptop", "last_valid_before")) - DAY)
    monkeypatch.setattr(sys, "stdin", io.StringIO("secret-passphrase\n"))
    code, doc, _ = ask(caenv)
    assert code == 0
    assert doc["certificate"]["renewed"] is False and doc["certificate"]["line"]
    assert doc["certificate"]["not_renewed"]["code"] == "sign-failed"
    assert sys.stdin.read() == "secret-passphrase\n"  # nothing read it
    assert audited()[-1] == ("laptop", "returned, not renewed: sign-failed")


def test_no_prompt_run_closes_stdin_and_has_no_terminal(monkeypatch):
    monkeypatch.setenv("SSH_ASKPASS", "/bin/false")
    proc = machinebundle.no_prompt_run(
        [sys.executable, "-c", "import os, sys; print(repr(sys.stdin.read()), "
         "os.getsid(0) == os.getpid(), os.environ.get('SSH_ASKPASS'), "
         "os.environ['SSH_ASKPASS_REQUIRE'])"], capture_output=True, text=True)
    assert proc.stdout.split() == ["''", "True", "None", "never"]


# --- who may ask ------------------------------------------------------------------------
def test_an_unknown_or_untrusted_machine_gets_nothing_and_the_same_words(
        caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path, "laptop")
    code, _, unknown = ask(caenv, "tablet")
    assert code == 1 and "No trusted machine called tablet" in unknown
    code, _, err = caenv.run("untrust", "laptop", "--yes")
    assert code == 0, err
    code, _, untrusted = ask(caenv, "laptop")
    assert code == 1
    assert untrusted.replace("laptop", "X") == unknown.replace("tablet", "X")
    for odd_name in ("../laptop", "laptop\n", "Laptop"):
        code, _, odd = ask(caenv, odd_name)
        assert code == 1 and "laptop" not in odd.lower()
    assert audited() == [("tablet", "refused: unknown"), ("laptop", "refused: untrusted"),
                         *[("-", "refused: not a machine name")] * 3]


def test_it_takes_no_options_reads_no_stdin_and_ignores_the_ssh_command(
        caenv, tmp_path, monkeypatch):  # noqa: F811
    trust(caenv, tmp_path, "laptop", "--scope", "tag:work")
    trust(caenv, tmp_path, "desk")
    for extra in (["--json"], ["desk"], ["--scope", "all"], ["--machine", "desk"]):
        with pytest.raises(SystemExit) as exc:
            caenv.run("machine-bundle", "laptop", *extra)
        assert exc.value.code == 2

    class Untouchable(io.StringIO):
        def read(self, *a):
            raise AssertionError("stdin read")

        readline = read

    monkeypatch.setattr(sys, "stdin", Untouchable())
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "ship-fleet machine-bundle desk")
    _, doc, _ = ask(caenv, "laptop")
    assert doc["machine"] == "laptop" and [s["name"] for s in doc["ships"]] == ["ship-01"]


def test_it_writes_only_its_own_record_and_audit_line(caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path, "laptop")
    trust(caenv, tmp_path, "desk")
    before = caenv.store.load().doc
    _, doc, _ = ask(caenv, "laptop")
    after = caenv.store.load().doc
    assert after["machine"]["laptop"].pop("last_refreshed") == doc["at"]
    assert after == before
    # Asked again within minutes: ships.toml is not written again.
    mtime = caenv.store.path.stat().st_mtime_ns
    ask(caenv, "laptop")
    assert caenv.store.path.stat().st_mtime_ns == mtime
    assert audited() == [("laptop", "returned"), ("laptop", "returned")]


# --- the idle limit ----------------------------------------------------------------------
def test_a_machine_idle_for_30_days_is_refused_until_allowed_again(caenv, tmp_path,  # noqa: F811
                                                                  monkeypatch):
    trust(caenv, tmp_path)
    real = ca.utc_now
    start = real()
    at(monkeypatch, start)
    ask(caenv)
    assert field(caenv, "laptop", "last_refreshed") == ca.stamp(start)
    at(monkeypatch, start + timedelta(days=30) - timedelta(minutes=1))
    assert ask(caenv)[0] == 0  # in daily use: never refused
    asked = start + timedelta(days=30) - timedelta(minutes=1)
    later = asked + timedelta(days=30, minutes=1)
    at(monkeypatch, later)
    code, _, err = ask(caenv)
    assert code == 1
    assert f"laptop has not asked for its bundle since {ca.stamp(asked)[:10]}" in err
    assert "over 30 days" in err and "ship-fleet trust laptop --allow-again" in err
    assert field(caenv, "laptop", "idle_refused") == ca.stamp(later)
    assert field(caenv, "laptop", "last_refreshed") == ca.stamp(asked)  # refusals do not count
    at(monkeypatch, later + DAY)
    assert ask(caenv)[0] == 1
    assert field(caenv, "laptop", "idle_refused") == ca.stamp(later)  # the first refusal
    assert audited()[-1] == ("laptop", f"refused: idle since {ca.stamp(asked)}")
    # Allowed again: one command, which signs nothing; then it asks as before.
    monkeypatch.setattr(ca, "utc_now", real)
    caenv.app.signer = NoSigning()
    code, out, err = caenv.run("trust", "laptop", "--allow-again")
    assert code == 0, err
    assert "may ask for its bundle again" in out
    assert field(caenv, "laptop", "idle_refused") is None
    assert field(caenv, "laptop", "refresh_allowed")
    caenv.app.signer = ca.Signer(agent_keys=lambda: set())
    assert ask(caenv)[0] == 0
    entries = [(a["action"], a["service"]) for a in audit.read()]
    assert ("trust-allow-again", "laptop") in entries


def test_with_the_idle_limit_off_a_machine_stays_trusted_until_untrusted(
        caenv, tmp_path, monkeypatch):  # noqa: F811
    trust(caenv, tmp_path)
    set_field(caenv, "laptop", "last_refreshed", ca.stamp(ca.utc_now() - timedelta(days=90)))
    assert ask(caenv)[0] == 1
    code, out, err = caenv.run("trust", "laptop", "--idle-limit", "off")
    assert code == 0, err
    assert "stays trusted until you untrust it" in out
    assert field(caenv, "laptop", "idle_limit") is False
    assert field(caenv, "laptop", "idle_refused") is None
    code, doc, _ = ask(caenv)
    assert code == 0 and doc["idle_limit"] == {"on": False, "days": 30}
    set_field(caenv, "laptop", "last_refreshed", ca.stamp(ca.utc_now() - timedelta(days=400)))
    assert ask(caenv)[0] == 0
    code, out, _ = caenv.run("trust", "laptop", "--idle-limit", "on")
    assert code == 0 and field(caenv, "laptop", "idle_limit") is None  # on: the default
    set_field(caenv, "laptop", "last_refreshed", ca.stamp(ca.utc_now() - timedelta(days=31)))
    assert ask(caenv)[0] == 1


def test_allow_again_and_idle_limit_change_only_the_record(caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path)
    for more in (["--scope", "all"], ["--print-cert"], ["--hours", "2"], ["--install"]):
        code, _, err = caenv.run("trust", "laptop", "--allow-again", *more)
        assert code == 1 and "change only the record" in err
    code, _, err = caenv.run("trust", "tablet", "--allow-again")
    assert code == 1 and "No trusted machine called tablet" in err
    caenv.run("untrust", "laptop", "--yes")
    code, _, err = caenv.run("trust", "laptop", "--idle-limit", "off")
    assert code == 1 and "No trusted machine called laptop" in err


def test_a_machine_trusted_before_the_idle_limit_starts_counting_at_its_first_ask(
        caenv, tmp_path, monkeypatch):  # noqa: F811
    """No last_refreshed, trusted long ago, never asked: its first ask is served and
    starts its 30 days, so the upgrade locks no machine out."""
    trust(caenv, tmp_path)
    set_field(caenv, "laptop", "added", "2025-01-01T00:00:00Z")
    assert field(caenv, "laptop", "last_refreshed") is None
    now = ca.utc_now()
    at(monkeypatch, now)
    assert ask(caenv)[0] == 0
    assert field(caenv, "laptop", "last_refreshed") == ca.stamp(now)
    at(monkeypatch, now + timedelta(days=31))
    assert ask(caenv)[0] == 1


def test_fleetledgers_own_computer_has_no_idle_limit(caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path, "desktop")
    paths.write_private(paths.config_dir() / trustcmd.THIS_COMPUTER,
                        json.dumps({"machine": "desktop", "identity": "~/.ssh/x"}).encode())
    set_field(caenv, "desktop", "last_refreshed",
              ca.stamp(ca.utc_now() - timedelta(days=90)))
    code, doc, err = ask(caenv, "desktop")
    assert code == 0, err
    assert doc["idle_limit"]["on"] is False
    assert not [i for i in ops.machine_cert_attention(caenv.store.load(),
                                                      caenv.store.load().ships())]


# --- a copy on standby or retired ---------------------------------------------------------
@pytest.mark.parametrize("role", ["standby", "retired"])
def test_a_handed_over_copy_refuses_and_says_where_the_fleet_is(
        caenv, tmp_path, role):  # noqa: F811
    trust(caenv, tmp_path)
    handover.write(caenv.store, handover.Marker(role, handover.now(), "fleetledger.example", 1))
    code, _, err = ask(caenv)
    assert code == 1 and "fleetledger.example" in err
    assert field(caenv, "laptop", "last_refreshed") is None
    assert audited() == []  # this copy does not act, not even to write its audit log


# --- attention ------------------------------------------------------------------------------
def _items(e):
    code, out, err = e.run("attention", "--json", "--offline")
    assert code == 0, err
    return {i["subject"]: i for i in json.loads(out) if i["ship"] is None
            and i["code"].startswith("machine-")}


def test_a_machine_that_stopped_asking_needs_attention_once(
        caenv, tmp_path, monkeypatch):  # noqa: F811
    trust(caenv, tmp_path, "laptop")
    trust(caenv, tmp_path, "desk")  # never asks: no refresher, nothing to say
    ask(caenv, "laptop")
    assert _items(caenv) == {}
    asked = ca.parse_stamp(field(caenv, "laptop", "last_refreshed"))
    monkeypatch.setattr(ca, "utc_now", lambda: asked + timedelta(days=3, hours=23))
    assert _items(caenv) == {}  # a long weekend switched off
    monkeypatch.setattr(ca, "utc_now", lambda: asked + timedelta(days=4, hours=1))
    items = _items(caenv)
    assert set(items) == {"laptop"}
    item = items["laptop"]
    assert (item["code"], item["severity"]) == ("machine-refresh-stopped", "medium")
    assert "has not asked for it since" in item["message"] and "(4 days)" in item["message"]
    assert "refused its bundle until you allow it again" in item["message"]
    assert "ship-fleet machine-bundle laptop" in item["fix"]
    # Its certificate in its last day too: one item, the certificate's, saying why.
    monkeypatch.setattr(ca, "utc_now", lambda: asked + timedelta(days=6, hours=12))
    items = _items(caenv)
    assert {n: i["code"] for n, i in items.items()} == {"laptop": "machine-cert-expiring",
                                                         "desk": "machine-cert-expiring"}
    assert "Download bundle" in items["desk"]["fix"]  # renewed by hand, as before
    assert "renews its certificate by asking for its bundle" in items["laptop"]["fix"]
    assert "Download bundle" not in items["laptop"]["fix"]
    monkeypatch.setattr(vault, "state", lambda: "locked")
    assert "vault is locked" in _items(caenv)["laptop"]["fix"]


def test_a_machine_refused_for_idleness_needs_attention_once(
        caenv, tmp_path, monkeypatch):  # noqa: F811
    trust(caenv, tmp_path)
    set_field(caenv, "laptop", "last_refreshed", ca.stamp(ca.utc_now() - timedelta(days=31)))
    assert ask(caenv)[0] == 1
    set_field(caenv, "laptop", "last_valid_before", ca.stamp(ca.utc_now() - DAY))  # lapsed
    items = _items(caenv)
    assert [i["code"] for i in items.values()] == ["machine-refresh-refused"]
    assert "over 30 days" in items["laptop"]["message"]
    assert "ship-fleet trust laptop --allow-again" in items["laptop"]["fix"]
    code, out, _ = caenv.run("doctor", "--offline")
    assert "ship-fleet trust laptop --allow-again" in out
    code, out, _ = caenv.run("ca", "show")
    assert "refused its bundle since" in out
    caenv.run("trust", "laptop", "--allow-again")
    assert [i["code"] for i in _items(caenv).values()] == ["machine-cert-lapsed"]


def test_ca_show_says_when_each_machine_last_asked(caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path, "laptop")
    trust(caenv, tmp_path, "desk")
    caenv.run("trust", "desk", "--idle-limit", "off")
    _, doc, _ = ask(caenv, "laptop")
    code, out, _ = caenv.run("ca", "show")
    lines = {x.split()[0]: x for x in out.splitlines() if x.startswith("  ") and "until" in x}
    assert f"last asked {doc['at'][:16].replace('T', ' ')} UTC" in lines["laptop"]
    assert "no idle limit" in lines["desk"] and "last asked" not in lines["desk"]
    machines = {m["machine"]: m for m in json.loads(caenv.run("ca", "show", "--json")[1])
                ["machines"]}
    assert machines["laptop"]["last_refreshed"] == doc["at"]
    assert machines["desk"]["idle_limit"] is False


def test_the_bundle_is_never_written_to_a_script_or_a_file_but_its_certificate(caenv,  # noqa: F811
                                                                              tmp_path):
    trust(caenv, tmp_path)
    folder = paths.data_dir() / "trust" / "laptop"
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    set_field(caenv, "laptop", "last_serial", 1)  # the kept certificate no longer counts
    _, doc, _ = ask(caenv)
    after = {p.name: p.read_bytes() for p in folder.iterdir()}
    assert set(after) == set(before)
    changed = {n for n in after if after[n] != before[n]}
    assert changed == {"laptop-cert.pub"}
    assert after["laptop-cert.pub"].decode().strip() == doc["certificate"]["line"]
    assert os.stat(folder / "laptop-cert.pub").st_mode & 0o777 == 0o600
