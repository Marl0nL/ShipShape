"""The network guard every FleetLedger test runs under (fl_netguard.py, conftest.py)."""

from __future__ import annotations

import socket
import subprocess
import threading

import fl_netguard
import pytest
from fl_netguard import NetworkRefused

from fleetledger import cli


def refused_and_cleared(guard, *parts):
    """The guard noted each refusal; cleared so this test itself passes."""
    assert len(guard.refused) == 1 and all(p in guard.refused[0] for p in parts), guard.refused
    guard.refused.clear()


def test_a_real_ssh_to_a_ship_is_refused(no_network):
    with pytest.raises(NetworkRefused, match="another machine"):
        subprocess.run(["ssh", "-o", "BatchMode=yes", "--", "fleet@ship-01.local", "true"])
    refused_and_cleared(no_network, "ship-01.local")


def test_ssh_keyscan_and_scp_to_a_ship_are_refused(no_network):
    with pytest.raises(NetworkRefused):
        subprocess.run(["ssh-keyscan", "-t", "ed25519", "-T", "1", "--", "ship-02.local"])
    refused_and_cleared(no_network, "ship-02.local")
    with pytest.raises(NetworkRefused):
        subprocess.run(["scp", "a.txt", "fleet@ship-02.local:/tmp/a.txt"])
    refused_and_cleared(no_network, "ship-02.local")


def test_a_refusal_in_a_background_thread_is_still_noted(no_network):
    """The web UI's poller swallows every error: the guard fails the test at teardown."""

    def poller():
        try:
            subprocess.run(["ssh", "ship-01.local", "sudo", "-n", "ship-ctl", "status"])
        except Exception:  # noqa: BLE001 - as fleet.Fleet.run does
            pass

    thread = threading.Thread(target=poller)
    thread.start()
    thread.join()
    refused_and_cleared(no_network, "ship-01.local")


def test_a_local_ssh_needs_the_local_sshd_mark(no_network):
    with pytest.raises(NetworkRefused, match="local_sshd"):
        subprocess.run(["ssh", "-p", "2222", "agent@127.0.0.1", "true"])
    refused_and_cleared(no_network, "127.0.0.1")


@pytest.mark.local_sshd
def test_with_the_mark_a_loopback_ssh_may_run_but_never_one_to_a_ship(no_network):
    with pytest.raises(NetworkRefused, match="another machine"):
        subprocess.run(["ssh", "-J", "jump.example.com", "agent@127.0.0.1", "true"])
    refused_and_cleared(no_network, "jump.example.com")


def test_ssh_that_opens_no_connection_is_left_alone(no_network):
    subprocess.run(["ssh", "-G", "ship-01.local"], capture_output=True)
    subprocess.run(["ssh", "-O", "check", "-o", "ControlPath=/nonexistent", "ship-01.local"],
                   capture_output=True)
    subprocess.run(["ssh-keygen", "-q", "-l", "-f", "/dev/null"], capture_output=True)
    assert no_network.refused == []


def test_a_dns_lookup_is_refused_but_an_address_or_localhost_is_not(no_network):
    with pytest.raises(NetworkRefused, match="real_resolver"):
        socket.getaddrinfo("ship-31.local", 22)
    refused_and_cleared(no_network, "ship-31.local")
    socket.getaddrinfo("127.0.0.1", 22)
    socket.getaddrinfo("localhost", 22)
    assert no_network.refused == []


def test_a_connection_to_another_machine_is_refused(no_network):
    with socket.socket() as s, pytest.raises(NetworkRefused):
        s.connect(("192.0.2.44", 22))
    refused_and_cleared(no_network, "192.0.2.44")


def test_names_resolve_through_the_fake_resolver(no_network):
    assert cli._resolves is fl_netguard.fake_resolves
    assert cli._resolves("192.0.2.44") and cli._resolves("2001:db8::44")
    assert cli._resolves("localhost")
    assert not cli._resolves("ship-31.local")
    assert no_network.refused == []


@pytest.mark.real_resolver
def test_the_real_resolver_mark_keeps_the_real_resolver(no_network):
    assert cli._resolves is not fl_netguard.fake_resolves
    assert no_network.real_resolver


@pytest.mark.parametrize("argv, hosts", [
    (["ssh", "-T", "-o", "BatchMode=yes", "--", "fleet@ship-01.local", "ship-ctl"],
     ["ship-01.local"]),
    (["ssh", "-tt", "-i", "key", "-p22", "agent@ship-02.local", "echo", "hi"],
     ["ship-02.local"]),
    (["ssh", "-oProxyJump=jump.example.com", "ssh://u@[::1]:2222"],
     ["::1", "jump.example.com"]),
    (["/usr/bin/ssh-keyscan", "-T", "10", "--", "a.example.com,b.example.com"],
     ["a.example.com", "b.example.com"]),
    (["ssh", "-G", "ship-01.local"], None),
    (["ssh", "-O", "exit", "ship-01.local"], None),
])
def test_ssh_destinations(argv, hosts):
    assert fl_netguard.ssh_destinations(argv) == hosts


def test_a_tests_own_stand_in_named_ssh_may_run(no_network, tmp_path):
    fake = tmp_path / "ssh"
    fake.write_text("#!/bin/sh\necho stand-in\n")
    fake.chmod(0o755)
    out = subprocess.run([str(fake), "fleet@ship-01.local", "true"], capture_output=True,
                         text=True).stdout
    assert out == "stand-in\n"
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    assert subprocess.run(["ssh", "ship-01.local"], env=env, capture_output=True,
                          text=True).stdout == "stand-in\n"
    assert no_network.refused == []
