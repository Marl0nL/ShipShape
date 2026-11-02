import getpass
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from fleetledger.errors import FleetError
from fleetledger.ssh import SSH, HostKeyMismatch, Unreachable, as_agent, ship_ctl


class FakeRunner:
    """Stands in for subprocess.run; answers per host name."""

    def __init__(self, answers):
        self.answers = answers  # host -> (returncode, stdout, stderr)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        host = argv[argv.index("--") + 1]
        code, out, err = self.answers[host]
        return subprocess.CompletedProcess(argv, code, out, err.encode())


def opt(argv, key):
    values = [a.split("=", 1)[1] for a in argv if a.startswith(key + "=")]
    return values[-1] if values else None


def test_known_hosts_from_inventory(store):
    inv = store.load()
    ssh = SSH(inv)
    path = ssh.sync_known_hosts()
    lines = path.read_text().splitlines()
    assert lines[0].startswith("#")
    assert lines[1] == "ship-01 " + inv.ship("ship-01").host_key_type_and_blob
    assert lines[2].startswith("ship-02 ssh-ed25519 ")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_argv_pins_and_locks_down(store):
    inv = store.load()
    ship = inv.ship("ship-02")
    argv = SSH(inv).argv(ship, "ship-02.local", ship_ctl("status", "--json"), tty=True)
    assert argv[0] == "ssh"
    assert opt(argv, "HostKeyAlias") == "ship-02"
    assert opt(argv, "StrictHostKeyChecking") == "yes"
    assert opt(argv, "UserKnownHostsFile") == str(inv.known_hosts)
    assert opt(argv, "GlobalKnownHostsFile") == "/dev/null"
    assert opt(argv, "HostKeyAlgorithms") == "ssh-ed25519"
    assert opt(argv, "ForwardAgent") == "no"
    assert opt(argv, "ClearAllForwardings") == "yes"
    assert opt(argv, "UpdateHostKeys") == "no" and opt(argv, "CheckHostIP") == "no"
    assert opt(argv, "PermitLocalCommand") == "no" and opt(argv, "ForwardX11") == "no"
    assert opt(argv, "BatchMode") == "yes"
    assert opt(argv, "IdentitiesOnly") == "yes"
    assert argv[argv.index("-i") + 1] == str(inv.admin_key)
    assert argv[argv.index("-l") + 1] == "fleet"
    assert "-tt" in argv
    assert argv[-2:] == ["ship-02.local", "sudo -n /usr/local/bin/ship-ctl status --json"]


def test_long_runtime_path_disables_sharing(store, monkeypatch, tmp_path):
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", str(tmp_path / ("x" * 80)))
    inv = store.load()
    argv = SSH(inv).argv(inv.ship("ship-01"), "ship-01.local")
    assert opt(argv, "ControlMaster") == "no" and opt(argv, "ControlPath") == "none"


def test_agent_login_uses_your_own_keys(store):
    inv = store.load()
    argv = SSH(inv).argv(inv.ship("ship-01"), "ship-01.local", user="agent", batch=False)
    assert "-i" not in argv and opt(argv, "BatchMode") is None
    assert argv[argv.index("-l") + 1] == "agent"
    assert opt(argv, "ForwardAgent") == "no"


def test_as_agent_quotes_the_command():
    cmd = as_agent("gh auth status --hostname github.com >/dev/null")
    assert cmd[:6] == ["sudo", "-n", "-iu", "agent", "--", "bash"]
    from fleetledger.ssh import remote_command

    assert remote_command(cmd).endswith("-c 'gh auth status --hostname github.com >/dev/null'")


def test_run_tries_each_name(store):
    inv = store.load()
    runner = FakeRunner(
        {
            "ship-02.local": (255, b"", "ssh: Could not resolve hostname ship-02.local"),
            "ship-02.example.net": (0, b"hello\n", ""),
        }
    )
    result = SSH(inv, runner=runner).run(inv.ship("ship-02"), ["echo", "hello"])
    assert result.text == "hello\n" and result.host == "ship-02.example.net"
    assert len(runner.calls) == 2


def test_run_refuses_wrong_host_key_without_trying_other_names(store):
    inv = store.load()
    runner = FakeRunner(
        {
            "ship-02.local": (255, b"", "Host key verification failed."),
            "ship-02.example.net": (0, b"x", ""),
        }
    )
    with pytest.raises(HostKeyMismatch, match="pinned host key"):
        SSH(inv, runner=runner).run(inv.ship("ship-02"), ["true"])
    assert len(runner.calls) == 1


def test_run_unreachable_and_refused(store):
    inv = store.load()
    down = FakeRunner({"ship-01.local": (255, b"", "ssh: connect to host: Connection refused")})
    with pytest.raises(Unreachable, match="Connection refused"):
        SSH(inv, runner=down).run(inv.ship("ship-01"), ["true"])
    denied = FakeRunner({"ship-01.local": (255, b"", "fleet@ship-01: Permission denied")})
    with pytest.raises(FleetError, match="refused FleetLedger's admin key"):
        SSH(inv, runner=denied).run(inv.ship("ship-01"), ["true"])


def test_remote_exit_codes_pass_through(store):
    inv = store.load()
    runner = FakeRunner({"ship-01.local": (3, b"{}", "not root")})
    result = SSH(inv, runner=runner).run(inv.ship("ship-01"), ["x"])
    assert result.returncode == 3 and result.stderr == "not root"


# --- against a real OpenSSH server ------------------------------------------

SSHD = os.environ.get("FLEETLEDGER_TEST_SSHD") or shutil.which("sshd") or "/usr/sbin/sshd"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def short_runtime(monkeypatch):
    """A runtime folder short enough for an SSH control socket."""
    folder = tempfile.mkdtemp(prefix="fl-", dir="/tmp")
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", folder)
    yield Path(folder)
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def sshd(tmp_path, home, short_runtime):
    """An unprivileged sshd on localhost that lets the test's admin key in as you."""
    if not os.access(SSHD, os.X_OK):
        pytest.skip("no sshd binary (set FLEETLEDGER_TEST_SSHD)")
    folder = tmp_path / "sshd"
    folder.mkdir()
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(folder / "hostkey")],
        check=True,
    )
    shutil.copy(home / "keys" / "fleet_admin.pub", folder / "authorized_keys")
    port = _free_port()
    (folder / "config").write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {folder / 'hostkey'}\n"
        f"AuthorizedKeysFile {folder / 'authorized_keys'}\nUsePAM no\nStrictModes no\n"
        "PidFile none\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
    )
    proc = subprocess.Popen(
        [SSHD, "-D", "-e", "-f", str(folder / "config")],
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield port, (folder / "hostkey.pub").read_text().strip()
    proc.terminate()
    proc.wait(timeout=5)


def _pin(store, key):
    with store.edit() as ed:
        ed.set_field("ship-01", "names", ["127.0.0.1"])
        ed.set_field("ship-01", "host_key", key)


def test_real_ssh_runs_with_pinned_key(store, sshd):
    port, host_key = sshd
    _pin(store, host_key)
    inv = store.load()
    ssh = SSH(inv, fleet_user=getpass.getuser(), extra_options=["-p", str(port)])
    result = ssh.run(inv.ship("ship-01"), ["echo", "a b", "$HOME"])
    assert result.returncode == 0
    assert result.text == "a b $HOME\n"  # quoted, not expanded by the remote shell
    # The connection is shared: a master socket exists in the runtime folder.
    assert any(p.is_socket() for p in Path(os.environ["FLEETLEDGER_RUNTIME_DIR"]).iterdir())
    again = ssh.run(inv.ship("ship-01"), ["echo", "reused"])
    assert again.text == "reused\n"
    subprocess.run(
        ["ssh", "-O", "exit", *ssh.options(inv.ship("ship-01")), "127.0.0.1"],
        capture_output=True,
    )


def test_repin_is_checked_even_with_a_shared_connection_open(store, sshd, tmp_path):
    """The control socket is named after the pinned key: a re-pin never reuses
    a connection that was checked against the old key."""
    port, host_key = sshd
    _pin(store, host_key)
    first = SSH(store.load(), fleet_user=getpass.getuser(), extra_options=["-p", str(port)])
    assert first.run(store.load().ship("ship-01"), ["true"]).returncode == 0  # master stays up
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    _pin(store, (tmp_path / "other.pub").read_text().strip())
    second = SSH(store.load(), fleet_user=getpass.getuser(), extra_options=["-p", str(port)])
    with pytest.raises(HostKeyMismatch):
        second.run(store.load().ship("ship-01"), ["true"])
    _pin(store, host_key)
    subprocess.run(["ssh", "-O", "exit", *first.options(store.load().ship("ship-01")),
                    "127.0.0.1"], capture_output=True)


def test_control_socket_follows_the_pin(store, short_runtime):
    inv = store.load()
    one = opt(SSH(inv).argv(inv.ship("ship-01"), "h"), "ControlPath")
    two = opt(SSH(inv).argv(inv.ship("ship-02"), "h"), "ControlPath")
    assert one != two and one.endswith("-%C") and two.endswith("-%C")


def test_real_ssh_refuses_impostor(store, sshd, tmp_path):
    """A server at the Ship's name with a different host key is refused."""
    port, _ = sshd
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    _pin(store, (tmp_path / "other.pub").read_text().strip())
    inv = store.load()
    ssh = SSH(inv, fleet_user=getpass.getuser(), extra_options=["-p", str(port)])
    with pytest.raises(HostKeyMismatch):
        ssh.run(inv.ship("ship-01"), ["true"])
    # Nothing was learned: known_hosts still holds only the pinned keys.
    assert (tmp_path / "other.pub").read_text().split()[1] in inv.known_hosts.read_text()
    assert len(inv.known_hosts.read_text().splitlines()) == 3


