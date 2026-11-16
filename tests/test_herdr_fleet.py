"""The Herdr Fleet panel (herdr/mods/fleet/): what can be checked without a running Herdr.

Its logic, model.js, is tested under npm test (herdr/tests/fleet/). Here: the mod's files parse,
its install script installs it alone and never docks it or writes its settings, and the rules the
panel keeps that can be read from its source.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERDR = ROOT / "herdr"
FLEET = HERDR / "mods" / "fleet"
INSTALLED = ["README.md", "main.js", "mod.json", "model.js", "settings.example.json"]


def test_its_javascript_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for path in sorted(FLEET.glob("*.js")):
        r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 0, f"{path.relative_to(ROOT)}: {r.stderr}"


def test_its_json_files_parse_and_it_docks_nowhere():
    for path in FLEET.glob("*.json"):
        json.loads(path.read_text("utf-8"))
    assert json.loads((FLEET / "mod.json").read_text("utf-8"))["place"] == "none"
    example = json.loads((FLEET / "settings.example.json").read_text("utf-8"))
    assert set(example) == {"ssh", "identity_file"}


def test_the_logic_imports_neither_gpui_nor_herdr():
    """model.js imports nothing, so npm test can load it; only main.js talks to Herdr."""
    model = (FLEET / "model.js").read_text("utf-8")
    assert not re.search(r"^\s*import\b", model, re.M)
    assert "gpui" not in re.sub(r"//.*", "", model)
    main = (FLEET / "main.js").read_text("utf-8")
    assert re.findall(r'from "([^"]+)"', main) == ["gpui-kit", "gpui-base", "herdr", "./model.js"]


def test_it_changes_nothing_and_confirms_nothing():
    """F1 only reads: no --yes anywhere, no ship-fleet verb but its three reads, and every ssh
    call goes through model.js's quoting."""
    code = "".join((FLEET / f).read_text("utf-8") for f in ("main.js", "model.js"))
    assert "--yes" not in code
    verbs = set(re.findall(r'ask\(host, st\.where, \["([a-z-]+)"', code))
    assert verbs == {"ls", "attention", "about"}
    assert code.count('exec("ssh"') == 1  # model.js's ask(), which quotes
    assert not re.search(r"catch\s*(\([^)]*\))?\s*\{\s*\}", code)  # no failure is swallowed


def install(tmp_path, *extra):
    home = tmp_path / "herdr-app"
    env = {**os.environ, "HERDR_APP_HOME": str(home), "HOME": str(tmp_path / "home")}
    r = subprocess.run(["bash", str(HERDR / "install-fleet.sh"), *extra], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return home, r


def test_the_install_script_installs_this_mod_only(tmp_path):
    home, r = install(tmp_path)
    assert sorted(p.name for p in home.iterdir()) == ["mods"]
    assert sorted(p.name for p in (home / "mods").iterdir()) == ["fleet"]
    dest = home / "mods" / "fleet"
    assert sorted(p.name for p in dest.iterdir()) == INSTALLED
    for name in INSTALLED:
        assert (dest / name).read_bytes() == (FLEET / name).read_bytes(), name
    assert "settings.json" in r.stdout
    assert not (tmp_path / "home").exists()


def test_the_install_script_keeps_your_settings(tmp_path):
    home, _ = install(tmp_path)
    settings = home / "mods" / "fleet" / "settings.json"
    settings.write_text('{"ssh": "fleetledger@fleetledger.example.invalid"}\n', "utf-8")
    _, r = install(tmp_path)
    assert settings.read_text("utf-8") == '{"ssh": "fleetledger@fleetledger.example.invalid"}\n'
    assert "next:" not in r.stdout


def test_mod_json_goes_in_first():
    script = (HERDR / "install-fleet.sh").read_text("utf-8")
    order = re.search(r"for f in ([^;]+); do", script).group(1).split()
    assert order[0] == "mod.json"
    assert order.index("model.js") < order.index("main.js")
    assert sorted(order) == INSTALLED
