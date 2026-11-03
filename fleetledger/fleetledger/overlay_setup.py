"""ship-fleet overlay setup and overlay check (phase 5).

setup walks a new user through the overlay, once: provider, key source and, for
Tailscale's API, the OAuth client and how much it may do. check verifies the
overlay afterwards and whenever you like: the configuration, the tailnet policy
(Ships own nothing and reach nothing), the devices, each Ship's overlay, and
optionally a live probe that a Ship cannot open a connection to this computer or
to another Ship.

Secrets: the OAuth client secret is read without echo, checked in memory, and
only then written to its 0600 file. It never goes into ships.toml, a command
line, a message or the audit log. See docs/guide/your-own-fleet.md.
"""

from __future__ import annotations

import difflib
import getpass
import ipaddress
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from fleetledger import overlay, paths
from fleetledger.commands import table
from fleetledger.errors import FleetError
from fleetledger.overlay import hujson
from fleetledger.overlay import rules as pol
from fleetledger.overlay.providers import Check
from fleetledger.overlay.tsapi import TailscaleAPI
from fleetledger.shipctl import ShipCtl

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")
DEFAULT_SCOPES = ("auth_keys", "devices:core", "policy_file:read")
MANAGE_SCOPE = "policy_file"
SECRET_FILE = "tailscale-oauth"

MANAGE_WARNING = (
    "With this level the stored OAuth secret can change your tailnet's access rules:\n"
    "anyone who gets it can open your tailnet up. FleetLedger only ever adds the\n"
    "Ships' rules (after showing you a diff and asking), but the secret allows more.\n"
    "Most people should keep the default: paste the rules once, and `ship-fleet\n"
    "overlay check` keeps verifying them."
)


# --- small prompts --------------------------------------------------------------


def _ask(app, prompt: str, default: str = "") -> str:
    answer = app.confirm(prompt).strip()
    return answer or default


def _choose(app, prompt: str, choices: tuple[str, ...], default: str) -> str:
    while True:
        answer = _ask(app, f"{prompt} [{'/'.join(choices)}] ({default}): ", default).lower()
        if answer in choices:
            return answer
        app.note(f"Please answer one of: {', '.join(choices)}.")


def _secret(app, prompt: str) -> str:
    ask = getattr(app, "ask_secret", None) or (
        lambda p: getpass.getpass(p, stream=sys.stderr))
    return ask(prompt).strip()


def _api(app, api_cfg: dict, secret: Callable[[], str] | None = None):
    factory = getattr(app, "overlay_api", None)
    if factory is not None:
        return factory(api_cfg, secret=secret) if secret else factory(api_cfg)
    return TailscaleAPI(api_cfg, secret=secret) if secret else TailscaleAPI(api_cfg)


# --- setup ------------------------------------------------------------------------


def setup(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    if not app.stdin_isatty():
        raise FleetError("overlay setup asks questions; run it in a terminal.")
    if inv.overlay and not args.force:
        raise FleetError("An [overlay] is already set up in ships.toml.",
                         hint="ship-fleet overlay setup --force replaces it.")
    app.say("Ships can join an overlay network so you reach them from anywhere, with no\n"
            "router ports opened. SSH with each Ship's pinned key stays the only way in.\n")
    provider = _choose(app, "Overlay provider: Tailscale's service, or your own Headscale",
                       ("tailscale", "headscale"), "tailscale")
    if provider == "headscale":
        return _setup_headscale(app, store)
    app.say("\nWhere join keys come from:\n"
            "  oauth  FleetLedger mints a single-use key per Ship with an OAuth client\n"
            "         whose secret is stored on this computer (0600). Convenient; it can\n"
            "         also remove destroyed Ships' devices and check your access rules.\n"
            "  paste  You make a single-use key in the admin console for each Ship and\n"
            "         paste it. Nothing is stored: the lowest risk, a little more work.\n")
    source = _choose(app, "Key source", ("oauth", "paste"), "oauth")
    if source == "paste":
        config: dict[str, Any] = {"provider": "tailscale", "tags": ["tag:ship"],
                                  "key_source": "prompt"}
        with store.edit() as ed:
            ed.set_overlay(config)
        app.say("\nSaved [overlay] in ships.toml (pasted keys).")
        _print_policy(app, config)
        app.say("For each Ship, make a key under Settings > Keys: not reusable, not ephemeral,\n"
                "pre-approved, tag tag:ship, expiry 1 hour or less; then\n"
                "  ship-fleet overlay join SHIP     (it asks for the key; it is not shown)\n"
                "To check your access rules: export them from the admin console, then\n"
                "  ship-fleet overlay check --policy FILE")
        return 0
    return _setup_oauth(app, store)


def _setup_headscale(app, store) -> int:
    server = _ask(app, "Headscale server address (https://...): ")
    if not URL_RE.match(server):
        raise FleetError("That is not an https:// address.")
    config = {"provider": "headscale", "login_server": server, "tags": ["tag:ship"],
              "key_source": "prompt"}
    with store.edit() as ed:
        ed.set_overlay(config)
    app.say("\nSaved [overlay] in ships.toml (Headscale, pasted keys).")
    _print_policy(app, config)
    app.say("For each Ship, on the Headscale server:\n"
            "  headscale preauthkeys create --user <user> --expiration 1h --tags tag:ship\n"
            "then: ship-fleet overlay join SHIP   (it asks for the key; it is not shown)")
    return 0


def _setup_oauth(app, store) -> int:
    app.say("\nMake an OAuth client in the admin console (Settings > Trust credentials)\n"
            "with the tag tag:ship and these scopes (names as Tailscale lists them):\n"
            "  auth_keys, devices:core, policy_file:read,\n"
            "  and what policy_file:read needs: devices:core:read,\n"
            "  devices:posture_attributes:read\n"
            "See docs/overlay-providers.md for what each is for.\n")
    level = _choose(app, "Access level: default (FleetLedger reads and checks your rules), "
                    "or manage (it may also add the Ships' rules)", ("default", "manage"),
                    "default")
    if level == "manage":
        app.say("\n" + MANAGE_WARNING + "\nIt also needs the scopes policy_file and "
                "devices:posture_attributes.\n")
        if _ask(app, "Type yes to let the stored secret change access rules: ").lower() != "yes":
            app.say("Keeping the default level.")
            level = "default"
    client_id = _ask(app, "OAuth client id: ")
    if not CLIENT_ID_RE.match(client_id):
        raise FleetError("That does not look like an OAuth client id.")
    secret = _secret(app, "OAuth client secret (not shown): ")
    if not secret or "\n" in secret or len(secret) > 512:
        raise FleetError("That does not look like an OAuth client secret.")
    api_cfg = {"tailnet": "-", "oauth_client_id": client_id,
               "oauth_secret_file": f"~/.config/fleetledger/{SECRET_FILE}",
               "key_expiry_s": 600, "manage_policy": level == "manage"}

    # Check the credentials before anything is written.
    api = _api(app, api_cfg, secret=lambda: secret)
    api.token()
    wanted = set(DEFAULT_SCOPES) | ({MANAGE_SCOPE} if level == "manage" else set())
    if api.granted:
        missing = sorted(wanted - api.granted)
        if missing:
            raise FleetError(f"The OAuth client lacks the scopes: {', '.join(missing)}. "
                             "Nothing was saved.")
    else:
        app.note("Tailscale did not say which scopes the client has; checking by use.")
    api.devices()
    text, etag = api.policy_text()

    secret_path = paths.config_dir() / SECRET_FILE
    paths.private_dir(secret_path.parent)
    paths.write_private(secret_path, (secret + "\n").encode("utf-8"))
    config = {"provider": "tailscale", "tags": ["tag:ship"], "key_source": "tailscale-api",
              "api": api_cfg}
    with store.edit() as ed:
        ed.set_overlay(config)
    app.say(f"\nCredentials work. Saved [overlay] in ships.toml; the secret is in "
            f"{secret_path} (0600).")

    if level == "manage":
        return _merge_rules(app, api, text, etag, config)
    _print_policy(app, config)
    app.say("Then: ship-fleet overlay check   (it verifies them now and any time later)")
    return 0


def _print_policy(app, config: dict) -> None:
    app.say("\nAccess rules for your tailnet policy (admin console > Access controls). "
            "Add these\npieces to what is there; nothing else in your policy should let "
            "tag:ship be a source:\n")
    app.say(overlay.policy(config).rstrip() + "\n")


def _merge_rules(app, api, text: str, etag: str | None, config: dict) -> int:
    tags = overlay.tags(config)
    new, added = pol.merge(text, tags)
    if not added:
        app.say("Your policy already has the Ships' rules.")
    else:
        diff = "".join(difflib.unified_diff(
            text.splitlines(keepends=True), new.splitlines(keepends=True),
            "tailnet policy (now)", "tailnet policy (with the Ships' rules)"))
        app.say("\nFleetLedger would add: " + "; ".join(added) + ".\n")
        app.say(diff)
        problems = api.validate(new)
        if problems:
            raise FleetError("Tailscale refuses the merged policy: " + "; ".join(problems[:3])
                             + ". Nothing was changed.")
        if _ask(app, "Apply these changes to your tailnet policy? Type yes: ").lower() != "yes":
            app.say("Not changed. You can paste the rules yourself (ship-fleet overlay policy).")
            return 0
        api.set_policy(new, etag)
        app.say("Policy updated.")
    for finding in pol.findings(hujson.parse(new), tags):
        if not finding.ok:
            app.say(f"  ! {finding.name}: {finding.detail}")
    return 0


# --- check ------------------------------------------------------------------------


def _local_overlay_ips() -> list[str]:
    try:
        out = subprocess.run(["tailscale", "ip"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.stdout.splitlines() if _is_ip(line.strip())]


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _connects(app, inv, ship, ip: str) -> bool | None:
    """Can the Ship open a TCP connection to ip:22? None if the probe could not run."""
    if not _is_ip(ip):
        return None
    probe = ["timeout", "5", "bash", "-c", 'exec 3<>"/dev/tcp/$1/22"', "probe", ip]
    try:
        result = app.ssh_factory(inv).run(ship, probe, timeout=30)
    except FleetError:
        return None
    return result.returncode == 0


def check(args, app) -> int:
    inv = app.store_factory().load()
    config = overlay.settings(inv)
    tags = overlay.tags(config)
    provider = config.get("provider", "tailscale")
    uses_api = provider == "tailscale" and config.get("key_source") == "tailscale-api"
    items: list[Check] = []

    # 1. Configuration.
    if uses_api:
        expiry = int(config["api"].get("key_expiry_s", 600))
        items.append(Check("join keys expire", "ok" if expiry <= 3600 else "warn",
                           f"minted keys are single use and expire after {expiry} s"))
        try:
            overlay.read_secret(config["api"]["oauth_secret_file"])
            items.append(Check("OAuth secret file", "ok", "present, mode 0600"))
        except FleetError as exc:
            items.append(Check("OAuth secret file", "fail", exc.message))
    else:
        items.append(Check("join keys", "ok", "pasted one at a time; nothing stored"))

    # 2. The policy.
    api = _api(app, config["api"]) if uses_api else None
    text = None
    if args.policy:
        text = paths.expand(args.policy).read_text(encoding="utf-8")
    elif api is not None:
        try:
            text, _ = api.policy_text()
        except FleetError as exc:
            items.append(Check("read the policy", "fail", exc.message))
    else:
        items.append(Check("access rules", "skip",
                           "export your policy from the admin console and run "
                           "ship-fleet overlay check --policy FILE"))
    if text is not None:
        try:
            doc = hujson.parse(text)
        except hujson.HuJSONError as exc:
            items.append(Check("access rules", "fail", f"the policy is {exc}"))
            doc = None
        if isinstance(doc, dict):
            for f in pol.findings(doc, tags):
                items.append(Check(f.name, "ok" if f.ok else f.severity, f.detail))
            if api is not None:
                with_tests, _ = pol.merge(text, tags)
                try:
                    problems = api.validate(with_tests)
                    items.append(Check("Tailscale runs the Ship tests", "fail" if problems
                                       else "ok", "; ".join(problems[:3]) or "they pass"))
                except FleetError as exc:
                    items.append(Check("Tailscale runs the Ship tests", "fail", exc.message))

    # 3. Devices.
    if api is not None:
        try:
            devices = [d for d in api.devices() if set(d.get("tags") or []) & set(tags)]
            known = {n for s in inv.ships() for n in s.names} | {s.name for s in inv.ships()}
            stale = [str(d.get("name") or d.get("hostname")) for d in devices
                     if str(d.get("name") or "").rstrip(".") not in known
                     and d.get("hostname") not in known]
            items.append(Check("tailnet devices", "warn" if stale else "ok",
                               ("not in the inventory: " + ", ".join(stale)) if stale else
                               f"{len(devices)} Ship device(s), all in the inventory"))
        except FleetError as exc:
            items.append(Check("tailnet devices", "fail", exc.message))

    # 4. Each Ship, and 5. optionally, live probes.
    joined: list[tuple[Any, dict]] = []
    for ship in inv.ships():
        try:
            doc = overlay.status(ShipCtl(app.ssh_factory(inv), ship))
        except FleetError as exc:
            items.append(Check(f"{ship.name} overlay", "warn", exc.message[:120]))
            continue
        if not doc or not doc.get("joined"):
            continue
        state = doc.get("state")
        items.append(Check(f"{ship.name} overlay", "ok" if state == "running" else "fail",
                           f"{state} as {doc.get('name') or '?'}"))
        joined.append((ship, doc))
    if args.probe:
        local = getattr(app, "local_overlay_ips", None) or _local_overlay_ips
        mine = local()
        for ship, _doc in joined:
            targets = [("this computer", ip) for ip in mine[:1]]
            targets += [(other.name, other_doc["addresses"][0]) for other, other_doc in joined
                        if other.name != ship.name and other_doc.get("addresses")][:1]
            if not targets:
                items.append(Check(f"{ship.name} cannot reach others", "skip",
                                   "nothing to probe (is this computer on the overlay?)"))
            for label, ip in targets:
                reached = _connects(app, inv, ship, ip)
                items.append(Check(
                    f"{ship.name} cannot reach {label}",
                    "skip" if reached is None else ("fail" if reached else "ok"),
                    "probe could not run" if reached is None else
                    (f"it opened a connection to {ip}:22" if reached else f"{ip}:22 refused")))

    rows = [[c.state.upper(), c.name, c.detail] for c in items]
    app.say(table(rows, ["", "CHECK", "DETAIL"]))
    return 1 if any(c.state == "fail" for c in items) else 0
