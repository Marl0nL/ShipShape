"""User-configurable local settings, stored as a JSON overlay at
`state/settings.json` — kept SEPARATE from the tracked, comment-rich shipshape.toml
so the first-launch wizard and the Settings pane can rewrite it freely.

Anything that is (a) machine-written by the TUI and (b) local-only (not a secret,
not something you'd commit) lives here: the firstmate source repo, the proxy
bandwidth cap, whether the first-run wizard has been completed, and the mute state.
"""

from __future__ import annotations

import json

from .config import Paths

# Curated firstmate source repos offered by the wizard (plus "type your own").
FIRSTMATE_REPOS: dict[str, str] = {
    "Marl0nL/firstmate": "https://github.com/Marl0nL/firstmate.git",
    "kunchenguid/firstmate": "https://github.com/kunchenguid/firstmate.git",
}

DEFAULTS: dict = {
    "firstmate_repo": FIRSTMATE_REPOS["Marl0nL/firstmate"],
    "bandwidth_mbps": 20,
    "first_run_done": False,
    "muted": False,
}


def _file(paths: Paths):
    return paths.state / "settings.json"


def load(paths: Paths) -> dict:
    data = dict(DEFAULTS)
    f = _file(paths)
    if f.exists():
        try:
            data.update(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return data


def save(paths: Paths, data: dict) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    f = _file(paths)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(f)


def get(paths: Paths, key: str):
    return load(paths).get(key, DEFAULTS.get(key))


def update(paths: Paths, **changes) -> dict:
    data = load(paths)
    data.update(changes)
    save(paths, data)
    return data
