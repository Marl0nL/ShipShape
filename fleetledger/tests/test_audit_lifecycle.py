"""The audit log records changes to Ships (D7): from the command functions, so the
CLI and the web UI both do. Never a secret or a path."""

from __future__ import annotations

import json

import pytest
from conftest import keygen
from fleetledger.concierge import audit
from test_ca import caenv, cakeys  # noqa: F401  (fixtures)
from test_cli import RUNNING, env, stranger  # noqa: F401  (fixtures)

from fleetledger import ca, paths


def entries():
    return [(e["ship"], e["action"], e["service"], e["outcome"]) for e in audit.read()]


def test_making_and_destroying_a_ship_are_recorded(env):  # noqa: F811
    assert env.run("new", "ship-07", "--driver", "proxmox")[0] == 0
    assert env.run("destroy", "ship-07", "--yes")[0] == 0
    assert entries() == [("ship-07", "create", "proxmox", "ok"),
                         ("ship-07", "destroy", "proxmox", "ok")]


def test_the_manual_driver_records_steps_and_forget(env):  # noqa: F811
    assert env.run("new", "ship-08")[0] == 0  # manual: the VM is made by hand
    assert env.run("destroy", "ship-08", "--yes")[0] == 0
    assert env.run("destroy", "ship-08", "--yes", "--forget")[0] == 0
    assert entries() == [("ship-08", "create", "manual", "steps"),
                         ("ship-08", "destroy", "manual", "steps"),
                         ("ship-08", "forget", "manual", "ok")]


def test_a_create_that_fails_is_recorded_as_failed(env):  # noqa: F811
    env.driver.fail = True
    with pytest.raises(RuntimeError):
        env.run("new", "ship-09", "--driver", "proxmox")
    assert entries() == [("ship-09", "create", "proxmox", "failed")]


def test_snapshot_rollback_and_delete_are_recorded_but_not_take(env):  # noqa: F811
    env.run("new", "ship-10", "--driver", "proxmox")
    env.run("snapshot", "take", "ship-10", "before")
    env.run("snapshot", "rollback", "ship-10", "before", "--yes")
    env.run("snapshot", "rm", "ship-10", "before")
    assert entries()[1:] == [("ship-10", "snapshot-rollback", "before", "ok"),
                             ("ship-10", "snapshot-delete", "before", "ok")]
    env.run("snapshot", "rollback", "ship-10", "before")  # refused: no --yes, no terminal
    assert len(entries()) == 3


def test_re_pin_and_untrust_are_recorded(caenv, tmp_path):  # noqa: F811
    keygen(tmp_path / "new", "new")
    key = " ".join((tmp_path / "new.pub").read_text().split()[:2])
    caenv.app.keyscan = lambda host: key
    assert caenv.run("adopt", "ship-01", "--replace", "--fingerprint",
                     ca.fingerprint(key))[0] == 0
    keygen(tmp_path / "laptop", "laptop")
    caenv.run("trust", "laptop", "--key", str(tmp_path / "laptop.pub"),
              "--out", str(tmp_path / "b"))
    assert caenv.run("untrust", "laptop")[0] == 0
    assert entries() == [("ship-01", "re-pin", "host-key", "ok"),
                         ("-", "untrust", "laptop", "ok")]


def test_entries_hold_no_paths_keys_or_secrets(caenv, tmp_path):  # noqa: F811
    keygen(tmp_path / "laptop", "laptop")
    caenv.run("trust", "laptop", "--key", str(tmp_path / "laptop.pub"),
              "--out", str(tmp_path / "b"))
    caenv.run("untrust", "laptop")
    caenv.run("new", "ship-11", "--driver", "proxmox")
    caenv.run("destroy", "ship-11", "--yes")
    text = paths.audit_log_path().read_text()
    assert "/" not in "".join(json.dumps(e["service"]) + e["ship"] for e in audit.read())
    assert "ssh-ed25519" not in text and "AAAA" not in text and str(tmp_path) not in text


def test_an_unusable_snapshot_name_is_refused_before_anything_is_recorded(env):  # noqa: F811
    env.run("new", "ship-12", "--driver", "proxmox")
    for argv in (["snapshot", "rm", "ship-12", "/etc/passwd"],
                 ["snapshot", "rollback", "ship-12", "../x", "--yes"]):
        code, _, err = env.run(*argv)
        assert code != 0 and "not a usable snapshot name" in err
    assert entries() == [("ship-12", "create", "proxmox", "ok")]
    assert not [s for s in env.driver.snaps if s[0] != "take"]  # the driver never asked


def test_the_manual_drivers_rollback_and_delete_record_steps(env):  # noqa: F811
    env.run("new", "ship-13")  # manual
    assert env.run("snapshot", "rollback", "ship-13", "before", "--yes")[0] == 0
    assert env.run("snapshot", "rm", "ship-13", "before")[0] == 0
    assert entries()[1:] == [("ship-13", "snapshot-rollback", "before", "steps"),
                             ("ship-13", "snapshot-delete", "before", "steps")]


def test_a_destroy_that_raises_is_recorded_as_failed_and_keeps_the_ship(env):  # noqa: F811
    env.run("new", "ship-14", "--driver", "proxmox")

    def broken(ship):
        raise RuntimeError("the node went away")

    env.driver.destroy = broken
    with pytest.raises(RuntimeError):
        env.run("destroy", "ship-14", "--yes")
    assert entries()[1:] == [("ship-14", "destroy", "proxmox", "failed")]
    assert env.store.load().has_ship("ship-14")


def test_an_audit_log_that_cannot_be_written_never_orphans_a_vm(
        env, tmp_path, monkeypatch, capsys):  # noqa: F811
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    monkeypatch.setattr(paths, "audit_log_path", lambda: blocker / "audit.log")
    code, _, err = env.run("new", "ship-15", "--driver", "proxmox")
    assert code == 0, err
    assert env.store.load().ship("ship-15").driver_ref["vmid"] == 9101  # the VM id is kept
    assert "could not write the audit log" in capsys.readouterr().err


def test_a_first_adopt_records_nothing(env, stranger):  # noqa: F811
    key, fp = stranger
    env.app.keyscan = lambda host: key
    env.ship.status_override["ship-07"] = RUNNING
    assert env.run("adopt", "ship-07", "--host", "192.0.2.17", "--fingerprint", fp)[0] == 0
    assert entries() == []  # adopting a new Ship is not re-pinning one


def test_a_second_untrust_records_nothing(caenv, tmp_path):  # noqa: F811
    keygen(tmp_path / "laptop", "laptop")
    caenv.run("trust", "laptop", "--key", str(tmp_path / "laptop.pub"),
              "--out", str(tmp_path / "b"))
    assert caenv.run("untrust", "laptop")[0] == 0
    caenv.run("untrust", "laptop")  # already revoked: nothing changes
    assert entries() == [("-", "untrust", "laptop", "ok")]
