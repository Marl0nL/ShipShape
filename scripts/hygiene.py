#!/usr/bin/env python3
"""Public-repo hygiene check for ShipShape.

ShipShape is a public repository. Nothing committed may point at a specific
person, employer, machine or network. This script enforces that in two layers:

1. Generic rules, committed here, that catch the common leaks: private IPv4
   addresses, home-directory paths and real-looking email addresses. The
   home-path and email rules skip frozen folders (legacy/, docs/archive/);
   private IPs are checked everywhere. Findings never repeat the matched value.
2. A private deny-list of exact terms (names, hostnames, project ids...). The
   list lives OUTSIDE the repo, so the terms themselves are never committed, and
   a match is reported by entry number only, never by the matched text.

Usage:
  hygiene.py --tree                 scan tracked and untracked (not ignored) files
  hygiene.py --commits RANGE        scan commit messages, e.g. origin/main..HEAD
  hygiene.py FILE...                scan the given files (e.g. a PR body draft)

Options:
  --denylist PATH   private deny-list (default: $SHIPSHAPE_HYGIENE_DENYLIST,
                    else $XDG_CONFIG_HOME/shipshape/hygiene-denylist, else
                    ~/.config/shipshape/hygiene-denylist). Missing file: generic
                    rules only. A deny-list inside the repo is refused.
  --no-denylist     skip the deny-list even if one exists.

Deny-list format: one entry per line; blank lines and lines starting with '#'
are ignored; 're:<regex>' is a case-insensitive regular expression; anything
else is a case-insensitive literal.

A line containing the marker 'hygiene: allow' is skipped by the generic rules
(use it sparingly, for documented examples). Exit status: 0 clean, 1 findings,
2 usage or setup error.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOW_MARKER = "hygiene: allow"
MAX_BYTES = 2 * 1024 * 1024

# Frozen material that predates the rule. The home-path and email rules skip it
# (it uses a container user's home and old placeholders); the private-IP rule and
# the deny-list still apply there.
FROZEN_PREFIXES = ("legacy/", "docs/archive/")

# Users that may appear in paths: the Ship's own accounts and common placeholders.
ALLOWED_HOME_USERS = {
    "agent",
    "fleet",
    "ubuntu",
    "user",
    "username",
    "example",
    "runner",
    "you",
    "<user>",
    "$user",
    "${user}",
}

# Email domains reserved for documentation and testing (RFC 2606, RFC 6761).
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")
# ".local" covers generic mDNS names such as fleet@ship-01.local in ssh examples.
ALLOWED_EMAIL_SUFFIXES = (".example", ".invalid", ".test", ".localhost", ".local")

IPV4_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?(?!\d|\.\d)")
HOME_RE = re.compile(
    r"(?:/home/|/Users/|[Cc]:\\Users\\)([A-Za-z0-9_.$<>{}-]+)", re.IGNORECASE
)
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")

# Ranges that identify a private network. Documentation ranges (192.0.2.0/24,
# 198.51.100.0/24, 203.0.113.0/24), loopback and 0.0.0.0 are fine.
CANONICAL_BLOCKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
PRIVATE_NETS = [ipaddress.ip_network(n) for n in CANONICAL_BLOCKS]
# A whole block written out (e.g. in a firewall rule) names no one's network, so it
# passes; any narrower subnet or single address inside a block is flagged.


@dataclass(frozen=True)
class Finding:
    where: str  # "path:line" or "commit <sha>"
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: {self.rule}: {self.detail}"


@dataclass(frozen=True)
class DenyEntry:
    number: int
    pattern: re.Pattern[str]


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    )
    return Path(out.stdout.strip())


def default_denylist_path() -> Path:
    env = os.environ.get("SHIPSHAPE_HYGIENE_DENYLIST")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "shipshape" / "hygiene-denylist"


def load_denylist(path: Path, root: Path | None) -> list[DenyEntry]:
    if not path.is_file():
        return []
    resolved = path.resolve()
    if root is not None and resolved.is_relative_to(root.resolve()):
        raise SystemExit(
            f"hygiene: refusing a deny-list inside the repository ({path}); keep it outside"
        )
    entries: list[DenyEntry] = []
    for number, raw in enumerate(resolved.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            pattern = re.compile(line[3:], re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(line), re.IGNORECASE)
        entries.append(DenyEntry(number=number, pattern=pattern))
    return entries


def _is_private(address: str, prefix: str | None) -> bool:
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not any(addr in net for net in PRIVATE_NETS):
        return False
    return prefix is None or f"{address}/{prefix}" not in CANONICAL_BLOCKS


def check_generic(line: str, frozen: bool = False) -> list[tuple[str, str]]:
    """Return (rule, detail) pairs for generic-rule violations in one line.

    Details name the rule and the fix, never the matched value: findings end up in
    public CI logs. `frozen` lines (legacy/, docs/archive/) get the private-IP rule
    only.
    """
    if ALLOW_MARKER in line:
        return []
    found: list[tuple[str, str]] = []
    for match in IPV4_RE.finditer(line):
        if _is_private(match.group(1), match.group(2)):
            found.append(("private-ip", "private IPv4 address; use 192.0.2.x instead"))
    if frozen:
        return found
    for match in HOME_RE.finditer(line):
        user = match.group(1).rstrip("/\\").lower()
        if user not in ALLOWED_HOME_USERS:
            found.append(("home-path", "a home-directory path names a user; use ~/ instead"))
    for match in EMAIL_RE.finditer(line):
        domain = match.group(1).lower()
        if domain.rsplit(".", 1)[-1].isdigit():
            continue  # name@1.2.3 is a package version, not an address
        if domain in ALLOWED_EMAIL_DOMAINS or domain.endswith(ALLOWED_EMAIL_SUFFIXES):
            continue
        found.append(("email", "email address outside the example domains; use example.com"))
    return found


def check_denylist(line: str, entries: list[DenyEntry]) -> list[tuple[str, str]]:
    return [
        ("denylist", f"matches private deny-list entry #{e.number}")
        for e in entries
        if e.pattern.search(line)
    ]


def scan_text(
    text: str,
    where: str,
    entries: list[DenyEntry],
    generic: bool = True,
    frozen: bool = False,
) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        loc = f"{where}:{lineno}"
        hits = check_denylist(line, entries)
        if generic:
            hits += check_generic(line, frozen=frozen)
        findings.extend(Finding(loc, rule, detail) for rule, detail in hits)
    return findings


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_BYTES or b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def tree_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return sorted({p for p in out.stdout.decode().split("\0") if p})


def scan_tree(root: Path, entries: list[DenyEntry]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in tree_files(root):
        path = root / rel
        if not path.is_file():
            continue
        # The path itself can leak too (e.g. a folder named after someone).
        findings.extend(scan_text(rel, f"{rel} (path)", entries, generic=False))
        text = read_text(path)
        if text is None:
            continue
        frozen = rel.startswith(FROZEN_PREFIXES)
        findings.extend(scan_text(text, rel, entries, frozen=frozen))
    return findings


def scan_commits(root: Path, rev_range: str, entries: list[DenyEntry]) -> list[Finding]:
    out = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x01", rev_range],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    findings: list[Finding] = []
    for record in out.stdout.split("\x01"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, message = record.partition("\x00")
        findings.extend(scan_text(message, f"commit {sha[:12]}", entries))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Public-repo hygiene check for ShipShape.",
        epilog="See docs/hygiene.md.",
    )
    parser.add_argument("--tree", action="store_true", help="scan the working tree")
    parser.add_argument("--commits", metavar="RANGE", help="scan commit messages in RANGE")
    parser.add_argument("--denylist", metavar="PATH", help="private deny-list file")
    parser.add_argument("--no-denylist", action="store_true", help="skip the deny-list")
    parser.add_argument("files", nargs="*", help="extra files to scan (e.g. a PR body)")
    args = parser.parse_args(argv)

    if not (args.tree or args.commits or args.files):
        parser.error("nothing to scan: pass --tree, --commits RANGE or FILE...")

    try:
        root: Path | None = repo_root()
    except (subprocess.CalledProcessError, FileNotFoundError):
        root = None
        if args.tree or args.commits:
            print("hygiene: --tree and --commits need a git checkout", file=sys.stderr)
            return 2

    entries: list[DenyEntry] = []
    if not args.no_denylist:
        path = Path(args.denylist).expanduser() if args.denylist else default_denylist_path()
        if args.denylist and not path.is_file():
            print(f"hygiene: deny-list not found: {path}", file=sys.stderr)
            return 2
        entries = load_denylist(path, root)
        if entries:
            print(f"hygiene: private deny-list loaded ({len(entries)} entries)")
        else:
            print("hygiene: no private deny-list found; generic rules only")

    findings: list[Finding] = []
    if args.tree and root is not None:
        findings += scan_tree(root, entries)
    if args.commits and root is not None:
        findings += scan_commits(root, args.commits, entries)
    for name in args.files:
        text = read_text(Path(name))
        if text is None:
            print(f"hygiene: cannot read {name}", file=sys.stderr)
            return 2
        findings += scan_text(text, name, entries)

    for finding in findings:
        print(finding)
    if findings:
        print(f"hygiene: {len(findings)} finding(s); see docs/hygiene.md", file=sys.stderr)
        return 1
    print("hygiene: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
