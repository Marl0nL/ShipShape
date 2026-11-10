"""The plugin that stops the gpg-agents tests leave behind."""

import shutil
import subprocess
from pathlib import Path

import leftover_daemons
import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("gpg") and shutil.which("gpgconf")), reason="needs gpg and gpgconf")


def start_agent(home: Path) -> Path:
    """Make a gnupg home with a key in it (which starts its agent); returns its
    socket folder."""
    home.mkdir(mode=0o700, parents=True)
    subprocess.run(["gpg", "--batch", "--homedir", str(home), "--passphrase", "",
                    "--quick-gen-key", "Test <test@example.com>", "ed25519", "sign", "never"],
                   check=True, capture_output=True)
    return Path(subprocess.run(["gpgconf", "--homedir", str(home), "--list-dirs", "socketdir"],
                               check=True, capture_output=True, text=True).stdout.strip())


def test_stops_the_agents_under_the_folder_and_removes_their_socket_folders(tmp_path):
    root = tmp_path / "run"
    socketdir = start_agent(root / "test0" / "gpg")
    agent = start_agent(root / "deeper" / "test1" / "gnupg")
    assert set(leftover_daemons.agents_under(root.resolve()).values()) == {
        (root / "test0" / "gpg").resolve(), (root / "deeper" / "test1" / "gnupg").resolve()}

    assert leftover_daemons.stop_agents(root) == {}
    assert leftover_daemons.agents_under(root.resolve()) == {}
    if socketdir != root / "test0" / "gpg":  # gpg keeps sockets in /run/user when it can
        assert not socketdir.exists() and not agent.exists()


def test_leaves_agents_outside_the_folder_alone(tmp_path):
    outside = tmp_path / "other-run" / "gpg"
    start_agent(outside)
    try:
        leftover_daemons.stop_agents(tmp_path / "this-run-is-empty")
        assert list(leftover_daemons.agents_under(outside.resolve()).values()) == [
            outside.resolve()]
    finally:
        leftover_daemons.stop_agents(outside)


def test_a_folder_with_no_gnupg_home_is_fine(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert leftover_daemons.gnupg_homes(tmp_path) == set()
    assert leftover_daemons.stop_agents(tmp_path) == {}
