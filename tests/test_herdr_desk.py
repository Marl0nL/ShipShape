"""The Herdr Desk (herdr/): what can be checked without a running Herdr.

The mods run only inside Herdr, so here their JavaScript is only parsed. The `desk` CLI runs
for real against a scratch board, outside any Herdr terminal (no HC_TERMINAL_ID), so it never
calls `hc`.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERDR = ROOT / "herdr"
DESK = HERDR / "mods" / "desk"
MODS = ("desk", "desk-agents", "desk-spaces")


def test_the_mods_javascript_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    files = sorted(p for m in MODS for p in (HERDR / "mods" / m).glob("*.js"))
    assert len(files) >= 7
    for path in files:
        r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 0, f"{path.relative_to(ROOT)}: {r.stderr}"


def test_its_json_files_parse():
    files = [*HERDR.glob("layouts/*.json"), *HERDR.glob("themes/*.json"),
             *(HERDR / "mods").glob("*/mod.json"), *(HERDR / "mods").glob("*/jsconfig.json"),
             *DESK.glob("samples/*.json")]
    assert len(files) >= 12
    for path in files:
        json.loads(path.read_text("utf-8"))


def test_every_file_the_desk_names_is_here():
    """Each desk palette looks.js offers has its theme, and every picture the mods' JavaScript
    or the layout names under mods/desk or layouts is in the repo, as is each of the three mods
    the layout places."""
    looks = (DESK / "looks.js").read_text("utf-8")
    for theme in set(re.findall(r'"(desk-[a-z-]+)"', looks)):
        assert (HERDR / "themes" / f"{theme}.json").is_file(), theme
    layout = (HERDR / "layouts" / "desk.json").read_text("utf-8")
    code = "".join(p.read_text("utf-8") for m in MODS for p in (HERDR / "mods" / m).glob("*.js"))
    named = set(re.findall(r'"((?:mods/desk|layouts)/[^"]+\.(?:png|svg))"', code + layout))
    assert "mods/desk/icons/caution.svg" in named
    for path in named:
        assert (HERDR / path).is_file(), path
    for mod in MODS:
        assert f'"mod": "{mod}"' in layout, mod
        assert (HERDR / "mods" / mod / "mod.json").is_file(), mod


def test_no_generated_typings_are_committed():
    r = subprocess.run(["git", "ls-files", "--", "herdr/*gpui-kit.d.ts"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == []


def desk(tmp_path, *args, board=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HC_", "DESK_"))}
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    if board:
        env["DESK_FILE"] = str(board)
    r = subprocess.run([sys.executable, str(DESK / "bin" / "desk"), *args], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (args, r.stderr)
    return r


def test_the_desk_cli_keeps_a_board(tmp_path):
    out = desk(tmp_path, "init", "crew", "--name", "fm-example", "--tags", "work",
               "--parent", "lead", "--task", "fm-example")
    assert "labels not set" in out.stderr
    board = tmp_path / "state" / "herdr-desk" / "fm-example.json"
    assert out.stdout.strip() == str(board)
    desk(tmp_path, "summary", "Reproducing the bug", board=board)
    desk(tmp_path, "finding", "add", "Only with SSO on", "--tone", "warn", board=board)
    desk(tmp_path, "next", "set", "Write a test", "Open the PR", board=board)
    desk(tmp_path, "next", "done", "1", board=board)
    desk(tmp_path, "card", "add", "risk", "--title", "Watch out", "--tone", "bad", board=board)
    b = json.loads(board.read_text("utf-8"))
    assert b["kind"] == "crew"
    assert b["tags"] == ["work"]
    assert b["crew"] == {"parent": "lead", "task": "fm-example"}
    assert b["summary"] == "Reproducing the bug"
    assert [(f["text"], f["tone"]) for f in b["findings"]] == [("Only with SSO on", "warn")]
    assert [(s["text"], s["done"]) for s in b["next"]] == [("Write a test", True),
                                                           ("Open the PR", False)]
    assert [(c["id"], c["tone"]) for c in b["cards"]] == [("risk", "bad")]
    # Written whole, by rename: no temporary file is left beside it (only the lock file).
    assert sorted(p.name for p in board.parent.iterdir()) == ["fm-example.json",
                                                              "fm-example.json.lock"]


def test_the_desk_cli_refuses_a_bad_tone(tmp_path):
    board = tmp_path / "board.json"
    env ={k: v for k, v in os.environ.items() if not k.startswith(("HC_", "DESK_"))}
    r = subprocess.run([sys.executable, str(DESK / "bin" / "desk"), "card", "add", "x",
                        "--title", "X", "--tone", "loud"],
                       env={**env, "DESK_FILE": str(board)}, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode != 0
    assert not board.exists()


def test_the_desk_shares_the_fleet_panels_reader():
    """The desk reads fleet.json and ship-fleet's answer with the Fleet panel's own checks: its
    fleet-model.js is a link to that mod's model.js, which install-desk.sh installs as a copy."""
    link = DESK / "fleet-model.js"
    assert link.is_symlink()
    assert os.readlink(link) == "../fleet/model.js"
    shiptags = (DESK / "shiptags.js").read_text("utf-8")
    assert re.findall(r'from "([^"]+)"', shiptags) == ["./fleet-model.js"]


def install_desk(tmp_path):
    home = tmp_path / "herdr-app"
    env = {**os.environ, "HERDR_APP_HOME": str(home), "HOME": str(tmp_path / "home")}
    r = subprocess.run(["bash", str(HERDR / "install-desk.sh")], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return home


def test_install_desk_installs_the_fleet_panel_too_and_again(tmp_path):
    home = install_desk(tmp_path)
    settings = home / "mods" / "fleet" / "settings.json"
    settings.write_text('{"ssh": "fleetledger@fleetledger.example.invalid"}\n', "utf-8")
    home = install_desk(tmp_path)  # again: an update, nothing doubled, settings kept
    assert sorted(p.name for p in (home / "mods").iterdir()) == ["desk", "desk-agents",
                                                                 "desk-spaces", "fleet"]
    fleet = HERDR / "mods" / "fleet"
    for name in ("mod.json", "model.js", "main.js"):
        assert (home / "mods" / "fleet" / name).read_bytes() == (fleet / name).read_bytes()
    copy = home / "mods" / "desk" / "fleet-model.js"
    assert not copy.is_symlink()
    assert copy.read_bytes() == (fleet / "model.js").read_bytes()
    assert (home / "mods" / "desk" / "shiptags.js").is_file()
    assert settings.read_text("utf-8") == '{"ssh": "fleetledger@fleetledger.example.invalid"}\n'
    assert (tmp_path / "home" / ".local" / "bin" / "desk").is_symlink()
