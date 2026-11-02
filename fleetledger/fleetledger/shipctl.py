"""Call ship-ctl on a Ship over SSH (contracts/ship-ctl.md).

Every call is `sudo -n /usr/local/bin/ship-ctl <command> ... --json`; the one
JSON document it prints is checked against ship-ctl-result.schema.json. Before
changing anything FleetLedger checks the Ship speaks contract versions it knows.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from fleetledger import schemas
from fleetledger.errors import FleetError
from fleetledger.inventory import Ship
from fleetledger.ssh import SSH, Result, ship_ctl

RESULT_SCHEMA = "shipshape.ship-ctl-result/1"
STATUS_SCHEMA = "shipshape.ship-status/1"

# Contract majors this FleetLedger understands (ship-ctl version --json).
KNOWN_CONTRACTS = {
    "ship-status": 1,
    "ship-toml": 1,
    "ship-ctl-result": 1,
    "bundle": 1,
    "seed": 1,
    "runtime": 1,
}


class ShipCtlError(FleetError):
    """ship-ctl ran and reported a failure, or did not answer as the contract says."""

    def __init__(self, message: str, *, code: str | None = None, hint: str | None = None):
        super().__init__(message, hint=hint)
        self.code = code


@dataclass(frozen=True)
class CtlResult:
    command: str
    ok: bool
    changed: list[str]
    messages: list[str]
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None


def not_installed(result: Result) -> bool:
    """sudo could not find ship-ctl: the Ship is still on its first boot."""
    err = result.stderr.lower()
    return result.returncode in (1, 127) and not result.stdout.strip() and (
        "command not found" in err or "no such file" in err
    )


def parse_result(ship: Ship, result: Result, command: str) -> CtlResult:
    try:
        doc = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise ShipCtlError(
            f"{ship.name}: ship-ctl {command} did not print JSON (exit {result.returncode})."
        ) from exc
    if not isinstance(doc, dict) or doc.get("schema") != RESULT_SCHEMA:
        raise ShipCtlError(f"{ship.name}: ship-ctl {command} printed an unknown document.")
    problems = schemas.errors("ship-ctl-result", doc)
    if problems:
        raise ShipCtlError(
            f"{ship.name}: ship-ctl {command} result breaks the contract: {problems[0]}"
        )
    return CtlResult(
        command=doc["command"],
        ok=doc["ok"],
        changed=doc["changed"],
        messages=doc["messages"],
        data=doc.get("data", {}),
        error=doc["error"],
    )


class ShipCtl:
    def __init__(self, ssh: SSH, ship: Ship) -> None:
        self.ssh = ssh
        self.ship = ship
        self._checked = False

    def call(
        self,
        command: str,
        *args: str,
        input: bytes | None = None,
        timeout: float = 120,
        check_version: bool = True,
    ) -> CtlResult:
        if check_version:
            self.ensure_compatible()
        result = self.ssh.run(
            self.ship, ship_ctl(command, *args, "--json"), input=input, timeout=timeout
        )
        if not_installed(result):
            raise ShipCtlError(
                f"{self.ship.name}: ship-ctl is not installed yet (still provisioning?).",
                code="not-installed",
            )
        parsed = parse_result(self.ship, result, command)
        if not parsed.ok:
            err = parsed.error or {"code": "internal", "message": "unknown error"}
            raise ShipCtlError(
                f"{self.ship.name}: ship-ctl {command} failed ({err['code']}): {err['message']}",
                code=err["code"],
            )
        return parsed

    def ensure_compatible(self) -> dict[str, Any]:
        """Refuse a Ship whose contract versions this FleetLedger does not know."""
        if self._checked:
            return {}
        result = self.call("version", check_version=False, timeout=30)
        contracts = result.data.get("contracts", {})
        unknown = {
            name: contracts.get(name)
            for name, major in KNOWN_CONTRACTS.items()
            if contracts.get(name) != major
        }
        if unknown:
            listed = ", ".join(f"{k}={v}" for k, v in sorted(unknown.items()))
            raise ShipCtlError(
                f"{self.ship.name} speaks contract versions this FleetLedger does not know "
                f"({listed}).",
                code="incompatible",
                hint="Update FleetLedger (git pull, then fleetledger/install.sh).",
            )
        self._checked = True
        return result.data

    # --- commands FleetLedger uses ---------------------------------------
    def set(self, assignments: Sequence[str]) -> CtlResult:
        return self.call("set", *assignments)

    def set_file(self, ship_toml: str) -> CtlResult:
        return self.call("set", "--file", "-", input=ship_toml.encode("utf-8"))

    def logs(self, source: str, lines: int) -> list[str]:
        return self.call("logs", source, "--lines", str(lines)).data.get("lines", [])

    def tools(self, action: str, ids: Sequence[str] = ()) -> CtlResult:
        return self.call("tools", action, *ids, timeout=900)

    def converge(self, bundle_path: str) -> CtlResult:
        return self.call("converge", "--bundle", bundle_path, timeout=1800)
