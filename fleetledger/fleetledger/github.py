"""GitHub repos named for a Ship's agents (an [[autostart]] entry's repo).

The Ship clones the repo itself, as its agent (contracts/ship-ctl.md, autostart):
a public repo at once, a private one after the Ship's GitHub sign-in. FleetLedger
only checks the name, and asks GitHub, without signing in, whether the repo is
public, so it can say which of the two will happen.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from fleetledger.errors import FleetError

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")  # as the schema
API = "https://api.github.com/repos/"


def check_name(repo: str) -> str:
    repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not REPO_RE.match(repo) or repo.split("/")[1] in (".", ".."):
        raise FleetError(f"'{repo}' is not a GitHub repo name: use owner/name, such as "
                         "someone/agent-setup.")
    return repo


def folder(repo: str) -> str:
    """Where the Ship clones it: ~/work/<name>."""
    return "~/work/" + repo.rsplit("/", 1)[-1]


def visibility(repo: str, timeout: float = 5) -> str:
    """public | not-public (private, or not there) | unknown (GitHub did not answer)."""
    request = urllib.request.Request(API + repo, headers={
        "User-Agent": "fleetledger", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            doc = json.loads(response.read(1 << 20))
        return "not-public" if doc.get("private") else "public"
    except urllib.error.HTTPError as exc:
        return "not-public" if exc.code == 404 else "unknown"
    except (OSError, ValueError):
        return "unknown"


def what_happens(repo: str, seen: str) -> str:
    if seen == "public":
        return f"{repo} is public: the Ship clones it into {folder(repo)} as it starts."
    if seen == "not-public":
        return (f"{repo} is not public (or does not exist): the Ship clones it once you sign "
                "it in to GitHub, then starts the agent. If it should be public, check the "
                "name.")
    return f"Could not ask GitHub about {repo}; the Ship tries to clone it as it starts."
