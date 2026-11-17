"""The Herdr Fleet panel (herdr/mods/fleet/): what can be checked without a running Herdr.

Its logic, model.js, is tested under npm test (herdr/tests/fleet/). Here: the mod's files parse,
its install script installs it alone and never docks it or writes its settings, and the rules the
panel keeps that can be read from its source: it skips no question ship-fleet asks, asks none of
its own, takes no secret, and checks what is typed against ship-fleet's own patterns.
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


def mod_code():
    return "".join((FLEET / f).read_text("utf-8") for f in ("main.js", "model.js"))


def test_its_reads_and_where_its_actions_run():
    """It reads with five offline or local ship-fleet verbs, every one through model.js's ask(),
    which quotes; everything else opens a terminal tab, and only model.js builds one."""
    code = mod_code()
    verbs = set(re.findall(r'ask\((?:host|this\.host), (?:st\.where|where), \["([a-z-]+)"', code))
    assert verbs == {"ls", "attention", "about", "signins", "ca"}
    assert code.count('exec("ssh"') == 1  # model.js's ask(), which quotes
    assert not re.search(r"catch\s*(\([^)]*\))?\s*\{\s*\}", code)  # no failure is swallowed
    main = (FLEET / "main.js").read_text("utf-8")
    model = (FLEET / "model.js").read_text("utf-8")
    assert "argv" not in main  # main.js runs no command of its own: it hands model.js a job
    assert '"tab.new"' not in main and '"space.new"' not in main
    assert model.count("argv: job.argv") == 2  # jobPlan(): tab.new, or space.new if none is local
    assert model.count('"space.new"') == 1
    assert set(re.findall(r'host\.exec\("([^"]+)"', model)) == {"ssh", "test", "xdg-open"}


SKIPPING = ["--yes", "--force", "--revoke-my-key", "--stdin"]


def test_no_flag_that_skips_a_question_is_anywhere_in_it():
    """The panel never passes --yes (destroy, rollback, update and reboot of many), --force,
    --revoke-my-key or --stdin: not in any file of the mod, not even as a word to refuse
    (model.js spells those without their dashes)."""
    for path in sorted(FLEET.glob("*.js")):
        code = path.read_text("utf-8")
        for flag in SKIPPING:
            assert flag not in code, f"{path.name}: {flag}"
        assert not re.search(r"""["'`]-y["'`]""", code), path.name


def test_it_asks_nothing_of_its_own_and_types_nothing_into_a_terminal():
    """Confirmations are ship-fleet's, typed by the person in the terminal: the panel has no
    question of its own that could be taken for one, and never writes into a terminal."""
    code = mod_code()
    assert not re.search(r"are you sure|\bconfirm", code, re.I)
    assert "terminal.paste" not in code and '"send"' not in code and "'send'" not in code
    hc_calls = re.findall(r"\.hc\(([^)]*)\)", code)
    assert hc_calls == ['["machine", "ls", "--json"]']


def test_nothing_secret_is_typed_into_it():
    """Its only inputs are a new Ship's name, tag, repository and size, a snapshot's name, a
    machine's name, public key and scope, and a device's name: no passphrase, password, code,
    token or private key, and nothing drawn masked."""
    main = (FLEET / "main.js").read_text("utf-8")
    assert "set_masked" not in main
    placeholders = re.findall(r'input\("([^"]*)"\)', main)
    assert placeholders == ["ship-07", "or a new tag", "owner/name (optional)", "vCPUs",
                            "memory MB", "disk GB", "snapshot name", "laptop",
                            "ssh-ed25519 AAAA... (the one line of its .pub file)",
                            "or type tag:TAG ship:NAME", "phone"]
    assert main.count("InputState.new(") == 1  # input(), above


def test_what_it_checks_is_what_ship_fleet_enforces():
    """Every pattern and limit model.js checks a typed value against is ship-fleet's own."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = ("const m = await import(process.argv[1]); console.log(JSON.stringify({"
              "NAME: m.NAME.source, TAG: m.TAG.source, REPO: m.REPO.source, "
              "SNAPSHOT: m.SNAPSHOT.source, SIGNIN: m.SIGNIN.source, SERVICE: m.SERVICE.source, "
              "SIZE: m.SIZE, SCOPE: m.SCOPE.source, DEVICE: m.DEVICE.source, LOGINS: m.LOGINS, "
              "LOGIN_CHOICES: m.LOGIN_CHOICES}))")
    r = subprocess.run([node, "--input-type=module", "-e", script, (FLEET / "model.js").as_uri()],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    js = {k: (v.replace("\\/", "/") if isinstance(v, str) else v)
          for k, v in json.loads(r.stdout).items()}

    from fleetledger.drivers.base import SNAPSHOT_NAME
    from fleetledger.lifecycle import size_limits

    from fleetledger import ca, github, ops, signinreg
    from fleetledger.web import devices

    ships_toml = json.loads((ROOT / "contracts" / "ships.toml.schema.json").read_text("utf-8"))
    signin = json.loads((ROOT / "contracts" / "fleetledger-signin.schema.json").read_text("utf-8"))
    assert js["NAME"] == ops.NAME_RE.pattern
    assert js["TAG"] == ships_toml["$defs"]["tag"]["pattern"]
    assert js["REPO"] == github.REPO_RE.pattern
    assert js["SNAPSHOT"] == SNAPSHOT_NAME.pattern
    assert js["SIGNIN"] == signinreg.ID.pattern
    assert js["SERVICE"] == signin["properties"]["provider"]["pattern"]
    assert js["SIZE"] == {k: list(v) for k, v in size_limits().items()}
    assert js["SCOPE"] == ca.SCOPE.pattern
    assert js["DEVICE"] == devices.NAME_RE.pattern
    assert js["LOGINS"] == list(ca.LOGINS)
    for choice in js["LOGIN_CHOICES"]:
        ca.parse_logins(choice)  # each one ship-fleet trust --logins takes


def test_a_key_it_sends_is_one_ship_fleet_takes_as_a_line():
    """Every public key checkPublicKey lets through is an ed25519 line ship-fleet trust --key
    reads as a line (not a path) and accepts, and carries no comment."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    blob = "AAAAC3NzaC1lZDI1NTE5AAAAI" + "B" + "x" * 42
    pasted = [f"ssh-ed25519 {blob}", f"ssh-ed25519 {blob} me@laptop\n",
              f"ssh-ed25519\t{blob}\tit's \"mine\"", f"  ssh-ed25519 {blob}\r\n"]
    script = ("const m = await import(process.argv[1]); "
              "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(m.checkPublicKey)))")
    r = subprocess.run([node, "--input-type=module", "-e", script, (FLEET / "model.js").as_uri(),
                        json.dumps(pasted)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr

    from fleetledger import ca, trustcmd

    for key in json.loads(r.stdout):
        assert key == f"ssh-ed25519 {blob}"
        assert ca.ED25519_LINE.match(key)
        assert trustcmd._read_key(key) == key


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
