"""Tests for scripts/hygiene.py.

Samples that would trip the check are assembled at run time, so this file
itself stays clean under the check it tests.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("hygiene", ROOT / "scripts" / "hygiene.py")
hygiene = importlib.util.module_from_spec(_spec)
sys.modules["hygiene"] = hygiene
_spec.loader.exec_module(hygiene)

AT = "@"
HOME = "/" + "home" + "/"


def rules(text: str, entries=None) -> list[str]:
    return [f.rule for f in hygiene.scan_text(text, "sample", entries or [])]


@pytest.mark.parametrize(
    "text",
    [
        "server at " + "10." + "1.2.3",
        "dns " + "192." + "168.1.1.",
        "cidr " + "172." + "20.0.0/16",
        "overlay " + "100." + "100.10.20",
    ],
)
def test_private_addresses_are_flagged(text):
    assert rules(text) == ["private-ip"]


@pytest.mark.parametrize(
    "text",
    [
        "use 192.0.2.10 in examples",
        "or 198.51.100.7 and 203.0.113.9",
        "loopback 127.0.0.1 and 0.0.0.0",
        "metadata 169.254.169.254",
        "version 1.2.3 and 10.1.2",
        "build 2.1.316.0.5",
    ],
)
def test_documentation_addresses_and_versions_pass(text):
    assert rules(text) == []


def test_home_paths_name_users():
    assert rules("see " + HOME + "alice" + "/projects") == ["home-path"]
    assert rules("see " + "/Users/" + "bob" + "/code") == ["home-path"]


@pytest.mark.parametrize("user", ["agent", "fleet", "ubuntu", "user", "example", "$USER"])
def test_ship_users_and_placeholders_pass(user):
    assert rules(HOME + user + "/work") == []


def test_tilde_paths_pass():
    assert rules("~/.config/fleetledger/ships.toml") == []


def test_real_emails_are_flagged():
    assert rules("contact alice" + AT + "corp-mail.io") == ["email"]


@pytest.mark.parametrize(
    "text",
    [
        "work" + AT + "example.com",
        "you" + AT + "example.org",
        "ops" + AT + "pve.test",
        "ssh fleet" + AT + "ship-01.local",
        "token shipshape" + AT + "pve!build",
    ],
)
def test_generic_emails_and_ssh_targets_pass(text):
    assert rules(text) == []


def test_allow_marker_skips_generic_rules_only():
    line = "10." + "0.0.1  # hygiene: allow"
    assert rules(line) == []
    entry = hygiene.DenyEntry(1, hygiene.re.compile("allow", hygiene.re.IGNORECASE))
    assert rules(line, [entry]) == ["denylist"]


def test_denylist_literals_and_regexes(tmp_path):
    deny = tmp_path / "deny"
    deny.write_text("# comment\n\nAcme Corp\nre:proj-[0-9]{4}\n", encoding="utf-8")
    entries = hygiene.load_denylist(deny, root=None)
    assert [e.number for e in entries] == [3, 4]
    assert rules("we use ACME corp tools", entries) == ["denylist"]
    assert rules("ticket proj-1234", entries) == ["denylist"]
    assert rules("nothing here", entries) == []


def test_denylist_findings_never_echo_the_term(tmp_path):
    deny = tmp_path / "deny"
    deny.write_text("secretname\n", encoding="utf-8")
    entries = hygiene.load_denylist(deny, root=None)
    findings = hygiene.scan_text("hello secretname", "f", entries)
    assert findings and all("secretname" not in str(f) for f in findings)
    assert "entry #1" in str(findings[0])


def test_denylist_inside_repo_is_refused():
    with pytest.raises(SystemExit):
        hygiene.load_denylist(ROOT / "versions.toml", root=ROOT)


def test_missing_denylist_means_generic_only(tmp_path):
    assert hygiene.load_denylist(tmp_path / "absent", root=None) == []


def test_frozen_folders_are_listed():
    assert hygiene.FROZEN_PREFIXES == ("docs/archive/",)


def test_frozen_lines_still_get_the_private_ip_rule():
    home_line = "workdir " + HOME + "olduser" + "/app"
    ip_line = "resolver " + "10." + "0.0.53"
    assert hygiene.check_generic(home_line, frozen=True) == []
    assert [r for r, _ in hygiene.check_generic(ip_line, frozen=True)] == ["private-ip"]


@pytest.mark.parametrize("block", hygiene.CANONICAL_BLOCKS)
def test_whole_private_blocks_pass(block):
    assert rules("acl localnet src " + block) == []


NARROW_SUBNETS = ["10." + "0.0.0/24", "192." + "168.50.0/24", "172." + "16.0.0/16"]


@pytest.mark.parametrize("subnet", NARROW_SUBNETS)
def test_narrower_private_subnets_are_flagged(subnet):
    assert rules("my lan is " + subnet) == ["private-ip"]


def test_generic_findings_never_echo_the_value():
    ip = "10." + "20.30.40"
    domain = "corp-mail" + ".io"
    findings = hygiene.scan_text(f"host {ip} mail alice{AT}{domain}", "f", [])
    assert [f.rule for f in findings] == ["private-ip", "email"]
    assert all(ip not in str(f) and domain not in str(f) for f in findings)


def test_ssh_algorithm_names_are_not_emails():
    assert rules("ssh-ed25519-cert-v01" + AT + "openssh.com AAAAC3Nza ship-01") == []
    assert rules("HostKeyAlgorithms ssh-ed25519-cert-v01" + AT + "openssh.com,ssh-ed25519") == []
    assert rules("mail admin" + AT + "openssh.com") == ["email"]


def test_package_versions_are_not_emails():
    assert rules("npm i example-helper" + AT + "2.0.1") == []


def test_scan_files_mode_flags_a_pr_body(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("Reach me at alice" + AT + "corp-mail.io\n", encoding="utf-8")
    code = hygiene.main(["--no-denylist", str(body)])
    assert code == 1


def test_repository_tree_is_clean_under_generic_rules():
    """The committed tree must pass the generic rules (CI has no private deny-list)."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "hygiene.py"), "--tree", "--no-denylist"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_requires_something_to_scan():
    with pytest.raises(SystemExit):
        hygiene.main([])


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=Example", "-c", "user.email=dev" + AT + "example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_scan_commits_reads_messages(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    (tmp_path / "a.txt").write_text("hello again\n", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "point at " + "10." + "1.1.1")
    findings = hygiene.scan_commits(tmp_path, "HEAD~1..HEAD", [])
    assert [f.rule for f in findings] == ["private-ip"]
    assert findings[0].where.startswith("commit ")


def test_scan_tree_checks_file_paths_against_the_denylist(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "notes-for-acme.md").write_text("nothing here\n", encoding="utf-8")
    entries = [hygiene.DenyEntry(1, hygiene.re.compile("acme", hygiene.re.IGNORECASE))]
    findings = hygiene.scan_tree(tmp_path, entries)
    assert [(f.rule, f.where) for f in findings] == [("denylist", "notes-for-acme.md (path):1")]


def test_scan_tree_applies_private_ip_rule_in_frozen_folders(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs" / "archive").mkdir(parents=True)
    (tmp_path / "docs" / "archive" / "old.conf").write_text(
        "dns " + "10." + "0.0.53\nhome " + HOME + "olduser" + "/x\n", encoding="utf-8"
    )
    findings = hygiene.scan_tree(tmp_path, [])
    assert [(f.rule, f.where) for f in findings] == [("private-ip", "docs/archive/old.conf:1")]


def test_binary_and_oversize_files_are_skipped(tmp_path, monkeypatch):
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\0" + ("10." + "1.2.3").encode())
    assert hygiene.read_text(binary) is None
    big = tmp_path / "big.txt"
    big.write_text("x" * 64, encoding="utf-8")
    monkeypatch.setattr(hygiene, "MAX_BYTES", 32)
    assert hygiene.read_text(big) is None
