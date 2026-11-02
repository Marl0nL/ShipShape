"""Errors FleetLedger shows to people.

Every message must be safe to print: no tokens, codes or private keys.
"""

from __future__ import annotations


class FleetError(Exception):
    """A failure with a message meant for the person running ship-fleet."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class ConfigError(FleetError):
    """Local configuration (ships.toml, keys, repo checkout) is missing or invalid."""

    exit_code = 2


class NotFound(FleetError):
    """A Ship, tag, tool or profile that does not exist."""

    exit_code = 2
