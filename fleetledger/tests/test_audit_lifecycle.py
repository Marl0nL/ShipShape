"""The audit log records changes to Ships (D7): from the command functions, so the
CLI and the web UI both do. Never a secret or a path."""

from __future__ import annotations

import json

import pytest
from conftest import keygen
from fleetledger.concierge import audit
from test_ca import caenv, cakeys  # noqa: F401  (fixtures)
from test_cli import env  # noqa: F401  (a fixture)

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
