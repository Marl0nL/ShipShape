"""Persistent JSON stores for control-plane state.

Lives under control-plane/state/, which the agent-sandbox does NOT mount — so
nothing here is readable from inside the sandbox.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class PendingStore:
    """De-duplicated queue of denied domains awaiting operator approval."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.data = data if isinstance(data, dict) else {}
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def record(
        self,
        host: str,
        method: str,
        reason: str | None = None,
        rid: str | None = None,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        self.load()  # re-read so a concurrent writer (watcher vs harvester) isn't clobbered
        entry = self.data.get(host)
        if entry:
            entry["count"] += 1
            entry["last_seen"] = now
            entry["method"] = method
        else:
            entry = {"count": 1, "first_seen": now, "last_seen": now, "method": method}
            self.data[host] = entry
        if reason:
            entry["reason"] = reason
        if rid and "rid" not in entry:
            entry["rid"] = rid  # first broker requester gets the spool reply on approval
        self.save()

    def remove(self, host: str) -> None:
        self.load()
        if host in self.data:
            del self.data[host]
            self.save()

    def items(self) -> list[tuple[str, dict]]:
        return sorted(self.data.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True)


class CommandStore:
    """Agent-proposed commands awaiting operator accept/edit/decline."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.data = data if isinstance(data, dict) else {}
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def add(self, rid: str, command: str, reason: str = "", now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.load()
        self.data[rid] = {
            "command": command,
            "reason": reason,
            "status": "pending",
            "created": now,
            "output": "",
        }
        self.save()

    def get(self, rid: str) -> dict | None:
        return self.data.get(rid)

    def claim(self, rid: str) -> bool:
        """Atomically move a pending command to 'running' (blocks double-execution)."""
        self.load()
        entry = self.data.get(rid)
        if not entry or entry["status"] != "pending":
            return False
        entry["status"] = "running"
        self.save()
        return True

    def edit(self, rid: str, command: str) -> bool:
        self.load()
        entry = self.data.get(rid)
        if entry and entry["status"] == "pending":
            entry["command"] = command
            self.save()
            return True
        return False

    def set_result(self, rid: str, status: str, output: str) -> None:
        self.load()
        entry = self.data.get(rid)
        if entry:
            entry["status"] = status
            entry["output"] = output
            self.save()

    def pending(self) -> list[tuple[str, dict]]:
        return [(rid, e) for rid, e in self.items() if e["status"] == "pending"]

    def items(self) -> list[tuple[str, dict]]:
        return sorted(self.data.items(), key=lambda kv: kv[1].get("created", 0), reverse=True)
