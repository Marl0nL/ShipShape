"""Certificates and trusted machines, under Settings ▸ Access, plus a Ship's Renew.

- Certificates: what `ship-fleet ca show` reports (trustcmd.ca_rows), read from files
  only: Ships are judged against the cached revocation list, never a fresh one (that
  needs ssh-keygen). **Renew all** (`ca sign`, with `--force` to re-issue even if
  current) and a Ship's **Renew** (`ca sign SHIP`, from its row, its More menu and its
  certificate alerts) run as jobs.
- Machines: **Trust a machine** runs `ship-fleet trust NAME --key LINE`. The key field
  takes one `ssh-ed25519` public-key line and nothing else: the CLI reads anything not
  starting with `ssh-` as a file path, so a path (or a private key) is refused here,
  before the CLI sees it. Changing a machine's Ships (`trust NAME --scope`), Untrust
  (`untrust NAME`, behind the dialog) and renewing this computer's certificate
  (`trust --this-computer`) are jobs too. What a machine may log in as stays the
  /setup/machine form.
- Every action is the CLI's own command (jobs.run_cli), so its checks, messages and
  audit entries are the CLI's.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from fleetledger.errors import FleetError

KINDS = ("certs", "trust")  # the jobs this module starts
HOURS_MAX = 24 * 365
SCOPE_HOW = "Tick the Ships it may reach: every Ship, or some tags and Ships (not both)."
NOT_A_KEY = ("Paste the machine's public key: the one line in its .pub file, starting "
             "ssh-ed25519. Never a private key, and not a file's path.")


def _ui(request: Request):
    return request.app.state.ui


def _flash(request: Request, kind: str, text: str) -> None:
    from fleetledger.web.app import session_of

    _ui(request).flash(session_of(request), kind, text)


def public_key_line(text: str) -> str | None:
    """The one ed25519 public-key line pasted, or None for anything else (a path, a
    private key, several lines, another key type)."""
    from fleetledger import ca

    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) != 1 or not ca.ED25519_LINE.match(lines[0]):
        return None
    return lines[0]


def scope_choices(inv: Any) -> list[tuple[str, str]]:
    """(scope, what it says) for the Ships a machine may reach: all, a tag, one Ship."""
    tags = sorted({t for s in inv.ships() for t in (s.tags or ())})
    return ([("all", "every Ship")] + [(f"tag:{t}", f"Ships tagged {t}") for t in tags]
            + [(f"ship:{s.name}", s.name) for s in inv.ships()])


def _scopes(fields: dict[str, list[str]]) -> list[str] | None:
    from fleetledger import ca

    wanted = list(dict.fromkeys(v for v in fields.get("scope", []) if v))
    if not wanted or any(not ca.SCOPE.match(s) for s in wanted):
        return None
    if "all" in wanted and len(wanted) > 1:  # every Ship and some: ambiguous
        return None
    return wanted


def section_context(ui: Any, inv: Any) -> dict[str, Any]:
    """What Settings ▸ Access shows about certificates and machines. Reads files only."""
    from fleetledger import ca, trustcmd
    from fleetledger.web.setup import _tilde

    jobs = {kind: [j for j in ui.jobs.recent() if j.kind == kind][:3] for kind in KINDS}
    if ca.config(inv) is None:
        return {"certs": None}
    machines = ca.machines(inv)
    bundles = {}
    for m in machines:
        folder = ca.bundle_dir(m.name)
        if (folder / "install.sh").is_file():
            bundles[m.name] = _tilde(folder)
    return {
        "certs": trustcmd.ca_rows(inv, ca.cached_krl(inv)),
        "revoked": [m for m in machines if m.revoked],
        "bundles": bundles,
        "fingerprints": {m.name: ca.fingerprint(m.public_key) for m in machines},
        "this_until": trustcmd.this_computer_until(inv),
        "scope_choices": scope_choices(inv),
        "cert_jobs": jobs["certs"],
        "machine_jobs": jobs["trust"],
    }


def _start(request: Request, kind: str, ships: list[str], argv: list[str],
           title: str) -> bool:
    from fleetledger.web import jobs

    ui = _ui(request)

    def run(job: Any) -> int:
        return jobs.run_cli(ui.app, argv, job)

    try:
        ui.jobs.start(kind, ships, run, title=title)
    except FleetError as exc:
        _flash(request, "bad", exc.message)
        return False
    _flash(request, "ok", f"{title}: started; it shows below.")
    return True


def _inventory(request: Request):
    """The inventory, if the fleet has a certificate authority; else None (flashed)."""
    from fleetledger import ca

    try:
        inv = _ui(request).app.store_factory().load()
    except FleetError as exc:
        _flash(request, "bad", exc.message)
        return None
    if ca.config(inv) is None:
        _flash(request, "bad", "The fleet has no certificate authority yet: make it first.")
        return None
    return inv


# Where an action returns on Settings: its block, which shows the flash and its jobs
# (a flash at the top of the page would be out of sight at the anchor).
TO_MACHINES = "/settings?said=machines#machines"
TO_CERTIFICATES = "/settings?said=certificates#certificates"


PLACED = ("certificates", "machines", "overlay")  # Settings blocks that show their flashes


def placed_flashes(request: Request) -> dict[str, Any]:
    """For the Settings page: when an action here sent it back (?said=BLOCK), its
    flashes, to show in that block instead of the top of the page."""
    from fleetledger.web.app import session_of

    said = request.query_params.get("said")
    if said not in PLACED:
        return {}
    return {"flash_at": said, "placed": _ui(request).take_flashes(session_of(request)),
            "flashes": []}


def _to_machines() -> Response:
    return RedirectResponse(TO_MACHINES, status_code=303)


# --- certificates -------------------------------------------------------------------
async def renew_all(request: Request) -> Response:
    """Every Ship's certificates (`ca sign`), or every Ship with a tag (`ca sign --tag
    T`, from the Fleet page's Every Ship menu, which says from=fleet)."""
    from fleetledger import ops
    from fleetledger.web.app import back, form, one
    from fleetledger.web.fleetactions import TAG_RE

    fields = await form(request)
    inv = _inventory(request)
    tag = one(fields, "tag")
    tag = tag if TAG_RE.match(tag) else ""
    if inv is not None:
        force = one(fields, "force") == "yes"
        ships = ops.select(inv, [], tag) if tag else inv.ships()
        title = ("Re-issue every certificate" if force else "Renew certificates") + (
            f" of every {tag} Ship" if tag else "")
        if ships:
            _start(request, "certs", [s.name for s in ships],
                   ["ca", "sign", *(["--tag", tag] if tag else []),
                    *(["--force"] if force else [])], title)
        else:
            _flash(request, "bad", "No Ships to renew.")
    if one(fields, "from") == "fleet":
        return back("/", tag=tag)
    return RedirectResponse(TO_CERTIFICATES, status_code=303)


async def renew_ship(request: Request) -> Response:
    """One Ship's certificates (`ca sign SHIP`): from its row under Certificates, its
    More menu, and its certificate alerts."""
    from fleetledger.web.app import NO_SUCH_SHIP, _known_ship, back, form, not_found, one

    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    if _inventory(request) is not None:
        _start(request, "certs", [name], ["ca", "sign", name], f"Renew {name}'s certificate")
    where = one(fields, "from")
    if where == "fleet":
        return back("/")
    if where == "settings":
        return RedirectResponse(TO_CERTIFICATES, status_code=303)
    if where == "access":
        return back(f"/ships/{name}/access")
    return back(f"/ships/{name}")


# --- machines -----------------------------------------------------------------------
async def trust_machine(request: Request) -> Response:
    from fleetledger import ca, trustcmd
    from fleetledger.web.app import form, one

    fields = await form(request)
    inv = _inventory(request)
    if inv is None:
        return _to_machines()
    name = one(fields, "machine").strip()
    key = public_key_line(one(fields, "key"))
    scope = _scopes(fields)
    hours = one(fields, "hours").strip()
    if not trustcmd.MACHINE_RE.match(name):
        problem = "A machine's name is a-z, 0-9 and -, up to 31 characters (laptop, say)."
    elif ca.machine(inv, name) is not None:
        problem = f"{name} is trusted already: change it in its row, or pick a new name."
    elif key is None:
        problem = NOT_A_KEY
    elif scope is None:
        problem = SCOPE_HOW
    elif one(fields, "logins") not in ("agent", "owner", "both"):
        problem = "Choose what it may log in as."
    elif hours and not (hours.isdigit() and 1 <= int(hours) <= HOURS_MAX):
        problem = f"Hours: a whole number from 1 to {HOURS_MAX}, or empty for the default."
    else:
        problem = None
    if problem:
        _flash(request, "bad", problem)
        return _to_machines()
    # --key=LINE: one argument, and it starts with ssh-, so the CLI never reads a file.
    argv = ["trust", name, f"--key={key}", *[f"--scope={s}" for s in scope],
            f"--logins={one(fields, 'logins')}", *([f"--hours={hours}"] if hours else [])]
    _start(request, "trust", [], argv, f"Trust {name}")
    return _to_machines()


def _trusted(request: Request, inv: Any, name: str) -> Any:
    """The machine called name, if it is trusted (not untrusted); else None (flashed)."""
    from fleetledger import ca, trustcmd

    m = ca.machine(inv, name) if trustcmd.MACHINE_RE.match(name) else None
    if m is None or m.revoked:
        _flash(request, "bad", f"No trusted machine called {name}.")
        return None
    return m


async def machine_scope(request: Request) -> Response:
    """The Ships a machine may reach (`trust NAME --scope ...`): narrowing revokes its
    wider certificates on every Ship at once, so the form has a dialog."""
    from fleetledger import trustcmd
    from fleetledger.web.app import form, one

    fields = await form(request)
    inv = _inventory(request)
    m = inv and _trusted(request, inv, one(fields, "machine"))
    if not m:
        return _to_machines()
    here = trustcmd.this_computer()
    scope = _scopes(fields)
    if here and here["machine"] == m.name:
        _flash(request, "bad", "This computer reaches every Ship (it is yours).")
    elif scope is None:
        _flash(request, "bad", SCOPE_HOW)
    elif set(scope) == set(m.scope):  # the form's order is not the CLI's
        _flash(request, "ok", "No change.")
    elif one(fields, "confirm") != "yes":
        _flash(request, "bad", "Tick the box to confirm.")
    else:
        _start(request, "trust", [], ["trust", m.name, *[f"--scope={s}" for s in scope]],
               f"Change the Ships {m.name} reaches")
    return _to_machines()


async def untrust(request: Request) -> Response:
    from fleetledger.web.app import form, one

    fields = await form(request)
    inv = _inventory(request)
    m = inv and _trusted(request, inv, one(fields, "machine"))
    if not m:
        return _to_machines()
    if one(fields, "confirm") != "yes":
        _flash(request, "bad", "Tick the box to confirm.")
    else:
        _start(request, "trust", [], ["untrust", m.name], f"Untrust {m.name}")
    return _to_machines()


async def renew_here(request: Request) -> Response:
    """This computer's certificate and SSH config, now (`trust --this-computer`)."""
    from fleetledger.web.app import form

    await form(request)
    if _inventory(request) is not None:
        _start(request, "trust", [], ["trust", "--this-computer"],
               "Renew this computer's certificate")
    return _to_machines()


def routes() -> list[Route]:
    return [
        Route("/settings/certs/renew", renew_all, methods=["POST"]),
        Route("/ships/{name}/certs/renew", renew_ship, methods=["POST"]),
        Route("/settings/machines/trust", trust_machine, methods=["POST"]),
        Route("/settings/machines/scope", machine_scope, methods=["POST"]),
        Route("/settings/machines/untrust", untrust, methods=["POST"]),
        Route("/settings/machines/renew-here", renew_here, methods=["POST"]),
    ]
