"""ship-fleet destroy, resize, snapshot and image (phase 4).

Hypervisor work goes through the driver interface (drivers/base.py); nothing
here knows which hypervisor a Ship runs on.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fleetledger import enrol, images, overlay, status
from fleetledger.commands import table
from fleetledger.concierge import audit
from fleetledger.drivers import NotSupported, PublishedImage
from fleetledger.drivers.base import check_snapshot_name
from fleetledger.errors import FleetError
from fleetledger.inventory import STOCK_IMAGE

SNAPSHOT_WARNING = (
    "A snapshot holds the Ship's sign-ins (Claude, gcloud, GitHub) as they were: "
    "treat it like the Ship itself."
)


def add_parsers(sub, add) -> None:
    p = add(sub, "destroy", "delete a Ship's VM, its seed and its inventory entry",
            "Stop and delete the Ship's VM and disks through its driver, delete any seed\n"
            "left over, and remove it from the inventory. Asks you to type the Ship's\n"
            "name unless --yes. The driver checks the VM's name before deleting it.")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--forget", action="store_true",
                   help="only remove it from the inventory (the VM is already gone, or you "
                   "deleted it by hand)")

    p = add(sub, "resize", "change a Ship's CPU cores, memory or disk",
            "  ship-fleet resize ship-01 --cores 8 --memory-mb 16384\n"
            "  ship-fleet resize ship-01 --disk-gb 80 --restart\n\n"
            "Cores and memory apply when the VM next restarts through the hypervisor\n"
            "(--restart does it now; a reboot from inside the Ship does not). A disk only\n"
            "grows: it grows at once, and the Ship's filesystem with it (ship-ctl grow-root).")
    p.add_argument("name")
    p.add_argument("--cores", type=int)
    p.add_argument("--memory-mb", type=int)
    p.add_argument("--disk-gb", type=int)
    p.add_argument("--restart", action="store_true",
                   help="restart the VM through the hypervisor now, so the change applies")
    p.add_argument("--record-only", action="store_true",
                   help="only record the size in ships.toml (you changed the VM by hand)")

    p = add(sub, "snapshot", "take, list, roll back or delete a Ship's disk snapshots",
            "  ship-fleet snapshot ls ship-01\n"
            "  ship-fleet snapshot take ship-01 [NAME] [--note TEXT]\n"
            "  ship-fleet snapshot rollback ship-01 NAME     (asks first)\n"
            "  ship-fleet snapshot rm ship-01 NAME\n\n"
            "Snapshots are of the disk only, never memory. " + SNAPSHOT_WARNING)
    p.add_argument("action", choices=("ls", "take", "rollback", "rm"))
    p.add_argument("name", help="the Ship")
    p.add_argument("snapshot", nargs="?", help="snapshot name (take: default snap-<time>)")
    p.add_argument("--note", default="", help="description stored with the snapshot")
    p.add_argument("--yes", action="store_true", help="roll back without asking")

    p = add(sub, "image", "build, publish and pin baked base images",
            "  ship-fleet image build [--recipes ID,...] [--backend virt-customize|qemu]\n"
            "  ship-fleet image ls\n"
            "  ship-fleet image publish ID [--driver proxmox] [--template-vmid N]\n"
            "  ship-fleet image use ID|stock      pin the image new Ships start from\n"
            "  ship-fleet image rm ID\n\n"
            "Existing Ships are never re-imaged; update them in place.\n"
            "See docs/runbooks/images.md.")
    p.add_argument("action", choices=("build", "ls", "publish", "use", "rm"))
    p.add_argument("image", nargs="?", help="image id (or stock, for use)")
    p.add_argument("--recipes", help="build: bake these recipes (default: every bake = true)")
    p.add_argument("--backend", choices=("virt-customize", "qemu"), default="virt-customize",
                   help="build: libguestfs offline (default) or one local QEMU boot")
    p.add_argument("--driver", help="publish: driver (default: defaults.driver)")
    p.add_argument("--template-vmid", type=int,
                   help="publish: record a template you made by hand instead")
    p.add_argument("--keep-file", action="store_true",
                   help="rm: keep the local qcow2")


# --- destroy -------------------------------------------------------------------


def cmd_destroy(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    ship = inv.ship(args.name)
    if not args.yes:
        if not app.stdin_isatty():
            raise FleetError(f"Destroying {ship.name} deletes its VM and disks; rerun with --yes.")
        answer = app.confirm(f"This deletes {ship.name}'s VM, disks and sign-ins. "
                             "Type its name to go on: ")
        if answer.strip() != ship.name:
            raise FleetError("Not destroyed.")
    if not args.forget:
        hook = getattr(app, "before_destroy", None)
        if hook is None:  # leave the overlay first, so the Ship does not linger there
            overlay.before_destroy(app, inv, ship)
        else:
            hook(inv, ship)
        driver = app.driver_factory(inv, ship.driver)
        with audit.recorded(ship.name, "destroy", ship.driver) as audited:
            outcome = driver.destroy(ship)
            if not outcome.done:
                audited["outcome"] = "steps"
        for message in outcome.messages:
            app.say(message)
        if not outcome.done:
            app.say(f"Delete {ship.name} by hand:")
            for line in outcome.instructions:
                app.say(f"  {line}")
            app.say(f"Then: ship-fleet destroy {ship.name} --forget")
            return 0
    with store.edit() as ed:
        ed.remove_ship(ship.name)
    if args.forget:
        audit.record(ship.name, "forget", ship.driver, "ok")
    seed = enrol.local_seed(ship)
    if seed is not None:
        seed.unlink(missing_ok=True)
    status.cache_path(ship.name).unlink(missing_ok=True)
    app.say(f"{ship.name} is removed from the inventory.")
    return 0


# --- resize ---------------------------------------------------------------------
def size_limits() -> dict[str, tuple[int, int]]:
    """(minimum, maximum) of cores, memory_mb and disk_gb: ships.toml's size contract,
    the one source of them (checks here, and the web forms' min and max)."""
    from fleetledger import schemas

    props = schemas.definition("ships.toml", "size")["properties"]
    return {key: (int(props[key]["minimum"]), int(props[key]["maximum"]))
            for key in ("cores", "memory_mb", "disk_gb")}


def check_size(size: dict) -> dict[str, int]:
    """The given cores / memory_mb / disk_gb, each within the contract's limits, so a
    size the inventory would refuse never reaches the hypervisor."""
    limits = size_limits()
    out = {}
    for key, value in size.items():
        if value is None:
            continue
        low, high = limits[key]
        if not isinstance(value, int) or not low <= value <= high:
            raise FleetError(f"{key.replace('_', ' ')}: {low} to {high}.")
        out[key] = value
    return out


def resize(app, store, name: str, size: dict, *, restart: bool = False,
           record_only: bool = False) -> list[str]:
    """Resize a Ship through its driver and record the size. Returns what was said."""
    wanted = check_size(size)
    if not wanted:
        raise FleetError("Give --cores, --memory-mb and/or --disk-gb.")
    inv = store.load()
    ship = inv.ship(name)
    said = []
    if not record_only:
        driver = app.driver_factory(inv, ship.driver)
        said += driver.resize(ship, wanted).messages
        if "disk_gb" in wanted:
            said.append(_grow_root(app, inv, ship))
        if restart:
            said += driver.reboot(ship).messages
    with store.edit() as ed:
        ed.set_field(name, "size", {**ship.size, **wanted})
    return said


def _grow_root(app, inv, ship) -> str:
    """The Ship's root filesystem, grown online into the bigger disk."""
    from fleetledger.shipctl import ShipCtl, ShipCtlError

    try:
        result = ShipCtl(app.ssh_factory(inv), ship).call("grow-root", timeout=600)
    except (FleetError, ShipCtlError) as exc:
        return (f"The filesystem was not grown yet ({exc.message}); run ship-fleet update "
                f"{ship.name} if its tooling is older, then ship-fleet resize {ship.name} "
                "--disk-gb again.")
    return " ".join(result.messages) or "Its filesystem fills the disk."


def cmd_resize(args, app) -> int:
    for line in resize(app, app.store_factory(), args.name,
                       {"cores": args.cores, "memory_mb": args.memory_mb,
                        "disk_gb": args.disk_gb},
                       restart=args.restart, record_only=args.record_only):
        app.say(line)
    app.say(f"Recorded in ships.toml: {args.name}'s size.")
    return 0


# --- snapshot -------------------------------------------------------------------


def cmd_snapshot(args, app) -> int:
    inv = app.store_factory().load()
    ship = inv.ship(args.name)
    driver = app.driver_factory(inv, ship.driver)
    if args.action == "ls":
        if args.snapshot:
            raise FleetError("snapshot ls takes only the Ship's name.")
        snaps = driver.snapshots(ship)
        if not snaps:
            app.say(f"{ship.name} has no snapshots.")
            return 0
        app.say(table([[s.name, s.created or "-", s.description] for s in snaps],
                      ["SNAPSHOT", "TAKEN", "NOTE"]))
        return 0
    if args.action == "take":
        name = args.snapshot or datetime.now(UTC).strftime("snap-%Y%m%d-%H%M%S")
        outcome = driver.snapshot(ship, name, args.note)
        _report(app, outcome)
        app.note(SNAPSHOT_WARNING)
        return 0
    if not args.snapshot:
        raise FleetError(f"snapshot {args.action} needs a snapshot name.")
    check_snapshot_name(args.snapshot)  # before asking or recording: never log a bad name
    if args.action == "rollback":
        if not args.yes:
            if not app.stdin_isatty():
                raise FleetError("Rolling back discards everything since the snapshot; "
                                 "rerun with --yes.")
            answer = app.confirm(f"Roll {ship.name} back to {args.snapshot}? Everything since "
                                 "then is lost, sign-ins included. Type yes: ")
            if answer.strip().lower() != "yes":
                raise FleetError("Not rolled back.")
        with audit.recorded(ship.name, "snapshot-rollback", args.snapshot) as audited:
            outcome = driver.rollback(ship, args.snapshot)
            audited["outcome"] = "ok" if outcome.done else "steps"
        _report(app, outcome)
        return 0
    with audit.recorded(ship.name, "snapshot-delete", args.snapshot) as audited:
        outcome = driver.delete_snapshot(ship, args.snapshot)
        audited["outcome"] = "ok" if outcome.done else "steps"
    _report(app, outcome)
    return 0


def _report(app, outcome) -> None:
    for message in outcome.messages:
        app.say(message)
    for line in outcome.instructions:
        app.say(f"  {line}")


# --- image ------------------------------------------------------------------------


def cmd_image(args, app) -> int:
    handler = {
        "build": _image_build,
        "ls": _image_ls,
        "publish": _image_publish,
        "use": _image_use,
        "rm": _image_rm,
    }[args.action]
    if args.action in ("publish", "use", "rm") and not args.image:
        raise FleetError(f"image {args.action} needs an image id.")
    if args.action in ("build", "ls") and args.image:
        raise FleetError(f"image {args.action} takes no image id.")
    return handler(args, app)


def _image_build(args, app) -> int:
    store = app.store_factory()
    store.load()  # fail early on a broken inventory
    recipes = [r.strip() for r in args.recipes.split(",") if r.strip()] if args.recipes else None
    builder = getattr(app, "image_builder", None) or images.build
    built = builder(app.repo_factory(), recipes=recipes, backend=args.backend, note=app.note)
    with store.edit() as ed:
        ed.add_image(built.id, built.entry)
    app.say(f"Built image {built.id}: {built.path} (sha256 {built.entry['sha256'][:16]}...).")
    app.say(f"Next: ship-fleet image publish {built.id}, then ship-fleet image use {built.id}")
    return 0


def _image_ls(args, app) -> int:
    inv = app.store_factory().load()
    found = images.listing(inv)
    if not found:
        app.say("No baked images yet; new Ships use the stock template. "
                "Build one with ship-fleet image build.")
        return 0
    rows = [[("* " if i["pinned"] else "  ") + i["id"], i["ubuntu"],
             i["tooling"] + (" dirty" if i["dirty"] else ""), ",".join(i["recipes"]) or "-",
             ", ".join(f"{d} {'/'.join(str(v) for v in ref.values())}"
                       for d, ref in i["published"].items()) or "-",
             str(i["ships"]), "kept" if i["file"] else "-"] for i in found]
    app.say(table(rows, ["IMAGE", "UBUNTU", "TOOLING", "BAKED", "PUBLISHED", "SHIPS", "FILE"]))
    app.say("* pinned: new Ships start from it" if inv.pinned_image != STOCK_IMAGE
            else "New Ships use the stock template (ship-fleet image use ID to pin one).")
    return 0


def _image_publish(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    inv.image(args.image)
    driver_id = args.driver or inv.defaults.get("driver", "manual")
    if args.template_vmid is not None:
        ref = {"template_vmid": args.template_vmid}
        app.say(f"Recorded image {args.image} as {driver_id} template {args.template_vmid}.")
    else:
        image = images.local_file(inv, args.image)
        driver = app.driver_factory(inv, driver_id)
        outcome = driver.publish_image(image)
        _report(app, outcome)
        if not outcome.done:
            return 0
        ref = dict(outcome.driver_ref)
    with store.edit() as ed:
        current = dict(store.load().image(args.image).get("published", {}))
        current[driver_id] = ref
        ed.set_image_field(args.image, "published", current)
    app.say(f"Pin it for new Ships with: ship-fleet image use {args.image}")
    return 0


def _image_use(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    if args.image != STOCK_IMAGE:
        entry = inv.image(args.image)
        driver_id = inv.defaults.get("driver", "manual")
        if driver_id not in entry.get("published", {}):
            raise FleetError(
                f"Image {args.image} is not published to the {driver_id} driver (defaults.driver).",
                hint=f"ship-fleet image publish {args.image} --driver {driver_id}",
            )
    with store.edit() as ed:
        ed.set_default("image", None if args.image == STOCK_IMAGE else args.image)
    chosen = "the stock template" if args.image == STOCK_IMAGE else args.image
    app.say(f"New Ships now start from {chosen}. Existing Ships are not changed.")
    return 0


def _image_rm(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    entry = inv.image(args.image)
    if inv.pinned_image == args.image:
        raise FleetError(f"Image {args.image} is pinned for new Ships.",
                         hint="Pin another first: ship-fleet image use ID|stock")
    remaining = dict(entry.get("published", {}))
    for driver_id, ref in sorted(entry.get("published", {}).items()):
        driver = app.driver_factory(inv, driver_id)
        try:
            outcome = driver.remove_image(PublishedImage(args.image, dict(ref)))
        except NotSupported as exc:
            app.note(f"{driver_id}: {exc.message}")
            continue
        _report(app, outcome)
        if outcome.done:
            remaining.pop(driver_id)
    if remaining:
        with store.edit() as ed:
            ed.set_image_field(args.image, "published", remaining)
        app.say(f"Image {args.image} is still published to {', '.join(remaining)}; "
                "remove it there, then run this again.")
        return 1
    if not args.keep_file:
        images.remove_local(inv, args.image)
    with store.edit() as ed:
        ed.remove_image(args.image)
    app.say(f"Removed image {args.image}.")
    if any(s.image == args.image for s in inv.ships()):
        app.say("Ships made from it keep running: they are full copies.")
    return 0
