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
- **Download bundle** (a machine's row; from a paired device only its own machine's,
  in its row, on a Ship's Access tab and in the banner when its certificate is due;
  every other machine's stays with this computer's browser and the CLI) renews the
  machine's certificate with `ship-fleet trust NAME` (with `--hostnames overlay` from
  a paired device: what resolves on this computer may not there) and answers with
  the bundle as shipshape-NAME.tar.gz. It holds no secret: a certificate for a key
  whose private half stays on that machine, the CA's public line, SSH config and
  install.sh. Run in the request (it signs here and contacts no Ship), so the
  download is the answer; never for this computer's own entry, which renews itself.
  Each is in the audit log (ui-bundle, with the device's login).
- Every action is the CLI's own command (jobs.run_cli), so its checks, messages and
  audit entries are the CLI's.
"""

from __future__ import annotations

import io
import re
import tarfile
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from fleetledger.errors import FleetError
from fleetledger.web.common import (
    NO_SUCH_SHIP,
    NOT_FROM_A_DEVICE,
    back,
    flash,
    known_ship,
    session_of,
    ui_of,
)
from fleetledger.web.forms import NAME_RE, form, one

KINDS = ("certs", "trust")  # the jobs this module starts
HOURS_MAX = 24 * 365
SCOPE_HOW = "Tick the Ships it may reach: every Ship, or some tags and Ships (not both)."
NOT_A_KEY = ("Paste the machine's public key: the one line in its .pub file, starting "
             "ssh-ed25519. Never a private key, and not a file's path.")


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
    tags = sorted({t for s in inv.ships() for t in inv.effective_tags(s)})
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

    jobs = {kind: [j for j in ui.jobs.shown() if j.kind == kind][:3] for kind in KINDS}
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
           title: str, after: Any = None) -> bool:
    """Start the command as a job; after(), if given, runs once it has succeeded."""
    from fleetledger.web import jobs

    ui = ui_of(request)

    def run(job: Any) -> int:
        code = jobs.run_cli(ui.app, argv, job)
        if code == 0 and after is not None:
            after(job)
        return code

    try:
        ui.jobs.start(kind, ships, run, title=title)
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return False
    flash(request, "ok", f"{title}: started; it shows below.")
    return True


def _inventory(request: Request):
    """The inventory, if the fleet has a certificate authority; else None (flashed)."""
    from fleetledger import ca

    try:
        inv = ui_of(request).app.store_factory().load()
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return None
    if ca.config(inv) is None:
        flash(request, "bad", "The fleet has no certificate authority yet: make it first.")
        return None
    return inv


# Where an action returns on Settings: its block, which shows the flash and its jobs
# (a flash at the top of the page would be out of sight at the anchor).
TO_MACHINES = "/settings?said=machines#machines"
TO_CERTIFICATES = "/settings?said=certificates#certificates"


# Settings blocks that show their own flashes.
PLACED = ("certificates", "machines", "overlay", "devices")


def placed_flashes(request: Request) -> dict[str, Any]:
    """For the Settings page: when an action here sent it back (?said=BLOCK), its
    flashes, to show in that block instead of the top of the page."""
    said = request.query_params.get("said")
    if said not in PLACED:
        return {}
    return {"flash_at": said, "placed": ui_of(request).take_flashes(session_of(request)),
            "flashes": []}


def _to_machines() -> Response:
    return RedirectResponse(TO_MACHINES, status_code=303)


# --- certificates -------------------------------------------------------------------
async def renew_all(request: Request) -> Response:
    """Every Ship's certificates (`ca sign`), or every Ship with a tag (`ca sign --tag
    T`, from the Fleet page's Every Ship menu, which says from=fleet)."""
    from fleetledger import ops

    fields = await form(request)
    tag = one(fields, "tag")
    if one(fields, "from") == "fleet":  # the Fleet page's Every Ship menu
        where = back("/", tag=tag if ops.TAG_RE.match(tag) else "")
    else:
        where = RedirectResponse(TO_CERTIFICATES, status_code=303)
    if tag and not ops.TAG_RE.match(tag):  # never widened to every Ship
        flash(request, "bad", "Choose every Ship or a tag.")
        return where
    inv = _inventory(request)
    if inv is not None:
        force = one(fields, "force") == "yes"
        try:
            ships = ops.select(inv, [], tag) if tag else inv.ships()
        except FleetError as exc:  # no Ship has that tag (now)
            flash(request, "bad", exc.message)
            return where
        title = ("Re-issue every certificate" if force else "Renew certificates") + (
            f" of every {tag} Ship" if tag else "")
        if ships:
            _start(request, "certs", [s.name for s in ships],
                   ["ca", "sign", *(["--tag", tag] if tag else []),
                    *(["--force"] if force else [])], title)
        else:
            flash(request, "bad", "No Ships to renew.")
    return where


async def renew_ship(request: Request) -> Response:
    """One Ship's certificates (`ca sign SHIP`): from its row under Certificates, its
    More menu, and its certificate alerts."""
    from fleetledger.web.app import not_found

    name = known_ship(request)
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
        flash(request, "bad", problem)
        return _to_machines()
    # --key=LINE: one argument, and it starts with ssh-, so the CLI never reads a file.
    argv = ["trust", name, f"--key={key}", *[f"--scope={s}" for s in scope],
            f"--logins={one(fields, 'logins')}", *([f"--hours={hours}"] if hours else [])]
    device = session_of(request).device
    after = None
    if device is not None and one(fields, "mine") == "yes":
        argv.append("--hostnames=overlay")  # its bundle is for a machine elsewhere
        after = _runs_on(request, device, name)
    _start(request, "trust", [], argv, f"Trust {name}", after)
    return _to_machines()


def _runs_on(request: Request, device: Any, machine: str) -> Any:
    """After trusting a machine from a paired device that says it runs on it: record that
    (devices.change, as ship-fleet ui devices DEVICE --machine NAME)."""
    from fleetledger.web import devices

    ui = ui_of(request)
    store = ui.gate.devices or devices.Devices()

    def after(job: Any) -> None:
        try:
            devices.change(store, ui.app.store_factory().load(), device, machine=machine)
        except FleetError as exc:
            job.write(f"(Not recorded as this device's machine: {exc.message})\n")
        else:
            job.write(f"{device.name} runs on {machine}: Download bundle in its row.\n")

    return after


def _trusted(request: Request, inv: Any, name: str) -> Any:
    """The machine called name, if it is trusted (not untrusted); else None (flashed)."""
    from fleetledger import ca, trustcmd

    m = ca.machine(inv, name) if trustcmd.MACHINE_RE.match(name) else None
    if m is None or m.revoked:
        flash(request, "bad", f"No trusted machine called {name}.")
        return None
    return m


async def machine_scope(request: Request) -> Response:
    """The Ships a machine may reach (`trust NAME --scope ...`): narrowing revokes its
    wider certificates on every Ship at once, so the form has a dialog."""
    from fleetledger import trustcmd

    fields = await form(request)
    inv = _inventory(request)
    m = inv and _trusted(request, inv, one(fields, "machine"))
    if not m:
        return _to_machines()
    here = trustcmd.this_computer()
    scope = _scopes(fields)
    if here and here["machine"] == m.name:
        flash(request, "bad", "This computer reaches every Ship (it is yours).")
    elif scope is None:
        flash(request, "bad", SCOPE_HOW)
    elif set(scope) == set(m.scope):  # the form's order is not the CLI's
        flash(request, "ok", "No change.")
    elif one(fields, "confirm") != "yes":
        flash(request, "bad", "Tick the box to confirm.")
    else:
        _start(request, "trust", [], ["trust", m.name, *[f"--scope={s}" for s in scope]],
               f"Change the Ships {m.name} reaches")
    return _to_machines()


async def untrust(request: Request) -> Response:
    fields = await form(request)
    inv = _inventory(request)
    m = inv and _trusted(request, inv, one(fields, "machine"))
    if not m:
        return _to_machines()
    if one(fields, "confirm") != "yes":
        flash(request, "bad", "Tick the box to confirm.")
    else:
        _start(request, "trust", [], ["untrust", m.name], f"Untrust {m.name}")
    return _to_machines()


async def renew_here(request: Request) -> Response:
    """This computer's certificate and SSH config, now (`trust --this-computer`). Never
    from a paired device: it is the FleetLedger computer's, not the device's."""

    await form(request)
    if session_of(request).device is not None:
        flash(request, "bad", NOT_FROM_A_DEVICE)
    elif _inventory(request) is not None:
        _start(request, "trust", [], ["trust", "--this-computer"],
               "Renew this computer's certificate")
    return _to_machines()


NOT_ITS_MACHINE = ("From {device}, only the bundle of the machine it runs on: {machine} is "
                   "not it. Say which machine it is on a Ship's Access tab. Any machine's "
                   "bundle comes from the FleetLedger computer's own browser, or ship-fleet "
                   "trust NAME there.")
BUNDLE_FILES = ("config", "known_hosts", "install.sh", "bundle.json")  # and NAME-cert.pub
IDENTITY_LINE = re.compile(r"^\s*IdentityFile\s+(\S+)\s*$", re.M)


def _identity_before(name: str) -> str | None:
    """The private key path the machine's last bundle named, so a renewal keeps it."""
    from fleetledger import ca

    try:
        found = IDENTITY_LINE.search((ca.bundle_dir(name) / "config").read_text())
    except OSError:
        return None
    path = found.group(1) if found else None
    return path if path and re.fullmatch(r"[~A-Za-z0-9_./-]{1,200}", path) else None


def bundle_archive(name: str) -> bytes:
    """The machine's bundle folder as shipshape-NAME.tar.gz: its known files only, owned
    by nobody in particular, 0600 in a 0700 folder."""
    from fleetledger import ca

    folder = ca.bundle_dir(name)
    top = f"shipshape-{name}"

    def tidy(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mode = 0o700 if info.isdir() else 0o600
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(folder, arcname=top, recursive=False, filter=tidy)
        for file in (*BUNDLE_FILES, f"{name}-cert.pub"):
            tar.add(folder / file, arcname=f"{top}/{file}", filter=tidy)
    return buf.getvalue()


async def download_bundle(request: Request) -> Response:
    """Renew a trusted machine's certificate (`ship-fleet trust NAME`) and answer with its
    bundle to download. from=access&ship=S returns to that Ship's Access tab on a
    refusal; else Trusted machines."""
    from fleetledger import trustcmd
    from fleetledger.concierge import audit
    from fleetledger.web import devicesweb, jobs

    fields = await form(request)
    ship = one(fields, "ship")
    where = (f"/ships/{ship}/access" if one(fields, "from") == "access" and ship
             and NAME_RE.match(ship) else TO_MACHINES)
    inv = _inventory(request)
    m = inv and _trusted(request, inv, one(fields, "machine"))
    if not m:
        return RedirectResponse(where, status_code=303)
    here = trustcmd.this_computer()
    if here and here["machine"] == m.name:
        flash(request, "bad", f"{m.name} is this computer: FleetLedger renews its certificate "
                              "itself (Renew now).")
        return RedirectResponse(where, status_code=303)
    device = session_of(request).device
    if device is not None:  # a paired device gets its own machine's bundle, no other
        own = devicesweb.machine_of(device, inv)
        if own is None or own.name != m.name:
            flash(request, "bad", NOT_ITS_MACHINE.format(device=device.name, machine=m.name))
            return RedirectResponse(where, status_code=303)
    identity = _identity_before(m.name)
    argv = ["trust", m.name, *([f"--identity={identity}"] if identity else []),
            *(["--hostnames=overlay"] if device is not None else [])]
    said = io.StringIO()
    code = await run_in_threadpool(jobs.run_cli, ui_of(request).app, argv, said)
    if code != 0:
        lines = [x for x in said.getvalue().splitlines() if x.strip()]
        flash(request, "bad", f"{m.name}'s bundle was not renewed: "
                              + (lines[-1] if lines else "the command failed."))
        return RedirectResponse(where, status_code=303)
    audit.record("-", "ui-bundle", m.name, "ok", account=device.login if device else None)
    # The pages read the poller's inventory: take the new certificate's date in now
    # (no Ship is asked), so a "certificate due" banner goes at the next page.
    await run_in_threadpool(ui_of(request).fleet.poll, [])
    try:
        data = await run_in_threadpool(bundle_archive, m.name)
    except OSError as exc:
        flash(request, "bad", f"{m.name}'s certificate was renewed, but its bundle could not "
                              f"be read ({exc.strerror}).")
        return RedirectResponse(where, status_code=303)
    return Response(data, media_type="application/gzip", headers={
        "content-disposition": f'attachment; filename="shipshape-{m.name}.tar.gz"'})


def routes() -> list[Route]:
    return [
        Route("/settings/certs/renew", renew_all, methods=["POST"]),
        Route("/ships/{name}/certs/renew", renew_ship, methods=["POST"]),
        Route("/settings/machines/trust", trust_machine, methods=["POST"]),
        Route("/settings/machines/scope", machine_scope, methods=["POST"]),
        Route("/settings/machines/untrust", untrust, methods=["POST"]),
        Route("/settings/machines/renew-here", renew_here, methods=["POST"]),
        Route("/settings/machines/bundle", download_bundle, methods=["POST"]),
    ]
