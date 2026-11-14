"""`ship-ctl apply`: turn ship.toml into real configuration (contracts/settings.md).

Writes only what changed and returns the paths it wrote. Files in the agent's home
are merged key by key: herdr's integration, Claude Code and the agents edit them
too, so ship-ctl touches only the keys it owns.

It also records every running agent in the agent record (record.py) first. A
converge runs the new release's apply, so a Ship updated from tooling where herdr
resumed its agents has them recorded before herdr's own resume is turned off here.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import languard, overlay, record, shiptoml, sshtrust, tomlw, wake
from .context import Context
from .errors import CtlError
from .fsutil import remove, write_atomic
from .homefs import UnsafePath
from .naming import RC_OFF
from .paths import Paths
from .shiptoml import ShipToml

LEDGER_EVENTS = ("UserPromptSubmit", "SessionStart", "SessionEnd")
SUDOERS_LINE = "agent ALL=(ALL:ALL) NOPASSWD: ALL\n"
HERDR_KEYS = {
    ("", "onboarding"): False,
    ("update", "version_check"): False,
    # ship-ctl resumes agents from its own record (record.py); herdr restores only
    # its layout, so the two never start one agent twice.
    ("session", "resume_agents_on_restore"): False,
}


HERDR_CONFIG = ".config/herdr/config.toml"  # in the agent's home


def herdr_config(text: bytes | None) -> str:
    """herdr's config with ShipShape's keys set (HERDR_KEYS), the rest kept."""
    try:
        return tomlw.set_keys(text.decode("utf-8") if text else "", HERDR_KEYS)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CtlError("internal", f"cannot update ~/{HERDR_CONFIG}: {exc}") from exc


def herdr_resume_off(ctx: Context) -> bool:
    """Make sure herdr's own resume is off (`session.resume_agents_on_restore =
    false`), as apply does; True if the file had to change."""
    return ctx.home.write(HERDR_CONFIG, herdr_config(ctx.home.read(HERDR_CONFIG)), 0o644,
                          dir_mode=0o755, keep_mode=True)


def managed_settings(ship: ShipToml, versions: dict) -> dict:
    c = ship.claude
    # "on": Remote Control only for the agents the Ship starts, which get
    # `--remote-control` at launch (naming.remote_control_args); every other session
    # (one a person or an agent starts) has it off, and a managed false keeps a user
    # setting from turning it on. "all": every session, as "on" used to be.
    # "off" must also set disableRemoteControl: a resumed session reattaches to the
    # Remote Control session saved in its transcript whatever remoteControlAtStartup
    # says (phase 0). "blocked" is an older name for the same thing.
    rc = c["remote_control"]
    doc: dict = {"remoteControlAtStartup": rc == "all"}
    if rc in RC_OFF:
        doc["disableRemoteControl"] = True
    permissions: dict = {"defaultMode": c["default_mode"]}
    if not c["allow_bypass"]:
        permissions["disableBypassPermissionsMode"] = "disable"
    doc["permissions"] = permissions
    doc["autoUpdatesChannel"] = c["update_channel"]
    minimum = versions.get("claude", {}).get("minimum_version")
    if minimum:
        doc["minimumVersion"] = minimum
    # Only a fallback: the prefix names a session until its first message. Sessions
    # the Ship starts get a lasting name from `claude --name` (naming.py).
    doc["env"] = {"CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX": c["session_prefix"]}
    return doc


UNATTENDED_CONF = "/etc/apt/apt.conf.d/52shipshape-unattended-upgrades"


def unattended_upgrades(ship: ShipToml) -> str:
    """The automatic reboot policy (contracts/settings.md, "OS updates")."""
    policy = ship.updates
    reboot = "true" if policy["auto_reboot"] else "false"
    return (
        "// Written by ship-ctl apply from /etc/shipshape/ship.toml [updates].\n"
        f'Unattended-Upgrade::Automatic-Reboot "{reboot}";\n'
        'Unattended-Upgrade::Automatic-Reboot-WithUsers "true";\n'
        f'Unattended-Upgrade::Automatic-Reboot-Time "{policy["reboot_time"]}";\n'
    )


UTC_NAMES = frozenset({"UTC", "Etc/UTC"})


def sync_timezone(ctx: Context, ship: ShipToml, dry_run: bool = False
                  ) -> tuple[list[str], list[str]]:
    """Set the system time zone to ship.timezone (timedatectl). Returns (changed, messages).

    A zone tzdata does not have is left alone with a message, so one bad value
    never stops the rest of apply (`ship-ctl set` refuses it before saving).
    """
    want = ship.timezone
    r = ctx.runner.run(["timedatectl", "show", "--property=Timezone", "--value"], timeout=15)
    if not r.ok:
        detail = (r.err.strip().splitlines() or [f"exit {r.rc}"])[-1][:200]
        return [], [f"time zone: cannot read it ({detail}); left as it is"]
    have = r.out.strip()
    if have == want or {have, want} <= UTC_NAMES:
        return [], []
    if not shiptoml.zone_installed(ctx.paths, want):
        return [], [f"time zone {want} is not installed on this Ship; left at {have}"]
    if not dry_run:
        r = ctx.runner.run(["timedatectl", "set-timezone", want], timeout=15)
        if not r.ok:
            detail = (r.err.strip().splitlines() or [f"exit {r.rc}"])[-1][:200]
            raise CtlError("internal", f"time zone: setting {want} failed: {detail}")
    return [f"time zone {want} (was {have or 'unset'})"], []


def avahi_service(ship: ShipToml) -> str:
    """The LAN announcement (contracts/ship-runtime.md): a hint for `ship-fleet scan`."""
    return (
        '<?xml version="1.0" standalone="no"?>\n'
        '<!DOCTYPE service-group SYSTEM "avahi-service.dtd">\n'
        "<!-- Written by ship-ctl apply from /etc/shipshape/ship.toml. -->\n"
        "<service-group>\n"
        f"  <name>{escape(ship.name)}</name>\n"
        "  <service>\n"
        "    <type>_shipshape._tcp</type>\n"
        "    <port>22</port>\n"
        "    <txt-record>schema=1</txt-record>\n"
        f"    <txt-record>name={escape(ship.name)}</txt-record>\n"
        f"    <txt-record>id={escape(ship.id)}</txt-record>\n"
        "  </service>\n"
        "</service-group>\n"
    )


def merge_user_settings(existing: dict, allow_bypass: bool, hook_command: str) -> dict:
    """~/.claude/settings.json: skipDangerousModePermissionPrompt and the ledger hooks."""
    doc = copy.deepcopy(existing)
    if allow_bypass:
        doc["skipDangerousModePermissionPrompt"] = True
    else:
        doc.pop("skipDangerousModePermissionPrompt", None)
    hooks = doc.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = doc["hooks"] = {}
    for event in LEDGER_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            entries = hooks[event] = []
        present = any(
            isinstance(entry, dict)
            and any(
                isinstance(h, dict) and h.get("command") == hook_command
                for h in entry.get("hooks", [])
            )
            for entry in entries
        )
        if not present:
            entries.append({"hooks": [{"type": "command", "command": hook_command}]})
    return doc


def merge_claude_json(existing: dict, trusted: list[str]) -> dict:
    """~/.claude.json: onboarding done, and trust for every autostart folder."""
    doc = copy.deepcopy(existing)
    doc["hasCompletedOnboarding"] = True
    projects = doc.setdefault("projects", {})
    if not isinstance(projects, dict):
        projects = doc["projects"] = {}
    for cwd in trusted:
        project = projects.setdefault(cwd, {})
        if isinstance(project, dict):
            project["hasTrustDialogAccepted"] = True
    return doc


def expand_cwd(cwd: str, agent_home: str) -> str:
    return agent_home + cwd[1:] if cwd.startswith("~/") else cwd


def _json_obj(raw: bytes | None, label: str) -> dict:
    if raw is None:
        return {}
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise CtlError("internal", f"{label} is not valid JSON; not touching it") from exc
    if not isinstance(doc, dict):
        raise CtlError("internal", f"{label} is not a JSON object; not touching it")
    return doc


def _check_sudoers(ctx: Context, text: str) -> None:
    if ctx.paths.sandbox:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as fh:
        fh.write(text)
    try:
        result = ctx.runner.run(["visudo", "-c", "-q", "-f", fh.name], timeout=10)
    finally:
        os.unlink(fh.name)
    if not result.ok:
        raise CtlError("internal", "visudo rejected the agent sudoers rule")


RESERVED_USERS = ("root", "fleet", "agent", "nobody")
OWNER_SUDOERS = "/etc/sudoers.d/90-shipshape-owner"


def _passwd_uid(ctx: Context, name: str) -> int | None:
    try:
        lines = ctx.paths.p("/etc/passwd").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split(":")
        if len(fields) > 2 and fields[0] == name:
            try:
                return int(fields[2])
            except ValueError:
                return None
    return None


def sync_owner(ctx: Context, ship: ShipToml, dry_run: bool = False) -> tuple[list[Path], list[str]]:
    """The owner's login ([owner]): made once with useradd, and given sudo. Without an
    [owner], its sudo rule goes (the user itself is left, with its files)."""
    sudoers = ctx.paths.p(OWNER_SUDOERS)
    owner = ship.owner
    if owner is None:
        return ([sudoers] if remove(sudoers, dry_run=dry_run) else []), []
    name = owner["name"]
    if name in RESERVED_USERS:
        raise CtlError("invalid-ship-toml", f"owner.name: {name!r} is a ShipShape login")
    messages = []
    uid = _passwd_uid(ctx, name)
    if uid is not None and (uid < 1000 or uid >= 60000):  # system accounts, nobody
        raise CtlError("invalid-ship-toml", f"owner.name: {name!r} is a system account")
    if uid is None and not dry_run:
        r = ctx.runner.run(["useradd", "--create-home", "--shell", "/bin/bash", "--comment",
                            "ShipShape owner", name], timeout=30)
        if not r.ok:
            raise CtlError("internal", f"could not create the owner login {name}")
        messages.append(f"Created the owner login {name} (sudo; your keys and certificates).")
    line = f"{name} ALL=(ALL:ALL) NOPASSWD: ALL\n"
    _check_sudoers(ctx, line)
    return ([sudoers] if write_atomic(sudoers, line, 0o440, dry_run=dry_run) else []), messages


def apply(ctx: Context, ship: ShipToml, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """Returns (changed paths, messages)."""
    paths: Paths = ctx.paths
    changed: list[str] = []
    messages: list[str] = []
    if not dry_run:
        messages += record.capture(ctx, ship)

    def note(path: Path, did: bool) -> None:
        if did:
            changed.append(paths.agent_path(path))

    # Root-owned: Claude Code managed settings.
    managed = json.dumps(managed_settings(ship, ctx.versions), indent=2) + "\n"
    note(
        paths.managed_settings,
        write_atomic(paths.managed_settings, managed, 0o644, dry_run=dry_run),
    )

    # Root-owned: passwordless sudo for agent, only when switched on.
    if ship.agent_sudo:
        _check_sudoers(ctx, SUDOERS_LINE)
        note(
            paths.sudoers_agent,
            write_atomic(paths.sudoers_agent, SUDOERS_LINE, 0o440, dry_run=dry_run),
        )
    else:
        note(paths.sudoers_agent, remove(paths.sudoers_agent, dry_run=dry_run))

    # The owner's login: a user with sudo (its keys and certificates: sshtrust below).
    owner_changed, owner_messages = sync_owner(ctx, ship, dry_run=dry_run)
    for path in owner_changed:
        note(path, True)
    messages += owner_messages

    # Root-owned: the LAN announcement. avahi reloads its services folder by itself.
    avahi = paths.p("/etc/avahi/services/shipshape.service")
    note(avahi, write_atomic(avahi, avahi_service(ship), 0o644, dry_run=dry_run))

    # Root-owned: who may log in with a certificate, and sshd's certificate settings.
    ssh_changed, ssh_messages = sshtrust.apply_files(ctx, ship, dry_run=dry_run)
    for path in ssh_changed:
        note(path, True)
    messages += ssh_messages

    # The system time zone, which updates.reboot_time is read in.
    tz_changed, tz_messages = sync_timezone(ctx, ship, dry_run=dry_run)
    changed += tz_changed
    messages += tz_messages

    # Root-owned: the unattended-upgrades reboot policy, written in full.
    upgrades = paths.p(UNATTENDED_CONF)
    note(upgrades, write_atomic(upgrades, unattended_upgrades(ship), 0o644, dry_run=dry_run))

    # The LAN guard: one nftables table, plus the unit that reloads it at boot.
    guard_changed, guard_messages = languard.sync(ctx, ship, dry_run=dry_run)
    changed += guard_changed
    messages += guard_messages

    # The boot timer that wakes the primary agent ([wake]).
    wake_changed, wake_messages = wake.sync(ctx, ship, dry_run=dry_run)
    changed += wake_changed
    messages += wake_messages

    # Root-owned: where sshd accepts logins (network.ssh_listen, phase 5).
    did, notes = overlay.apply_listen(ctx, ship, dry_run=dry_run)
    note(overlay.listen_conf_path(ctx), did)
    messages += notes

    # Agent-owned files: merge only our keys. The agent controls this folder, so
    # everything goes through HomeFS, which never follows a symlink (homefs.py).
    home = paths.agent_home
    if not home.is_dir():
        messages.append(f"{paths.agent_path(home)} does not exist; agent files skipped.")
        return changed, messages
    fs = ctx.home
    agent_home = paths.agent_path(home)
    trusted = [expand_cwd(a["cwd"], agent_home) for a in ship.autostart]

    def settings(text: bytes | None) -> str:
        doc = merge_user_settings(
            _json_obj(text, "~/.claude/settings.json"),
            ship.claude["allow_bypass"],
            Paths.LEDGER_HOOK,
        )
        return json.dumps(doc, indent=2) + "\n"

    def claude_json(text: bytes | None) -> str:
        doc = merge_claude_json(_json_obj(text, "~/.claude.json"), trusted)
        return json.dumps(doc, indent=2) + "\n"

    for rel, render, mode, dir_mode in (
        (".claude/settings.json", settings, 0o644, 0o700),
        (".claude.json", claude_json, 0o600, 0o700),
        (HERDR_CONFIG, herdr_config, 0o644, 0o755),
    ):
        try:
            text = render(fs.read(rel))
            did = fs.write(rel, text, mode, dry_run=dry_run, dir_mode=dir_mode, keep_mode=True)
        except UnsafePath as exc:
            messages.append(str(exc))
            continue
        note(home / rel, did)

    return changed, messages
