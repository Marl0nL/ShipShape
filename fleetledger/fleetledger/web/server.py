"""Run the web UI: `ship-fleet ui`.

- Binds 127.0.0.1 only; there is deliberately no option to listen elsewhere.
  Your other devices reach it only through Tailscale Serve, once you turn that
  on (`ship-fleet ui remote`, web/remote.py), and only after a check that Serve
  sends exactly this computer's name to this port, on the tailnet only.
- Prints a one-time sign-in link and, unless --no-browser, opens it. The
  browser is opened on a private (0600) local file that refreshes to the link,
  so the token never appears in any process's command line. The link is always
  printed too, for browsers that cannot read that file (snap-confined ones).
- Press Enter in the terminal for a new link (each works once, for 15 minutes).
- Request logging is off: no paths, queries, tokens or form bodies are logged.
"""

from __future__ import annotations

import socket
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from fleetledger import paths
from fleetledger.errors import FleetError

HOST = "127.0.0.1"


def bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, port))
    except OSError as exc:
        sock.close()
        raise FleetError(f"Cannot listen on {HOST}:{port} ({exc.strerror}).",
                         hint="Pick another --port, or --port 0 for any free one.") from exc
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


def serve(app: Any, *, port: int = 0, open_browser: bool = True,
          interval: float = 30.0, start_page: str | None = None,
          remote: Any = None) -> int:
    """remote: a checked web.remote.Remote to accept as well (cmd_ui), else None."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - a broken install
        raise FleetError("The web UI needs FleetLedger's web dependencies.",
                         hint="Rerun fleetledger/install.sh.") from exc
    from fleetledger.history import History, Operations
    from fleetledger.web.app import create_app
    from fleetledger.web.fleet import Fleet
    from fleetledger.web.flows import Concierge
    from fleetledger.web.jobs import Jobs
    from fleetledger.web.security import Gate

    store = app.store_factory()
    if store.exists():
        store.load()  # fail early on a broken inventory (none at all: the setup page)
    else:
        start_page = "/setup"
    sock = bind(remote.port if remote is not None else port)
    port = sock.getsockname()[1]
    gate = Gate(port, remote=remote)
    fleet = Fleet(app, interval=interval, history=History())
    concierge = Concierge(app, on_finished=lambda ship: fleet.poll([ship]))
    jobs = Jobs(app, on_finished=lambda ships: fleet.poll(ships), store=Operations())
    asgi = create_app(app, gate, fleet=fleet, concierge_=concierge, start_page=start_page,
                      jobs=jobs)
    server = uvicorn.Server(uvicorn_config(asgi))

    token = gate.new_token()
    link = gate.link(token)
    opened = None
    # The link goes to this terminal only (your own screen, not a command line
    # or a log). The browser gets a private page that refreshes to it, so the
    # token never appears in the browser's command line.
    if remote is None:
        app.say(f"FleetLedger is running on {gate.base_url} (this computer only).")
        app.say(f"Sign-in link (works once, for 15 minutes):\n  {link}")
    else:
        app.say(f"FleetLedger is running on {gate.base_url}, and on {remote.origin} for "
                "your other devices, through Tailscale Serve.")
        app.say("Sign-in link (works once, for 15 minutes, on whichever address opens it "
                f"first):\n  {link}\n  {gate.link(token, remote.origin)}")
    if open_browser:
        opened = open_file(link, port)
        if webbrowser.open(opened.as_uri()):
            app.say("Opened it in your browser. If the page is blank or not found (a "
                    "sandboxed browser, such as a snap, may not read that file), open the "
                    "link above instead.")
    if sys.stdin.isatty():
        app.say("Press Enter for a new sign-in link; Ctrl-C to stop.")

        def links() -> None:
            for _ in sys.stdin:
                fresh = gate.new_token()
                app.say(f"  {gate.link(fresh)}")
                if remote is not None:
                    app.say(f"  {gate.link(fresh, remote.origin)}")

        threading.Thread(target=links, name="ui-links", daemon=True).start()

    fleet.start()
    try:
        server.run(sockets=[sock])
    finally:
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


def cmd_ui(args, app) -> int:
    from fleetledger.web import remote

    if getattr(args, "ui_command", None) == "remote":
        return remote.cmd_remote(args, app)
    found = remote.for_ui(app, port=args.port, local=args.local)  # refuses on a bad setup
    kwargs = {"remote": found} if found is not None else {}
    return serve(app, port=args.port, open_browser=not args.no_browser,
                 interval=args.interval, **kwargs)
