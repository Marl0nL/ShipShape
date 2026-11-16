"""Tag accounts: which account each tag's Ships sign in to each service as.

ships.toml `[tag.<tag>]` maps a sign-in id to an account name (an email address or a
login); after every sign-in and on every status poll the signed-in account is compared
with it (Inventory.expected_account). `ship-fleet signins --accounts` lists them, and
`ship-fleet signins --tag TAG --set-account SIGNIN=ACCOUNT --clear-account SIGNIN`
changes them. The JSON listing is `contracts/fleetledger-tag-accounts.schema.json`.

They stay in ships.toml (contracts/settings.md): nothing here reaches a Ship, and only
account names are kept, never a credential.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from fleetledger import signinreg
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory

SCHEMA = "shipshape.fleetledger-tag-accounts/1"
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")  # contracts: $defs.tag
MAX_ACCOUNT = 254  # the longest email address
# What a credential looks like where an account name belongs: GitHub, Anthropic and
# Google tokens. An account name has no spaces either.
TOKEN_SHAPES = re.compile(r"^(gh[pousr]_|github_pat_|sk-ant-|ya29\.)", re.I)


def conflicts(inv: Inventory, tag: str | None = None) -> list[dict[str, Any]]:
    """Ships whose tags expect different accounts for one sign-in: that sign-in shows
    as the wrong account whichever it is signed in as."""
    found = []
    for ship in inv.ships(tag):
        for signin in signinreg.registry().choices:
            expected = inv.expected_account(ship, signin)
            if expected.conflicting:
                found.append({"ship": ship.name, "signin": signin,
                              "accounts": sorted(expected.accounts)})
    return found


def document(inv: Inventory, tag: str | None = None) -> dict[str, Any]:
    """The listing (contracts/fleetledger-tag-accounts.schema.json): every tag that names
    accounts or that a Ship has (defaults.tags counting); tag: that one only."""
    listed = [t for t in inv.all_tags() if tag is None or t == tag]
    return {
        "schema": SCHEMA,
        "signins": list(signinreg.registry().choices),
        "tags": [{"tag": t, "accounts": dict(sorted(inv.tag_accounts.get(t, {}).items())),
                  "ships": [s.name for s in inv.ships(t)]} for t in listed],
        "conflicts": conflicts(inv, tag),
    }


def check_tag(tag: str) -> str:
    if not TAG_RE.match(tag):
        raise FleetError(f"'{tag}' is not a tag: a-z, 0-9 and -, up to 31 characters, "
                         "starting with a letter or digit.")
    return tag


def check_signin(signin: str) -> str:
    reg = signinreg.registry()
    if reg.unknown([signin]):
        raise FleetError(f"Unknown sign-in '{signin}' (choose from {', '.join(reg.choices)}).")
    return signin


def check_account(signin: str, account: str) -> str:
    """An account name (an email address or a login), never a credential."""
    if not account:
        raise FleetError(f"No account given for {signin}; to drop it, use "
                         f"--clear-account {signin}.")
    if len(account) > MAX_ACCOUNT or any(c.isspace() or not c.isprintable() for c in account):
        raise FleetError(f"'{account[:40]}' is not an account name for {signin}: an email "
                         f"address or a login, with no spaces, up to {MAX_ACCOUNT} "
                         "characters.")
    if TOKEN_SHAPES.match(account):
        raise FleetError(f"That looks like a token, not an account name, so it was not "
                         f"saved. Give the account {signin} signs in as (an email address "
                         "or a login); FleetLedger keeps no credential in ships.toml.")
    return account


def parse_sets(values: Sequence[str]) -> dict[str, str]:
    """--set-account SIGNIN=ACCOUNT ... -> {signin: account}, each one checked."""
    out: dict[str, str] = {}
    for value in values:
        signin, eq, account = value.partition("=")
        if not eq:
            raise FleetError(f"--set-account takes SIGNIN=ACCOUNT, e.g. "
                             f"claude=you@example.com (got '{value}').")
        signin = check_signin(signin.strip())
        out[signin] = check_account(signin, account.strip())
    return out


def change(store, tag: str, sets: dict[str, str], clears: Sequence[str]) -> dict[str, str]:
    """Set and clear one tag's accounts in ships.toml (validated, locked, written as
    every other change). Returns the tag's accounts afterwards."""
    check_tag(tag)
    clears = [check_signin(c) for c in clears]
    both = sorted(set(sets) & set(clears))
    if both:
        raise FleetError(f"Both set and cleared: {', '.join(both)}.")
    with store.edit() as ed:
        for signin, account in sets.items():
            ed.set_tag_account(tag, signin, account)
        for signin in clears:
            ed.set_tag_account(tag, signin, None)
    return dict(store.load().tag_accounts.get(tag, {}))
