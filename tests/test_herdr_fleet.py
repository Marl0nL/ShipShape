"""The Herdr Fleet panel (herdr/mods/fleet/): what can be checked without a running Herdr.

Its logic, model.js, settings.js, agents.js and browsers.js (its link handler's), is tested
under npm test (herdr/tests/fleet/).
Here: the mod's files parse, its install script installs it alone and never docks it or writes its
settings, and the rules the panel keeps that can be read from its source: it skips no question
ship-fleet asks, asks none of its own, takes no secret, and checks what is typed against
ship-fleet's own patterns.
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
INSTALLED = ["README.md", "agents.js", "browsers.js", "link.js", "main.js", "mod.json", "model.js",
             "settings.example.json", "settings.js"]


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
    """model.js imports nothing, and settings.js, agents.js and browsers.js only model.js, so npm
    test can load them (and the Desk's link to model.js needs nothing beside it); only main.js and
    link.js (the link handler, which hands browsers.js Herdr's functions) talk to Herdr."""
    model = (FLEET / "model.js").read_text("utf-8")
    assert not re.search(r"^\s*import\b", model, re.M)
    assert "gpui" not in re.sub(r"//.*", "", model)
    for name in ("settings.js", "agents.js", "browsers.js"):
        code = (FLEET / name).read_text("utf-8")
        assert re.findall(r'^import .* from "([^"]+)";$', code, re.M) == ["./model.js"], name
        assert "gpui" not in re.sub(r"//.*", "", code), name
    main = (FLEET / "main.js").read_text("utf-8")
    assert re.findall(r'from "([^"]+)"', main) == ["gpui-kit", "gpui-base", "herdr", "./model.js",
                                                   "./settings.js", "./browsers.js", "./agents.js"]
    link = (FLEET / "link.js").read_text("utf-8")
    assert re.findall(r'from "([^"]+)"', link) == ["herdr", "./browsers.js"]


def mod_code():
    return "".join((FLEET / f).read_text("utf-8")
                   for f in ("main.js", "model.js", "settings.js", "agents.js", "browsers.js",
                             "link.js"))


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


SKIPPING = ["--yes", "--force", "--revoke-my-key", "--stdin", "--code", "--record-only"]


def test_no_flag_that_skips_a_question_is_anywhere_in_it():
    """The panel never passes --yes (destroy, rollback, update and reboot of many), --force,
    --revoke-my-key, --stdin or --code (a pairing's code is typed in its terminal): not in any
    file of the mod, not even as a word to refuse (model.js spells those without their
    dashes)."""
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
    """Its only inputs are a new Ship's name, tags, repository and size, and under Advanced its
    first agent's name and folder, host names, VM id and time zone (never the first prompt, which
    `new` treats as possibly secret), a snapshot's name, a machine's name, public key and scope,
    a device's name, in a Ship's Settings its session prefix, LAN guard addresses, reboot
    time, time zone, wake delay and message, and new tags, and in its Agents and size an
    autostart agent's name and folder and a new size, and in Browsers a tag and a browser
    profile's name (for settings.json, never sent): no passphrase, password, code, token or
    private key, and nothing drawn masked."""
    main = (FLEET / "main.js").read_text("utf-8")
    assert "set_masked" not in main
    placeholders = re.findall(r'input\("([^"]*)"\)', main)
    assert placeholders == ["ship-07", "or new tags", "owner/name (optional)", "vCPUs",
                            "memory MB", "disk GB", "main", "~/work", "default: NAME.local",
                            "the driver's choice", "FleetLedger's own", "snapshot name", "laptop",
                            "ssh-ed25519 AAAA... (the one line of its .pub file)",
                            "or type tag:TAG ship:NAME", "phone", "prefix", "none", "HH:MM",
                            "Area/City", "minutes", "the default message", "or new tags", "lead",
                            "~/work/lead", "vCPUs", "memory MB", "disk GB", "or type a tag",
                            "profile"]
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
              "LOGIN_CHOICES: m.LOGIN_CHOICES, PAIR_ID: m.PAIR_ID.source, MODES: m.MODES, "
              "GITHUB_SCOPES: m.GITHUB_SCOPES, AGENT: m.AGENT.source, FOLDER: m.FOLDER.source, "
              "HOST: m.HOST.source, TIMEZONE: m.TIMEZONE.source, VMID: m.VMID, "
              "FIRST_AGENT: m.FIRST_AGENT}))")
    r = subprocess.run([node, "--input-type=module", "-e", script, (FLEET / "model.js").as_uri()],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    js = {k: (v.replace("\\/", "/") if isinstance(v, str) else v)
          for k, v in json.loads(r.stdout).items()}

    from fleetledger.drivers.base import SNAPSHOT_NAME
    from fleetledger.lifecycle import size_limits
    from fleetledger.web import devices

    from fleetledger import ca, github, ops, signinreg

    ships_toml = json.loads((ROOT / "contracts" / "ships.toml.schema.json").read_text("utf-8"))
    signin = json.loads((ROOT / "contracts" / "fleetledger-signin.schema.json").read_text("utf-8"))
    assert js["NAME"] == ops.NAME_RE.pattern
    assert js["TAG"] == ships_toml["$defs"]["tag"]["pattern"] == ops.TAG_RE.pattern
    assert js["REPO"] == github.REPO_RE.pattern
    assert js["SNAPSHOT"] == SNAPSHOT_NAME.pattern
    assert js["SIGNIN"] == signinreg.ID.pattern
    assert js["SERVICE"] == signin["properties"]["provider"]["pattern"]
    assert js["SIZE"] == {k: list(v) for k, v in size_limits().items()}
    assert js["SCOPE"] == ca.SCOPE.pattern
    assert js["DEVICE"] == devices.NAME_RE.pattern
    assert js["LOGINS"] == list(ca.LOGINS)
    from fleetledger import pairing

    assert js["PAIR_ID"] == f"^{pairing.ID_RE.pattern}$"
    for choice in js["LOGIN_CHOICES"]:
        ca.parse_logins(choice)  # each one ship-fleet trust --logins takes

    # New Ship's advanced options.
    from fleetledger.overlay import HOST_RE
    from fleetledger.seed.shiptoml import TIMEZONE_RE
    from fleetledger.web import newship

    from fleetledger import ghscopes

    ship_toml = json.loads((ROOT / "contracts" / "ship.toml.schema.json").read_text("utf-8"))
    assert js["MODES"] == list(ops.MODES) == ship_toml["$defs"]["permissionMode"]["enum"]
    assert js["GITHUB_SCOPES"] == list(ghscopes.IDS)
    assert js["AGENT"] == ops.AGENT_NAME_RE.pattern == ship_toml["$defs"]["agentName"]["pattern"]
    cwd = ship_toml["properties"]["autostart"]["items"]["properties"]["cwd"]["pattern"]
    assert js["FOLDER"] == cwd + "[^:]*$"  # and no ":", which parse_autostart splits at
    assert js["HOST"] == HOST_RE.pattern
    assert js["TIMEZONE"] == TIMEZONE_RE.pattern
    vmid = ships_toml["properties"]["fleet"]["properties"]["self"]["properties"]["vmid"]
    assert js["VMID"] == [vmid["minimum"], vmid["maximum"]]
    web_check = f"{vmid['minimum']} <= int(vmid) <= {vmid['maximum']}"
    assert web_check in Path(newship.__file__).read_text("utf-8")
    web = newship.FormChoices
    assert js["FIRST_AGENT"] == {"name": web.agent, "folder": web.cwd}
    assert web.agent_on is True  # the web form's first agent is on by default


def test_the_new_ship_choices_it_reads_are_fleetledgers():
    """New Ship's driver, image, tools and profiles: the ids it accepts from FleetLedger's lists
    are ships.toml's, and the shapes it reads are the ones ship-fleet prints."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = ("const m = await import(process.argv[1]); console.log(JSON.stringify({"
              "TOOL: m.TOOL.source, IMAGE: m.IMAGE.source, DRIVERS: m.DRIVERS, "
              "SCHEMA_TOOLS: m.SCHEMA_TOOLS, SCHEMA_IMAGES: m.SCHEMA_IMAGES, "
              "SCHEMA_DEFAULTS: m.SCHEMA_DEFAULTS}))")
    r = subprocess.run([node, "--input-type=module", "-e", script, (FLEET / "model.js").as_uri()],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)

    from fleetledger import cli, commands, images, newdefaults

    ships_toml = json.loads((ROOT / "contracts" / "ships.toml.schema.json").read_text("utf-8"))
    defs = ships_toml["$defs"]
    tool = js["TOOL"].replace("\\/", "/")  # a JS regex literal escapes its /
    assert tool == defs["toolId"]["pattern"] == defs["profileId"]["pattern"]
    assert js["IMAGE"] == defs["imageRef"]["pattern"]
    assert js["DRIVERS"] == defs["driverId"]["enum"] == list(images.DRIVERS)
    new = next(a for a in cli.build_parser()._subparsers._group_actions[0].choices["new"]._actions
               if a.dest == "driver")
    assert list(new.choices) == js["DRIVERS"]
    assert js["SCHEMA_TOOLS"] == commands.TOOLS_SCHEMA
    assert js["SCHEMA_IMAGES"] == images.SCHEMA
    assert js["SCHEMA_DEFAULTS"] == newdefaults.SCHEMA
    for name, version in (("fleetledger-tools", js["SCHEMA_TOOLS"]),
                          ("fleetledger-images", js["SCHEMA_IMAGES"]),
                          ("fleetledger-defaults", js["SCHEMA_DEFAULTS"])):
        schema = json.loads((ROOT / "contracts" / f"{name}.schema.json").read_text("utf-8"))
        assert schema["properties"]["schema"]["const"] == version


def test_a_first_agent_it_sends_is_one_ship_fleet_takes():
    """Every first agent checkFirstAgent lets through, `new --autostart` reads back as that name,
    folder and mode (and `set autostart.add=` takes it too); every one it refuses, they refuse."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    tried = [["", "", ""], ["lead", "/srv/work", "plan"], ["agent_2-b", "~/a folder/it's", "auto"],
             ["main", "~/work", "bypassPermissions"], ["x" * 32, "/", ""],
             ["Main", "", ""], ["2nd", "", ""], ["", "work", ""], ["", "~/a:b", ""],
             ["", "", "yolo"], ["a:b", "", ""], ["x" * 33, "", ""], ["", "~work", ""]]
    script = ("const m = await import(process.argv[1]); console.log(JSON.stringify("
              "JSON.parse(process.argv[2]).map((a) => { try { return m.checkFirstAgent(...a); }"
              " catch { return null; } })))")
    r = subprocess.run([node, "--input-type=module", "-e", script, (FLEET / "model.js").as_uri(),
                        json.dumps(tried)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr

    from fleetledger.commands import parse_autostart
    from fleetledger.errors import FleetError

    from fleetledger import ops

    def taken(name, folder, mode):
        value = f"{name}:{folder}" + (f":{mode}" if mode else "")
        try:
            agent = parse_autostart([value])[0]
            ops.parse_autostart_change("autostart.add", value)
        except FleetError:
            return None
        same = (agent.name, agent.cwd, agent.mode) == (name, folder, mode or None)
        return value if same else None

    for (name, folder, mode), sent in zip(tried, json.loads(r.stdout), strict=True):
        if sent is None:
            assert taken(name or "main", folder or "~/work", mode) is None, (name, folder, mode)
        else:
            assert taken(*(sent.split(":") + [""])[:3]) == sent, sent


def test_a_key_it_sends_is_one_ship_fleet_takes_as_a_line():
    """Every public key checkPublicKey lets through is an ed25519 line ship-fleet trust --key
    reads as a line (not a path) and accepts, and carries no comment."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    blob = "AAAAC3NzaC1lZDI1NTE5AAAAI" + "B" + "x" * 42
    pasted = [f"ssh-ed25519 {blob}", f"ssh-ed25519 {blob} my-laptop\n",
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
    assert order.index("settings.js") < order.index("main.js")
    assert order.index("agents.js") < order.index("main.js")
    assert order.index("browsers.js") < order.index("main.js")
    assert order.index("browsers.js") < order.index("link.js")
    assert sorted(order) == INSTALLED


def settings_js(expr, *args):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = ("const m = await import(process.argv[1]); "
              f"console.log(JSON.stringify(({expr})(m, ...process.argv.slice(2))))")
    r = subprocess.run([node, "--input-type=module", "-e", script,
                        (FLEET / "settings.js").as_uri(), *args],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_its_settings_are_the_web_settings_tabs():
    """The Settings view's groups, fields, labels, choices and hints are the web tab's
    (web/shipsettings.py GROUPS and SSH_LISTEN), each key a `ship-fleet set` key in the same
    place of the status settings, and its choices the ones `set` takes; and what a typed value
    is checked against is what ops.parse_assignments checks it against."""
    js = settings_js("(m) => ({GROUPS: m.GROUPS, SSH_LISTEN: m.SSH_LISTEN, PATHS: m.PATHS, "
                     "CIDR: m.CIDR.source, TIME: m.TIME.source, PREFIX: m.PREFIX.source, "
                     "WAKE_DELAY: m.WAKE_DELAY, WAKE_TEXT_MAX: m.WAKE_TEXT_MAX})")
    from fleetledger.web import shipsettings

    from fleetledger import ops

    def fields(fs):
        return [{"key": f.key, "label": f.label, "hint": f.hint,
                 "choices": None if f.choices is None else [list(c) for c in f.choices]}
                for f in fs]

    keep = ("key", "label", "hint", "choices")
    assert [[t, fields(fs)] for t, fs in shipsettings.GROUPS] == [
        [t, [{k: f[k] for k in keep} for f in fs]] for t, fs in js["GROUPS"]]
    js_ssh = {k: js["SSH_LISTEN"][k] for k in keep}
    assert fields([shipsettings.SSH_LISTEN]) == [js_ssh]
    # Every `set` key but tags (the view sends `ship-fleet tag`), in the status settings' place.
    assert set(js["PATHS"]) == set(ops.SET_KEYS) - {"tags"}
    for key, path in js["PATHS"].items():
        assert ops.SET_KEYS[key][0] == path, key
    for _, fs in js["GROUPS"]:
        for f in fs:
            allowed = ops.SET_KEYS[f["key"]][1]
            if f["choices"] is not None:
                assert [v for v, _ in f["choices"]] == list(allowed), f["key"]
            else:
                assert allowed is None, f["key"]
    assert js["CIDR"].replace("\\/", "/") == ops.CIDR_RE.pattern
    assert js["TIME"] == ops.TIME_RE.pattern
    assert js["PREFIX"] == ops.PREFIX_RE.pattern
    source = Path(ops.__file__).read_text("utf-8")
    low, high = js["WAKE_DELAY"]
    assert f"{low} <= int(val) <= {high}" in source
    assert f"len(val) > {js['WAKE_TEXT_MAX']}" in source


def test_a_setting_it_sends_is_one_ship_fleet_set_takes():
    """Every value assignment() lets through, ops.parse_assignments takes as it is; every one it
    refuses, parse_assignments refuses too."""
    tried = [
        ["session_prefix", "lab-ship.1"], ["session_prefix", "-x"], ["session_prefix", ""],
        ["session_prefix", "a" * 63], ["session_prefix", "a" * 64], ["session_prefix", "a b"],
        ["lan_guard_allow", "192.0.2.0/24, 2001:db8::/32"], ["lan_guard_allow", ""],
        ["lan_guard_allow", "192.0.2.7 198.51.100.0/24"], ["lan_guard_allow", "host.example.com"],
        ["reboot_time", "04:30"], ["reboot_time", "4:30"], ["reboot_time", "24:00"],
        ["timezone", "Europe/Berlin"], ["timezone", "UTC"], ["timezone", "../etc"],
        ["timezone", ""], ["wake_delay_minutes", "15"], ["wake_delay_minutes", "0"],
        ["wake_delay_minutes", "1441"], ["wake_delay_minutes", "1440"], ["wake_delay_minutes", "x"],
        ["wake_text", "Back {delay} after a boot: carry on."], ["wake_text", ""],
        ["wake_text", "x" * 1000], ["wake_text", "x" * 1001], ["wake_text", "two\nlines"],
        ["remote_control", "all"], ["remote_control", "maybe"], ["default_mode", "plan"],
        ["allow_bypass", "true"], ["sudo", "yes"], ["update_channel", "stable"],
        ["lan_guard", "false"], ["auto_reboot", "true"], ["wake_after_boot", "false"],
    ]
    # A Ship that reports nothing: every value given is sent (blank is "unchanged").
    sent = settings_js("(m, t) => JSON.parse(t).map(([k, v]) => { try { return m.assignment("
                       "{name: 'ship-01', tags: [], settings: null}, k, v); } "
                       "catch { return false; } })", json.dumps(tried))
    from fleetledger.errors import FleetError

    from fleetledger import ops

    for (key, value), pair in zip(tried, sent, strict=True):
        if pair is None:
            assert value.strip() == "", (key, value)  # unchanged: nothing to send
            continue
        try:
            took = ops.parse_assignments([f"{key}={value}"])
        except FleetError:
            took = None
        if pair is False:
            assert took is None, (key, value)
        else:
            k, _, v = pair.partition("=")
            assert ops.parse_assignments([pair]) == [f"{ops.SET_KEYS[k][0]}={v}"], pair


def agents_js(expr, *args):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = ("const m = await import(process.argv[1]); "
              f"console.log(JSON.stringify(await ({expr})(m, ...process.argv.slice(2))))")
    r = subprocess.run([node, "--input-type=module", "-e", script,
                        (FLEET / "agents.js").as_uri(), *args],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_agent_states_it_reads_are_the_status_contracts():
    """The Agents and size view reads herdr's agents, the agent record and remote_control in the
    shapes ship-status.schema.json gives them; Sign out is offered on the rows the web UI's Sign
    out is (signinlist.SIGNED_IN_STATES)."""
    js = agents_js("(m) => ({AGENT_STATES: [...m.AGENT_STATES], RECORD_STATES: "
                   "[...m.RECORD_STATES], REMOTE_CONTROL: [...m.REMOTE_CONTROL]})")
    status = json.loads((ROOT / "contracts" / "ship-status.schema.json").read_text("utf-8"))
    herdr_agent = status["properties"]["herdr"]["properties"]["agents"]["items"]["properties"]
    record_agent = status["properties"]["agent_record"]["properties"]["agents"]["items"]
    assert js["AGENT_STATES"] == herdr_agent["state"]["enum"]
    assert js["RECORD_STATES"] == record_agent["properties"]["state"]["enum"]
    assert js["REMOTE_CONTROL"] == status["$defs"]["remoteControl"]["enum"]
    for key in ("name", "state", "mode", "autostart", "remote_control"):
        assert key in record_agent["properties"], key

    from fleetledger import signinlist

    model = (FLEET / "model.js").read_text("utf-8")
    listed = re.search(r"export const SIGNED_IN_STATES = (\[[^\]]*\]);", model).group(1)
    assert json.loads(listed) == list(signinlist.SIGNED_IN_STATES)


def _parsed(argv):
    """`ship-fleet ARGV` as cli.main's parser reads it (SystemExit if it refuses it)."""
    from fleetledger import cli

    return cli.build_parser().parse_args(argv)


def test_every_agents_and_size_line_is_one_ship_fleet_takes():
    """Each line the Agents and size view builds, ship-fleet's parser and checks take as they
    are: the agent, mode and fresh restart; Remote Control on and off; an autostart agent added
    or removed (as ops.parse_autostart_change reads it back); a resize."""
    tried = {
        "restart": [["main", "", False], ["main", "plan", True], ["builder", "auto", False]],
        "add": [["lead", "~/work/a folder", "plan"], ["agent_2-b", "/srv/x", ""],
                ["x" * 32, "~/it's", "dontAsk"], ["lead", "~/a:b", ""], ["Lead", "~/x", ""],
                ["lead", "work", ""]],  # a mode it does not have: agents.test.js
        "resize": [["8", "16384", "", True], ["", "", "80", False], ["64", "262144", "4096", True],
                   ["65", "", "", False], ["", "4095", "", False], ["", "", "19", False]],
    }
    built = agents_js(
        "(m, t) => { const tried = JSON.parse(t);"
        " const ship = { name: 'ship-01', tags: [], state: 'running', problem: null,"
        "   herdrAgents: [{ kind: 'claude', name: 'main', state: 'idle', mode: 'auto' },"
        "                 { kind: 'claude', name: 'builder', state: 'working', mode: 'plan' }],"
        "   record: { agents: [{ name: 'main', state: 'running', mode: 'auto', autostart: true,"
        "                        remote_control: true }] },"
        "   settings: { claude: { allow_bypass: true, default_mode: 'auto' }, autostart: [] },"
        "   host: { cpus: 4, mem_total_mb: 7951, disk_total_gb: 38.6 } };"
        " const st = { where: { ssh: 'fleetledger@fleetledger.example.invalid' },"
        "   about: { copy: 'active', vault: 'off' }, shipsFrom: 'FleetLedger', older: false };"
        " const words = (j) => j.job ? j.job.remote : null;"
        " return {"
        "  restart: tried.restart.map(([a, mo, f]) => words(m.restartJob(st, ship,"
        "    { agent: a, mode: mo, fresh: f }))),"
        "  rc: [words(m.remoteControlJob(st, ship, 'main', false))],"
        "  add: tried.add.map(([n, f, mo]) => words(m.autostartAddJob(st, ship,"
        "    { name: n, folder: f, mode: mo }))),"
        "  remove: [words(m.autostartRemoveJob(st, { ...ship, settings: { ...ship.settings,"
        "    autostart: [{ name: 'main', cwd: '~/work' }] } }, 'main'))],"
        "  resize: tried.resize.map(([c, mem, d, r]) => words(m.resizeJob(st, ship,"
        "    { cores: c, memory_mb: mem, disk_gb: d, restart: r }))) }; }",
        json.dumps(tried))

    import shlex

    from fleetledger.errors import FleetError
    from fleetledger.lifecycle import check_size

    from fleetledger import ops

    def argv(remote):
        words = shlex.split(remote)
        assert words[0] == "ship-fleet", remote
        return words[1:]

    for line, (agent, mode, fresh) in zip(built["restart"], tried["restart"], strict=True):
        args = _parsed(argv(line))
        assert (args.command, args.action, args.ship, args.agents) == ("agent", "restart",
                                                                        "ship-01", [agent])
        assert (args.mode or "", args.fresh, args.force, args.yes) == (mode, fresh, False, False)
        ops.check_restart(args.agents, everyone=args.all, mode=args.mode)
    args = _parsed(argv(built["rc"][0]))
    assert (args.action, args.agents, args.off) == ("remote-control", ["main"], True)
    for line, (name, folder, mode) in zip(built["add"], tried["add"], strict=True):
        value = f"{name}:{folder}" + (f":{mode}" if mode else "")
        try:
            took = ops.parse_assignments([f"autostart.add={value}"])
        except FleetError:
            took = None
        if line is None:  # refused by both; the panel also refuses a folder with ":" in it,
            assert took is None or ":" in folder, value  # which could be read as a mode
            continue
        args = _parsed(argv(line))
        assert (args.command, args.args, args.yes) == ("set", ["ship-01", f"autostart.add={value}"],
                                                       False)
        assert took == [f"autostart.add={value}"], value
    args = _parsed(argv(built["remove"][0]))
    assert args.args == ["ship-01", "autostart.remove=main"]
    assert ops.parse_assignments(args.args[1:]) == ["autostart.remove=main"]
    for line, (cores, mem, disk, restart) in zip(built["resize"], tried["resize"], strict=True):
        size = {"cores": cores, "memory_mb": mem, "disk_gb": disk}
        try:
            wanted = check_size({k: int(v) for k, v in size.items() if v})
        except FleetError:
            wanted = None
        if line is None:
            assert wanted is None, size
            continue
        args = _parsed(argv(line))
        assert {k: v for k, v in (("cores", args.cores), ("memory_mb", args.memory_mb),
                                  ("disk_gb", args.disk_gb)) if v is not None} == wanted
        assert (args.restart, args.record_only, args.yes) == (restart, False, False)
