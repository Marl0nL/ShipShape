"""Run the web UI: `ship-fleet ui`.

- Binds 127.0.0.1 only; there is deliberately no option to listen elsewhere.
  Your other devices reach it only through Tailscale Serve, once you turn that
  on (`ship-fleet ui remote`, web/remote.py), and only after a check that Serve
  sends exactly this computer's name to this port, on the tailnet only. There
  each browser pairs once (web/devices.py: `ship-fleet ui link`, `devices`,
  `revoke`).
- Prints a one-time sign-in link and, unless --no-browser, opens it. The
  browser is opened on a private (0600) local file that refreshes to the link,
  so the token never appears in any process's command line. The link is always
  printed too, for browsers that cannot read that file (snap-confined ones).
- Press Enter in the terminal for a new link (each works once, for 15 minutes), or
  run `ship-fleet ui link --local` anywhere on this computer, which asks the running
  UI over its private control socket (web/control.py).
- Request logging is off: no paths, queries, tokens or form bodies are logged.

As a service (`ship-fleet ui --service`, which `ship-fleet service install` puts in a
systemd user unit; docs/runbooks/fleetledger-service.md):

- no terminal is needed and no browser is opened, and no sign-in link is ever
  printed (its output goes to the journal): `ship-fleet ui link --local` asks the
  service for one, and `ship-fleet ui link` pairs a remote device as ever;
- it needs a fixed port (the remote UI's, when that is on);
- the poller polls every Ship all the time, not just while a page is open, so the
  charts and Needs attention are current whenever you look;
- a stop (SIGTERM: systemctl stop or restart) first drains: nothing new starts and
  the running jobs finish, for up to DRAIN_TIMEOUT, while the pages keep answering
  and say so. A second signal stops it at once, cutting them short.
"""

from __future__ import annotations

import math
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from fleetledger import paths
from fleetledger.errors import FleetError

HOST = "127.0.0.1"
DRAIN_TIMEOUT = 40 * 60  # an image bake's own limit; the unit's TimeoutStopSec is longer
# A second stop signal under systemd (a second systemctl stop is merged into the first).
STOP_NOW = "systemctl --user kill -s TERM fleetledger"


def bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, port))
    except OSError as exc:
        sock.close()
        raise FleetError(f"Cannot listen on {HOST}:{port} ({exc.strerror}).",
                         hint="Is another ship-fleet ui running there already? Else pick "
                              "another --port (--port 0 for any free one; with the remote "
                              "UI on, ship-fleet ui remote ... --port to move it).") from exc
    sock.listen(64)
    return sock


def open_file(link: str, port: int) -> Path:
    """A private page that sends the browser to the link (the token stays out of argv)."""
    folder = paths.private_dir(paths.runtime_dir())
    path = folder / f"ui-{port}.html"
    page = (
        '<!doctype html><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0;url={link}">'
        "<title>FleetLedger</title><p>Opening FleetLedger…</p>"
    )
    paths.write_private(path, page.encode("utf-8"))
    return path


def uvicorn_config(asgi: Any, **kwargs: Any):
    import uvicorn

    return uvicorn.Config(
        asgi,
        access_log=False,  # never log paths: the sign-in link carries a token
        log_level="warning",
        proxy_headers=False,
        server_header=False,
        date_header=False,
        ws="websockets-sansio",
        lifespan="off",
        **kwargs,
    )


def service_server(config: Any, jobs: Any, say: Any, timeout: float = DRAIN_TIMEOUT):
    """A uvicorn server whose stop drains first (the service, ship-fleet ui --service)."""
    import uvicorn

    class ServiceServer(uvicorn.Server):
        """SIGTERM or SIGINT: refuse new jobs and let the running ones finish (up to
        timeout), then stop; a second signal stops at once. The signal is not raised
        again after the stop (uvicorn's own handler does that, which would end the
        process before the jobs' records are saved)."""

        def __init__(self, cfg: Any) -> None:
            super().__init__(cfg)
            self.draining = threading.Event()

        def handle_exit(self, sig: int, frame: Any) -> None:
            # A signal handler: only flags and a thread here, never a lock the
            # interrupted code may hold.
            if self.draining.is_set():
                self.should_exit = True
                return
            self.draining.set()
            threading.Thread(target=self.drain, name="ui-drain", daemon=True).start()

        def drain(self) -> None:
            left = jobs.drain()
            if left:
                say(f"Stopping once {len(left)} running job(s) finish (at most "
                    f"{int(timeout // 60)} minutes); nothing new starts. To stop at once, "
                    f"cutting them short: {STOP_NOW}")
                still = jobs.wait(timeout)
                if still:
                    say(f"Stopping now: {len(still)} job(s) still running are cut short.")
            self.should_exit = True

    return ServiceServer(config)


def serve(app: Any, *, port: int = 0, open_browser: bool = True,
          interval: float = 30.0, start_page: str | None = None,
          remote: Any = None, service: bool = False) -> int:
    """remote: a checked web.remote.Remote to accept as well (cmd_ui), else None.
    service: run as a service (see the module's docstring)."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - a broken install
        raise FleetError("The web UI needs FleetLedger's web dependencies.",
                         hint="Rerun fleetledger/install.sh.") from exc
    from fleetledger.history import History, Operations
    from fleetledger.web.app import create_app
    from fleetledger.web.control import Control
    from fleetledger.web.devices import Devices
    from fleetledger.web.fleet import Fleet
    from fleetledger.web.flows import Concierge
    from fleetledger.web.jobs import Jobs
    from fleetledger.web.remote import Allowed
    from fleetledger.web.security import Gate

    store = app.store_factory()
    if store.exists():
        store.load()  # fail early on a broken inventory (none at all: the setup page)
    else:
        start_page = "/setup"
    sock = bind(remote.port if remote is not None else port)
    port = sock.getsockname()[1]
    # The remote address's paired browsers, kept across restarts (web/devices.py).
    # Who may use it follows remote-ui.json while it runs (web/remote.py, Allowed).
    gate = Gate(port, remote=remote, devices=Devices() if remote is not None else None,
                allowed=Allowed(remote) if remote is not None else None)
    # A service polls all the time: nobody need be looking for the charts to fill.
    fleet = Fleet(app, interval=interval, history=History(),
                  **({"idle_after": math.inf} if service else {}))
    concierge = Concierge(app, on_finished=lambda ship: fleet.poll([ship]))
    jobs = Jobs(app, on_finished=lambda ships: fleet.poll(ships), store=Operations())
    asgi = create_app(app, gate, fleet=fleet, concierge_=concierge, start_page=start_page,
                      jobs=jobs)
    config = uvicorn_config(asgi)
    server = service_server(config, jobs, app.say) if service else uvicorn.Server(config)

    def status() -> dict[str, Any]:
        return {"port": port, "url": gate.base_url,
                "remote": remote.origin if remote is not None else None,
                "service": service, "stopping": jobs.stopping, "pid": os.getpid(),
                "jobs": [{"id": j.id, "title": j.title, "ships": list(j.ships)}
                         for j in jobs.running()]}

    control = Control({"link": lambda: {"link": gate.link(gate.new_token())},
                       "status": status})
    try:
        listening = control.start()
    except FleetError:
        listening = False
    link = None
    if service:
        _say_service(app, gate, remote, listening)
    else:
        link = _say_terminal(app, gate, remote)
    opened = None
    if open_browser and link is not None:
        opened = open_file(link, port)
        if webbrowser.open(opened.as_uri()):
            app.say("Opened it in your browser. If the page is blank or not found (a "
                    "sandboxed browser, such as a snap, may not read that file), open the "
                    "link above instead.")
    if sys.stdin.isatty() and not service:
        app.say("Press Enter for a new sign-in link; Ctrl-C to stop.")

        def links() -> None:
            for _ in sys.stdin:  # one new link per address, each with its own token
                app.say(f"  {gate.link(gate.new_token())}")
                if remote is not None:
                    app.say(f"  {gate.link(gate.new_token(), remote.origin)}")

        threading.Thread(target=links, name="ui-links", daemon=True).start()

    fleet.start()
    try:
        server.run(sockets=[sock])
    finally:
        control.stop()
        for job in jobs.stop():  # cut short: say what to do about each
            app.note(f"Stopped while running: {job.title} ({', '.join(job.ships)}). "
                     + ("Its Ship is in the inventory but not enrolled: open the UI "
                        "again to finish enrolling it, or destroy it."
                        if job.kind == "create" else "Check its Ships once the UI is back."))
        concierge.cancel_all()
        fleet.stop()
        if opened is not None:
            opened.unlink(missing_ok=True)
        sock.close()
    return 0


def _say_service(app: Any, gate: Any, remote: Any, listening: bool) -> None:
    """What a service says as it starts: where it is and how to get in, never a link
    (this goes to the journal, which is no place for a token)."""
    app.say(f"FleetLedger service is running on {gate.base_url}"
            + (f", and on {remote.origin} for your devices (Tailscale Serve)."
               if remote is not None else " (this computer only)."))
    if listening:
        app.say("A sign-in link for this computer, or an ssh tunnel to it: ship-fleet ui "
                "link --local.")
    else:
        app.say("Another ship-fleet ui on this computer holds the control socket, so "
                "ship-fleet ui link --local reaches that one, not this.")
    if remote is not None:
        app.say("Pair one of your devices: ship-fleet ui link --name NAME (over ssh is fine).")


def _say_terminal(app: Any, gate: Any, remote: Any) -> str:
    """What ship-fleet ui says in its terminal; returns this computer's first link."""
    link = gate.link(gate.new_token())
    # The link goes to this terminal only (your own screen, not a command line
    # or a log). The browser gets a private page that refreshes to it, so the
    # token never appears in the browser's command line.
    if remote is None:
        app.say(f"FleetLedger is running on {gate.base_url} (this computer only).")
        app.say(f"Sign-in link (works once, for 15 minutes):\n  {link}")
    else:
        app.say(f"FleetLedger is running on {gate.base_url}, and on {remote.origin} for "
                "your other devices, through Tailscale Serve.")
        app.say(f"Sign-in link for this computer (works once, for 15 minutes):\n  {link}")
        # Its own token: the browser opened here uses up the one above at once.
        app.say("Sign-in link for one of your other devices (works once, for 15 minutes):"
                f"\n  {gate.link(gate.new_token(), remote.origin)}")
        app.say(f"Devices paired before stay signed in. Pair another: ship-fleet ui link "
                f"--name NAME (over ssh is fine). Only {', '.join(remote.logins)} may use "
                f"{remote.origin}.")
    return link


def cmd_link_local(args, app) -> int:
    """ship-fleet ui link --local: a one-time sign-in link for this computer's address,
    from the running UI (web/control.py)."""
    from fleetledger.web import control

    for flag, given in (("--name", args.name), ("--machine", args.machine)):
        if given:
            raise FleetError(f"--local makes a sign-in link for this computer; {flag} is for "
                             "pairing a remote device (leave out --local).")
    answer = control.ask("link")
    link, port = str(answer.get("link", "")), _port_of(answer.get("link", ""))
    app.say(f"Sign-in link for this computer (works once, for 15 minutes):\n  {link}")
    if args.qr:
        app.say("(No QR code for this one: it opens on this computer, or through an ssh "
                "tunnel to it, not on a phone.)")
    app.say(f"From another computer, tunnel to it first, then open the link there: ssh -L "
            f"{port}:127.0.0.1:{port} THIS-COMPUTER")
    return 0


def _port_of(link: str) -> str:
    from urllib.parse import urlsplit

    try:
        return str(urlsplit(link).port or "PORT")
    except ValueError:
        return "PORT"


def cmd_ui(args, app) -> int:
    from fleetledger.web import devices, remote

    verb = getattr(args, "ui_command", None)
    if verb == "remote":
        return remote.cmd_remote(args, app)
    if verb == "link" and getattr(args, "link_local", False):
        return cmd_link_local(args, app)
    if verb in ("link", "devices", "revoke"):
        return getattr(devices, f"cmd_{verb}")(args, app)
    service = getattr(args, "service", False)
    found = remote.for_ui(app, port=args.port, local=args.local)  # refuses on a bad setup
    if service and found is None and not args.port:
        raise FleetError("A service needs a fixed port: --port N (8765, say), so an ssh "
                         "tunnel or Tailscale Serve knows where it is.")
    kwargs = {"remote": found} if found is not None else {}
    if service:
        kwargs["service"] = True
    return serve(app, port=args.port, open_browser=not (args.no_browser or service),
                 interval=args.interval, **kwargs)
