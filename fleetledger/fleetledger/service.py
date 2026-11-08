"""FleetLedger as a service: `ship-fleet service` (docs/runbooks/fleetledger-service.md).

The web UI and its poller, always on, under systemd, with no terminal: what an
always-on FleetLedger computer (a small VM, say) runs. Off unless you install it;
nothing changes for a FleetLedger you start yourself with `ship-fleet ui`.

- install: write a systemd **user** unit (~/.config/systemd/user/fleetledger.service)
  that runs `ship-fleet ui --service --port N`, then enable and start it. A user
  unit runs as you, with your FleetLedger folders and ssh, and needs no root; with
  lingering on (`loginctl enable-linger`, which needs root once) it starts at boot
  and keeps running when you log out. The unit carries the FLEETLEDGER_* and XDG
  folder settings of the shell you install it from, so the service uses the same
  folders as your commands.
- status: whether systemd runs it, and what the running service says over its
  control socket (web/control.py): its address, and the jobs still running.
- restart: stop it (it lets running jobs finish first: web/server.py) and start it
  again, for an update of FleetLedger itself.
- uninstall: stop and disable it, and remove the unit.

In the unit: Restart=on-failure with a growing delay (Tailscale may not be up yet
just after boot, and the remote UI's check then refuses), no start limit, but no
restart after a refusal only a person can fix (exit status 2: a broken setting, or a
copy handed over by ship-fleet migrate); a stop timeout above the drain's;
KillMode=mixed (the stop signal goes to FleetLedger only, so its ssh children finish
with their jobs); UMask=0077, NoNewPrivileges and the seccomp settings a user unit
can have.

A reboot stops the whole user manager, and Ubuntu gives that 5 seconds
(user@.service's TimeoutStopSec), so a reboot cuts running jobs short unless root
raises it for this user: install says so, with the command (REBOOT_DROP_IN).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from fleetledger import paths
from fleetledger.errors import ConfigError, FleetError

UNIT = "fleetledger.service"
DEFAULT_PORT = 8765
RUNBOOK = "docs/runbooks/fleetledger-service.md"
# Values written into the unit unquoted: no spaces, quotes, % (a systemd specifier),
# $ or newlines.
SAFE = re.compile(r"^[A-Za-z0-9/._+@:,-]{1,400}$")
CARRIED = ("FLEETLEDGER_CONFIG_DIR", "FLEETLEDGER_DATA_DIR", "FLEETLEDGER_RUNTIME_DIR",
           "XDG_CONFIG_HOME", "XDG_DATA_HOME")
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
HEADER = "# Written by ship-fleet service install"
REFUSED = 2  # ui --service exits with it when only a person can fix what stops it
STOP_NOW = "systemctl --user kill -s TERM fleetledger"
DRAIN_FOR = 45 * 60  # seconds a stop may take (the unit's TimeoutStopSec)


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".config") / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT


def run_system(argv: list[str]) -> tuple[int, str, str]:
    """systemctl or loginctl: (exit code, stdout, stderr)."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except FileNotFoundError:
        return 127, "", f"{argv[0]} not found"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", type(exc).__name__
    return proc.returncode, proc.stdout, proc.stderr


def _runner(app: Any):
    return getattr(app, "system", None) or run_system


def _systemctl(app: Any, *args: str) -> tuple[int, str, str]:
    return _runner(app)(["systemctl", "--user", *args])


def _last(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1] if lines else ""


def ship_fleet_path(given: str | None = None) -> str:
    """The ship-fleet command the unit runs, as an absolute path."""
    candidates = [given] if given else [shutil.which("ship-fleet"), _argv0()]
    for found in candidates:
        if found:
            path = Path(found).expanduser()
            if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
                text = str(path)
                if not SAFE.match(text):
                    raise ConfigError(f"{text} has a character a systemd unit would need "
                                      "quoted; install ship-fleet somewhere plainer.")
                return text
    raise ConfigError("Cannot find the ship-fleet command to run as a service.",
                      hint="Install it (fleetledger/install.sh), or name it: --ship-fleet "
                           "/path/to/ship-fleet.")


def _argv0() -> str | None:
    name = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    return str(Path(sys.argv[0]).resolve()) if name == "ship-fleet" else None


def carried_environment(env: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """The folder settings of this shell the service must share."""
    env = os.environ if env is None else env
    out = []
    for name in CARRIED:
        value = env.get(name)
        if not value:
            continue
        value = str(Path(value).expanduser())
        if not SAFE.match(value):
            raise ConfigError(f"{name} has a character a systemd unit would need quoted "
                              "(a space, say); the service could not share it.")
        out.append((name, value))
    return out


def render_unit(ship_fleet: str, port: int, interval: float,
                env: list[tuple[str, str]]) -> str:
    interval_text = f"{interval:g}"
    path = f"{Path(ship_fleet).parent}:{SYSTEM_PATH}"
    lines = [
        f"{HEADER} ({RUNBOOK}).",
        "# ship-fleet service install writes it again; ship-fleet service uninstall removes it.",
        "[Unit]",
        "Description=FleetLedger: the ShipShape fleet's web UI and poller",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        "Type=exec",
        f"ExecStart={ship_fleet} ui --service --port {port} --interval {interval_text}",
        f"Environment=PATH={path}",
        "Environment=PYTHONUNBUFFERED=1",
        *(f"Environment={name}={value}" for name, value in env),
        "WorkingDirectory=%h",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "SystemCallArchitectures=native",
        "Restart=on-failure",
        f"RestartPreventExitStatus={REFUSED}",
        "RestartSec=15s",
        "RestartSteps=6",
        "RestartMaxDelaySec=15min",
        "KillMode=mixed",
        "TimeoutStopSec=45min",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _port(args: Any) -> int:
    from fleetledger.web import remote

    found = remote.load()
    port = args.port if args.port is not None else (found.port if found else DEFAULT_PORT)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise FleetError("--port must be between 1024 and 65535.")
    if found is not None and port != found.port:
        raise FleetError(f"The remote web UI is set up on port {found.port} (Tailscale Serve "
                         f"sends {found.origin} there), not {port}.",
                         hint="Leave out --port, or move the remote UI first: ship-fleet ui "
                              f"remote {found.origin} --port {port} --login ...")
    return port


def _check_systemd(app: Any) -> None:
    code, _out, err = _systemctl(app, "show-environment")
    if code != 0:
        raise FleetError("No systemd user manager answers here, so FleetLedger cannot run as "
                         f"a service{': ' + _last(err) if _last(err) else ''}.",
                         hint="Log in as this user (a desktop session, or ssh, so "
                              "XDG_RUNTIME_DIR is set), then try again. Or run ship-fleet ui in "
                              "a terminal that stays open (tmux).")


def lingering(app: Any) -> bool | None:
    """Does this user's systemd manager run without a login (so the service starts at
    boot)? None when loginctl cannot say."""
    import getpass

    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        return None
    code, out, _err = _runner(app)(["loginctl", "show-user", user, "--property=Linger",
                                    "--value"])
    if code != 0:
        return None
    return out.strip() == "yes"


def reboot_stop_seconds(app: Any) -> float | None:
    """How long a reboot gives this user's systemd manager (and so the service) to
    stop, from user@<uid>.service's TimeoutStopUSec; None when systemctl cannot say."""
    code, out, _err = _runner(app)(["systemctl", "show", f"user@{os.getuid()}.service",
                                    "--property=TimeoutStopUSec", "--value"])
    return _seconds(out.strip()) if code == 0 else None


def _seconds(text: str) -> float | None:
    """systemd's time span ("5s", "2min", "1h 30min", "infinity") in seconds."""
    if text == "infinity":
        return float("inf")
    units = {"us": 1e-6, "ms": 1e-3, "s": 1, "min": 60, "h": 3600, "d": 86400}
    total = 0.0
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(us|ms|min|s|h|d)", text):
        total += float(number) * units[unit]
    return total if text and total else None


def reboot_drop_in() -> str:
    """The root command that lets a reboot wait for running jobs too."""
    uid = os.getuid()
    folder = f"/etc/systemd/system/user@{uid}.service.d"
    return (f"sudo mkdir -p {folder} && printf '[Service]\\nTimeoutStopSec=45min\\n' | "
            f"sudo tee {folder}/fleetledger-drain.conf >/dev/null && sudo systemctl "
            "daemon-reload")


def _say_reboot(app: Any) -> None:
    allowed = reboot_stop_seconds(app)
    if allowed is not None and allowed >= DRAIN_FOR:
        app.say("A reboot also lets running jobs finish first (this user's systemd manager "
                "may take as long as they need).")
        return
    app.say("A reboot cuts running jobs short: it gives this user's systemd manager "
            + (f"{allowed:g} seconds" if allowed is not None else "only a few seconds")
            + " to stop. Before a reboot, ship-fleet service status shows what is running. "
            "To let a reboot wait for them too (once, as root):")
    app.say(f"  {reboot_drop_in()}")


def _say_linger(app: Any) -> None:
    import getpass

    linger = lingering(app)
    if linger:
        app.say("Lingering is on: it starts at boot and runs while you are logged out.")
        return
    user = getpass.getuser()
    app.say("To start it at boot and keep it running when you log out, turn lingering on "
            f"(once, as root): sudo loginctl enable-linger {user}"
            + ("" if linger is not None else "  (loginctl could not say whether it is on)"))


def cmd_install(args: Any, app: Any) -> int:
    from fleetledger.web import control

    port = _port(args)
    interval = args.interval
    if not 5 <= interval <= 3600:
        raise FleetError("--interval: between 5 and 3600 seconds.")
    ship_fleet = ship_fleet_path(args.ship_fleet)
    text = render_unit(ship_fleet, port, interval, carried_environment())
    _check_systemd(app)
    path = unit_path()
    old = path.read_text(encoding="utf-8") if path.is_file() else None
    if old is not None and not old.startswith(HEADER):
        raise FleetError(f"{paths.tilde(path)} exists and ship-fleet did not write it; not "
                         "replacing it.", hint="Move it away first.")
    path.parent.mkdir(parents=True, exist_ok=True)
    paths.write_private(path, text.encode("utf-8"))
    for step in (["daemon-reload"], ["enable", UNIT]):
        code, _out, err = _systemctl(app, *step)
        if code != 0:
            raise FleetError(f"systemctl --user {' '.join(step)} failed: "
                             f"{_last(err) or 'no reason given'}.")
    app.say(f"Installed {paths.tilde(path)}: ship-fleet ui --service on 127.0.0.1:{port}.")
    from fleetledger.concierge import audit

    audit.record("-", "service-install", "service", "ok")
    if args.no_start:
        app.say("Not started (--no-start): systemctl --user start fleetledger.")
    else:
        code, out, _err = _systemctl(app, "is-active", UNIT)
        running = code == 0 and out.strip() == "active"
        if running and old != text:
            app.say("It was running with the old settings: ship-fleet service restart uses "
                    "the new ones (it lets running jobs finish first).")
        elif not running:
            code, _out, err = _systemctl(app, "start", UNIT)
            if code != 0:
                raise FleetError(f"Installed, but it did not start: {_last(err) or '?'}.",
                                 hint="journalctl --user -u fleetledger says why.")
            app.say("Started.")
    _say_linger(app)
    _say_reboot(app)
    app.say(f"Sign in from this computer, or an ssh tunnel to it: ship-fleet ui link --local "
            f"(the socket is {paths.tilde(control.socket_path())}).")
    app.say("Its output: journalctl --user -u fleetledger. ship-fleet service status shows "
            "what it is doing.")
    return 0


def cmd_status(args: Any, app: Any) -> int:
    from fleetledger.web import control

    path = unit_path()
    if not path.is_file():
        app.say("FleetLedger is not installed as a service here (ship-fleet service install).")
    code, out, _err = _systemctl(app, "is-active", UNIT)
    state = out.strip() or "unknown"
    _code, enabled, _err = _systemctl(app, "is-enabled", UNIT)
    app.say(f"Service: {state}" + (f", {enabled.strip()}" if enabled.strip() else "") + ".")
    try:
        answer = control.ask("status")
    except FleetError:
        app.say("The running UI does not answer on its control socket"
                + (" (it may still be starting: journalctl --user -u fleetledger)."
                   if state in ("active", "activating") else "."))
        return 0 if state == "active" else 1
    app.say(f"Answers on {answer.get('url')}" + (f" and {answer['remote']}"
                                                  if answer.get("remote") else "")
            + ("" if answer.get("service") else " (a ship-fleet ui in a terminal, not the "
               "service)") + ".")
    jobs = answer.get("jobs") or []
    if answer.get("stopping"):
        app.say(f"Stopping: waiting for {len(jobs)} running job(s) to finish. To stop at "
                f"once, cutting them short: {STOP_NOW}")
    for job in jobs:
        app.say(f"  running: {job.get('title')} ({', '.join(job.get('ships') or [])})")
    if not jobs and not answer.get("stopping"):
        app.say("No job is running.")
    if path.is_file():
        _say_linger(app)
    return 0 if state == "active" else 1


def cmd_restart(args: Any, app: Any) -> int:
    from fleetledger.web import control

    if not unit_path().is_file():
        raise FleetError("FleetLedger is not installed as a service here.",
                         hint="ship-fleet service install")
    try:
        jobs = control.ask("status").get("jobs") or []
    except FleetError:
        jobs = []
    code, _out, err = _systemctl(app, "--no-block", "restart", UNIT)
    if code != 0:
        raise FleetError(f"systemctl --user restart failed: {_last(err) or '?'}.")
    from fleetledger.concierge import audit

    audit.record("-", "service-restart", "service", "ok")
    app.say("Restarting" + (f": it lets {len(jobs)} running job(s) finish first, starting "
                            "nothing new meanwhile" if jobs else "") + ". ship-fleet service "
            "status shows it.")
    return 0


def cmd_uninstall(args: Any, app: Any) -> int:
    path = unit_path()
    if not path.is_file():
        app.say("FleetLedger is not installed as a service here.")
        return 0
    if not path.read_text(encoding="utf-8").startswith(HEADER):
        raise FleetError(f"{paths.tilde(path)} was not written by ship-fleet; not removing it.")
    app.say("Stopping it (running jobs finish first) ...")
    code, _out, err = _systemctl(app, "disable", "--now", UNIT)
    if code != 0:
        raise FleetError(f"systemctl --user disable --now failed: {_last(err) or '?'}.")
    path.unlink(missing_ok=True)
    _systemctl(app, "daemon-reload")
    from fleetledger.concierge import audit

    audit.record("-", "service-uninstall", "service", "ok")
    app.say(f"Removed {paths.tilde(path)}. Run ship-fleet ui yourself from now on.")
    return 0


VERBS = ("install", "status", "restart", "uninstall")


def add_parsers(sub, add) -> None:
    import argparse

    p = add(sub, "service", "run the web UI and poller as a systemd service (always on)",
            "Run FleetLedger's web UI and poller as a systemd user service: always on,\n"
            "no terminal, started at boot (with lingering on). A stop lets running jobs\n"
            "finish first. Sign in with ship-fleet ui link --local (or pair a device:\n"
            "ship-fleet ui link). See docs/runbooks/fleetledger-service.md.\n\n"
            "  ship-fleet service install            write, enable and start the unit\n"
            "  ship-fleet service status             is it running, and what it is doing\n"
            "  ship-fleet service restart            after an update of FleetLedger\n"
            "  ship-fleet service uninstall          stop it and remove the unit")
    verbs = p.add_subparsers(dest="service_command", metavar="<action>", title="actions")
    i = verbs.add_parser("install", help="write the systemd user unit, enable and start it",
                         formatter_class=argparse.RawDescriptionHelpFormatter)
    i.add_argument("--port", type=int, default=None,
                   help=f"port on 127.0.0.1 (default: the remote UI's, else {DEFAULT_PORT})")
    i.add_argument("--interval", type=float, default=30.0,
                   help="seconds between status checks of every Ship (default 30)")
    i.add_argument("--ship-fleet", metavar="PATH",
                   help="the ship-fleet command to run (default: the one on PATH)")
    i.add_argument("--no-start", action="store_true", help="enable it, but do not start it now")
    verbs.add_parser("status", help="is it running, and the jobs it is running")
    verbs.add_parser("restart", help="restart it, once running jobs finish")
    verbs.add_parser("uninstall", help="stop and disable it, and remove the unit")


def cmd_service(args: Any, app: Any) -> int:
    verb = getattr(args, "service_command", None)
    if verb not in VERBS:
        return cmd_status(args, app)
    return globals()[f"cmd_{verb}"](args, app)
