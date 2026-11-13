import getpass
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import fl_harness
import pytest
from fl_harness import start_sshd
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


def test_forget_closes_shared_connections_for_every_name(store):
    inv = store.load()
    runner = FakeRunner({"ship-02.local": (255, b"", "no master"),
                         "ship-02.example.net": (0, b"", "")})
    SSH(inv, runner=runner).forget(inv.ship("ship-02"))
    assert [c[c.index("--") - 2:c.index("--")] for c in runner.calls] == [["-O", "exit"]] * 2
    assert [c[c.index("--") + 1] for c in runner.calls] == ["ship-02.local",
                                                          "ship-02.example.net"]
    # Same options as a real call, so it addresses the same control socket.
    assert opt(runner.calls[0], "ControlPath") == opt(
        SSH(inv).argv(inv.ship("ship-02"), "ship-02.local"), "ControlPath")


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


@pytest.fixture
def short_runtime(monkeypatch, short_dir):
    """A runtime folder short enough for an SSH control socket."""
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", str(short_dir))
    return short_dir


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
    proc, port = start_sshd(SSHD, folder, lambda port: (
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {folder / 'hostkey'}\n"
        f"AuthorizedKeysFile {folder / 'authorized_keys'}\nUsePAM no\nStrictModes no\n"
        "PidFile none\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
    ))
    yield port, (folder / "hostkey.pub").read_text().strip()
    proc.terminate()
    proc.wait(timeout=5)


def test_sshd_start_tries_another_port_when_its_port_is_taken(tmp_path, monkeypatch):
    """Another test worker can take the picked port before sshd binds it."""
    if not os.access(SSHD, os.X_OK):
        pytest.skip("no sshd binary (set FLEETLEDGER_TEST_SSHD)")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "hostkey")],
                   check=True)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        held = taken.getsockname()[1]
        ports = iter([held])
        free = fl_harness._free_port
        monkeypatch.setattr(fl_harness, "_free_port", lambda: next(ports, None) or free())
        proc, port = fl_harness.start_sshd(SSHD, tmp_path, lambda port: (
            f"Port {port}\nListenAddress 127.0.0.1\nHostKey {tmp_path / 'hostkey'}\n"
            "UsePAM no\nPidFile none\n"))
        try:
            assert port != held
            assert proc.poll() is None
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def _pin(store, key):
    with store.edit() as ed:
        ed.set_field("ship-01", "names", ["127.0.0.1"])
        ed.set_field("ship-01", "host_key", key)


@pytest.mark.local_sshd
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


@pytest.mark.local_sshd
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


@pytest.mark.local_sshd
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




def test_probe_certificate_never_shares_a_connection(store, short_runtime):
    """The CA-only check must open its own connection, even where options() would
    multiplex (a short runtime dir): an open pinned connection cannot answer for it."""
    inv = store.load()
    ship = inv.ship("ship-02")
    normal = SSH(inv).options(ship)
    assert opt(normal, "ControlMaster") == "auto" and opt(normal, "ControlPersist")
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    kh = short_runtime / "kh"
    kh.write_text("@cert-authority ship-02 ssh-ed25519 AAAA\n")
    assert SSH(inv, runner=run).probe_certificate(ship, kh) is None
    argv = seen[-1]
    assert opt(argv, "ControlMaster") == "no"
    assert opt(argv, "ControlPath") == "none"
    assert not any(a.startswith("ControlPersist=") for a in argv)
    assert sum(1 for a in argv if a.startswith("ControlMaster=")) == 1
    assert sum(1 for a in argv if a.startswith("ControlPath=")) == 1


def test_probe_certificate_tries_each_name_and_stops_on_a_bad_key(store, tmp_path):
    inv = store.load()
    ship = inv.ship("ship-02")  # names: ship-02.local, ship-02.example.net
    kh = tmp_path / "kh"
    kh.write_text("@cert-authority ship-02 ssh-ed25519 AAAA\n")
    seen = []

    def runner(answers):
        def run(argv, **kw):
            seen.append(argv)
            code, err = answers[argv[-2]]
            return subprocess.CompletedProcess(argv, code, b"", err.encode())
        return run

    ok = SSH(inv, runner=runner({
        "ship-02.local": (255, "ssh: Could not resolve hostname ship-02.local"),
        "ship-02.example.net": (0, ""),
    }))
    assert ok.probe_certificate(ship, kh) is None
    argv = seen[-1]
    assert opt(argv, "UserKnownHostsFile") == str(kh)
    assert opt(argv, "HostKeyAlgorithms") == "ssh-ed25519-cert-v01@openssh.com"
    assert opt(argv, "ControlPath") == "none"  # never an open pinned connection
    assert argv.count("-o") == len([a for a in argv if a == "-o"])
    assert sum(1 for a in argv if a.startswith("UserKnownHostsFile=")) == 1
    seen.clear()
    bad = SSH(inv, runner=runner({
        "ship-02.local": (255, "Host key verification failed."),
        "ship-02.example.net": (0, ""),
    }))
    assert "verification failed" in bad.probe_certificate(ship, kh)
    assert len(seen) == 1  # a wrong key is not retried at another name
