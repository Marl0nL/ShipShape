"""The first-run setup in the browser (/setup): the same steps as the terminal
wizard (setupcmd.py), through the same functions, with room to say what each
thing is.

The path a newcomer takes asks only for what they alone can give: where Ships
run (a Proxmox address and token, or manual), which of their SSH keys may log in
to Ships, optionally a Tailscale OAuth client, and a first Ship. Every key
FleetLedger itself needs is made and protected by it (keys.py, "managed keys");
bringing keys from another computer is the one place key choices appear.

Each step shows done or next and can be redone. Secrets typed here (the Proxmox
token secret, the OAuth client secret, a key's current passphrase) arrive in a
POST body only, go straight to the check that needs them and then to their
store, and are never echoed back, flashed, logged or audited.
"""

from __future__ import annotations

import dataclasses
import re
import threading
import time
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from fleetledger import keys, paths
from fleetledger.errors import FleetError
from fleetledger.web.common import flash, ui_of
from fleetledger.web.forms import form as _form
from fleetledger.web.forms import one as _one

ADMIN_KEY = "~/.ssh/shipshape_fleet_ed25519"
HOST_CA_KEY = "~/.ssh/shipshape_host_ca_ed25519"
USER_CA_KEY = "~/.ssh/shipshape_user_ca_ed25519"
YOUR_NEW_KEY = "~/.ssh/shipshape_ed25519"
STATE_TTL = 30 * 60

TAILSCALE_ACLS = "https://login.tailscale.com/admin/acls/file"
TAILSCALE_OAUTH = "https://login.tailscale.com/admin/settings/trust-credentials"


# --- per-session state (memory only) ------------------------------------------------------


class SetupShipView:
    """The first Ship's job (web.jobs), as the setup page words it."""

    def __init__(self, job) -> None:
        self.job = job
        self.name = job.ships[0]
        self.done = job.done
        self.ok = bool(job.ok)
        for key in ("enrolled", "manual", "still_starting", "attach_here", "from_device"):
            setattr(self, key, bool(job.result.get(key)))
        self.attach_cmd = job.result.get("attach_cmd")  # by its runtime (attach.py)

    def snapshot(self) -> list[str]:
        return self.job.snapshot()


@dataclasses.dataclass
class SetupState:
    """One browser session's progress through the wizard. Holds no secret: the
    Proxmox token is stored (and read back from its store) once it is checked."""

    touched: float = dataclasses.field(default_factory=time.monotonic)
    proxmox: dict[str, Any] = dataclasses.field(default_factory=dict)
    overlay_skipped: bool = False
    overlay_checks: list[tuple[bool, str]] = dataclasses.field(default_factory=list)
    old_copy: dict[str, Any] | None = None
    ship: int | None = None  # the first Ship's job id (web.jobs)
    # The first Ship's form as last posted, when it came back to confirm removing a
    # failed attempt's leftovers (no secret: `new` options only); shown once.
    ship_form: dict[str, list[str]] | None = None


class Setups:
    def __init__(self) -> None:
        self._states: dict[str, SetupState] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> SetupState:
        now = time.monotonic()
        with self._lock:
            # A running first Ship carries on in the job registry (web.jobs): the
            # session's state can go.
            for sid in [s for s, st in self._states.items() if now - st.touched > STATE_TTL]:
                del self._states[sid]
            state = self._states.setdefault(session_id, SetupState())
            state.touched = now
            return state


# --- what is done ----------------------------------------------------------------------------


@dataclasses.dataclass
class Step:
    key: str
    title: str
    state: str  # done | next | later | optional | skipped
    summary: str


def _inventory(app):
    store = app.store_factory()
    if not store.exists():
        return store, None, None
    try:
        return store, store.load(), None
    except FleetError as exc:
        return store, None, exc.message


def your_key_choices() -> list[Path]:
    """Public keys in ~/.ssh that look like a person's own (not FleetLedger's)."""
    folder = Path.home() / ".ssh"
    found = []
    for pub in sorted(folder.glob("*.pub")) if folder.is_dir() else []:
        if pub.name.endswith("-cert.pub") or pub.name.startswith("shipshape_fleet") \
                or "_ca_" in pub.name:
            continue
        try:
            first = pub.read_text(encoding="utf-8").split()[:1]
        except (OSError, UnicodeDecodeError):
            continue
        if first and first[0].startswith(("ssh-ed25519", "ecdsa-", "ssh-rsa", "sk-")):
            found.append(pub)
    preferred = folder / "id_ed25519.pub"
    return sorted(found, key=lambda p: (p != preferred, p.name))


def steps(app, state: SetupState) -> tuple[list[Step], Any]:
    store, inv, error = _inventory(app)
    out = []
    if inv is None:
        out.append(Step("fleet", "Your fleet", "next",
                        error or "Not set up yet: start here."))
    else:
        has_ca = bool(inv.doc.get("ca"))
        store_kind, managed = keys.managed(inv)
        how = ("FleetLedger looks after its keys" if inv.admin_key in managed
               else "keys protected by passphrases you chose")
        out.append(Step("fleet", "Your fleet", "done" if has_ca else "next",
                        f"Set up; {how}." if has_ca else
                        "Set up, but there is no certificate authority yet."))
    driver = (inv.defaults.get("driver") if inv is not None and "driver" in inv.defaults
              else None)
    if inv is None:
        out.append(Step("hypervisor", "Where Ships run", "later", ""))
    elif driver == "proxmox" and inv.driver_config("proxmox"):
        out.append(Step("hypervisor", "Where Ships run", "done",
                        f"Proxmox at {inv.driver_config('proxmox')['api_url']}."))
    elif driver == "manual":
        out.append(Step("hypervisor", "Where Ships run", "done",
                        "Manual: FleetLedger prints the steps for your hypervisor."))
    else:
        out.append(Step("hypervisor", "Where Ships run", "next", "Not chosen yet."))
    if inv is not None and inv.overlay:
        out.append(Step("overlay", "Reach Ships from anywhere (optional)", "done",
                        f"{inv.overlay.get('provider', 'tailscale')} is set up."))
    else:
        out.append(Step("overlay", "Reach Ships from anywhere (optional)",
                        "skipped" if state.overlay_skipped else "optional",
                        "Skipped for now." if state.overlay_skipped else ""))
    ships = inv.ships() if inv is not None else []
    out.append(Step("ship", "Your first Ship", "done" if ships else "later",
                    f"{len(ships)} Ship(s): " + ", ".join(s.name for s in ships[:5])
                    if ships else ""))
    # The first required step not done is "next"; those after it wait. Optional steps
    # never hold the next one back.
    seen_open = False
    for step in out:
        if step.state in ("done", "skipped", "optional"):
            continue
        step.state = "later" if seen_open else "next"
        seen_open = True
    return out, inv


def agent_holds_admin(inv) -> bool | None:
    """Does ssh-agent hold the admin key? None when there is no agent to ask, or when
    the agent is GNOME's, which lists every key in ~/.ssh whether it holds it or not."""
    from fleetledger import firstrun

    held = firstrun.agent_fingerprints()
    if held is None:
        return None
    if keys.desktop_agent_preloads() and inv.admin_key.parent == Path.home() / ".ssh":
        return None
    fpr = firstrun.fingerprint(str(inv.admin_public_key_path))
    return bool(fpr and fpr in held)


def unmanaged_keys(inv) -> list[tuple[str, Path]]:
    """(what, path) of FleetLedger's keys that keep a passphrase you chose."""
    _store, managed = keys.managed(inv)
    out = [("fleet admin key", inv.admin_key)]
    ca = inv.doc.get("ca") or {}
    if ca.get("host_key"):
        out.append(("host CA key", paths.expand(ca["host_key"])))
    if ca.get("user_key"):
        out.append(("user CA key", paths.expand(ca["user_key"])))
    return [(what, p) for what, p in out if p not in managed and p.exists()
            and p.with_name(p.name + ".pub").exists()]


# --- handlers --------------------------------------------------------------------------------


def _state(request: Request) -> SetupState:
    ui = ui_of(request)
    return ui.setups.get(request.scope["state"]["session"].id)


def _back(anchor: str = "", fields: dict[str, list[str]] | None = None) -> RedirectResponse:
    """Back to the setup step, or to Settings' Access section when the form was
    posted from there (back=settings)."""
    if fields is not None and _one(fields, "back") == "settings":
        return RedirectResponse("/settings#access", status_code=303)
    return RedirectResponse("/setup" + (f"#{anchor}" if anchor else ""), status_code=303)


def _secret_field(fields, key: str) -> str:
    """A token or client secret (surrounding whitespace removed); never logged.
    The same as _one(), named so a secret field reads as one."""
    return _one(fields, key)


def _passphrase_field(fields, key: str) -> str:
    """A key's passphrase exactly as typed (spaces count); never logged."""
    values = fields.get(key, [])
    return values[0] if len(values) == 1 else ""


async def page(request: Request) -> Response:
    from fleetledger.web.app import render

    ui = ui_of(request)
    state = _state(request)
    found_steps, inv = await run_in_threadpool(steps, ui.app, state)
    context: dict[str, Any] = {"steps": found_steps, "inv": inv, "state": state,
                               "setup_job": setup_job(ui, state),
                               "inventory_path": _tilde(ui.app.store_factory().path),
                               "TAILSCALE_ACLS": TAILSCALE_ACLS,
                               "TAILSCALE_OAUTH": TAILSCALE_OAUTH}
    context.update(await access_context(inv))
    if inv is None:
        context["your_keys"] = [_tilde(p) for p in await run_in_threadpool(your_key_choices)]
    else:
        context["agent_ok"] = await run_in_threadpool(agent_holds_admin, inv)
        context["policy"] = _policy_text()
        context["size"] = inv.effective_size()
        from fleetledger.web import newship

        try:
            context["new_form"] = await run_in_threadpool(newship.choices, ui.app, inv)
        except FleetError as exc:  # no ShipShape checkout to read the catalogue from
            context["new_form"], context["new_form_error"] = None, exc.message
        posted, state.ship_form = state.ship_form, None
        if posted is not None and context["new_form"] is not None:
            context["new_form"] = newship.refill(context["new_form"], posted)
            context["new_clearing"] = await run_in_threadpool(
                newship.clearing, None, newship.one_field(posted, "name"))
    from fleetledger import proxmox_setup

    context["px"] = state.proxmox
    if inv is not None:
        if state.proxmox.get("stage") != "choose":
            context["px_here"] = await run_in_threadpool(proxmox_on_this_computer, ui.app,
                                                         state.proxmox)
        if not inv.overlay:
            context["overlay_here"] = await run_in_threadpool(overlay_on_this_computer)
    context["token_commands"] = proxmox_setup.user_and_token_commands(proxmox_setup.Names())
    return render(request, "setup.html", **context)


async def access_context(inv) -> dict[str, Any]:
    """What the fleet's access forms need (_fleet_access.html, on Settings and Setup):
    the keyring, keys you look after yourself, the managed ones, trusted machines.
    Reads files and asks the keyring if it is usable; runs no command."""
    usable, why = await run_in_threadpool(_keyring_usable)
    context: dict[str, Any] = {"keyring_usable": usable, "keyring_why": why,
                               "default_store": "keyring" if usable else "file"}
    if inv is not None:
        context["unmanaged"] = [(what, _tilde(p)) for what, p in
                                await run_in_threadpool(unmanaged_keys, inv)]
        context["desktop_agent"] = keys.desktop_agent_preloads()
        context["admin_managed"] = inv.admin_key in keys.managed(inv)[1]
        context.update(await run_in_threadpool(_machines_context, inv))
    return context


def _keyring_usable() -> tuple[bool, str]:
    from fleetledger.overlay import secret_store

    return secret_store.keyring_usable()


def _policy_text() -> str:
    from fleetledger import overlay

    return overlay.policy({"provider": "tailscale", "tags": ["tag:ship"]}).rstrip()


def _tilde(path) -> str:
    try:
        return "~/" + str(Path(path).resolve().relative_to(Path.home().resolve()))
    except ValueError:
        return str(path)


def _store_choice(fields) -> str:
    from fleetledger.overlay import secret_store

    if secret_store.vault_on():
        return "keyring"  # the vault: never a file while it is on
    chosen = _one(fields, "store")
    if chosen in ("keyring", "file"):
        return chosen
    return keys.default_store()


def _your_key(fields) -> tuple[str, bool]:
    """(the public key path to let in, whether FleetLedger must make it)."""
    choice = _one(fields, "your_key")
    other = _one(fields, "your_key_other")
    if other:
        choice = other
    if choice == "new":
        return YOUR_NEW_KEY + ".pub", True
    if not choice:
        raise FleetError("Choose the SSH key that may log in to your Ships.")
    path = paths.expand(choice)
    if not path.name.endswith(".pub") or path.name.endswith("-cert.pub") or not path.is_file():
        raise FleetError(f"{choice} is not a public key file (.pub).")
    return choice, False


def _new_fleet(app, fields) -> str:
    from fleetledger.commands import make_fleet
    from fleetledger.trustcmd import make_ca

    store = app.store_factory()
    if store.exists():
        raise FleetError("FleetLedger is already set up here.")
    managed = _store_choice(fields)
    admin = _one(fields, "admin_key") or ADMIN_KEY
    host_ca = _one(fields, "host_ca_key") or HOST_CA_KEY
    user_ca = _one(fields, "user_ca_key") or USER_CA_KEY
    for path in (admin, host_ca, user_ca):
        if paths.expand(path).exists():
            raise FleetError(f"{path} already exists. To use keys you already have, choose "
                             "\"Connect to an existing fleet or keys\".")
    your_key, make_yours = _your_key(fields)
    if make_yours:
        private = paths.expand(YOUR_NEW_KEY)
        if not private.exists():
            keys.make(private, "shipshape-you", managed)
    make_fleet(app, store, admin, [your_key], managed=managed)
    with store.edit() as ed:
        ed.set_default("driver", None)  # chosen in the next step
        if make_yours:
            keys.record(ed, managed, [YOUR_NEW_KEY])
    make_ca(app, store, host_ca, user_ca, managed=managed)
    return managed


def _import_fleet(app, fields, secrets_: dict[str, str]) -> None:
    """Use keys the person already has. Everything is checked first (paths, public
    halves, your key, and each passphrase when handing keys over); then ships.toml is
    written; only then are keys handed over, one by one, each recorded at once."""
    from fleetledger.commands import make_fleet
    from fleetledger.seed import cloudinit
    from fleetledger.trustcmd import make_ca

    store = app.store_factory()
    if store.exists():
        raise FleetError("FleetLedger is already set up here.")
    admin = _one(fields, "admin_key")
    host_ca, user_ca = _one(fields, "host_ca_key"), _one(fields, "user_ca_key")
    if not admin:
        raise FleetError("Give the path of the fleet admin key (the private key file).")
    if bool(host_ca) != bool(user_ca):
        raise FleetError("Give both CA keys, or neither (FleetLedger then makes new ones).")
    chosen = [("admin", admin)] + ([("host", host_ca), ("user", user_ca)] if host_ca else [])
    for _what, value in chosen:
        path = paths.expand(value)
        if not path.is_file() or path.name.endswith(".pub"):
            raise FleetError(f"{value} is not a private key file.")
        if not path.with_name(path.name + ".pub").is_file():
            raise FleetError(f"{value}: put its public half next to it ({path.name}.pub).")
    your_key, make_yours = _your_key(fields)
    if not make_yours:
        cloudinit.read_public_keys(paths.expand(your_key), "your key")
    if _one(fields, "protect") not in ("yes", "no"):
        raise FleetError("Choose who looks after the keys' passphrases: FleetLedger, or you.")
    protect = _one(fields, "protect") == "yes"
    managed = _store_choice(fields)
    handing = [(paths.expand(value), secrets_.get(what, ""), value)
               for what, value in chosen] if protect else []
    for path, current, _ in handing:  # a wrong passphrase stops here: nothing changed yet
        keys.check_opens(path, current)

    if make_yours and not paths.expand(YOUR_NEW_KEY).exists():
        keys.make(paths.expand(YOUR_NEW_KEY), "shipshape-you", managed)
    make_fleet(app, store, admin, [your_key])
    with store.edit() as ed:
        ed.set_default("driver", None)
        if make_yours:
            keys.record(ed, managed, [YOUR_NEW_KEY])
    make_ca(app, store, host_ca or HOST_CA_KEY, user_ca or USER_CA_KEY,
            managed=None if host_ca else managed)
    if handing:
        keys.adopt_all(store, handing, managed)


async def fleet_new(request: Request) -> Response:
    fields = await _form(request)
    try:
        managed = await run_in_threadpool(_new_fleet, ui_of(request).app, fields)
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return _back("fleet")
    flash(request, "ok", f"Your fleet is set up. FleetLedger made its keys and keeps their "
          f"passphrases in {keys.describe(managed)}.")
    return _back("hypervisor")


async def fleet_import(request: Request) -> Response:
    fields = await _form(request)
    secrets_ = {what: _passphrase_field(fields, f"{what}_passphrase")
                for what in ("admin", "host", "user")}
    try:
        await run_in_threadpool(_import_fleet, ui_of(request).app, fields, secrets_)
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return _back("fleet")
    finally:
        secrets_.clear()
    flash(request, "ok", "Your fleet is set up with the keys you brought.")
    return _back("hypervisor")


def _adopt(app, fields, secrets_: dict[str, str]) -> list[str]:
    store = app.store_factory()
    inv = store.load()
    managed = keys.managed(inv)[0] or _store_choice(fields)  # one store per fleet
    chosen = [(path, secrets_.get(str(index), ""), _tilde(path))
              for index, (_what, path) in enumerate(unmanaged_keys(inv))
              if _one(fields, f"take_{index}") == "yes"]
    return keys.adopt_all(store, chosen, managed)


async def fleet_adopt(request: Request) -> Response:
    fields = await _form(request)
    if _one(fields, "confirm") != "yes":
        flash(request, "bad", "Tick the box to confirm: FleetLedger re-encrypts these key "
              "files (the keys themselves stay the same).")
        return _back("fleet", fields)
    secrets_ = {str(i): _passphrase_field(fields, f"passphrase_{i}") for i in range(3)}
    try:
        taken = await run_in_threadpool(_adopt, ui_of(request).app, fields, secrets_)
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return _back("fleet", fields)
    finally:
        secrets_.clear()
    if taken:
        flash(request, "ok", "FleetLedger now looks after " + ", ".join(taken) +
              ". Their old passphrases no longer open them.")
    else:
        flash(request, "muted", "Nothing chosen.")
    return _back("fleet", fields)


async def fleet_ca(request: Request) -> Response:
    from fleetledger.trustcmd import make_ca

    app = ui_of(request).app
    fields = await _form(request)
    try:
        store = app.store_factory()
        managed = keys.managed(store.load())[0] or keys.default_store()  # the fleet's store
        await run_in_threadpool(make_ca, app, store, HOST_CA_KEY, USER_CA_KEY, managed=managed)
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return _back("fleet", fields)
    flash(request, "ok", "Made the certificate authority.")
    return _back("hypervisor", fields)


def _machines_context(inv) -> dict[str, Any]:
    from fleetledger import ca, trustcmd

    if ca.config(inv) is None:
        return {"machines": []}
    here = trustcmd.this_computer()
    return {"machines": [m for m in ca.machines(inv) if not m.revoked],
            "this_machine": here["machine"] if here else None}


async def machine_logins(request: Request) -> Response:
    """What one trusted machine may log in as (the agent's login, yours, or both)."""
    from fleetledger import ca, trustcmd

    app = ui_of(request).app
    fields = await _form(request)
    said: list[str] = []
    try:
        logins = ca.parse_logins(_one(fields, "logins"))
        await run_in_threadpool(trustcmd.set_logins, app, app.store_factory(),
                                _one(fields, "machine"), logins, said.append)
        ok = True
    except trustcmd.PushFailed:
        ok = False  # recorded; what was said names the Ships still to update
    except FleetError as exc:
        ok, said = False, [*said, exc.message]
    flash(request, "ok" if ok else "bad", " ".join(said) or "No change.")
    return _back("fleet", fields)


async def owner_name(request: Request) -> Response:
    """Your own login name on Ships (fleet.owner), set on every Ship."""
    from fleetledger import commands

    app = ui_of(request).app
    fields = await _form(request)
    said: list[str] = []
    try:
        ok = await run_in_threadpool(commands.set_owner, app, app.store_factory(),
                                     _one(fields, "owner"), said.append)
    except FleetError as exc:
        ok, said = False, [exc.message]
    flash(request, "ok" if ok else "bad", " ".join(said))
    return _back("fleet", fields)


async def load_keys(request: Request) -> Response:
    app = ui_of(request).app
    try:
        inv = app.store_factory().load()
        loaded = await run_in_threadpool(keys.ensure_loaded, inv)
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return _back("ship")
    flash(request, "ok", ("Loaded " + ", ".join(loaded) + " into ssh-agent.") if loaded
          else "ssh-agent already holds FleetLedger's keys.")
    return _back("ship")


# --- the hypervisor ------------------------------------------------------------------------


async def hypervisor_manual(request: Request) -> Response:
    app = ui_of(request).app
    with app.store_factory().edit() as ed:
        ed.set_default("driver", "manual")
    _state(request).proxmox.clear()
    flash(request, "ok", "Manual: for each new Ship, FleetLedger writes its seed ISO and "
          "prints the steps to make the VM.")
    return _back("overlay")


async def proxmox_start(request: Request) -> Response:
    _state(request).proxmox = {"stage": "address"}
    return _back("hypervisor")


async def proxmox_cancel(request: Request) -> Response:
    _state(request).proxmox.clear()
    return _back("hypervisor")


def _old_proxmox(app) -> dict[str, Any]:
    from fleetledger import proxmox_setup

    store = app.store_factory()
    saved = dict(store.load().driver_config("proxmox") or {}) if store.exists() else {}
    return {**saved, **proxmox_setup._load_progress()}


async def proxmox_address(request: Request) -> Response:
    from fleetledger import proxmox_setup
    from fleetledger.drivers import proxmox_api

    app = ui_of(request).app
    state = _state(request)
    fields = await _form(request)
    try:
        host, port, api_url = proxmox_setup.parse_address(_one(fields, "address"))
        fetch = getattr(app, "cert_fingerprint", None) or proxmox_api.server_fingerprint
        fingerprint = await run_in_threadpool(fetch, host, port)
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return _back("hypervisor")
    old = await run_in_threadpool(_old_proxmox, app)
    before = old.get("tls_fingerprint") if old.get("api_url") == api_url else None
    # pinned: a different certificate from the one confirmed before (flagged loudly);
    # known: the very one confirmed before (offered as such).
    state.proxmox = {"stage": "token", "api_url": api_url, "host": host, "port": port,
                     "fingerprint": fingerprint,
                     "pinned": before if before and before != fingerprint else None,
                     "known": bool(before) and before == fingerprint,
                     "token_id": old.get("token_id", "")}
    return _back("hypervisor")


def _stored_token_file(old: dict[str, Any], token_id: str) -> Path | None:
    """A token secret file already on this computer that the empty-secret path may use:
    the one ships.toml names for this token id, else the default one. Checked (0600,
    plausible), never shown."""
    from fleetledger import proxmox_setup
    from fleetledger.drivers import proxmox_api

    candidates = [paths.config_dir() / proxmox_setup.SECRET_FILE]
    if old.get("token_file") and (not token_id or old.get("token_id") == token_id):
        candidates.insert(0, paths.expand(old["token_file"]))
    for path in candidates:
        try:
            if proxmox_setup._plausible_token(proxmox_api.read_token(str(path))):
                return path
        except FleetError:
            continue
    return None


def _address_of(api_url: str | None) -> str:
    """The address as typed ("host" or "host:port") from a saved api_url."""
    match = re.match(r"^https://([^/]+)/api2/json$", api_url or "")
    if not match:
        return ""
    return match.group(1)[:-len(":8006")] if match.group(1).endswith(":8006") else match.group(1)


def proxmox_on_this_computer(app, px: dict[str, Any]) -> dict[str, Any]:
    """What the Proxmox step can offer instead of asking again (names and places
    only, never a secret): the saved address, token ids with a secret in the
    keyring, and a token secret file."""
    from fleetledger import proxmox_setup
    from fleetledger.overlay import secret_store

    old = _old_proxmox(app)
    listed = secret_store.stored_names(secret_store.PROXMOX_ITEM_PREFIX)
    ids = [i for i in (listed or []) if proxmox_setup.TOKEN_ID_RE.match(i)]
    token_id = px.get("token_id") or (ids[0] if len(ids) == 1 else "")
    if token_id and listed is None:  # a keyring that cannot list: ask for this one item
        in_keyring = secret_store.existing_in_keyring(
            secret_store.proxmox_item(token_id)) is not None
    else:
        in_keyring = token_id in ids
    stored_file = _stored_token_file(old, token_id)
    return {"address": _address_of(old.get("api_url")), "token_id": token_id,
            "token_ids": ids, "in_keyring": bool(token_id) and in_keyring,
            "file": _tilde(stored_file) if stored_file else None,
            "listable": listed is not None}


def _check_token(app, px: dict[str, Any], token_id: str, secret: str, reuse: bool,
                 store_kind: str) -> dict[str, Any]:
    """Check the token through the pin, store it, and find what it can see."""
    from fleetledger import proxmox_setup
    from fleetledger.drivers import proxmox_api
    from fleetledger.overlay import secret_store

    if not proxmox_setup.TOKEN_ID_RE.match(token_id):
        raise FleetError(f"'{token_id}' is not a token id (user@realm!name).")
    item = secret_store.proxmox_item(token_id)
    config: dict[str, Any] = {"api_url": px["api_url"], "token_id": token_id,
                              "tls_fingerprint": px["fingerprint"]}
    old = _old_proxmox(app)
    reused_path = None
    if reuse:
        existing = secret_store.existing_in_keyring(item)
        if existing is not None:
            secret, store_kind = existing, "keyring"
        else:
            reused_path = _stored_token_file(old, token_id)
            if reused_path is None:
                raise FleetError("No stored secret for this token; type it in.")
            secret, store_kind = proxmox_api.read_token(str(reused_path)), "file"
    if not proxmox_setup._plausible_token(secret):
        raise FleetError("That does not look like a Proxmox token secret.")
    if store_kind == "keyring" and not proxmox_setup.KEYRING_ITEM_RE.match(item):
        store_kind = "file"
    api = proxmox_setup._api(app, config, lambda: secret)
    try:
        version = api.get("/version") or {}
    except proxmox_api.ProxmoxError as exc:
        if exc.status == 401:
            raise FleetError("Proxmox refused the token (401): check the token id and "
                             "secret. Nothing was saved.") from None
        raise
    where = proxmox_setup.keep_secret(config, secret, store_kind, reused=reuse,
                                      secret_path=reused_path)
    proxmox_setup._save_progress(config)
    match = re.match(r"^(\d+)\.", str(version.get("version", "")))
    pve_major = int(match.group(1)) if match else 9
    nodes = proxmox_setup.list_nodes(api)
    node = old.get("node") if old.get("node") in nodes else nodes[0]
    return {"stage": "choose", "config": config, "where": where,
            "version": version.get("version", "?"), "pve_major": pve_major,
            "nodes": nodes, "old": {k: v for k, v in old.items() if k != "token_file"},
            **_find(app, config, node, pve_major, old)}


def _find(app, config, node: str, pve_major: int, old) -> dict[str, Any]:
    from fleetledger import proxmox_setup
    from fleetledger.drivers.proxmox_api import ProxmoxAPI

    factory = getattr(app, "proxmox_api", None)
    api = factory(config) if factory else ProxmoxAPI(config)  # reads the stored secret
    found = proxmox_setup.find(api, node, pve_major)
    proposed = proxmox_setup.defaults(found, old)
    return {"node": node, "found": found, "proposed": proposed}


def _problems(app, px: dict[str, Any], chosen: dict[str, Any]) -> list[tuple[str, list[str]]]:
    from fleetledger import proxmox_setup

    found = px["found"]
    n = proxmox_setup.Names(token_id=px["config"]["token_id"], pve_major=px["pve_major"])
    if chosen.get("pool"):
        n.pool = chosen["pool"]
    if chosen.get("disk_storage"):
        n.disk = chosen["disk_storage"]
    if chosen.get("seed_storage"):
        n.seeds = chosen["seed_storage"]
    if chosen.get("bridge"):
        n.bridge = chosen["bridge"]
    return proxmox_setup.problems(found, n, app.repo_factory().versions, px["old"])


async def proxmox_token(request: Request) -> Response:
    app = ui_of(request).app
    state = _state(request)
    px = state.proxmox
    fields = await _form(request)
    if px.get("stage") != "token":
        return _back("hypervisor")
    if _one(fields, "matches") != "yes" or (px.get("pinned") and
                                           _one(fields, "changed_ok") != "yes"):
        flash(request, "bad", "Not pinned: confirm the fingerprint matches the one on the "
              "host. Nothing was sent to it.")
        return _back("hypervisor")
    secret = _secret_field(fields, "secret")
    reuse = not secret  # empty: the secret already stored for this token (checked first)
    try:
        result = await run_in_threadpool(_check_token, app, px, _one(fields, "token_id"),
                                         secret, reuse, _store_choice(fields))
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return _back("hypervisor")
    finally:
        secret = ""
    state.proxmox = result
    state.proxmox["problems"] = await run_in_threadpool(_problems, app, result,
                                                        result["proposed"])
    flash(request, "ok", f"The token works (Proxmox VE {result['version']}); its secret is "
          f"in {result['where']}.")
    return _back("hypervisor")


def _chosen(fields, px) -> dict[str, Any]:
    """The posted choices, each one of what the token can see (or typed, for a bridge
    granted on a parent path)."""
    from fleetledger import proxmox_setup

    found = px["found"]
    chosen: dict[str, Any] = {}

    def pick(key: str, options) -> None:
        value = _one(fields, key)
        if value:
            if value not in options:
                raise FleetError(f"{key}: not one of the choices.")
            chosen[key] = value

    pick("pool", found.pools)
    pick("disk_storage", found.disks)
    pick("seed_storage", found.seed_options(chosen.get("disk_storage")))
    pick("image_storage", ["", *found.uploads])
    bridge = _one(fields, "bridge")
    if bridge:
        if bridge not in found.bridges and not (found.any_bridge and
                                                proxmox_setup.BRIDGE_RE.match(bridge)):
            raise FleetError("bridge: not one of the choices.")
        chosen["bridge"] = bridge
    template = _one(fields, "template_vmid")
    if template:
        if template not in [str(v) for v, _ in found.templates]:
            raise FleetError("template: not one of the choices.")
        chosen["template_vmid"] = int(template)
    vmids = _one(fields, "vmid_range")
    if vmids:
        chosen["vmid_range"] = proxmox_setup.parse_vmid_range(vmids)  # None: any free id
    return {**{k: v for k, v in px["proposed"].items() if v is not None}, **chosen}


async def proxmox_recheck(request: Request) -> Response:
    app = ui_of(request).app
    px = _state(request).proxmox
    if px.get("stage") != "choose":
        return _back("hypervisor")
    fields = await _form(request)
    node = _one(fields, "node")
    node = node if node in px["nodes"] else px["node"]
    try:
        px.update(await run_in_threadpool(_find, app, px["config"], node, px["pve_major"],
                                          px["old"]))
        px["problems"] = await run_in_threadpool(_problems, app, px, px["proposed"])
    except FleetError as exc:
        flash(request, "bad", exc.message)
    return _back("hypervisor")


def _save_proxmox(app, px, chosen) -> None:
    from fleetledger import proxmox_setup
    from fleetledger.drivers import proxmox_api

    todo = _problems(app, px, chosen)
    if todo:
        raise FleetError("Still missing on the Proxmox host: " +
                         "; ".join(what for what, _ in todo) + ".")
    config = {**px["config"], "node": px["node"],
              **{k: chosen.get(k) for k in ("pool", "template_vmid", "seed_storage",
                                            "disk_storage", "bridge", "image_storage",
                                            "vmid_range")}}
    if not config.get("image_storage"):
        config.pop("image_storage", None)
    # The connection test, through the stored secret and the pin, before saving.
    factory = getattr(app, "proxmox_api", None)
    api = factory(config) if factory else proxmox_api.ProxmoxAPI(config)
    api.get("/version")
    proxmox_setup.save(app.store_factory(), config)


async def proxmox_save(request: Request) -> Response:
    from fleetledger import proxmox_setup

    app = ui_of(request).app
    state = _state(request)
    px = state.proxmox
    if px.get("stage") != "choose":
        return _back("hypervisor")
    fields = await _form(request)
    saved_before = dict(px["old"])
    try:
        chosen = _chosen(fields, px)
        await run_in_threadpool(_save_proxmox, app, px, chosen)
    except FleetError as exc:
        flash(request, "bad", exc.message)
        px["problems"] = await run_in_threadpool(_problems, app, px, px["proposed"])
        return _back("hypervisor")
    before = proxmox_setup._secret_location(saved_before)
    after = proxmox_setup._secret_location(px["config"])
    state.proxmox = {}
    state.old_copy = ({"kind": before[0], "where": before[1]}
                      if before and after and before != after else None)
    flash(request, "ok", "Connected and saved: new Ships are made on Proxmox.")
    return _back("overlay")


async def proxmox_forget_old(request: Request) -> Response:
    from fleetledger.overlay import secret_store

    state = _state(request)
    old = state.old_copy
    fields = await _form(request)
    if old and _one(fields, "confirm") == "yes":
        try:
            if old["kind"] == "file":
                Path(old["where"]).unlink(missing_ok=True)
            else:
                secret_store.delete_from_keyring(old["where"])
            flash(request, "ok", "Removed the previous token secret.")
        except (FleetError, OSError):
            flash(request, "bad", "Could not remove the previous token secret.")
    state.old_copy = None
    return _back("hypervisor")


# --- the overlay ------------------------------------------------------------------------------


# A Tailscale OAuth client secret names its client: tskey-client-<client id>-<secret>.
OAUTH_CLIENT_IN_SECRET = re.compile(r"^tskey-client-([A-Za-z0-9]+)-")


def _stored_oauth_file() -> tuple[str, str | None] | None:
    """(secret, the client id it names, if it does) from the default OAuth secret file,
    when one is there and plausible; else None. The secret never leaves the server."""
    from fleetledger import overlay_setup
    from fleetledger.overlay import secret_store

    path = paths.config_dir() / overlay_setup.SECRET_FILE
    if not path.exists():
        return None
    try:
        secret = secret_store.read_file(str(path))
    except FleetError:
        return None
    if not overlay_setup._plausible_secret(secret):
        return None
    match = OAUTH_CLIENT_IN_SECRET.match(secret)
    return secret, match.group(1) if match else None


def overlay_on_this_computer() -> dict[str, Any]:
    """What the overlay step can offer instead of asking again: OAuth client ids with
    a secret already in the keyring, and an OAuth secret file (its client id, if the
    secret names it). Never a secret."""
    from fleetledger import overlay_setup
    from fleetledger.overlay import secret_store

    listed = secret_store.stored_names(secret_store.OAUTH_ITEM_PREFIX)
    ids = [i for i in (listed or []) if overlay_setup.CLIENT_ID_RE.match(i)]
    from_file = _stored_oauth_file()
    file_client = from_file[1] if from_file else None
    client_id = ids[0] if len(ids) == 1 else (file_client or "") if not ids else ""
    return {"client_ids": ids, "client_id": client_id,
            "file": _tilde(paths.config_dir() / overlay_setup.SECRET_FILE) if from_file else None,
            "file_client": file_client, "listable": listed is not None}


def _connect_overlay(app, fields, secret: str) -> list[tuple[bool, str]]:
    from fleetledger import overlay_setup
    from fleetledger.overlay import secret_store

    store = app.store_factory()
    client_id = _one(fields, "client_id")
    level = _one(fields, "level") or overlay_setup.DEFAULT_LEVEL
    reused = False
    store_kind = _store_choice(fields)
    if not secret and overlay_setup.CLIENT_ID_RE.match(client_id):
        existing = secret_store.existing_in_keyring(secret_store.oauth_item(client_id))
        if existing is not None and overlay_setup._plausible_secret(existing):
            secret, reused, store_kind = existing, True, "keyring"
        else:
            from_file = _stored_oauth_file()
            if from_file is not None and from_file[1] in (None, client_id):
                secret, reused, store_kind = from_file[0], True, "file"
    if not secret:
        raise FleetError("Paste the OAuth client secret (or store it in your keyring first).")
    checks: list[tuple[bool, str]] = []
    # Check the tag rules first, with a key made and deleted at once; then save.
    api_cfg = {"tailnet": "-", "oauth_client_id": client_id}
    probe = overlay_setup._api(app, api_cfg, secret=lambda: secret)
    probe.token()
    checks.append((True, "The OAuth client signs in."))
    try:
        probe.try_key(["tag:ship"])
    except FleetError as exc:
        raise FleetError("The client signs in, but cannot make keys for tag:ship: check "
                         "step 1 (tag:ship in tagOwners) and step 2 (the client has the "
                         f"tag:ship tag and Auth Keys: Write). Tailscale said: {exc.message}"
                         ) from None
    checks.append((True, "It can make tag:ship keys (your access rules own the tag)."))
    done = overlay_setup.connect_oauth(app, store, client_id=client_id, secret=secret,
                                       reused=reused, store_kind=store_kind, level=level)
    checks.append((True, f"Its scopes fit the level ({level}); the secret is {done.kept}."))
    checks.extend((True, note) for note in done.notes)
    return checks


async def overlay_save(request: Request) -> Response:
    app = ui_of(request).app
    state = _state(request)
    fields = await _form(request)
    secret = _secret_field(fields, "secret")
    try:
        state.overlay_checks = await run_in_threadpool(_connect_overlay, app, fields, secret)
    except FleetError as exc:
        state.overlay_checks = []
        flash(request, "bad", exc.message)
        return _back("overlay")
    finally:
        secret = ""
    flash(request, "ok", "The overlay is set up. Each new Ship joins it after it enrols.")
    return _back("ship")


async def overlay_skip(request: Request) -> Response:
    _state(request).overlay_skipped = True
    return _back("ship")


# --- the first Ship -------------------------------------------------------------------------


def setup_job(ui, state):
    """The session's first-Ship job, as the page shows it (None: not started)."""
    job = ui.jobs.get(state.ship) if state.ship is not None else None
    return SetupShipView(job) if job is not None else None


def size_from_form(fields) -> dict[str, int]:
    """cores / memory_mb / disk_gb from a form (memory typed in GB), checked."""
    from fleetledger.lifecycle import check_size

    def number(key: str) -> int | None:
        value = _one(fields, key)
        if not value:
            return None
        if not value.isdigit():
            raise FleetError(f"{key.replace('_', ' ')}: a whole number.")
        return int(value)

    memory_gb = number("memory_gb")
    return check_size({"cores": number("cores"), "disk_gb": number("disk_gb"),
                       "memory_mb": memory_gb * 1024 if memory_gb is not None else None})


async def ship_start(request: Request) -> Response:
    """Setup's first Ship: the New Ship form without its Advanced part (web.newship)."""
    from fleetledger.web import newship

    ui = ui_of(request)
    state = _state(request)
    fields = await _form(request)
    current = setup_job(ui, state)
    if current is not None and not current.done:
        return _back("ship")
    try:
        inv = await run_in_threadpool(lambda: ui.app.store_factory().load())
        name, argv = await run_in_threadpool(newship.argv, fields, ui.app, inv,
                                             advanced=False)
        # As on /new: a failed attempt's leftovers go only once the dialog named them.
        clearing = await run_in_threadpool(newship.clearing, None, name)
        try:
            cleared = newship.check_confirmed(fields, clearing)
        except FleetError:
            state.ship_form = {k: v for k, v in fields.items()
                               if k not in ("csrf", "confirm", "clear_left")}
            raise
        job = newship.start(ui, name, argv, cleared=cleared,
                            from_device=request.scope["state"]["session"].device is not None)
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return _back("ship")
    state.ship = job.id
    return _back("ship")


async def ship_progress(request: Request) -> Response:
    from fleetledger.web.app import partial

    job = setup_job(ui_of(request), _state(request))
    if job is None:
        return Response(status_code=286)
    # The finished card has no polling attributes, so swapping it in stops the polling.
    return partial(request, "_setup_ship.html", job=job, lines=job.snapshot())


def routes() -> list[Route]:
    return [
        Route("/setup", page),
        Route("/setup/fleet/new", fleet_new, methods=["POST"]),
        Route("/setup/fleet/import", fleet_import, methods=["POST"]),
        Route("/setup/fleet/adopt", fleet_adopt, methods=["POST"]),
        Route("/setup/fleet/ca", fleet_ca, methods=["POST"]),
        Route("/setup/keys/load", load_keys, methods=["POST"]),
        Route("/setup/owner", owner_name, methods=["POST"]),
        Route("/setup/machine", machine_logins, methods=["POST"]),
        Route("/setup/hypervisor/manual", hypervisor_manual, methods=["POST"]),
        Route("/setup/proxmox/start", proxmox_start, methods=["POST"]),
        Route("/setup/proxmox/cancel", proxmox_cancel, methods=["POST"]),
        Route("/setup/proxmox/address", proxmox_address, methods=["POST"]),
        Route("/setup/proxmox/token", proxmox_token, methods=["POST"]),
        Route("/setup/proxmox/recheck", proxmox_recheck, methods=["POST"]),
        Route("/setup/proxmox/save", proxmox_save, methods=["POST"]),
        Route("/setup/proxmox/forget-old", proxmox_forget_old, methods=["POST"]),
        Route("/setup/overlay", overlay_save, methods=["POST"]),
        Route("/setup/overlay/skip", overlay_skip, methods=["POST"]),
        Route("/setup/ship", ship_start, methods=["POST"]),
        Route("/setup/ship/progress", ship_progress),
    ]
