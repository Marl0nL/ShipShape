"""The one exception ship-ctl commands raise to fail with a contract error code."""

from __future__ import annotations

# error.code -> exit code (contracts/ship-ctl.md)
EXIT_CODES = {
    "usage": 2,
    "not-root": 3,
    "invalid-ship-toml": 4,
    "invalid-bundle": 1,
    "bundle-checksum": 1,
    "provision-failed": 1,
    "tool-failed": 1,
    "herdr-unavailable": 1,
    "internal": 1,
}


class CtlError(Exception):
    def __init__(self, code: str, message: str, exit_code: int | None = None):
        super().__init__(message)
        assert code in EXIT_CODES, code
        self.code = code
        self.message = message[:1000]
        self.exit_code = EXIT_CODES[code] if exit_code is None else exit_code
        self.data: dict | None = None
        self.changed: list[str] = []
        self.messages: list[str] = []

    def with_data(
        self, data: dict, changed: list[str] | None = None, messages: list[str] | None = None
    ) -> CtlError:
        """Attach the command's partial result, reported alongside the error."""
        self.data = data
        self.changed = list(changed or [])
        self.messages = list(messages or [])
        return self
