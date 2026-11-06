"""Which sign-ins a Ship uses (ships.toml ship.<name>.signins; absent: all of them).

A Ship may keep every tool installed and still not use some of the accounts: a
Ship that never touches Google Cloud has no business holding a gcloud login. A
sign-in that is off gets no prompt, no flow and no attention item from FleetLedger,
is shown as off, and is signed out on the Ship when it is turned off, so no
credentials for it stay there.
"""

from __future__ import annotations

from collections.abc import Iterable

from fleetledger.errors import FleetError
from fleetledger.inventory import Ship

# What a Ship may sign in to, as people name them (the concierge's providers).
CHOICES = ("claude", "gcloud", "github")
LABELS = {"claude": "Claude", "gcloud": "Google Cloud", "github": "GitHub"}
# The status document's auth keys each one covers (gcloud also covers its ADC).
STATUS_KEYS = {"claude": ("claude",), "gcloud": ("gcloud", "gcloud_adc"),
               "github": ("github",)}
# Concierge provider ids, for each choice.
PROVIDERS = {"claude": ("claude",), "gcloud": ("gcloud", "gcloud-adc"),
             "github": ("github",)}
SIGNIN_CODES = ("signin-missing", "signin-expiring", "signin-expired", "signin-wrong-account",
                "signin-scopes-differ")


def used(ship: Ship) -> tuple[str, ...]:
    return CHOICES if ship.signins is None else tuple(c for c in CHOICES if c in ship.signins)


def off(ship: Ship) -> tuple[str, ...]:
    return tuple(c for c in CHOICES if c not in used(ship))


def off_status_keys(ship: Ship) -> set[str]:
    return {key for choice in off(ship) for key in STATUS_KEYS[choice]}


def choice_of_provider(provider_id: str) -> str | None:
    return next((c for c, ids in PROVIDERS.items() if provider_id in ids), None)


def provider_on(ship: Ship, provider_id: str) -> bool:
    choice = choice_of_provider(provider_id)
    return choice is None or choice in used(ship)


def check(values: Iterable[str]) -> list[str]:
    """A list of choices, checked, in the usual order."""
    chosen = [v.strip() for v in values if v.strip()]
    unknown = [v for v in chosen if v not in CHOICES]
    if unknown:
        raise FleetError(f"Unknown sign-in {', '.join(unknown)} (choose from "
                         f"{', '.join(CHOICES)}).")
    return [c for c in CHOICES if c in chosen]


def change(ship: Ship, changes: Iterable[str]) -> list[str]:
    """New list from +name / -name changes (ship-fleet signins --on / --off), or a whole
    list (claude,github)."""
    current = list(used(ship))
    items = [c for c in changes if c]
    if items and all(c[0] in "+-" for c in items):
        for c in items:
            name = check([c[1:]])[0]
            if c[0] == "+" and name not in current:
                current.append(name)
            elif c[0] == "-" and name in current:
                current.remove(name)
        return check(current)
    return check(",".join(items).split(","))


def filter_attention(ship: Ship, items: list[dict], tools: dict | None) -> list[dict]:
    """Drop what is only about sign-ins this Ship does not use."""
    gone = off_status_keys(ship)
    if not gone:
        return items
    kept = []
    for item in items:
        if item.get("code") in SIGNIN_CODES and item.get("subject") in gone:
            continue
        if item.get("code") == "tool-waiting":
            waiting = set(((tools or {}).get(item.get("subject")) or {}).get("waiting_for", []))
            if waiting and waiting <= gone:
                continue
        kept.append(item)
    return kept
