"""Extra GitHub scopes a Ship's GitHub sign-in may ask for.

gh signs in with its own minimum (repo, read:org, gist). Some agent work needs more:
pushing a change to `.github/workflows/` is refused without `workflow`. Each Ship
chooses from a short catalogue (ships.toml ship.<name>.github_scopes, mirrored in its
ship.toml github.scopes), and FleetLedger's GitHub sign-in asks for exactly those:
`gh auth login --scopes ...` on a Ship not signed in, `gh auth refresh` on one that
is (the Ship's status lists the scopes its token has). Changing the choice never
changes the token by itself: the Ship reports `signin-scopes-differ` until you sign
in again with the chosen scopes.

Left out on purpose: scopes that delete or administer (delete_repo, admin:org,
admin:repo_hook, ...). An agent that holds them can do lasting damage, and that is
not worth a tickbox; a person who needs one adds it by hand on the Ship.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fleetledger.errors import FleetError

# gh's own minimum: every token has these, and gh never removes them.
MINIMUM = ("repo", "read:org", "gist")


@dataclass(frozen=True)
class Scope:
    id: str
    label: str
    explain: str  # one line: what it allows, and why you might want it


CATALOGUE = (
    Scope("workflow", "Workflows",
          "Change GitHub Actions workflows: agents can push commits that touch "
          ".github/workflows/ (GitHub refuses those pushes without it). Workflows run "
          "with the repository's secrets, so tick it only where agents maintain CI."),
    Scope("project", "Projects",
          "Read and change GitHub Projects (the boards): gh project, and agents that "
          "move their own cards."),
    Scope("write:packages", "Packages",
          "Publish and read packages in GitHub Packages, such as container images on "
          "ghcr.io, for agents that build and push images."),
)
IDS = tuple(s.id for s in CATALOGUE)
BY_ID = {s.id: s for s in CATALOGUE}


def check(values: Iterable[str]) -> list[str]:
    """Chosen scopes, checked, in catalogue order (an empty value or "none": none)."""
    chosen = [v.strip() for v in values if v.strip() and v.strip() != "none"]
    unknown = [v for v in chosen if v not in BY_ID]
    if unknown:
        raise FleetError(f"Unknown GitHub scope {', '.join(unknown)} (choose from "
                         f"{', '.join(IDS)}, or none).")
    return [s for s in IDS if s in chosen]


def parse(text: str) -> list[str]:
    """A comma-separated list, as `--scopes` and `github_scopes=` take it."""
    return check(text.split(","))


def reported(doc: Mapping[str, Any] | None) -> list[str] | None:
    """The scopes the Ship's own ship.toml chose (its status settings), or None if its
    tooling does not report them."""
    github = ((doc or {}).get("settings") or {}).get("github")
    return list(github["scopes"]) if isinstance(github, dict) else None


def chosen(ship: Any, doc: Mapping[str, Any] | None = None) -> list[str]:
    """What this Ship's GitHub sign-in asks for: what the Ship reports, else the
    inventory's copy (a Ship not answering, or with older tooling)."""
    found = reported(doc)
    return check(found if found is not None else getattr(ship, "github_scopes", ()) or ())


def granted(doc: Mapping[str, Any] | None) -> list[str] | None:
    """The scopes the Ship's GitHub token has, or None: not signed in, or not known."""
    entry = ((doc or {}).get("auth") or {}).get("github") or {}
    if entry.get("state") not in ("ok", "expiring"):
        return None
    scopes = entry.get("scopes")
    return list(scopes) if isinstance(scopes, list) else None


@dataclass(frozen=True)
class Difference:
    missing: tuple[str, ...]  # chosen, not on the token
    extra: tuple[str, ...]  # on the token, from the catalogue, not chosen

    def __bool__(self) -> bool:
        return bool(self.missing or self.extra)

    def describe(self) -> str:
        parts = []
        if self.missing:
            parts.append(f"lacks {', '.join(self.missing)}")
        if self.extra:
            parts.append(f"still has {', '.join(self.extra)}")
        return " and ".join(parts)


def difference(wanted: Sequence[str], token: Sequence[str] | None) -> Difference:
    """How the token's scopes differ from the choice. Only catalogue scopes count: gh's
    minimum, and scopes given by hand outside the catalogue, are left alone."""
    if token is None:
        return Difference((), ())
    have = set(token)
    return Difference(tuple(s for s in IDS if s in wanted and s not in have),
                      tuple(s for s in IDS if s in have and s not in wanted))


def extras(token: Sequence[str] | None) -> list[str]:
    """The token's scopes beyond gh's minimum, for display."""
    return sorted(s for s in (token or ()) if s not in MINIMUM)


def describe(scopes: Sequence[str]) -> str:
    return ", ".join(scopes) if scopes else "none (gh's minimum)"


@dataclass(frozen=True)
class Summary:
    """One Ship's GitHub scopes, as its pages show them."""

    chosen: tuple[str, ...]
    token: tuple[str, ...] | None  # None: not signed in, or not known
    differ: Difference
    reported: bool  # the Ship reports its choice (else the inventory's copy is shown)

    @property
    def token_extras(self) -> list[str]:
        return extras(self.token)


def summary(ship: Any, doc: Mapping[str, Any] | None) -> Summary:
    wanted = chosen(ship, doc)
    token = granted(doc)
    return Summary(tuple(wanted), None if token is None else tuple(token),
                   difference(wanted, token), reported(doc) is not None)
