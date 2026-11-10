"""Encrypted backups of the whole fleet: `ship-fleet backup` (docs/runbooks/fleetledger-vm.md).

A backup is exactly what `ship-fleet migrate` would move (the inventory, the keys,
every secret, the history), sealed under FleetLedger's vault (vault.py):

- the stream migrate sends, Fernet-encrypted with the vault's data key;
- a header line holding the vault's slots (the data key under your passphrase and
  under the recovery code; scrypt), plus when, which epoch and which commit.

So a backup opens with the vault's passphrase or recovery code as they were when it
was made, and with nothing else: written to a NAS or pulled to your desktop, it holds
nothing usable without them. There is never an unencrypted backup: with the vault
off, or locked, `backup now` refuses.

- now --to DIR [--keep N]: one backup, as fleetledger-<when, UTC>-e<epoch>.flbackup
  (0600, in a 0700 folder), keeping the newest N there.
- schedule --to DIR [--at HH:MM] [--keep N]: a daily systemd user timer for `now`
  (fleetledger-backup.timer, Persistent, so a missed day runs at the next boot).
- unschedule.

Restore with `ship-fleet migrate import FILE` (migrate.py): it asks for the
passphrase (or the recovery code), then takes the fleet exactly as `migrate receive`
does: on standby, every Ship checked, never over another FleetLedger, never a fleet
older than one this computer handed on; `ship-fleet migrate activate` makes it the
active copy (the epoch goes up).

A backup runs beside everything else (it holds the inventory's lock while it reads,
not the whole FleetLedger), so the service keeps running. It is skipped, and says
why, while something would stop a move (a Ship not enrolled yet, a handed-over copy).
"""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fleetledger import paths, vault
from fleetledger.errors import ConfigError, FleetError

SCHEMA = "fleetledger.backup/1"
SUFFIX = ".flbackup"
NAME_RE = re.compile(r"^fleetledger-\d{8}-\d{6}-\d{6}-e\d+\.flbackup$")
MAX_HEADER = 64 * 1024
TIMER = "fleetledger-backup.timer"
UNIT = "fleetledger-backup.service"
HEADER = "# Written by ship-fleet backup schedule"
LAST = "backup-last.json"  # in the data folder: the last result (migrate.BACKUP_LAST)
AT_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def seal(app: Any) -> tuple[bytes, dict[str, Any]]:
    """The whole fleet, sealed under the vault: (the backup's bytes, its header)."""
    from cryptography.fernet import Fernet

    from fleetledger import handover, migrate

    if not vault.is_on():
        raise FleetError("Backups are sealed with FleetLedger's vault, and it is off here: "
                         "there is never an unencrypted backup.",
                         hint="Turn it on: ship-fleet vault init.")
    key = vault.data_key()  # Locked when locked
    slots = vault.load_file()["slots"]
    store = app.store_factory()
    if not store.exists():
        raise ConfigError(f"No inventory at {store.path}: there is no fleet to back up.")
    with store.held() as text:
        inv = store.load()
        plan = migrate.build_plan(app, text, inv, for_backup=True)
        if plan.blockers:
            raise FleetError("Not backing up now: " + "; ".join(f"{what}: {why}" for what, why
                                                               in plan.blockers) + ".")
        stream = io.BytesIO()
        migrate.write_stream(plan, stream)
    header = {"schema": SCHEMA, "made": handover.now(), "epoch": handover.epoch(inv),
              "commit": plan.commit, "fleet": migrate.summary(inv), "slots": slots}
    header["left_out"] = [f"{what}: {why}" for what, why in plan.skipped]
    token = Fernet(key).encrypt(stream.getvalue())
    return json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n" + token, header


def open_backup(data: bytes, secret: str, *, recovery: bool = False) -> tuple[bytes, dict]:
    """The migrate stream inside a backup: (stream, header). WrongSecret if the secret
    does not open it; FleetError if it is not a backup FleetLedger wrote."""
    from cryptography.fernet import Fernet, InvalidToken

    line, sep, token = data.partition(b"\n")
    try:
        header = json.loads(line) if sep and len(line) <= MAX_HEADER else None
    except ValueError:
        header = None
    if not isinstance(header, dict) or header.get("schema") != SCHEMA \
            or not isinstance(header.get("slots"), list):
        raise FleetError("That is not a FleetLedger backup.")
    kind = "recovery" if recovery else "passphrase"
    slot = next((s for s in header["slots"] if isinstance(s, dict) and s.get("kind") == kind),
                None)
    if slot is None or not _slot_ok(slot):
        raise FleetError(f"That backup has no {kind} slot FleetLedger can open.")
    data_key = vault.open_slot(slot, vault.clean_recovery_code(secret) if recovery else secret)
    if data_key is None:
        raise vault.WrongSecret("That " + ("recovery code" if recovery else "passphrase")
                                + " does not open this backup (it opens with the vault's as "
                                "they were when it was made).")
    try:
        return Fernet(data_key).decrypt(token.strip()), header
    except InvalidToken:
        raise FleetError("That backup is damaged (it fails its check); nothing was "
                         "read.") from None


def _slot_ok(slot: dict[str, Any]) -> bool:
    """A slot within the vault's own bounds (a file asking for a huge scrypt cost is
    refused, not run)."""
    n, r, p = slot.get("n"), slot.get("r"), slot.get("p")
    return (slot.get("kdf") == "scrypt"
            and all(isinstance(v, int) and not isinstance(v, bool) for v in (n, r, p))
            and 2 ** 10 <= n <= vault.MAX_N and n & (n - 1) == 0
            and 1 <= r <= vault.MAX_R and 1 <= p <= vault.MAX_P)


def file_name(header: dict[str, Any]) -> str:
    when = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")  # sorts by when, never the same
    return f"fleetledger-{when}-e{int(header['epoch'])}{SUFFIX}"


def prune(folder: Path, keep: int) -> list[Path]:
    """The newest keep backups stay; older ones FleetLedger wrote go."""
    mine = sorted((f for f in folder.iterdir() if NAME_RE.match(f.name) and f.is_file()),
                  key=lambda f: f.name, reverse=True)
    gone = mine[keep:]
    for f in gone:
        f.unlink(missing_ok=True)
    return gone


def _nearest(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def on_this_disk(folder: Path) -> bool:
    """Is folder (or where it would be made) on the same file system as FleetLedger's
    own data? Then the backup goes with this computer's disk: a NAS mount that is not
    mounted just now looks exactly like this."""
    try:
        return _nearest(folder).stat().st_dev == _nearest(paths.data_dir()).stat().st_dev
    except OSError:
        return True


def last() -> dict[str, Any] | None:
    """The last backup's result here (doctor shows it), or None."""
    return paths.read_json(paths.data_dir() / LAST)


def _note(ok: bool, folder: Path, why: str | None = None, file: str | None = None,
          left_out: list[str] | None = None) -> None:
    from fleetledger import handover

    doc = {"at": handover.now(), "ok": ok, "to": str(folder), "why": why, "file": file,
           "left_out": left_out or []}
    try:
        folder_ = paths.private_dir(paths.data_dir())
        paths.write_private(folder_ / LAST, (json.dumps(doc, indent=1) + "\n").encode("utf-8"))
    except OSError:
        pass  # the result is a convenience; the backup's own outcome is what counts


def cmd_now(args: Any, app: Any) -> int:
    from fleetledger.concierge import audit

    folder = paths.expand(args.to)
    try:
        if on_this_disk(folder) and not args.same_disk:
            raise FleetError(f"{folder} is on this computer's own disk, so a backup there goes "
                             "with it (a NAS share that is not mounted just now looks like "
                             "this too). Nothing was written.",
                             hint="Mount it, or add --same-disk to keep backups here anyway.")
        data, header = seal(app)
        paths.private_dir(folder)
        target = folder / file_name(header)
        paths.write_private(target, data)
    except (FleetError, OSError) as exc:
        why = exc.message if isinstance(exc, FleetError) else exc.strerror
        _note(False, folder, why)
        audit.record("-", "backup", "backup", "failed")
        raise
    gone = prune(folder, args.keep)
    _note(True, folder, file=target.name, left_out=header["left_out"])
    audit.record("-", "backup", "backup", "ok")
    app.say(f"Backed up {header['fleet']} (epoch {header['epoch']}) to {paths.tilde(target)}: "
            "sealed under the vault, so it opens with its passphrase or recovery code only."
            + (f" Removed {len(gone)} older one(s) (keeping {args.keep})." if gone else ""))
    for item in header["left_out"]:
        app.say(f"  Left out (it would stop a move, not a backup): {item}")
    return 0


def _units(ship_fleet: str, folder: Path, keep: int, at: str,
           env: list[tuple[str, str]], same_disk: bool = False) -> tuple[str, str]:
    from fleetledger import service

    path = f"{Path(ship_fleet).parent}:{service.SYSTEM_PATH}"
    unit = "\n".join([
        f"{HEADER} ({service.RUNBOOK}).",
        "[Unit]",
        "Description=FleetLedger: an encrypted backup of the fleet",
        f"RequiresMountsFor={folder}",  # a NAS share: mounted first, or no backup
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={ship_fleet} backup now --to {folder} --keep {keep}"
        + (" --same-disk" if same_disk else ""),
        f"Environment=PATH={path}",
        *(f"Environment={name}={value}" for name, value in env),
        "UMask=0077",
        "NoNewPrivileges=yes",
        "LimitCORE=0",  # a core dump would hold the unlocked vault's key
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "SystemCallArchitectures=native",
        "",
    ])
    timer = "\n".join([
        f"{HEADER}.",
        "[Unit]",
        "Description=FleetLedger: a daily encrypted backup of the fleet",
        "",
        "[Timer]",
        f"OnCalendar=*-*-* {at}:00",
        "Persistent=true",
        "RandomizedDelaySec=10min",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])
    return unit, timer


def cmd_schedule(args: Any, app: Any) -> int:
    from fleetledger import service

    if not AT_RE.match(args.at):
        raise FleetError("--at: a time of day, HH:MM (24 hours).")
    if not 1 <= args.keep <= 365:
        raise FleetError("--keep: between 1 and 365.")
    folder = paths.expand(args.to).absolute()
    if not service.SAFE.match(str(folder)):
        raise ConfigError(f"{folder} has a character a systemd unit would need quoted; use a "
                          "plainer folder.")
    if not vault.is_on():
        raise FleetError("Backups are sealed with FleetLedger's vault, and it is off here.",
                         hint="Turn it on first: ship-fleet vault init.")
    ship_fleet = service.ship_fleet_path(args.ship_fleet)
    if on_this_disk(folder) and not args.same_disk:
        raise FleetError(f"{folder} is on this computer's own disk, so the backups would go "
                         "with it (or the NAS share is not mounted just now).",
                         hint="Mount it first, or add --same-disk to keep them here anyway.")
    unit, timer = _units(ship_fleet, folder, args.keep, args.at, service.carried_environment(),
                         args.same_disk)
    service._check_systemd(app)
    base = service.unit_dir()
    for name in (UNIT, TIMER):
        old = base / name
        if old.is_file() and not old.read_text(encoding="utf-8").startswith(HEADER):
            raise FleetError(f"{paths.tilde(old)} exists and ship-fleet did not write it; not "
                             "replacing it.")
    base.mkdir(parents=True, exist_ok=True)
    paths.write_private(base / UNIT, unit.encode("utf-8"))
    paths.write_private(base / TIMER, timer.encode("utf-8"))
    for step in (["daemon-reload"], ["enable", "--now", TIMER]):
        code, _out, err = service._systemctl(app, *step)
        if code != 0:
            raise FleetError(f"systemctl --user {' '.join(step)} failed: "
                             f"{service._last(err) or '?'}.")
    app.say(f"A backup every day at {args.at} (or at the next boot, if one was missed) into "
            f"{folder}, keeping the newest {args.keep}. Each is sealed under the vault: it runs "
            "only while the vault is unlocked, and says so in the journal when it cannot "
            "(journalctl --user -u fleetledger-backup).")
    app.say("Keep that folder off this computer's own disk (a NAS mount), or pull the files "
            "to another computer: a backup on the same disk goes with it.")
    return 0


def cmd_unschedule(args: Any, app: Any) -> int:
    from fleetledger import service

    base = service.unit_dir()
    if not (base / TIMER).is_file():
        app.say("No backup is scheduled here.")
        return 0
    service._systemctl(app, "disable", "--now", TIMER)
    for name in (TIMER, UNIT):
        (base / name).unlink(missing_ok=True)
    service._systemctl(app, "daemon-reload")
    app.say("No more scheduled backups. The backups already made stay where they are.")
    return 0


def add_parsers(sub, add) -> None:
    p = add(sub, "backup", "encrypted backups of the whole fleet, sealed under the vault",
            "Back the whole fleet up (what ship-fleet migrate would move: inventory,\n"
            "keys, secrets, history), sealed under FleetLedger's vault: it opens with the\n"
            "vault's passphrase or recovery code only. Restore: ship-fleet migrate import\n"
            "FILE. See docs/runbooks/fleetledger-vm.md.\n\n"
            "  ship-fleet backup now --to /mnt/backups/fleetledger\n"
            "  ship-fleet backup schedule --to /mnt/backups/fleetledger --at 03:30\n"
            "  ship-fleet backup unschedule")
    verbs = p.add_subparsers(dest="backup_command", metavar="<action>", title="actions")
    now = verbs.add_parser("now", help="one backup, now")
    now.add_argument("--to", required=True, metavar="DIR", help="the folder to write it in")
    now.add_argument("--keep", type=int, default=14, help="keep the newest N there (default 14)")
    now.add_argument("--same-disk", action="store_true",
                     help="allow a folder on this computer's own disk (it goes with the disk)")
    sc = verbs.add_parser("schedule", help="a daily backup (a systemd user timer)")
    sc.add_argument("--to", required=True, metavar="DIR", help="the folder to write them in")
    sc.add_argument("--at", default="03:30", metavar="HH:MM", help="when (default 03:30)")
    sc.add_argument("--keep", type=int, default=14, help="keep the newest N (default 14)")
    sc.add_argument("--ship-fleet", metavar="PATH", help="the ship-fleet command to run")
    sc.add_argument("--same-disk", action="store_true",
                    help="allow a folder on this computer's own disk (it goes with the disk)")
    verbs.add_parser("unschedule", help="no more scheduled backups")


def cmd_backup(args: Any, app: Any) -> int:
    verb = getattr(args, "backup_command", None)
    if verb is None:
        raise FleetError("ship-fleet backup now | schedule | unschedule (see --help).")
    if verb == "now" and not 1 <= args.keep <= 365:
        raise FleetError("--keep: between 1 and 365.")
    return {"now": cmd_now, "schedule": cmd_schedule, "unschedule": cmd_unschedule}[verb](args,
                                                                                        app)
