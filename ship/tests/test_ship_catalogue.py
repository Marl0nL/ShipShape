"""The catalogue (W6): recipes, the firstmate profile, and how they run on a sandbox Ship."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from pathlib import Path

import pytest
from ship_harness import assert_valid, make_bundle

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "ship" / "tools"
PROFILES = REPO / "ship" / "profiles"
VERSIONS = tomllib.loads((REPO / "versions.toml").read_text())
RECIPES = {p.stem: tomllib.loads(p.read_text()) for p in sorted(TOOLS.glob("*.toml"))}


def test_defaults_are_the_planned_four():
    defaults = sorted(r for r, doc in RECIPES.items() if doc.get("default"))
    assert defaults == ["chrome-devtools-axi", "gh-axi", "quota-axi", "ship-axi"]


@pytest.mark.parametrize("rid", sorted(RECIPES))
def test_recipe_is_complete(rid):
    doc = RECIPES[rid]
    if doc["kind"] == "npm":
        assert VERSIONS["npm"].get(doc["package"]) == doc["version"], "pin it in versions.toml"
    for spec in doc.get("also_npm", []):
        name, _, version = spec.rpartition("@")
        assert VERSIONS["npm"].get(name) == version, f"{spec}: pin it in versions.toml"
    if doc["kind"] == "script":
        assert (TOOLS / doc["script"]).is_file()
    apt_known = {p for repo in VERSIONS["apt"].values() for p in repo["packages"]}
    for pkg in doc.get("apt", []):
        assert pkg in apt_known, f"{pkg} must come from a versions.toml [apt.*] repo"
    for value in doc.get("env", {}).values():
        assert "'" not in value and "\n" not in value
    # Nothing unpinned at run time.
    assert "@latest" not in json.dumps(doc) and "npx" not in json.dumps(doc)


def test_release_binaries_are_pinned():
    for script in ("treehouse.sh", "no-mistakes.sh"):
        text = (TOOLS / script).read_text()
        assert re.search(r"install_release_binary \S+ v\d+\.\d+\.\d+ ", text)
        assert len(re.findall(r"\b[0-9a-f]{64}\b", text)) == 2


def test_firstmate_profile():
    doc = tomllib.loads((PROFILES / "firstmate.toml").read_text())
    assert doc["tools"] == ["treehouse", "no-mistakes", "lavish-axi", "tasks-axi"]
    assert all(t in RECIPES for t in doc["tools"])
    assert doc["options"]["repo_url"]["required"] is True
    assert (PROFILES / doc["setup"]).is_file()
    # The repo never holds a real URL: only the option's description.
    assert "github.com/" not in (PROFILES / "firstmate.toml").read_text()


def test_ship_axi_pins_match():
    pkg = json.loads((REPO / "ship/axi/package.json").read_text())
    assert pkg["dependencies"]["axi-sdk-js"] == VERSIONS["npm"]["axi-sdk-js"]
    lock = json.loads((REPO / "ship/axi/package-lock.json").read_text())
    assert lock["packages"]["node_modules/axi-sdk-js"]["version"] == VERSIONS["npm"]["axi-sdk-js"]
    for entry in lock["packages"].values():
        if "resolved" in entry:
            assert entry["resolved"].startswith("https://registry.npmjs.org/")
            assert entry.get("integrity", "").startswith("sha512-")


def test_skill_has_frontmatter():
    text = (REPO / "ship/skills/ship/SKILL.md").read_text()
    head = text.split("---")[1]
    assert "name: ship" in head and "description:" in head


@pytest.fixture
def fleet_ship(ship):
    """The sandbox Ship with the real catalogue and a typical ship.toml."""
    shutil.rmtree(ship.bundle)
    make_bundle(ship.bundle, catalogue=True)
    # Script recipes download release binaries; stand them in for this test.
    for script in ("treehouse.sh", "no-mistakes.sh"):
        name = script.removesuffix(".sh")
        (ship.bundle / "tools" / script).write_text(
            f'mkdir -p "$HOME/.local/bin"\nprintf "#!/bin/sh\\necho {name} 1.0\\n" > '
            f'"$HOME/.local/bin/{name}"\nchmod +x "$HOME/.local/bin/{name}"\n'
        )
    (ship.bundle / "profiles/firstmate/setup.sh").write_text(
        '[ -n "$FAIL_CLONE" ] && [ ! -e "$HOME/gh-ok" ] && exit 1\n'
        'echo "$SHIPSHAPE_OPT_REPO_URL" > "$HOME/cloned"\n'
    )
    with ship.p("/etc/shipshape/ship.toml").open("a") as fh:
        fh.write(
            '\n[tools]\ninstall = ["gh-axi", "quota-axi", "chrome-devtools-axi", "ship-axi"]\n'
            'profiles = ["firstmate"]\n\n[profile_options.firstmate]\n'
            'repo_url = "https://example.com/firstmate.git"\n'
        )
    # ship-axi is a release command on a real Ship; put a stand-in on PATH.
    axi = ship.p("/home/agent/.local/bin/ship-axi")
    axi.parent.mkdir(parents=True, exist_ok=True)
    axi.write_text('#!/bin/sh\n[ "$1" = --version ] && echo 0.1.0\nexit 0\n')
    axi.chmod(0o755)
    return ship


def test_default_catalogue_on_a_new_ship(fleet_ship):
    ship = fleet_ship
    ship.set(gh={"login": None}, claude={"logged_in": False})
    rc, doc, out = ship.ctl("tools", "run", "--json")
    assert rc == 0, out
    inst = doc["data"]["installed"]
    assert inst["gh-axi"]["state"] == "waiting" and inst["gh-axi"]["waiting_for"] == ["github"]
    assert inst["quota-axi"]["waiting_for"] == ["claude"]
    assert inst["chrome-devtools-axi"] == {"state": "ok", "version": "0.1.46"}
    assert inst["ship-axi"] == {"state": "ok", "version": "0.1.0"}
    for tid in ("treehouse", "no-mistakes", "lavish-axi", "tasks-axi"):
        assert inst[tid]["state"] == "ok", tid
    assert doc["data"]["profiles"] == {"firstmate": "ok"}
    assert "google-chrome-stable" in ship.scenario()["apt"]["installed"]
    installs = [spec for call in ship.scenario()["npm"]["installs"] for spec in call]
    assert "chrome-devtools-mcp@1.12.0" in installs
    assert (ship.p("/home/agent/.claude/skills/ship/SKILL.md")).exists()
    assert not ship.p("/home/agent/.claude/skills/ship/README.md").exists()
    env = ship.p("/home/agent/.config/shipshape/env").read_text()
    assert (
        "CHROME_DEVTOOLS_AXI_MCP_PATH='/home/agent/.local/lib/node_modules/chrome-devtools-mcp"
        "/build/src/bin/chrome-devtools-mcp.js'"
    ) in env

    _, status, _ = ship.ctl("status", "--json")
    assert_valid("ship-status", status)
    assert status["state"] == "needs-sign-in"
    waiting = {a["subject"] for a in status["attention"] if a["code"] == "tool-waiting"}
    assert waiting == {"gh-axi", "quota-axi"}


def test_profile_retries_after_sign_in(fleet_ship):
    ship = fleet_ship
    ship.env["FAIL_CLONE"] = "1"
    ship.set(gh={"login": None})
    rc, doc, _ = ship.ctl("tools", "run", "--json")
    assert rc == 1 and doc["data"]["profiles"] == {"firstmate": "failed"}
    # The first refresh compares against "not signed in" (contracts/ship-ctl.md), so it
    # retries too; GitHub is still missing, so the clone still fails.
    ship.ctl("refresh")
    assert ship.json("/var/lib/shipshape/profiles.json")["firstmate"]["state"] == "failed"
    ship.p("/home/agent/gh-ok").write_text("")
    ship.set(gh={"login": "example-user"})
    rc, _, out = ship.ctl("refresh")
    assert "profiles retried: firstmate ok" in out
    assert ship.p("/home/agent/cloned").read_text() == "https://example.com/firstmate.git\n"
    assert ship.json("/var/lib/shipshape/profiles.json")["firstmate"]["state"] == "ok"


# --- review follow-ups ------------------------------------------------------------------
import subprocess  # noqa: E402
import tarfile  # noqa: E402


def test_ship_axi_skill_install_is_idempotent(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    first = subprocess.run(
        ["bash", str(TOOLS / "ship-axi.sh")], env=env, capture_output=True, text=True
    )
    assert first.returncode == 0 and "installed" in first.stdout
    skill = tmp_path / ".claude/skills/ship"
    assert (skill / "SKILL.md").exists() and not (skill / "README.md").exists()
    inode = skill.stat().st_ino
    second = subprocess.run(
        ["bash", str(TOOLS / "ship-axi.sh")], env=env, capture_output=True, text=True
    )
    assert second.returncode == 0 and second.stdout == ""
    assert skill.stat().st_ino == inode


FAKE_CURL = """#!/bin/sh
echo "$@" >> "$FIXTURE/calls"
out=""; url=""
while [ $# -gt 0 ]; do case $1 in -o) out=$2; shift ;; https://*) url=$1 ;; esac; shift; done
cp "$FIXTURE/${url##*/}" "$out"
"""


@pytest.fixture
def release_server(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    payload = tmp_path / "demo"
    payload.write_text("#!/bin/sh\necho demo 1.0\n")
    payload.chmod(0o755)
    for arch in ("amd64", "arm64"):
        with tarfile.open(fixture / f"demo-v1.0.0-{arch}.tar.gz", "w:gz") as tar:
            tar.add(payload, arcname="demo")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(FAKE_CURL)
    (bindir / "curl").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    import hashlib

    def run(sha: str | None = None):
        sha = sha or hashlib.sha256((fixture / "demo-v1.0.0-amd64.tar.gz").read_bytes()).hexdigest()
        arm = hashlib.sha256((fixture / "demo-v1.0.0-arm64.tar.gz").read_bytes()).hexdigest()
        script = (
            f'. "{TOOLS}/lib/release-binary.sh"; install_release_binary demo v1.0.0 '
            f'"https://example.com/{{version}}/demo-{{version}}-{{arch}}.tar.gz" {sha} {arm}'
        )
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin", "FIXTURE": str(fixture)},
        )

    return run, home, fixture


def test_release_binary_checks_checksum_and_skips_when_installed(release_server):
    run, home, fixture = release_server
    if subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip() not in (
        "x86_64",
        "aarch64",
    ):
        pytest.skip("needs x86_64 or aarch64")
    bad = run("0" * 64)
    is_x86 = (
        subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip() == "x86_64"
    )
    if is_x86:
        assert bad.returncode == 1 and "checksum mismatch" in bad.stderr
        assert not (home / ".local/bin/demo").exists()
    ok = run()
    assert ok.returncode == 0, ok.stderr
    assert (home / ".local/bin/demo.version").read_text() == "v1.0.0\n"
    calls = (fixture / "calls").read_text().count("\n")
    again = run()
    assert again.returncode == 0 and again.stdout == ""
    assert (fixture / "calls").read_text().count("\n") == calls  # nothing downloaded


def test_firstmate_setup_clones_once(tmp_path):
    origin = tmp_path / "origin"
    git = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True, env=git)
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        env=git,
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "SHIPSHAPE_OPT_REPO_URL": str(origin)}
    setup = PROFILES / "firstmate/setup.sh"
    r = subprocess.run(["bash", str(setup)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (home / "work/firstmate/.git").is_dir()
    (home / "work/firstmate/mine").write_text("agent work")
    r = subprocess.run(["bash", str(setup)], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and (home / "work/firstmate/mine").exists()
    env["SHIPSHAPE_OPT_REPO_URL"] = str(tmp_path / "missing")
    shutil.rmtree(home / "work/firstmate")
    r = subprocess.run(["bash", str(setup)], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "could not clone" in r.stderr
    assert sorted(p.name for p in (home / "work").iterdir()) == []


@pytest.mark.parametrize("url,reason", [("-oProxyCommand=x", "not valid"), (None, "is not set")])
def test_profile_options_are_checked(fleet_ship, url, reason):
    ship = fleet_ship
    text = ship.p("/etc/shipshape/ship.toml").read_text()
    if url is None:
        text = text.split("[profile_options.firstmate]")[0]
    else:
        text = text.replace("https://example.com/firstmate.git", url)
    ship.write_toml(text)
    rc, doc, _ = ship.ctl("tools", "run", "--json")
    assert rc == 1 and doc["data"]["profiles"] == {"firstmate": "failed"}
    assert any(reason in m for m in doc["messages"])
    assert not ship.p("/home/agent/cloned").exists()
