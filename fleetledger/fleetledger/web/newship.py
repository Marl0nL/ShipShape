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


one_field = _one


def next_name(inv) -> str:
    """The first free ship-NN: not in the inventory, nor the name of a saved failed
    attempt (attempts.py), so a plain new Ship never takes over an attempt's name."""
    from fleetledger import attempts

    taken = {s.name for s in inv.ships()} if inv is not None else set()
    try:
        taken |= {a.name for a in attempts.load()}
    except OSError:
        pass
    n = 1
    while f"ship-{n:02d}" in taken:
        n += 1
    return f"ship-{n:02d}"


def images_for(inv, driver: str) -> list[str]:
    """The base images a Ship on this driver can start from (as `new` checks)."""
    return ["stock"] + sorted(
        i for i, e in (inv.images.items() if inv is not None else [])
        if driver in (e.get("published") or {}))


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
    overlay: bool  # the overlay is set up, so the form offers joining it
    signins: tuple[str, ...] = field(default_factory=tuple)
    # What the form shows as filled in: the defaults, or what was posted (refill).
    cores: str = ""
    memory_gb: str = ""
    disk_gb: str = ""
    signins_on: tuple[str, ...] = field(default_factory=tuple)
    agent_on: bool = True
    agent: str = "main"
    cwd: str = "~/work"
    repo: str = ""
    overlay_on: bool = True
    vmid: str = ""
    hosts: str = ""
    agent_mode: str = ""
    prompt: str = ""
    allow_bypass: bool = False
    agent_sudo: bool = False
    profiles_on: tuple[str, ...] = field(default_factory=tuple)
    open_advanced: bool = False


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
    images = images_for(inv, driver)
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
        signins_on=tuple(signins.CHOICES),
        **_size_text(inv.effective_size() if inv is not None else {}),
    )


def _size_text(size: dict[str, int]) -> dict[str, str]:
    memory = size.get("memory_mb")
    return {"cores": str(size.get("cores", "")),
            "memory_gb": str(memory // 1024) if memory else "",
            "disk_gb": str(size.get("disk_gb", ""))}


ADVANCED_FIELDS = ("vmid", "hosts", "agent_mode", "prompt", "allow_bypass", "agent_sudo",
                   "profile")


def refill(nf: FormChoices, fields: dict[str, list[str]]) -> FormChoices:
    """The form as it was posted, so a refused one comes back with what was typed."""
    import dataclasses

    def text(key: str, default: str) -> str:
        return fields[key][0] if fields.get(key) else default

    tools = nf.tools
    if _one(fields, "tools_shown"):
        ticked = set(fields.get("tool", []))
        tools = [(t, d, t in ticked) for t, d, _ in nf.tools]
    changed_tools = [on for *_, on in tools] != [on for *_, on in nf.tools]
    out = dataclasses.replace(
        nf,
        name=text("name", nf.name),
        tags=_one(fields, "tag").replace(",", " ").split() if "tag" in fields else nf.tags,
        cores=text("cores", nf.cores), memory_gb=text("memory_gb", nf.memory_gb),
        disk_gb=text("disk_gb", nf.disk_gb),
        signins_on=(tuple(fields.get("signin", [])) if _one(fields, "signins_shown")
                    else nf.signins_on),
        agent_on=_one(fields, "agent_on") == "yes",
        agent=text("agent", nf.agent), cwd=text("cwd", nf.cwd), repo=text("repo", ""),
        overlay_on=_one(fields, "overlay") == "yes",
        driver=text("driver", nf.driver), image=text("image", nf.image),
        vmid=text("vmid", ""), hosts=text("hosts", ""), mode=text("mode", nf.mode),
        agent_mode=text("agent_mode", ""), prompt=text("prompt", ""),
        allow_bypass=_one(fields, "allow_bypass") == "yes",
        agent_sudo=_one(fields, "agent_sudo") == "yes",
        timezone=text("timezone", nf.timezone), tools=tools,
        profiles_on=tuple(fields.get("profile", [])),
    )
    out.open_advanced = (changed_tools or any(_one(fields, k) for k in ADVANCED_FIELDS)
                         or bool(fields.get("profile"))
                         or (out.driver, out.image, out.mode, out.timezone)
                         != (nf.driver, nf.image, nf.mode, nf.timezone))
    return out


def fields_from(options: dict[str, Any], nf: FormChoices) -> dict[str, list[str]]:
    """The form fields that make these `ship-fleet new` options (a saved attempt,
    attempts.py): what refill() takes, so the form shows what was tried."""
    o = options
    out: dict[str, list[str]] = {"name": [str(o["name"])]}
    if o.get("tag"):
        out["tag"] = [" ".join(o["tag"])]
    for key in ("cores", "disk_gb"):
        if o.get(key) is not None:
            out[key] = [str(o[key])]
    if o.get("memory_mb") is not None:
        out["memory_gb"] = [str(max(1, round(o["memory_mb"] / 1024)))]
    if o.get("signins") is not None:
        out["signins_shown"], out["signin"] = ["yes"], o["signins"].split(",")
    first = (o.get("autostart") or [""])[0].split(":")
    if len(first) >= 2 or o.get("repo"):
        out["agent_on"] = ["yes"]
        if len(first) >= 2:
            out["agent"], out["cwd"] = [first[0]], [first[1]]
        if len(first) == 3:
            out["agent_mode"] = [first[2]]
        if o.get("repo"):
            out["repo"] = [o["repo"]]
    if o.get("autostart_prompt"):
        out["prompt"] = [o["autostart_prompt"]]
    out["overlay"] = [] if o.get("no_overlay") else ["yes"]
    for key, field_ in (("driver", "driver"), ("image", "image"), ("mode", "mode"),
                        ("timezone", "timezone")):
        if o.get(key):
            out[field_] = [o[key]]
    if o.get("vmid") is not None:
        out["vmid"] = [str(o["vmid"])]
    if o.get("host"):
        out["hosts"] = ["\n".join(o["host"])]
    for flag in ("allow_bypass", "agent_sudo"):
        if o.get(flag):
            out[flag] = ["yes"]
    extra = [t.strip() for t in (o.get("tools") or "").split(",") if t.strip()]
    if o.get("no_default_tools") or extra:
        base = [] if o.get("no_default_tools") else [t for t, _, on in nf.tools if on]
        out["tools_shown"], out["tool"] = ["yes"], list(dict.fromkeys([*base, *extra]))
    if o.get("profile"):
        out["profile"] = list(o["profile"])
    return out


def argv(fields: dict[str, list[str]], app, inv, *, advanced: bool = True
         ) -> tuple[str, list[str]]:
    """(name, the `ship-fleet new` argv) from the form, checked: raises FleetError with
    a plain message on anything `new` would refuse."""
    from fleetledger import attempts, github, ops, signins
    from fleetledger.overlay import HOST_RE
    from fleetledger.seed.shiptoml import TIMEZONE_RE, known_timezone
    from fleetledger.web import setup

    name = _one(fields, "name") or next_name(inv)
    if not NAME_RE.match(name):
        raise FleetError("A Ship name uses a-z, 0-9 and -, up to 31 characters.")
    if inv is not None and inv.has_ship(name):
        tried = attempts.get(name)
        raise FleetError((tried and attempts.blocker(inv, tried))
                         or f"{name} is already in the inventory: choose another name.")
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
        if image not in images_for(inv, driver or picked.driver):  # the driver chosen here
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


def make_ship(app, name: str, argv_: list[str], retry_of: str | None = None):
    """The job that makes a Ship: `ship-fleet new` itself, then this computer trusted
    so `ssh <ship>` and `herdr --remote <ship>` work at once. retry_of: the saved
    attempt this one tries again (attempts.py); what it left is removed first, and
    it goes once this one works. A failure is saved by `new` itself."""
    from fleetledger import attempts, trustcmd
    from fleetledger.enrol import StillStarting
    from fleetledger.web.jobs import BrowserApp, run_cli

    def run(job) -> None:
        try:
            inv = app.store_factory().load()
            driver = next((argv_[i + 1] for i, a in enumerate(argv_) if a == "--driver"),
                          inv.defaults.get("driver", "manual"))
            job.result["manual"] = driver == "manual"
        except FleetError:
            inv = None
        source = attempts.get(retry_of) if retry_of and retry_of != name else None
        if source is not None and source.left:  # renamed: `new` would not see it
            _offer(job, source.name)  # if this fails, offer that one again
            attempts.clear_left(BrowserApp(app, job), inv, source, job.say)
        try:
            code = run_cli(app, argv_, job)
        except BaseException:  # `new` saved it before this reached the job
            _offer(job, name)
            raise
        if code != 0 and code != StillStarting.exit_code:
            _offer(job, name)
        elif source is not None:
            job.result.pop("retry", None)
            attempts.drop(source.name)
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


def _offer(job, name: str) -> None:
    """The job card offers Edit and retry of the attempt saved at name, as it is now
    (retry_at: a later attempt at that name, or none, and the card stops offering it)."""
    from fleetledger import attempts

    at = attempts.saved_at(name)
    job.result["retry"] = name if at is not None else None
    job.result["retry_at"] = at


def start(ui: Any, name: str, argv_: list[str], retry_of: str | None = None,
          cleared: list[str] = ()):
    """Start making it (refused while something runs on that name, or on the
    attempt it retries). cleared: the attempts whose leftovers the person confirmed
    removing (check_confirmed); for the one at name, `new` gets --yes."""
    if name in cleared:
        argv_ = [*argv_, "--yes"]
    ships = [name] + ([retry_of] if retry_of and retry_of != name else [])
    return ui.jobs.start("create", [name], make_ship(ui.app, name, argv_, retry_of),
                         f"Make {name}", locks=[f"attempt {s}" for s in ships])


def clearing(retry, name: str) -> list:
    """The saved attempts whose leftovers on the hypervisor making name removes first:
    the one retried, and the one saved at name itself (`new` clears that one)."""
    from fleetledger import attempts

    at_name = attempts.get(name) if NAME_RE.match(name or "") else None
    out = [a for a in (retry, at_name) if a is not None and a.left]
    return [a for i, a in enumerate(out) if a.name not in {b.name for b in out[:i]}]


def confirm_token(attempt) -> str:
    """What the form's clear_left carries for an attempt: its name and when it was
    made, so a confirmation never covers a later attempt at that name."""
    return f"{attempt.name}@{attempt.at}"


def check_confirmed(fields: dict[str, list[str]], clearing_: list) -> list[str]:
    """Raise FleetError unless the confirmed dialog named every attempt in clearing_
    (nothing on the hypervisor is removed that it did not name); returns their names."""
    confirmed = set(fields.get("clear_left", [])) if _one(fields, "confirm") == "yes" else set()
    unasked = [a for a in clearing_ if confirm_token(a) not in confirmed]
    if unasked:
        raise FleetError(
            "; ".join(f"The failed attempt at {a.name} left {', '.join(a.describe_left())}"
                      for a in unasked)
            + ". Making the Ship removes that first: confirm it in the dialog.")
    return [a.name for a in clearing_]


def retry_of(fields: dict[str, list[str]]):
    """The saved attempt a posted form retries, if any (attempts.py)."""
    from fleetledger import attempts

    name = _one(fields, "retry_of")
    return attempts.get(name) if name and NAME_RE.match(name) else None
