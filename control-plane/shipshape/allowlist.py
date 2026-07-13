"""Parse, edit and render egress/allowed_domains.txt.

The file itself is the single source of truth (it stays hand-editable). A
disabled domain is represented in place with a sentinel prefix so section
headers and ordering survive round-trips:

    .github.com            -> enabled entry
    #SS-OFF# .github.com   -> disabled entry (kept, not deleted)
    # --- GitHub ---       -> decoration (header/comment), preserved verbatim
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

SENTINEL = "#SS-OFF#"
APPROVED_HEADER = "# --- Approved via control plane ---"


@dataclass
class Line:
    kind: str  # "entry" | "deco"
    text: str  # verbatim text for deco lines
    domain: str = ""
    enabled: bool = True

    def render(self) -> str:
        if self.kind == "deco":
            return self.text
        return self.domain if self.enabled else f"{SENTINEL} {self.domain}"


@dataclass
class Allowlist:
    lines: list[Line] = field(default_factory=list)
    path: Path | None = None

    @classmethod
    def parse(cls, text: str, path: Path | None = None) -> "Allowlist":
        lines: list[Line] = []
        for raw in text.splitlines():
            s = raw.strip()
            if s.startswith(SENTINEL):
                rest = s[len(SENTINEL):].strip()
                # Only a single bare token is a disabled entry; "#SS-OFF# note: …"
                # (with whitespace) is an operator comment, preserved as decoration.
                if rest and not any(c.isspace() for c in rest):
                    lines.append(Line("entry", raw, rest, enabled=False))
                else:
                    lines.append(Line("deco", raw))
            elif s == "" or s.startswith("#"):
                lines.append(Line("deco", raw))
            else:
                lines.append(Line("entry", raw, s, enabled=True))
        return cls(lines, path)

    @classmethod
    def load(cls, path: Path) -> "Allowlist":
        return cls.parse(path.read_text(), path)

    # --- queries ---
    def entries(self) -> list[Line]:
        return [ln for ln in self.lines if ln.kind == "entry"]

    def find(self, domain: str) -> Line | None:
        for ln in self.lines:
            if ln.kind == "entry" and ln.domain == domain:
                return ln
        return None

    def enabled_domains(self) -> list[str]:
        return [ln.domain for ln in self.entries() if ln.enabled]

    # --- edits ---
    def set_enabled(self, domain: str, enabled: bool) -> bool:
        ln = self.find(domain)
        if ln is None:
            return False
        ln.enabled = enabled
        return True

    def add(self, domain: str, enabled: bool = True) -> bool:
        """Add a domain. Returns True if newly added, False if it already
        existed (in which case its enabled state is updated)."""
        existing = self.find(domain)
        if existing is not None:
            existing.enabled = enabled
            return False
        if not any(ln.kind == "deco" and ln.text.strip() == APPROVED_HEADER for ln in self.lines):
            if self.lines and self.lines[-1].render().strip() != "":
                self.lines.append(Line("deco", ""))
            self.lines.append(Line("deco", APPROVED_HEADER))
        self.lines.append(Line("entry", domain, domain, enabled))
        return True

    # --- serialisation ---
    def render(self) -> str:
        return "\n".join(ln.render() for ln in self.lines) + "\n"

    def save(self, path: Path | None = None) -> Path:
        """Atomically write the file, keeping a .bak rollback copy."""
        target = path or self.path
        if target is None:
            raise ValueError("no path to save to")
        if target.exists():
            shutil.copy2(target, target.with_name(target.name + ".bak"))
        tmp = target.with_name(target.name + ".tmp")
        try:
            with open(tmp, "w") as f:
                f.write(self.render())
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return target
