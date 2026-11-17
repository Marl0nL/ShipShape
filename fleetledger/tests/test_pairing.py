"""Pairing by code (pairing.py, paircmd.py): an unknown computer asks through the well-known
pairing key's forced line, and you approve it by typing its code.

The first part runs FleetLedger in-process as on its own VM (test_authkeys' vm), asking as
sshd would (SSH_ORIGINAL_COMMAND, SSH_CONNECTION). The second, test_pairing_e2e, runs the
installer against a real, unprivileged sshd on 127.0.0.1. Nothing touches this computer's
real ~/.ssh, sshd or FleetLedger.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fl_harness import keygen
from test_authkeys import ak, seed, vm  # noqa: F401  (vm is a fixture)
from test_ca import caenv, cakeys  # noqa: F401  (fixtures)
from test_cli import env  # noqa: F401  (a fixture)

from fleetledger import authkeys, ca, firstrun, pairing, schemas

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

REPO = Path(__file__).resolve().parents[2]
ADDRESS = "192.0.2.10"


def new_key(e, name: str) -> str:
    """A computer's own key: its public line's base64."""
    keygen(e.tmp / name, name)
    return (e.tmp / f"{name}.pub").read_text().split()[1]


def asks(e, monkeypatch, command: str, source: str = ADDRESS) -> tuple[int, dict]:
    """What the forced command answers, as sshd would run it."""
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    monkeypatch.setenv("SSH_CONNECTION", f"{source} 50000 192.0.2.1 22")
    code, out, err = e.run(pairing.VERB)
    assert err == ""  # nothing of FleetLedger's own on stderr either
    lines = out.splitlines()
    assert len(lines) == 1, out
    doc = json.loads(lines[0])
    assert schemas.errors("machine-pair", doc) == [], doc
    return code, doc


def request(e, monkeypatch, blob: str, host: str = "laptop", **kw) -> dict:
    code, doc = asks(e, monkeypatch, f"request {blob} {host}", **kw)
    assert code == 0 and doc["state"] == "pending", doc
    return doc


@pytest.fixture
def on(vm, monkeypatch):  # noqa: F811
    """FleetLedger on its own VM with pairing by code turned on."""
    seed()
    code, out, err = vm.run("pair", "on")
    assert code == 0, out + err
    return vm


def later(monkeypatch, minutes: float) -> None:
    now = ca.utc_now()
    monkeypatch.setattr(ca, "utc_now", lambda: now + timedelta(minutes=minutes))


def block() -> list[str]:
    f = authkeys.read()
    return [x for _, x in f.block]


# --- on and off ----------------------------------------------------------------------------------
def test_pairing_is_off_by_default_and_on_adds_one_forced_line_alone(vm, monkeypatch):  # noqa: F811
    before = seed()
    assert pairing.is_on(vm.store.load()) is False
    authkeys.sync(vm.store)
    assert ak().read_bytes() == before  # off: no line at all
    assert firstrun.pairing_items(vm.app, vm.store.load()) == []  # and doctor says nothing
    code, out, err = vm.run("pair", "on")
    assert code == 0, out + err
    assert block() == [authkeys.pairing_line()]
    assert block()[0] == (f'restrict,command="{authkeys.SHIP_FLEET} machine-pair" '
                          f"ssh-ed25519 {pairing.BLOB} shipshape-pairing")
    assert ak().read_bytes().startswith(before)  # your own lines as they were
    assert "Pairing key line added" in out and "phone is out of scope" in out
    (item,) = firstrun.pairing_items(vm.app, vm.store.load())
    assert item.state == "ok" and "0 waiting" in item.detail
    code, out, err = vm.run("pair", "off")
    assert code == 0, out + err
    assert ak().read_bytes() == before
    assert "Pairing key line removed" in out


def test_while_off_or_away_from_its_own_vm_nobody_can_ask(vm, monkeypatch):  # noqa: F811
    blob = new_key(vm, "laptop")
    code, doc = asks(vm, monkeypatch, f"request {blob} laptop")
    assert (code, doc["state"]) == (1, "off")
    assert not pairing.requests_path().exists()
    seed()
    assert vm.run("pair", "on")[0] == 0
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "desktop")  # not FleetLedger's VM
    code, doc = asks(vm, monkeypatch, f"request {blob} laptop")
    assert (code, doc["state"]) == (1, "off")
    code, out, err = vm.run("pair", "on")
    assert code == 1 and "own VM" in err
    authkeys.sync(vm.store)
    assert block() == []  # granting less is always allowed: its line goes there


# --- what an unknown computer can do -----------------------------------------------------------
def test_a_request_is_filed_and_says_nothing_of_the_fleet(on, monkeypatch):
    blob = new_key(on, "laptop")
    doc = request(on, monkeypatch, blob)
    assert set(doc) == {"schema", "state", "id", "token", "nonce", "code", "expires_in"}
    assert doc["code"] == pairing.code_for(doc["nonce"], blob)
    assert doc["expires_in"] == 600
    said = json.dumps(doc)
    inv = on.store.load()
    for word in [s.name for s in inv.ships()] + [m.name for m in ca.machines(inv)] + ["ship-0"]:
        assert word not in said
    # Kept: the token's hash, never the token; 0600 in a 0700 folder.
    stored = pairing.requests_path()
    assert doc["token"] not in stored.read_text()
    assert stored.stat().st_mode & 0o777 == 0o600
    assert stored.parent.stat().st_mode & 0o777 == 0o700
    # Its status and withdrawal need the token; any other token learns nothing.
    assert asks(on, monkeypatch, f"status {'0' * 32}")[1] == {"schema": pairing.SCHEMA,
                                                              "state": "unknown"}
    assert asks(on, monkeypatch, f"status {doc['token']}")[1]["state"] == "pending"
    assert asks(on, monkeypatch, f"cancel {'0' * 32}")[1]["state"] == "unknown"
    assert len(pairing.listing()) == 1
    assert asks(on, monkeypatch, f"cancel {doc['token']}")[1]["state"] == "unknown"
    # Withdrawn: nothing of it left (the hour's count of requests filed stays, no more).
    assert pairing.read().requests == [] and blob not in stored.read_text()
    assert set(json.loads(stored.read_text())) == {"requests", "filed"}


HOSTILE = [
    "",
    "sh -c id",
    "machine-bundle laptop",
    "request {b} laptop; touch /tmp/x",
    "request {b} $(id)",
    "request {b} ../../etc",
    "request {b} Laptop",
    "request {b} -laptop",
    "request {b} " + "a" * 40,
    "request {b} lap\ttop",
    "request {b} laptop extra",
    "request {b}",
    "request {b}\nrequest {b} laptop",
    "request {b} laptöp",
    "request {b} laptop\x1b[2J",
    "request {b}X laptop",
    "request AAAAB3NzaC1yc2EAAAADAQABAAABAQ laptop",
    "request " + pairing.BLOB + " laptop",  # the pairing key itself
    "request {b} laptop" + " " * 300,
    "status ../../../ships.toml",
    "status " + "0" * 31,
    "cancel " + "g" * 32,
]


@pytest.mark.parametrize("command", HOSTILE)
def test_a_hostile_command_is_refused_and_nothing_is_written(on, monkeypatch, command):
    blob = new_key(on, "laptop")
    keys_before = ak().read_bytes()
    code, doc = asks(on, monkeypatch, command.format(b=blob))
    assert code == 1 and doc == {"schema": pairing.SCHEMA, "state": "refused",
                                 "why": pairing.BAD}
    assert not pairing.requests_path().exists()
    assert ak().read_bytes() == keys_before


def test_a_flood_is_bounded_and_the_store_stays_small(on, monkeypatch):
    blobs = [new_key(on, f"c{i}") for i in range(30)]
    # From one address: MAX_PER_SOURCE at once.
    for b in blobs[:pairing.MAX_PER_SOURCE]:
        request(on, monkeypatch, b)
    code, doc = asks(on, monkeypatch, f"request {blobs[2]} laptop")
    assert (code, doc["state"]) == (1, "busy") and set(doc) == {"schema", "state", "why"}
    # From many addresses: MAX_PENDING at once, all told.
    for i, b in enumerate(blobs[2:pairing.MAX_PENDING]):
        request(on, monkeypatch, b, source=f"198.51.100.{i + 1}")
    code, doc = asks(on, monkeypatch, f"request {blobs[9]} laptop", source="203.0.113.9")
    assert (code, doc["state"]) == (1, "busy")
    assert pairing.requests_path().stat().st_size < 8 * 1024
    assert len(pairing.read().requests) <= pairing.MAX_PENDING * 4


def test_the_hourly_limit_holds_even_when_each_request_is_withdrawn(on, monkeypatch):
    b = new_key(on, "laptop")
    for _ in range(pairing.MAX_PER_HOUR):
        doc = request(on, monkeypatch, b)
        asks(on, monkeypatch, f"cancel {doc['token']}")
    code, doc = asks(on, monkeypatch, f"request {b} laptop")
    assert (code, doc["state"]) == (1, "busy")
    later(monkeypatch, 61)
    request(on, monkeypatch, b)


def test_a_store_that_is_not_fleetledgers_counts_as_empty(on, monkeypatch):
    p = pairing.requests_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"requests": [{"id": "p000000", "state": "approved",
                                           "token_sha256": "0" * 64, "asked": "x",
                                           "expires": "y", "machine": "laptop; id"}]}))
    assert pairing.read().requests == []
    p.write_text("x" * (pairing.MAX_STORE + 1))
    assert pairing.read().requests == []
    request(on, monkeypatch, new_key(on, "laptop"))  # and the next change writes it whole
    assert len(pairing.read().requests) == 1


def test_a_request_lapses_and_leaves_nothing(on, monkeypatch):
    blob = new_key(on, "laptop")
    doc = request(on, monkeypatch, blob)
    later(monkeypatch, 11)
    assert asks(on, monkeypatch, f"status {doc['token']}")[1]["state"] == "unknown"
    assert pairing.listing() == []
    code, out, err = on.run("pair", "approve", doc["id"], "--code", doc["code"], "--yes")
    assert code == 1 and "No pairing request" in err
    request(on, monkeypatch, new_key(on, "other"))  # any change: the lapsed one is not kept
    assert blob not in pairing.requests_path().read_text()


def test_a_pending_request_grants_nothing(on, monkeypatch):
    blob = new_key(on, "laptop")
    request(on, monkeypatch, blob)
    assert not any(blob in x for x in block())  # no line for its key: it cannot log in
    inv = on.store.load()
    assert ca.machine(inv, "laptop") is None
    code, out, err = on.run("machine-bundle", "laptop")
    assert code == 1 and out == "" and "No trusted machine called laptop" in err
    assert not (ca.bundle_dir("laptop")).exists()


# --- approving and refusing ----------------------------------------------------------------------
def test_approval_needs_the_code_typed_and_ends_in_trust(on, monkeypatch):
    blob = new_key(on, "laptop")
    doc = request(on, monkeypatch, blob)
    before = block()
    wrong = f"{(int(doc['code']) + 1) % 10 ** 6:06d}"
    code, out, err = on.run("pair", "approve", doc["id"], "--code", wrong, "--yes")
    assert code == 1 and "not this request's code" in err
    assert ca.machine(on.store.load(), "laptop") is None and block() == before
    # No terminal and no code: refused, never guessed.
    code, out, err = on.run("pair", "approve", doc["id"], "--yes")
    assert code == 1 and "--code" in err
    # The code as the computer shows it, with its space.
    code, out, err = on.run("pair", "approve", doc["id"], "--code", pairing.code_said(doc["code"]),
                            "--yes")
    assert code == 0, out + err
    assert "calls itself 'laptop' (its own claim, unchecked)" in out
    assert re.search(r"its key: +ED25519 SHA256:", out)
    assert "FleetLedger's host key, which that computer pinned: ED25519 SHA256:" in out
    assert (f"Approving runs: ship-fleet trust laptop --key 'ssh-ed25519 {blob}' --scope all "
            "--logins both") in out
    assert "sh -c '" not in out  # no line to paste: the installer there finishes
    m = ca.machine(on.store.load(), "laptop")
    assert m.public_key == f"ssh-ed25519 {blob}" and list(m.scope) == ["all"]
    assert list(m.logins) == list(ca.LOGINS)
    # authorized_keys: the one approved line, beside the pairing key's; nothing else.
    assert block() == [authkeys.line_for("laptop", m.public_key), authkeys.pairing_line()]
    # The computer waiting learns it, and how to finish, and nothing more.
    answer = asks(on, monkeypatch, f"status {doc['token']}")[1]
    assert answer["state"] == "approved" and answer["machine"] == "laptop"
    me = on.store.load().doc["fleet"]["self"]
    assert answer["fleetledger"]["host_key"] == " ".join(me["host_key"].split()[:2])
    import hashlib

    from fleetledger import refresher

    assert answer["refresher_sha256"] == hashlib.sha256(refresher.script()).hexdigest()
    # Kept until it would have lapsed: no key, name claimed or address.
    (r,) = pairing.read().requests
    assert r.key is None and r.host is None and r.source is None and r.nonce is None
    later(monkeypatch, 11)
    assert asks(on, monkeypatch, f"status {doc['token']}")[1]["state"] == "unknown"


def test_a_terminal_asks_the_code_then_name_scope_and_logins_with_defaults(on, monkeypatch):
    blob = new_key(on, "laptop")
    doc = request(on, monkeypatch, blob, host="work-laptop")
    typed = iter([doc["code"], "", "tag:work", "agent", "y"])
    asked = []

    def confirm(prompt):
        asked.append(prompt)
        return next(typed)

    on.app.stdin_isatty = lambda: True
    on.app.confirm = confirm
    code, out, err = on.run("pair", "approve")  # the one waiting: no id needed
    assert code == 0, out + err
    assert asked[0] == "Type the code the new computer shows: "
    assert "[work-laptop]" in asked[1] and "[all]" in asked[2] and "(both)" in asked[3]
    m = ca.machine(on.store.load(), "work-laptop")
    assert list(m.scope) == ["tag:work"] and list(m.logins) == ["agent"]


def test_a_second_computer_asking_meanwhile_is_not_what_gets_approved(on, monkeypatch):
    mine = new_key(on, "laptop")
    theirs = new_key(on, "intruder")
    a = request(on, monkeypatch, mine)
    b = request(on, monkeypatch, theirs, host="laptop", source="198.51.100.66")  # same claim
    assert a["code"] != b["code"]
    rows = pairing.listing()
    assert [r["host"] for r in rows] == ["laptop", "laptop"]
    assert rows[0]["fingerprint"] != rows[1]["fingerprint"]
    # Your code, on their request: refused.
    code, out, err = on.run("pair", "approve", b["id"], "--code", a["code"], "--yes")
    assert code == 1 and "not this request's code" in err
    # Your code, with no id: it finds your request, and only yours is trusted.
    code, out, err = on.run("pair", "approve", "--code", a["code"], "--yes")
    assert code == 0, out + err
    assert ca.machine(on.store.load(), "laptop").public_key == f"ssh-ed25519 {mine}"
    assert not any(theirs in x for x in block())
    assert [r["id"] for r in pairing.listing()] == [b["id"]]  # theirs still only waits
    assert asks(on, monkeypatch, f"status {b['token']}")[1]["state"] == "pending"
    # Their code was never shown here: the list and ca show hold none.
    code, out, err = on.run("pair", "ls")
    assert b["code"] not in out and b["token"] not in out and b["nonce"] not in out


def test_two_requests_with_one_code_are_never_approved_by_it(on, monkeypatch):
    monkeypatch.setattr(pairing, "code_for", lambda nonce, blob: "123456")
    request(on, monkeypatch, new_key(on, "laptop"))
    request(on, monkeypatch, new_key(on, "other"), source="198.51.100.2")
    code, out, err = on.run("pair", "approve", "--code", "123456", "--yes")
    assert code == 1 and "neither is approved" in err
    assert ca.machine(on.store.load(), "laptop") is None
    assert ca.machine(on.store.load(), "other") is None
    assert len(pairing.listing()) == 2


def test_approval_keeps_every_rule_of_trust(on, monkeypatch):
    # A key FleetLedger already trusts, asking again under another name: trust refuses it.
    blob = new_key(on, "laptop")
    assert on.run("trust", "laptop", "--key", f"ssh-ed25519 {blob}")[0] == 0
    doc = request(on, monkeypatch, blob, host="laptop2")
    code, out, err = on.run("pair", "approve", doc["id"], "--code", doc["code"], "--yes")
    assert code == 1 and "already trusted under another name" in err
    assert [r["id"] for r in pairing.listing()] == [doc["id"]]  # still only waits
    # A name taken, or not a machine name: never used.
    other = new_key(on, "other")
    doc = request(on, monkeypatch, other, host="laptop")
    code, out, err = on.run("pair", "approve", doc["id"], "--code", doc["code"], "--yes")
    assert code == 1 and "--name" in err
    for name in ("evil; touch x", "-x", "--install"):
        code, out, err = on.run("pair", "approve", doc["id"], "--code", doc["code"], "--yes",
                                f"--name={name}")
        assert code == 1 and "a-z, 0-9 and -" in err
    for scope in ("--install", "tag:Work", "all;id"):
        code, out, err = on.run("pair", "approve", doc["id"], "--code", doc["code"], "--yes",
                                "--name", "other", f"--scope={scope}")
        assert code == 1 and "tag:TAG or ship:NAME" in err
    assert ca.machine(on.store.load(), "other") is None
    # The pairing key itself is never a machine's.
    code, out, err = on.run("trust", "anyone", "--key", pairing.PUBLIC_KEY)
    assert code == 1 and "well-known pairing key" in err


def test_a_locked_vault_refuses_the_approval_but_not_the_request(on, monkeypatch):
    from fleetledger import vault

    blob = new_key(on, "laptop")
    monkeypatch.setattr(vault, "state", lambda: "locked")
    doc = request(on, monkeypatch, blob)
    code, out, err = on.run("pair", "approve", doc["id"], "--code", doc["code"], "--yes")
    assert code == 1 and "vault is locked" in err
    assert on.run("pair", "ls")[0] == 0
    assert on.run("pair", "refuse", doc["id"])[0] == 0


def test_a_refused_request_keeps_only_the_word(on, monkeypatch):
    blob = new_key(on, "laptop")
    doc = request(on, monkeypatch, blob)
    code, out, err = on.run("pair", "refuse", doc["id"])
    assert code == 0, out + err
    (r,) = pairing.read().requests
    assert r.as_dict().keys() == {"id", "token_sha256", "state", "asked", "expires"}
    assert blob not in pairing.requests_path().read_text()
    assert asks(on, monkeypatch, f"status {doc['token']}")[1] == {"schema": pairing.SCHEMA,
                                                                 "state": "refused"}
    later(monkeypatch, 11)
    assert asks(on, monkeypatch, f"status {doc['token']}")[1]["state"] == "unknown"
    assert ca.machine(on.store.load(), "laptop") is None
    assert not any(blob in x for x in block())


def test_pair_off_forgets_every_request(on, monkeypatch):
    doc = request(on, monkeypatch, new_key(on, "laptop"))
    assert on.run("pair", "off")[0] == 0
    assert not pairing.requests_path().exists()
    assert asks(on, monkeypatch, f"status {doc['token']}")[1]["state"] == "off"


def test_ca_show_lists_them_for_you_without_the_code(on, monkeypatch):
    doc = request(on, monkeypatch, new_key(on, "laptop"))
    code, out, err = on.run("ca", "show", "--json")
    assert code == 0, err
    shown = json.loads(out)
    assert schemas.errors("fleetledger-ca", shown) == []
    (row,) = shown["pairing"]["pending"]
    assert row["id"] == doc["id"] and row["host"] == "laptop" and row["source"] == ADDRESS
    for secret in (doc["code"], doc["token"], doc["nonce"]):
        assert secret not in out
    code, out, err = on.run("ca", "show")
    assert "Pairing by code: on, 1 computer(s) waiting" in out
    code, out, err = on.run("pair", "ls")
    assert f"{doc['id']}  calls itself 'laptop'" in out and doc["code"] not in out


# --- doctor: a misconfigured entry point --------------------------------------------------------
def test_doctor_says_when_the_pairing_line_is_wrong(vm, monkeypatch):  # noqa: F811
    seed()
    assert vm.run("pair", "on")[0] == 0
    path = ak()
    good = path.read_text()
    # Changed by hand (a wider line for the same key): fail, and sync puts it back.
    path.write_text(good.replace('restrict,command=', 'command='))
    (item,) = firstrun.pairing_items(vm.app, vm.store.load())
    assert item.state == "fail" and "not the one FleetLedger writes" in item.detail
    authkeys.sync(vm.store)
    assert path.read_text() == good
    # Missing while on.
    path.write_text(good.replace(authkeys.pairing_line() + "\n", ""))
    (item,) = firstrun.pairing_items(vm.app, vm.store.load())
    assert item.state == "missing" and item.next == authkeys.SYNC
    # There while off: an unknown computer could still ask.
    assert vm.run("pair", "off")[0] == 0
    path.write_text(good)
    (item,) = firstrun.pairing_items(vm.app, vm.store.load())
    assert item.state == "fail" and "while pairing is off" in item.detail
    authkeys.sync(vm.store)
    assert pairing.BLOB not in path.read_text()


def test_a_line_of_yours_for_the_pairing_key_is_refused_and_reported(vm, monkeypatch):  # noqa: F811
    seed(f"{pairing.PUBLIC_KEY}\n")
    code, out, err = vm.run("pair", "on")
    assert code == 1 and "lets anybody in" in err
    assert pairing.is_on(vm.store.load()) is False
    (item,) = firstrun.pairing_items(vm.app, vm.store.load())
    assert item.state == "fail" and "anybody may have" in item.detail


# --- the rest ------------------------------------------------------------------------------------
def test_the_installer_holds_the_pairing_keys_private_half(tmp_path):
    text = (REPO / "shipshape-setup").read_text()
    body = re.search(r"^PAIR_KEY_BODY='([A-Za-z0-9+/=\n]+)'$", text, re.M).group(1)
    key = tmp_path / "pairing"
    key.write_text(f"-----BEGIN OPENSSH {'PRIVATE KEY'}-----\n{body}\n"
                   f"-----END OPENSSH {'PRIVATE KEY'}-----\n")
    key.chmod(0o600)
    pub = subprocess.run(["ssh-keygen", "-y", "-f", str(key)], capture_output=True, text=True,
                         check=True, stdin=subprocess.DEVNULL).stdout.split()
    assert pub[:2] == pairing.PUBLIC_KEY.split()[:2]


def test_the_code_is_the_one_the_installer_works_out(tmp_path):
    # pairing.code_for and shipshape-setup's pair_code: the contract's formula.
    nonce, blob = "fedcba9876543210fedcba9876543210", "AAAAC3NzaC1lZDI1NTE5AAAAI" + "A" * 43
    script = ('sha256() { sha256sum <"$1" | cut -c 1-64; }; stage=$1; '
              + re.search(r"^pair_code\(\) \{\n(.*?)^\}", (REPO / "shipshape-setup").read_text(),
                          re.M | re.S).group(0) + '\npair_code "$2" "$3"')
    out = subprocess.run(["sh", "-c", script, "sh", str(tmp_path), nonce, blob],
                         capture_output=True, text=True, check=True).stdout
    assert out == pairing.code_for(nonce, blob)


def test_the_phone_is_out_of_scope_and_said_so(vm, capsys):  # noqa: F811
    from fleetledger import cli

    with pytest.raises(SystemExit):
        cli.main(["pair", "--help"])
    assert "A phone is out of scope" in capsys.readouterr().out
    text = (REPO / "docs" / "runbooks" / "trust-machines.md").read_text()
    assert "A phone is out of scope" in text


# --- found in review -----------------------------------------------------------------------------
def test_a_scoped_ipv6_source_is_kept_without_its_zone(on, monkeypatch):
    request(on, monkeypatch, new_key(on, "laptop"), source="fe80::1%eth0")
    (row,) = pairing.listing()
    assert row["source"] == "fe80::1"
    code, out, err = on.run("ca", "show", "--json")
    assert schemas.errors("fleetledger-ca", json.loads(out)) == []


def test_an_approval_in_the_last_seconds_is_still_heard(on, monkeypatch):
    doc = request(on, monkeypatch, new_key(on, "laptop"))
    later(monkeypatch, 9.9)
    code, out, err = on.run("pair", "approve", doc["id"], "--code", doc["code"], "--yes")
    assert code == 0, out + err
    later(monkeypatch, 0.6)  # 10.5 minutes in: past when the request would have lapsed
    assert asks(on, monkeypatch, f"status {doc['token']}")[1]["state"] == "approved"


def test_a_trust_that_fails_after_recording_the_machine_says_approved(on, monkeypatch):
    from fleetledger.errors import FleetError

    from fleetledger import trustcmd

    def fail(*a, **kw):
        raise FleetError("The CA could not sign (a test).")

    monkeypatch.setattr(trustcmd, "_sign_machine", fail)
    blob = new_key(on, "laptop")
    doc = request(on, monkeypatch, blob)
    code, out, err = on.run("pair", "approve", doc["id"], "--code", doc["code"], "--yes")
    assert code == 1 and "laptop is trusted all the same" in err
    assert ca.machine(on.store.load(), "laptop").public_key == f"ssh-ed25519 {blob}"
    assert pairing.listing() == []  # not shown as waiting, nor refusable as if nothing happened
    assert asks(on, monkeypatch, f"status {doc['token']}")[1]["state"] == "approved"


def test_an_internal_failure_is_audited_at_most_once_a_minute(on, monkeypatch):
    from fleetledger.concierge import audit

    def broken(*a, **kw):
        raise RuntimeError("a test")

    monkeypatch.setattr(pairing, "answer", broken)
    for _ in range(5):
        code, doc = asks(on, monkeypatch, "status " + "0" * 32)
        assert (code, doc) == (1, {"schema": pairing.SCHEMA, "state": "refused",
                                   "why": pairing.FAILED})
    failed = [x for x in audit.read() if x["action"] == "pair-request"]
    assert len(failed) == 1 and failed[0]["outcome"] == "failed: RuntimeError"


def test_a_copy_on_standby_answers_off_and_says_nothing_else(on, monkeypatch):
    from fleetledger import handover

    handover.write(on.store, handover.Marker("retired", handover.now(), "vm.example", 1))
    code, doc = asks(on, monkeypatch, f"request {new_key(on, 'laptop')} laptop")
    assert (code, doc["state"]) == (1, "off") and "vm.example" not in json.dumps(doc)
