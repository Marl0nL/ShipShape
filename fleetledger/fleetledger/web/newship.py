"""Making a Ship from the browser: the New Ship form (/new) and setup's first Ship.

One code path: the form becomes `ship-fleet new` arguments, checked here with the
same rules `cmd_new` applies (so nothing starts on bad input), and the job runs that
command through web.jobs.run_cli. The one exception, --no-wait, is left out: the
job waits, and a Ship it leaves unenrolled shows as "Not enrolled yet" from the
inventory, with Finish enrolling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fleetledger.errors import FleetError
from fleetledger.web.forms import NAME_RE

MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")


def _one(fields: dict[str, list[str]], key: str) -> str:
    values = fields.get(key, [])
    return values[0].strip() if len(values) == 1 else ""


def next_name(inv) -> str:
    """The first free ship-NN."""
    taken = {s.name for s in inv.ships()} if inv is not None else set()
    n = 1
    while f"ship-{n:02d}" in taken:
        n += 1
    return f"ship-{n:02d}"


@dataclass
class FormChoices:
    """What the form offers (and shows as its defaults)."""

    name: str
    tags: list[str]
    known_tags: list[str]
    size: dict[str, int]
    drivers: list[str]
    driver: str
    images: list[str]
    image: str
    mode: str
    timezone: str
    tools: list[tuple[str, str, bool]]  # (id, description, ticked by default)
    profiles: list[tuple[str, str]]
    overlay: bool
    signins: tuple[str, ...] = field(default_factory=tuple)


def choices(app, inv) -> FormChoices:
    from fleetledger import signins
    from fleetledger.catalogue import Catalogue
    from fleetledger.seed.shiptoml import local_timezone

    catalogue = Catalogue.from_repo(app.repo_factory())
    defaults = inv.defaults if inv is not None else {}
    default_tools = (list(defaults["tools"]) if "tools" in defaults
                     else catalogue.default_tools())
    drivers = ["manual"] + (["proxmox"] if inv is not None and inv.driver_config("proxmox")
                            else [])
    driver = defaults.get("driver", "manual")
    images = ["stock"] + sorted(
        i for i, e in (inv.images.items() if inv is not None else [])
        if driver in (e.get("published") or {}))
    known_tags = sorted({t for s in (inv.ships() if inv is not None else [])
                         for t in (s.tags or ())} | set(defaults.get("tags", [])))
    return FormChoices(
        name=next_name(inv),
        tags=list(defaults.get("tags", [])),
        known_tags=known_tags,
        size=inv.effective_size() if inv is not None else {},
        drivers=drivers,
        driver=driver if driver in drivers else "manual",
        images=images,
        image=inv.pinned_image if inv is not None else "stock",
        mode=(defaults.get("claude") or {}).get("default_mode", "auto"),
        timezone=local_timezone(),
        tools=[(r.id, r.description, r.id in default_tools)
               for r in sorted(catalogue.recipes.values(), key=lambda r: r.id)],
        profiles=[(p.id, p.description)
                  for p in sorted(catalogue.profiles.values(), key=lambda p: p.id)],
        overlay=bool(inv is not None and inv.overlay),
        signins=signins.CHOICES,
    )


def argv(fields: dict[str, list[str]], app, inv, *, advanced: bool = True
         ) -> tuple[str, list[str]]:
    """(name, the `ship-fleet new` argv) from the form, checked: raises FleetError with
    a plain message on anything `new` would refuse."""
    from fleetledger import github, ops, signins
    from fleetledger.overlay import HOST_RE
    from fleetledger.seed.shiptoml import TIMEZONE_RE, known_timezone
    from fleetledger.web import setup

    name = _one(fields, "name") or next_name(inv)
    if not NAME_RE.match(name):
        raise FleetError("A Ship name uses a-z, 0-9 and -, up to 31 characters.")
    if inv is not None and inv.has_ship(name):
        raise FleetError(f"{name} is already in the inventory: choose another name.")
    out = ["new", name]

    raw_tags = _one(fields, "tag").replace(",", " ").split()
    for tag in raw_tags:
        if not ops.TAG_RE.match(tag):
            raise FleetError(f"'{tag}' is not a tag: a-z, 0-9 and -, up to 31 characters.")
        out += ["--tag", tag]

    size = setup.size_from_form(fields)  # lifecycle.check_size: the size contract
    flags = {"cores": "--cores", "memory_mb": "--memory-mb", "disk_gb": "--disk-gb"}
    for key, value in size.items():
        out += [flags[key], str(value)]

    chosen = signins.check(fields.get("signin", []) if _one(fields, "signins_shown")
                           else list(signins.CHOICES))
    if chosen != list(signins.CHOICES):
        out += ["--signins", ",".join(chosen)]

    if _one(fields, "agent_on") == "yes":
        agent, cwd = _one(fields, "agent") or "main", _one(fields, "cwd") or "~/work"
        mode = _one(fields, "agent_mode") if advanced else ""
        if mode and mode not in MODES:
            raise FleetError("The first agent's mode is not one Claude Code has.")
        first = f"{agent}:{cwd}" + (f":{mode}" if mode else "")
        ops.parse_autostart_change("autostart.add", first)
        out += ["--autostart", first]
        if _one(fields, "repo"):
            out += ["--repo", github.check_name(_one(fields, "repo"))]
        if advanced and _one(fields, "prompt"):
            out += ["--autostart-prompt", _one(fields, "prompt")[:4000]]
    if _one(fields, "overlay") != "yes":
        out.append("--no-overlay")

    if advanced:
        out += _advanced(fields, app, inv, HOST_RE, TIMEZONE_RE, known_timezone)
    return name, out


def _advanced(fields, app, inv, host_re, timezone_re, known_timezone) -> list[str]:
    out: list[str] = []
    picked = choices(app, inv)
    driver = _one(fields, "driver")
    if driver:
        if driver not in picked.drivers:
            raise FleetError("Where it runs: not one of the choices.")
        out += ["--driver", driver]
    image = _one(fields, "image")
    if image and image != picked.image:
        if image not in picked.images:
            raise FleetError("Base image: not one of the images published there.")
        out += ["--image", image]
    vmid = _one(fields, "vmid")
    if vmid:
        if not vmid.isdigit() or not 100 <= int(vmid) <= 999999999:
            raise FleetError("VM id: a whole number, 100 or more.")
        out += ["--vmid", vmid]
    hosts = [h.strip() for h in "\n".join(fields.get("hosts", [])).splitlines() if h.strip()]
    for host in hosts:
        if not host_re.match(host):
            raise FleetError(f"'{host}' is not a host name or address.")
        out += ["--host", host]
    mode = _one(fields, "mode")
    if mode and mode != picked.mode:
        if mode not in MODES:
            raise FleetError("Default permission mode: not one Claude Code has.")
        out += ["--mode", mode]
    if _one(fields, "allow_bypass") == "yes":
        out.append("--allow-bypass")
    if _one(fields, "agent_sudo") == "yes":
        out.append("--agent-sudo")
    zone = _one(fields, "timezone")
    if zone and zone != picked.timezone:
        if not (timezone_re.match(zone) and known_timezone(zone)):
            raise FleetError("Time zone: an IANA name such as Europe/Berlin.")
        out += ["--timezone", zone]
    if _one(fields, "tools_shown"):
        known = {t for t, _, _ in picked.tools}
        ticked = [t for t in fields.get("tool", []) if t in known]
        if len(ticked) != len(fields.get("tool", [])):
            raise FleetError("Tools: not one of the catalogue's.")
        defaults = [t for t, _, on in picked.tools if on]
        if not set(defaults) <= set(ticked):  # a default unticked: say exactly which
            out += ["--no-default-tools", "--tools", ",".join(ticked)]
        elif set(ticked) - set(defaults):
            out += ["--tools", ",".join(t for t in ticked if t not in defaults)]
    known_profiles = {p for p, _ in picked.profiles}
    for profile in fields.get("profile", []):
        if profile not in known_profiles:
            raise FleetError("Profiles: not one of the catalogue's.")
        out += ["--profile", profile]
    return out


def make_ship(app, name: str, argv_: list[str]):
    """The job that makes a Ship: `ship-fleet new` itself, then this computer trusted
    so `ssh <ship>` and `herdr --remote <ship>` work at once."""
    from fleetledger import trustcmd
    from fleetledger.enrol import StillStarting
    from fleetledger.web.jobs import BrowserApp, run_cli

    def run(job) -> None:
        try:
            inv = app.store_factory().load()
            driver = next((argv_[i + 1] for i, a in enumerate(argv_) if a == "--driver"),
                          inv.defaults.get("driver", "manual"))
            job.result["manual"] = driver == "manual"
        except FleetError:
            pass
        code = run_cli(app, argv_, job)
        job.result["still_starting"] = code == StillStarting.exit_code
        try:
            ship = app.store_factory().load().ship(name)
        except FleetError:
            ship = None
        job.result["enrolled"] = bool(ship is not None and ship.enrolled)
        if job.result["enrolled"]:
            job.result["attach_here"] = trustcmd.trust_this_computer(
                BrowserApp(app, job), app.store_factory(), job.say)
        job.ok = code == 0

    return run


def start(ui: Any, name: str, argv_: list[str]):
    """Start making it (refused while something runs on that name)."""
    return ui.jobs.start("create", [name], make_ship(ui.app, name, argv_), f"Make {name}")
