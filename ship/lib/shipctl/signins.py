"""The Ship's sign-in registry: every sign-in the Ship checks and can be told about.

One table for the status document's auth keys (`ship-ctl status`) and the
services `ship-ctl notify-signin` takes (FleetLedger's provider ids). FleetLedger
keeps its own registry, from its provider files (fleetledger/signinreg.py); the
two name the same ids.

Each status key's check is a built-in probe in status.py (`probe`, by name), and
what the Ship does when one turns ok stays in code, keyed by id (cli.py: claude
starts autostart, github clones waiting repos).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SignIn:
    """One status auth key."""

    key: str
    label: str  # attention items and tool-waiting lines
    probe: str  # the built-in check in status.py (BUILTIN_PROBES)
    core: bool = False  # missing is an attention item of its own


@dataclass(frozen=True)
class Notice:
    """One service `ship-ctl notify-signin` takes (a FleetLedger provider id)."""

    service: str
    key: str  # the status auth key it reads the account from
    label: str  # as the notice line names it


SIGNINS = (
    SignIn("claude", "Claude Code", probe="claude", core=True),
    SignIn("gcloud", "gcloud", probe="gcloud"),
    SignIn("gcloud_adc", "Application Default Credentials", probe="adc"),
    SignIn("github", "GitHub", probe="github"),
)
NOTICES = (
    Notice("claude", "claude", "Claude Code"),
    Notice("gcloud", "gcloud", "gcloud (with Application Default Credentials)"),
    Notice("gcloud-adc", "gcloud_adc", "Google Application Default Credentials"),
    Notice("github", "github", "GitHub"),
)

KEYS = tuple(s.key for s in SIGNINS)
LABELS = {s.key: s.label for s in SIGNINS}
CORE = frozenset(s.key for s in SIGNINS if s.core)
NOTIFY = {n.service: n for n in NOTICES}
