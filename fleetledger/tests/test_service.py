"""FleetLedger as a service: the control socket, ship-fleet ui --service and
ship-fleet service (web/control.py, web/server.py, service.py).

systemd is simulated: a fake runs in place of systemctl and loginctl and records what
it was asked. The unit is written under a temporary XDG_CONFIG_HOME, never your own.
"""

from __future__ import annotations

import io
import os
import signal
import socket
import stat
import threading
import time
from pathlib import Path

import pytest
from fleetledger.errors import FleetError
from fleetledger.web import control, server
from fleetledger.web.jobs import Jobs
from test_keys import short_folder  # noqa: F401 - the short /tmp folder, cleaned up
from test_web import web  # noqa: F401 - the web UI fixture

from fleetledger import cli, service


@pytest.fixture
def runtime(monkeypatch):
    """A short private runtime folder (a Unix socket path must be short)."""
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="fl-"))
    os.chmod(folder, 0o700)
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", str(folder))
    yield folder
    for f in folder.iterdir():
        f.unlink()
    folder.rmdir()


@pytest.fixture
def units(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "systemd" / "user"


class FakeSystem:
    """systemctl --user and loginctl, as far as ship-fleet service uses them."""

    def __init__(self, active=False, linger="no", manager=True):
        self.calls: list[list[str]] = []
        self.active = active
        self.linger = linger
        self.manager = manager
        self.enabled = False

    reboot_stop = "5s"  # Ubuntu's user@.service timeout.conf

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "loginctl":
            return 0, self.linger + "\n", ""
        if argv[:2] == ["systemctl", "show"]:
            return 0, self.reboot_stop + "\n", ""
        args = [a for a in argv[2:] if a != "--no-block"]
        if args == ["show-environment"]:
            return (0, "HOME=/home/example\n", "") if self.manager else \
                (1, "", "Failed to connect to bus: No medium found")
        if args[0] == "is-active":
            return (0, "active\n", "") if self.active else (3, "inactive\n", "")
        if args[0] == "is-enabled":
            return (0, "enabled\n", "") if self.enabled else (1, "disabled\n", "")
        if args[0] == "enable":
            self.enabled = True
        if args[0] in ("start", "restart"):
            self.active = True
        if args[:2] == ["disable", "--now"]:
            self.enabled = self.active = False
        return 0, "", ""

    def verbs(self):
        return [[a for a in c[2:] if a != "--no-block"] for c in self.calls
                if c[:2] == ["systemctl", "--user"]]


def app_with(store, system, out=None):
    out = out or io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store)
    app.system = system
    return app, out


@pytest.fixture
def ship_fleet(tmp_path):
    path = tmp_path / "bin" / "ship-fleet"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


# --- the control socket ---------------------------------------------------------------


def test_the_control_socket_answers_you_only_and_is_private(runtime):
    calls = []
    ctl = control.Control({"status": lambda: {"port": 8765},
                           "link": lambda: calls.append(1) or {"link": "http://x/login?token=t"}})
    assert ctl.start()
    try:
        path = control.socket_path()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert control.ask("status") == {"ok": True, "port": 8765}
        assert control.ask("link")["link"].endswith("token=t") and calls == [1]
        with pytest.raises(FleetError, match="refused: unknown request"):
            control.ask("shutdown")
    finally:
        ctl.stop()
    assert not path.exists()
    with pytest.raises(FleetError, match="No ship-fleet ui is running"):
        control.ask("status")


def test_a_caller_with_another_user_id_gets_no_answer(runtime, monkeypatch):
    ctl = control.Control({"link": lambda: {"link": "secret"}})
    assert ctl.start()
    try:
        monkeypatch.setattr(control, "peer_uid", lambda conn: os.getuid() + 1)
        with pytest.raises(FleetError, match="No ship-fleet ui is running"):
            control.ask("link")
    finally:
        ctl.stop()


def test_a_request_too_long_or_not_json_is_not_answered_with_data(runtime):
    ctl = control.Control({"link": lambda: {"link": "secret"}})
    assert ctl.start()
    try:
        for payload in (b"x" * (control.MAX_REQUEST + 10), b"not json\n", b'["link"]\n'):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(str(control.socket_path()))
                try:
                    s.sendall(payload)
                    data = s.recv(4096)
                except OSError:
                    data = b""
            assert b"secret" not in data
    finally:
        ctl.stop()


def test_a_leftover_socket_is_replaced_and_a_live_one_kept(runtime):
    path = control.socket_path()
    left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    left.bind(str(path))
    left.close()  # a socket file nobody listens on: a UI that stopped
    first = control.Control({"status": lambda: {"which": "first"}})
    assert first.start()
    try:
        second = control.Control({"status": lambda: {"which": "second"}})
        assert second.start() is False  # the first one answers: it keeps the socket
        second.stop()
        assert control.ask("status")["which"] == "first"
    finally:
        first.stop()


# --- draining jobs ----------------------------------------------------------------------


def test_drain_refuses_new_jobs_and_waits_for_the_running_ones(home):
    jobs = Jobs(cli.App())
    release = threading.Event()
    job = jobs.start("update", ["ship-01"], lambda j: release.wait(10) and 0)
    assert jobs.drain() == [job]
    assert jobs.stopping
    with pytest.raises(FleetError, match="stopping"):
        jobs.start("reboot", ["ship-02"], lambda j: 0)
    assert jobs.wait(0.2) == [job]  # still running
    release.set()
    assert jobs.wait(10) == []
    assert job.done and job.ok


def test_the_service_lets_running_jobs_finish_before_it_stops(home):
    import uvicorn

    jobs = Jobs(cli.App())
    release = threading.Event()
    jobs.start("update", ["ship-01"], lambda j: release.wait(10) and 0)
    said = []
    srv = server.service_server(uvicorn.Config(lambda *a: None), jobs, said.append,
                                timeout=30)
    srv.handle_exit(signal.SIGTERM, None)
    deadline = time.monotonic() + 5
    while not jobs.stopping and time.monotonic() < deadline:
        time.sleep(0.01)
    assert jobs.stopping and not srv.should_exit  # draining, still serving pages
    release.set()
    deadline = time.monotonic() + 10
    while not srv.should_exit and time.monotonic() < deadline:
        time.sleep(0.01)
    assert srv.should_exit
    assert any("1 running job" in s for s in said)
    assert srv._captured_signals == []  # not raised again: the records get saved first


def test_a_second_signal_stops_the_service_at_once(home):
    import uvicorn

    jobs = Jobs(cli.App())
    release = threading.Event()
    jobs.start("update", ["ship-01"], lambda j: release.wait(10) and 0)
    srv = server.service_server(uvicorn.Config(lambda *a: None), jobs, lambda s: None,
                                timeout=30)
    srv.handle_exit(signal.SIGTERM, None)
    srv.handle_exit(signal.SIGTERM, None)
    assert srv.should_exit
    release.set()


def test_with_nothing_running_the_service_stops_straight_away(home):
    import uvicorn

    srv = server.service_server(uvicorn.Config(lambda *a: None), Jobs(cli.App()),
                                lambda s: None)
    srv.handle_exit(signal.SIGTERM, None)
    deadline = time.monotonic() + 5
    while not srv.should_exit and time.monotonic() < deadline:
        time.sleep(0.01)
    assert srv.should_exit


def test_pages_say_when_fleetledger_is_stopping(web):  # noqa: F811
    web.sign_in()
    assert "FleetLedger is stopping" not in web.client.get("/").text
    web.app_ui.jobs.drain()
    assert "FleetLedger is stopping" in web.client.get("/").text


# --- ship-fleet ui --service ------------------------------------------------------------


def test_the_service_prints_no_link_and_hands_one_out_on_its_socket(store, runtime,
                                                                    monkeypatch):
    import uvicorn

    seen = {}

    def fake_run(self, sockets=None):
        seen["status"] = control.ask("status")
        seen["link"] = control.ask("link")["link"]
        seen["server"] = type(self).__name__

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.setattr(server.webbrowser, "open",
                        lambda url: pytest.fail("a service opens no browser"))
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store)
    assert server.serve(app, port=0, open_browser=False, interval=3600, service=True) == 0
    assert "token" not in out.getvalue()  # the journal gets no link
    assert "ship-fleet ui link --local" in out.getvalue()
    assert seen["status"]["service"] is True and seen["status"]["jobs"] == []
    port = seen["status"]["port"]
    assert seen["link"].startswith(f"http://127.0.0.1:{port}/login?token=")
    assert seen["server"] == "ServiceServer"
    assert not control.socket_path().exists()  # removed on exit


def test_the_service_polls_with_nobody_looking(store, runtime, monkeypatch):
    import math

    import uvicorn
    from fleetledger.web import fleet as fleet_mod

    made = {}
    real = fleet_mod.Fleet

    def spy(*args, **kwargs):
        made.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(fleet_mod, "Fleet", spy)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)
    app = cli.App(out=io.StringIO(), err=io.StringIO(), store_factory=lambda: store)
    server.serve(app, port=0, open_browser=False, interval=3600, service=True)
    assert made["idle_after"] == math.inf
    made.clear()
    monkeypatch.setattr(server.sys.stdin, "isatty", lambda: False, raising=False)
    server.serve(app, port=0, open_browser=False, interval=3600)
    assert "idle_after" not in made  # ship-fleet ui as ever: only while a page is open


def test_ui_service_needs_a_fixed_port(home, monkeypatch):
    monkeypatch.setattr(server, "serve", lambda app, **kw: pytest.fail("must not start"))
    out = io.StringIO()
    assert cli.main(["ui", "--service"], cli.App(out=out, err=out)) == 1
    assert "fixed port" in out.getvalue()
    called = {}
    monkeypatch.setattr(server, "serve", lambda app, **kw: called.update(kw) or 0)
    assert cli.main(["ui", "--service", "--port", "8765"], cli.App()) == 0
    assert called == {"port": 8765, "open_browser": False, "interval": 30.0, "service": True}


def test_the_terminal_ui_also_answers_link_local(store, runtime, monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn.Server, "run",
                        lambda self, sockets=None: seen.update(link=control.ask("link")))
    monkeypatch.setattr(server.sys.stdin, "isatty", lambda: False, raising=False)
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store)
    server.serve(app, port=0, open_browser=False, interval=3600)
    assert "/login?token=" in seen["link"]["link"]
    assert seen["link"]["link"] not in out.getvalue()  # a new one, not the printed one


# --- ship-fleet ui link --local ----------------------------------------------------------


def test_link_local_asks_the_running_ui(home, runtime):
    ctl = control.Control({"link": lambda: {"link": "http://127.0.0.1:8765/login?token=abc"}})
    assert ctl.start()
    try:
        out = io.StringIO()
        assert cli.main(["ui", "link", "--local"], cli.App(out=out, err=out)) == 0
    finally:
        ctl.stop()
    text = out.getvalue()
    assert "http://127.0.0.1:8765/login?token=abc" in text
    assert "ssh -L 8765:127.0.0.1:8765" in text


def test_link_local_without_a_running_ui_says_so(home, runtime):
    out = io.StringIO()
    assert cli.main(["ui", "link", "--local"], cli.App(out=out, err=out)) == 1
    assert "No ship-fleet ui is running here" in out.getvalue()


def test_link_local_is_not_a_pairing(home, runtime):
    out = io.StringIO()
    assert cli.main(["ui", "link", "--local", "--name", "laptop"],
                    cli.App(out=out, err=out)) == 1
    assert "pairing a remote device" in out.getvalue()


# --- ship-fleet service ------------------------------------------------------------------


def test_install_writes_a_user_unit_and_starts_it(store, home, units, ship_fleet):
    system = FakeSystem()
    app, out = app_with(store, system)
    assert cli.main(["service", "install", "--ship-fleet", str(ship_fleet)], app) == 0
    unit = units / "fleetledger.service"
    text = unit.read_text()
    assert stat.S_IMODE(unit.stat().st_mode) == 0o600
    assert f"ExecStart={ship_fleet} ui --service --port 8765 --interval 30\n" in text
    # The folders of the shell it was installed from: the service shares them.
    assert f"Environment=FLEETLEDGER_CONFIG_DIR={home / 'config'}\n" in text
    assert f"Environment=FLEETLEDGER_DATA_DIR={home / 'data'}\n" in text
    for line in ("KillMode=mixed", "TimeoutStopSec=45min", "Restart=on-failure", "UMask=0077",
                 "NoNewPrivileges=yes", "WantedBy=default.target", "StartLimitIntervalSec=0",
                 "RestartPreventExitStatus=2", "LockPersonality=yes", "LimitCORE=0",
                 "SystemCallArchitectures=native"):
        assert line in text.splitlines()
    assert system.verbs() == [["show-environment"], ["daemon-reload"],
                              ["enable", "fleetledger.service"],
                              ["is-active", "fleetledger.service"],
                              ["start", "fleetledger.service"]]
    said = out.getvalue()
    assert "sudo loginctl enable-linger" in said
    assert "ship-fleet ui link --local" in said
    assert "token" not in text
    # Ubuntu gives the user manager 5 seconds at a reboot: said, with the root fix.
    assert "A reboot cuts running jobs short: it gives this user's systemd manager 5 seconds" \
        in said
    assert f"/etc/systemd/system/user@{os.getuid()}.service.d/zz-fleetledger-drain.conf" in said
    assert "TimeoutStopSec=45min" in said
    # systemd orders drop-ins by name across folders: it must come after Ubuntu's.
    assert service.DROP_IN > "timeout.conf"


def test_install_says_a_reboot_drains_once_root_allowed_it(store, home, units, ship_fleet):
    system = FakeSystem()
    system.reboot_stop = "45min"
    app, out = app_with(store, system)
    assert cli.main(["service", "install", "--ship-fleet", str(ship_fleet)], app) == 0
    assert "A reboot also lets running jobs finish first" in out.getvalue()
    assert "user@" not in out.getvalue()


def test_systemd_time_spans_are_read():
    assert [service._seconds(t) for t in ("5s", "2min", "1h 30min", "infinity", "100ms", "")] \
        == [5.0, 120.0, 5400.0, float("inf"), 0.1, None]


def test_a_handed_over_copy_tells_systemd_not_to_start_it_again(store, home, monkeypatch):
    from fleetledger import handover

    handover.write(store, handover.Marker("retired", handover.now(), "vm.example", 1))
    monkeypatch.setattr(server, "serve", lambda app, **kw: pytest.fail("must not start"))
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store)
    assert cli.main(["ui", "--service", "--port", "8765"], app) == service.REFUSED
    assert "retired" in out.getvalue()
    assert cli.main(["ui", "--port", "8765"], cli.App(out=out, err=out,
                                                      store_factory=lambda: store)) == 1


def test_install_uses_the_remote_uis_port(store, home, units, ship_fleet):
    from fleetledger.web import remote

    remote.save(remote.Remote("https://fleetledger.example-tailnet.ts.net", 8800,
                              ("you@example.com",)))
    app, out = app_with(store, FakeSystem(linger="yes"))
    assert cli.main(["service", "install", "--ship-fleet", str(ship_fleet)], app) == 0
    assert "--port 8800 " in (units / "fleetledger.service").read_text()
    assert "Lingering is on" in out.getvalue()
    app, out = app_with(store, FakeSystem())
    assert cli.main(["service", "install", "--ship-fleet", str(ship_fleet), "--port", "8765"],
                    app) == 1
    assert "set up on port 8800" in out.getvalue()


def test_install_refuses_without_a_systemd_user_manager(store, home, units, ship_fleet):
    system = FakeSystem(manager=False)
    app, out = app_with(store, system)
    assert cli.main(["service", "install", "--ship-fleet", str(ship_fleet)], app) == 1
    assert "No systemd user manager" in out.getvalue()
    assert not (units / "fleetledger.service").exists()


def test_install_never_replaces_a_unit_it_did_not_write(store, home, units, ship_fleet):
    units.mkdir(parents=True)
    (units / "fleetledger.service").write_text("[Service]\nExecStart=/bin/true\n")
    app, out = app_with(store, FakeSystem())
    assert cli.main(["service", "install", "--ship-fleet", str(ship_fleet)], app) == 1
    assert "did not write it" in out.getvalue()
    assert (units / "fleetledger.service").read_text().startswith("[Service]")


def test_install_refuses_a_folder_a_unit_cannot_hold(store, home, units, ship_fleet,
                                                    monkeypatch):
    monkeypatch.setenv("FLEETLEDGER_DATA_DIR", str(home / "my data"))
    app, out = app_with(store, FakeSystem())
    assert cli.main(["service", "install", "--ship-fleet", str(ship_fleet)], app) == 2
    assert "FLEETLEDGER_DATA_DIR" in out.getvalue()
    assert not (units / "fleetledger.service").exists()


def test_installing_again_over_a_running_service_says_to_restart(store, home, units,
                                                                ship_fleet):
    system = FakeSystem()
    app, _ = app_with(store, system)
    assert cli.main(["service", "install", "--ship-fleet", str(ship_fleet)], app) == 0
    app, out = app_with(store, system)
    assert cli.main(["service", "install", "--ship-fleet", str(ship_fleet), "--interval",
                     "60"], app) == 0
    assert "ship-fleet service restart" in out.getvalue()
    assert ["start", "fleetledger.service"] not in system.verbs()[5:]


def test_status_restart_and_uninstall(store, home, units, ship_fleet, runtime):
    system = FakeSystem()
    app, _ = app_with(store, system)
    assert cli.main(["service", "install", "--ship-fleet", str(ship_fleet)], app) == 0
    ctl = control.Control({"status": lambda: {
        "url": "http://127.0.0.1:8765", "remote": None, "service": True, "stopping": False,
        "jobs": [{"id": 3, "title": "Update", "ships": ["ship-01"]}]}})
    assert ctl.start()
    try:
        app, out = app_with(store, system)
        assert cli.main(["service", "status"], app) == 0
        said = out.getvalue()
        assert "Service: active, enabled." in said
        assert "running: Update (ship-01)" in said
        assert "systemctl --user kill" not in said  # not stopping
        app, out = app_with(store, system)
        assert cli.main(["service", "restart"], app) == 0
        assert "lets 1 running job(s) finish first" in out.getvalue()
        assert ["systemctl", "--user", "--no-block", "restart", "fleetledger.service"] in \
            system.calls
    finally:
        ctl.stop()
    app, out = app_with(store, system)
    assert cli.main(["service", "uninstall"], app) == 0
    assert ["disable", "--now", "fleetledger.service"] in system.verbs()
    assert not (units / "fleetledger.service").exists()
    app, out = app_with(store, system)
    assert cli.main(["service", "status"], app) == 1
    assert "not installed as a service" in out.getvalue()


def test_the_unit_is_one_systemd_accepts(tmp_path, ship_fleet):
    """systemd's own check of the unit, where systemd-analyze is installed."""
    import shutil
    import subprocess

    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("no systemd-analyze here")
    unit = tmp_path / "fleetledger.service"
    unit.write_text(service.render_unit(str(ship_fleet), 8765, 30.0,
                                        [("FLEETLEDGER_CONFIG_DIR", "/home/example/cfg")]))
    proc = subprocess.run([analyze, "--user", "verify", str(unit)], capture_output=True,
                          text=True, timeout=60, check=False)
    complaints = [line for line in (proc.stdout + proc.stderr).splitlines()
                  if "fleetledger.service" in line and "Unknown key" not in line]
    assert proc.returncode == 0 or not complaints, proc.stdout + proc.stderr


def test_a_real_service_process_stops_cleanly_on_sigterm(tmp_path, runtime):
    """ship-fleet ui --service as its own process, signalled as systemd stops it: it
    answers on its socket, prints no link, and exits 0 with the socket gone."""
    import subprocess
    import sys

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "FLEETLEDGER_CONFIG_DIR": str(tmp_path / "config"),
           "FLEETLEDGER_DATA_DIR": str(tmp_path / "data"),
           "FLEETLEDGER_RUNTIME_DIR": str(runtime), "PYTHONUNBUFFERED": "1"}
    env.pop("SSH_AUTH_SOCK", None)
    proc = subprocess.Popen([sys.executable, "-m", "fleetledger.cli", "ui", "--service",
                             "--local", "--port", str(port), "--interval", "3600"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, cwd=tmp_path)
    try:
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                status = control.ask("status")
                break
            except FleetError:
                time.sleep(0.1)
        assert status is not None and status["service"] is True and status["port"] == port
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, out
    assert b"token" not in out and b"FleetLedger service is running" in out
    assert not (runtime / control.SOCKET).exists()


def test_status_while_stopping_says_how_to_stop_at_once(store, home, units, ship_fleet,
                                                        runtime):
    system = FakeSystem(active=True)
    ctl = control.Control({"status": lambda: {
        "url": "http://127.0.0.1:8765", "remote": None, "service": True, "stopping": True,
        "jobs": [{"id": 3, "title": "Update", "ships": ["ship-01"]}]}})
    assert ctl.start()
    try:
        app, out = app_with(store, system)
        cli.main(["service", "status"], app)
    finally:
        ctl.stop()
    assert "Stopping: waiting for 1 running job(s) to finish. To stop at once, cutting them " \
        "short: systemctl --user kill -s TERM fleetledger" in out.getvalue()


def test_an_open_page_learns_fleetledger_is_stopping(web):  # noqa: F811
    web.sign_in()
    assert 'hx-get="/partials/banners"' in web.client.get("/").text
    assert "FleetLedger is stopping" not in web.client.get("/partials/banners").text
    web.app_ui.jobs.drain()
    assert "FleetLedger is stopping" in web.client.get("/partials/banners").text


def test_two_homes_with_long_runtime_folders_get_their_own_control_socket(monkeypatch,
                                                                         tmp_path,
                                                                         short_folder):  # noqa: F811
    first = tmp_path / ("x" * 60) / "one"
    second = tmp_path / ("x" * 60) / "two"
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", str(first))
    a = control.socket_path()
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", str(second))
    b = control.socket_path()
    assert a != b and a.parent == b.parent and len(str(a)) <= 100
    assert a.name.endswith("-ui.sock")
