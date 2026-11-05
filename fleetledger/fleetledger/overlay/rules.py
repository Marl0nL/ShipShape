"""The tailnet policy, as far as the Ships are concerned: what must be in it, how to
add what is missing, and how to tell whether it is still right.

The rules the Ships need (providers.policy_doc):

- tagOwners: each Ship tag is owned by admins only, so nobody else can make a
  device look like a Ship;
- one rule letting your devices (autogroup:member) reach the Ships on port 22;
- nothing letting a Ship tag, or every device ("*"), be a source: a Ship can then
  open no connection to your devices or to other Ships;
- a policy test that a Ship cannot reach another Ship on port 22, so the admin
  console refuses a later edit that would allow it.

merge() adds only what is missing, as text, keeping everything else in your file
(comments included); it never removes or changes a rule of yours. findings()
reports what is wrong, including rules of yours that undo the Ships' isolation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from fleetledger.overlay import hujson

MEMBER = "autogroup:member"
ADMINS = ["autogroup:admin"]
WIDE_OWNERS = {"autogroup:member", "*", "autogroup:tagged"}
# Sources that include the Ships: every device, every tagged device, or anything
# at all (autogroup:danger-all also covers sources outside the tailnet).
SHIP_COVERING_SOURCES = {"*", "autogroup:tagged", "autogroup:danger-all"}


@dataclass(frozen=True)
class Finding:
    name: str
    ok: bool
    detail: str
    severity: str = "fail"  # fail | warn, when not ok


def _rules(doc: dict) -> list[dict]:
    """acls and grants, in one shape: {src, dst (host:port strings or tags), ports, kind}."""
    out = []
    for rule in doc.get("acls") or []:
        if isinstance(rule, dict) and rule.get("action", "accept") == "accept":
            out.append({"src": list(rule.get("src") or []), "dst": list(rule.get("dst") or []),
                        "ports": None, "kind": "acl"})
    for grant in doc.get("grants") or []:
        if isinstance(grant, dict):
            out.append({"src": list(grant.get("src") or []), "dst": list(grant.get("dst") or []),
                        "ports": list(grant.get("ip") or []), "kind": "grant"})
    return out


def _host(dst: str) -> str:
    return dst.rsplit(":", 1)[0] if ":" in dst and not dst.endswith("]") else dst


def _port_spec_covers_22(spec: str) -> bool:
    """A Tailscale port list: *, 22, 22-80, or several of those comma-separated."""
    for part in spec.split(","):
        part = part.strip()
        if part == "*":
            return True
        low, _, high = part.partition("-")
        try:
            if int(low) <= 22 <= int(high or low):
                return True
        except ValueError:
            continue
    return False


def _covers_port_22(rule: dict, dst: str) -> bool:
    if rule["kind"] == "acl":
        return _port_spec_covers_22(dst.rsplit(":", 1)[1] if ":" in dst else "*")
    for spec in rule["ports"] or ["*"]:
        proto, _, ports = spec.rpartition(":")
        if proto in ("", "tcp", "6") and _port_spec_covers_22(ports):
            return True
    return False


def ship_sources(doc: dict, tags: list[str]) -> list[dict]:
    """Rules that let a Ship start a connection: their source is a Ship tag, "*",
    autogroup:tagged or autogroup:danger-all."""
    return [r for r in _rules(doc) if set(r["src"]) & (SHIP_COVERING_SOURCES | set(tags))]


def members_reach_ships(doc: dict, tags: list[str]) -> bool:
    for rule in _rules(doc):
        if not set(rule["src"]) & {MEMBER, "*", "autogroup:danger-all"}:
            continue
        for dst in rule["dst"]:
            host = _host(dst) if rule["kind"] == "acl" else dst
            if host in ({"*"} | set(tags)) and _covers_port_22(rule, dst):
                return True
    return False


def _has_ship_test(doc: dict, tags: list[str]) -> bool:
    for test in doc.get("tests") or []:
        if isinstance(test, dict) and test.get("src") in tags:
            denied = {_host(d) for d in test.get("deny") or []}
            if denied & set(tags):
                return True
    return False


def findings(doc: dict, tags: list[str]) -> list[Finding]:
    out = []
    owners = doc.get("tagOwners") or {}
    for tag in tags:
        who = owners.get(tag)
        if who is None:
            out.append(Finding(f"tag owner {tag}", False, f"{tag} has no tagOwners entry; "
                               "Ships cannot join with it."))
        elif not who:  # Tailscale's shorthand for admins only
            out.append(Finding(f"tag owner {tag}", True, f"{tag} owned by admins (empty list)"))
        elif set(who) & WIDE_OWNERS:
            out.append(Finding(f"tag owner {tag}", False,
                               f"{tag} is owned by {', '.join(who)}: any member could make "
                               "a device look like a Ship. Own it by admins only."))
        else:
            out.append(Finding(f"tag owner {tag}", True, f"{tag} owned by {', '.join(who)}"))
    out.append(Finding("your devices reach Ships on port 22", members_reach_ships(doc, tags),
                       "a rule from autogroup:member to the Ships on port 22"
                       if members_reach_ships(doc, tags) else
                       "no rule lets your devices reach the Ships on port 22"))
    wide = ship_sources(doc, tags)
    out.append(Finding(
        "Ships cannot start connections", not wide,
        "no rule has a Ship tag, *, autogroup:tagged or autogroup:danger-all as its source"
        if not wide else
        "these rules let Ships reach other devices (a Ship could reach your laptop or "
        "another Ship): " + "; ".join(json.dumps({"src": r["src"], "dst": r["dst"]})
                                     for r in wide[:3])))
    out.append(Finding("policy test: Ships cannot reach each other", _has_ship_test(doc, tags),
                       "present" if _has_ship_test(doc, tags) else
                       "missing: nothing stops a later edit from letting Ships reach each "
                       "other", severity="warn"))
    return out


def merge(text: str, tags: list[str]) -> tuple[str, list[str]]:
    """Add what the Ships need and is missing. Returns (new text, what was added)."""
    doc = hujson.parse(text)
    if not isinstance(doc, dict):
        raise hujson.HuJSONError("the policy is not an object")
    added: list[str] = []
    owners = doc.get("tagOwners")
    for tag in tags:
        if isinstance(owners, dict) and tag in owners:
            continue
        entry = f"{json.dumps(tag)}: {json.dumps(ADMINS)}"
        if "tagOwners" in doc:
            text = hujson.insert_first(text, "tagOwners", entry)
        else:
            text = hujson.add_key(text, "tagOwners", "{" + entry + "}")
            doc["tagOwners"] = {}
        added.append(f"tagOwners: {tag} owned by admins")
    if not members_reach_ships(doc, tags):
        if "grants" in doc and "acls" not in doc:
            rule = {"src": [MEMBER], "dst": tags, "ip": ["tcp:22"]}
            text = hujson.insert_first(text, "grants", json.dumps(rule))
        else:
            rule = {"action": "accept", "src": [MEMBER], "dst": [f"{t}:22" for t in tags]}
            text = (hujson.insert_first(text, "acls", json.dumps(rule)) if "acls" in doc
                    else hujson.add_key(text, "acls", "[" + json.dumps(rule) + "]"))
        added.append("a rule: your devices may reach the Ships on port 22")
    if not _has_ship_test(doc, tags):
        test = {"src": tags[0], "deny": [f"{t}:22" for t in tags]}
        text = (hujson.insert_first(text, "tests", json.dumps(test)) if "tests" in doc
                else hujson.add_key(text, "tests", "[" + json.dumps(test) + "]"))
        added.append("a test: a Ship cannot reach another Ship")
    hujson.parse(text)  # what we write must still parse
    return text, added
