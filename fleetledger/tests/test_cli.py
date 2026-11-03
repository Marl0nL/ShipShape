"""ship-fleet (W10): every command, against a fake Ship behind a real SSH object."""

import copy
import io
import json
import stat
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from fleetledger.concierge import engine
from fleetledger.drivers import get_driver
from fleetledger.drivers.base import Outcome
from fleetledger.scan import Announcement
from fleetledger.seed import hostkey, iso
from fleetledger.ssh import SSH, Result, Unreachable

from fleetledger import __version__, cli

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parents[1] / "contracts" / "examples"
RUNNING = json.loads((EXAMPLES / "ship-status" / "running.json").read_text())
CLAUDE_CODE = "EXAMPLE-CODE-0123456789abcdef#example-state"


def result(command, ok=True, changed=(), data=None, error=None):
    return {
        "schema": "shipshape.ship-ctl-result/1", "command": command, "ok": ok,
        "changed": list(changed), "messages": [], "data": data or {}, "error": error,
    }


VERSION = result("version", data={"tooling": "0.1.0", "commit": "0123456789ab", "contracts": {
    "ship-status": 1, "ship-toml": 1, "ship-ctl-result": 1, "bundle": 1, "seed": 1,
    "runtime": 1}})


class FakeShip:
    """Answers remote commands the way a provisioned Ship would."""

    def __init__(self):
        self.calls = []
        self.claude_account = "work@example.com"
        self.down = set()
        self.status_override = {}
        self.inputs = []
        self.overlay = {}  # ship name -> overlay doc (ship-ctl overlay)
        self.overlay_fails = False

    def status(self, ship):
        doc = copy.deepcopy(self.status_override.get(ship.name, RUNNING))
        doc["ship"].update(name=ship.name, id=ship.id, tags=list(ship.tags or ()))
        return doc

    def __call__(self, ship, command):
        self.calls.append((ship.name, command))
        if ship.name in self.down:
            raise Unreachable(f"Cannot reach {ship.name}")
        if command == ["true"]:
            return 0, "", ""
        if command[:3] == ["sudo", "-n", "/usr/local/bin/ship-ctl"]:
            verb = command[3]
            if verb == "version":
                return 0, json.dumps(VERSION), ""
            if verb == "status":
                return 0, json.dumps(self.status(ship)), ""
            if verb == "logs":
                return 0, json.dumps(result("logs", data={"lines": ["line one", "line two"]})), ""
            if verb == "overlay":
                return self.overlay_call(ship, command[4:])
            return 0, json.dumps(result(verb, changed=["/etc/shipshape/ship.toml"])), ""
        agent_command = command[-1]
        if "claude auth status" in agent_command:
            return 0, json.dumps({"loggedIn": True, "email": self.claude_account}), ""
        return 0, "", ""


def _overlay_call(self, ship, args):
    action = args[0]
    current = self.overlay.get(ship.name)
    if action == "join":
        if self.overlay_fails:
            err = {"code": "overlay-failed", "message": "tailscale up failed: [key] invalid"}
            return 1, json.dumps(result("overlay", ok=False, error=err)), ""
        tags = [args[i + 1] for i, a in enumerate(args) if a == "--tag"]
        current = {"provider": "tailscale", "joined": True, "state": "running",
                   "name": f"{ship.name}.example-tailnet.ts.net", "addresses": ["192.0.2.10"],
                   "tags": tags, "ssh_listen": "any"}
        self.overlay[ship.name] = current
    elif action == "leave":
        current = self.overlay.pop(ship.name, None)
        current = current and {**current, "joined": False, "state": "off", "name": None,
                               "addresses": []}
    return 0, json.dumps(result("overlay", data={"overlay": current})), ""


FakeShip.overlay_call = _overlay_call


class ShipSSH(SSH):
    def __init__(self, inventory, ship_side):
        super().__init__(inventory)
        self.ship_side = ship_side

    def run(self, ship, command, *, input=None, timeout=60):
        if input and hasattr(self.ship_side, "inputs"):
            self.ship_side.inputs.append((ship.name, input))
        code, out, err = self.ship_side(ship, list(command))
        return Result(code, out.encode(), err, ship.names[0])


class Prompter:
    def __init__(self, code=CLAUDE_CODE):
        self.code = code
        self.shown = []

    def show_link(self, url, code, expires_in):
        self.shown.append(url)

    def ask_code(self):
        return self.code

    def waiting(self, remaining):
        pass

    def offer_live(self, reason, seen):
        return False


class FakeDriver:
    id = "proxmox"

    def __init__(self, fail=False):
        self.fail = fail
        self.created = []
        self.finished = []
        self.images = []
        self.destroyed = []
        self.snaps = []
        self.published = []
        self.removed_images = []

    def create(self, ship, seed, size, image=None):
        if self.fail:
            raise RuntimeError("clone failed")
        self.created.append((ship.name, seed.instance_id, dict(size)))
        self.images.append(image)
        return Outcome(done=True, messages=["Started VM 9101."],
                       driver_ref={"vmid": 9101, "seed": seed.instance_id})

    def status(self, ship):
        from fleetledger.drivers.base import PowerState

        self.polls = getattr(self, "polls", 0) + 1
        return PowerState("running", ["192.0.2.44"] if self.polls > 1 else [])

    def finish_enrolment(self, ship):
        self.finished.append(ship.name)
        return Outcome(done=True, messages=["Detached and deleted the seed."])

    def destroy(self, ship):
        self.destroyed.append(ship.name)
        return Outcome(done=True, messages=[f"Destroyed VM {ship.driver_ref.get('vmid')}."])

    def snapshot(self, ship, name, description=""):
        self.snaps.append(("take", ship.name, name, description))
        return Outcome(done=True, messages=[f"Took snapshot {name}."])

    def snapshots(self, ship):
        from fleetledger.drivers import Snapshot

        return [Snapshot("before", "2026-11-01T00:00:00Z", "a note")]

    def rollback(self, ship, name):
        self.snaps.append(("rollback", ship.name, name))
        return Outcome(done=True, messages=[f"Rolled back to {name}."])

    def delete_snapshot(self, ship, name):
        self.snaps.append(("rm", ship.name, name))
        return Outcome(done=True, messages=[f"Deleted {name}."])

    def publish_image(self, image):
        self.published.append(image)
        return Outcome(done=True, messages=["Published."], driver_ref={"template_vmid": 9181})

    def remove_image(self, image):
        self.removed_images.append((image.id, dict(image.ref)))
        return Outcome(done=True, messages=["Deleted the template."])


@pytest.fixture
def env(store, fake_repo, monkeypatch, tmp_path):
    ship_side = FakeShip()
    fake_driver = FakeDriver()
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setenv("FAKE_CLI_INPUT_LOG", str(tmp_path / "typed.log"))
    fixture = {"name": "claude.txt"}

    def spawner(argv):
        env.argvs.append(argv)
        return engine.spawn([sys.executable, str(HERE / "fake_cli.py"),
                             str(HERE / "fixtures" / "concierge" / fixture["name"])])

    def drivers(inventory, driver_id, **kw):
        return fake_driver if driver_id == "proxmox" else get_driver(inventory, driver_id, **kw)

    app = cli.App(
        out=out, err=err,
        store_factory=lambda: store,
        repo_factory=lambda: fake_repo,
        ssh_factory=lambda inv: ShipSSH(inv, ship_side),
        driver_factory=drivers,
        prompter_factory=lambda ship, label, open_browser: Prompter(),
        spawner=spawner,
        browse=lambda timeout: [],
        keyscan=lambda host: (_ for _ in ()).throw(AssertionError("keyscan")),
        stdin_isatty=lambda: False,
        resolves=lambda host: False,
        sleep=lambda s: None,
    )

    class Env:
        pass

    env = Env()
    env.app, env.store, env.ship, env.driver = app, store, ship_side, fake_driver
    env.argvs = []

    def run(*argv):
        out.seek(0), out.truncate(), err.seek(0), err.truncate()
        code = cli.main(list(argv), app)
        return code, out.getvalue(), err.getvalue()

    env.run = run
    return env


# --- the parser --------------------------------------------------------------


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for command in cli.COMMANDS:
        assert f"    {command} " in text
    for command in cli.COMMANDS:
        with pytest.raises(SystemExit) as exc:
            cli.main([command, "--help"])
        assert exc.value.code == 0
        assert len(capsys.readouterr().out) > 100


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"ship-fleet {__version__}"


def test_no_arguments_prints_help(env):
    code, out, _ = env.run()
    assert code == 0 and "commands:" in out


# --- init --------------------------------------------------------------------


def test_init(home, tmp_path):
    from fleetledger.store import TomlFileStore

    store = TomlFileStore(home / "config" / "ships.toml")
    out = io.StringIO()
    app = cli.App(out=out, err=io.StringIO(), store_factory=lambda: store)
    admin = tmp_path / "newkeys" / "fleet_admin"
    argv = ["init", "--admin-key", str(admin), "--no-passphrase",
            "--agent-key", str(home / "keys" / "you.pub")]
    assert cli.main(argv, app) == 0
    assert stat.S_IMODE(admin.stat().st_mode) == 0o600
    inv = store.load()
    assert inv.admin_key == admin and inv.ships() == []
    assert "ssh-add" in out.getvalue()
    assert cli.main(argv, app) == 1  # already set up

    bad = TomlFileStore(home / "config" / "other.toml")
    app2 = cli.App(out=io.StringIO(), err=(err := io.StringIO()), store_factory=lambda: bad)
    code = cli.main(["init", "--admin-key", str(admin), "--agent-key", str(admin)], app2)
    assert code == 2 and "private key" in err.getvalue()


# --- new ---------------------------------------------------------------------


def test_new_manual_bypass_ship(env, home):
    code, out, err = env.run("new", "ship-03", "--mode", "bypassPermissions",
                             "--tools", "tasks-axi", "--autostart", "main:~/work/main:auto",
                             "--driver", "manual")
    assert code == 0, err
    ship = env.store.load().ship("ship-03")
    assert ship.tags is None  # no --tag: defaults.tags apply
    assert env.store.load().effective_tags(ship) == ("work",)
    assert ship.claude == {"default_mode": "bypassPermissions", "allow_bypass": True}
    assert ship.tools == ("gh-axi", "ship-axi", "tasks-axi")
    assert ship.enrolled is None and ship.driver_ref["seed"].startswith("ship-03-")
    assert hostkey.fingerprint(ship.host_key) in out
    assert "CIDATA" in out and "ship-fleet status ship-03 --wait" in out

    seed = home / "data" / "seeds" / f"{ship.driver_ref['seed']}.iso"
    assert stat.S_IMODE(seed.stat().st_mode) == 0o600
    _, files = iso.read(seed.read_bytes())
    user = yaml.safe_load(files["user-data"])
    assert user["ssh_keys"]["ed25519_public"] == ship.host_key
    ship_toml = tomllib.loads(user["write_files"][0]["content"])
    assert ship_toml["claude"]["default_mode"] == "bypassPermissions"
    assert ship_toml["claude"]["allow_bypass"] is True
    # No prompt given: none is written, and the Ship sends its own default.
    assert ship_toml["autostart"] == [{"name": "main", "cwd": "~/work/main", "mode": "auto"}]
    assert env.run("new", "ship-03")[0] == 1  # already there


def test_new_autostart_prompt(env, home):
    code, _, err = env.run("new", "ship-08", "--autostart", "a:~/work/a",
                           "--autostart-prompt", "Run the tests.")
    assert code == 0, err
    seed = next((home / "data" / "seeds").glob("ship-08-*.iso"))
    _, files = iso.read(seed.read_bytes())
    ship_toml = tomllib.loads(yaml.safe_load(files["user-data"])["write_files"][0]["content"])
    assert ship_toml["autostart"] == [{"name": "a", "cwd": "~/work/a", "prompt": "Run the tests."}]
    # An empty prompt is the same as none: the Ship's default applies.
    assert env.run("new", "ship-09", "--autostart", "b:~/work/b", "--autostart-prompt", "")[0] == 0
    seed = next((home / "data" / "seeds").glob("ship-09-*.iso"))
    _, files = iso.read(seed.read_bytes())
    ship_toml = tomllib.loads(yaml.safe_load(files["user-data"])["write_files"][0]["content"])
    assert ship_toml["autostart"] == [{"name": "b", "cwd": "~/work/b"}]


def test_new_proxmox_waits_and_enrols(env, home):
    code, out, err = env.run("new", "ship-04", "--tag", "work", "--driver", "proxmox",
                             "--cores", "2")
    assert code == 0, err
    assert env.driver.created[0][2] == {"cores": 2, "memory_mb": 8192, "disk_gb": 40}
    ship = env.store.load().ship("ship-04")
    assert ship.enrolled and ship.driver_ref == {"vmid": 9101}
    # ship-04.local does not resolve here: the hypervisor's address is the fallback.
    assert ship.names == ("ship-04.local", "192.0.2.44")
    assert env.driver.finished == ["ship-04"]
    assert not list((home / "data" / "seeds").glob("*.iso"))
    assert "ship-04 is enrolled." in out and "ship-fleet login ship-04 all" in out


def test_new_proxmox_keeps_names_that_resolve(env):
    env.app.resolves = lambda host: host == "ship-06.local"
    assert env.run("new", "ship-06", "--driver", "proxmox")[0] == 0
    assert env.store.load().ship("ship-06").names == ("ship-06.local",)


def test_new_proxmox_waits_for_a_late_mdns_name(env):
    """The announcement can come up a little after the address: no fallback then."""
    tries = []
    env.app.resolves = lambda host: tries.append(host) or len(tries) >= 3
    assert env.run("new", "ship-10", "--driver", "proxmox")[0] == 0
    assert env.store.load().ship("ship-10").names == ("ship-10.local",)


def test_new_proxmox_without_an_address(env):
    from fleetledger.drivers.base import PowerState

    env.driver.status = lambda ship: PowerState("running", [])
    code, out, err = env.run("new", "ship-11", "--driver", "proxmox")
    assert code == 0 and "reported no address" in err
    assert env.store.load().ship("ship-11").names == ("ship-11.local",)


def test_resolves_uses_the_system_resolver():
    assert cli._resolves("localhost")
    assert not cli._resolves("ship-01.invalid")


def test_new_failure_leaves_nothing(env, home):
    env.driver.fail = True
    with pytest.raises(RuntimeError):
        env.run("new", "ship-05", "--driver", "proxmox")
    assert not env.store.load().has_ship("ship-05")
    assert not list((home / "data" / "seeds").glob("*.iso"))


def _seed_ship_toml(home, name):
    seed = next((home / "data" / "seeds").glob(f"{name}-*.iso"))
    _, files = iso.read(seed.read_bytes())
    return tomllib.loads(yaml.safe_load(files["user-data"])["write_files"][0]["content"])


def test_new_takes_this_computers_timezone(env, home, monkeypatch):
    """So updates.reboot_time means the owner's local time on the Ship."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert env.run("new", "ship-08")[0] == 0
    assert _seed_ship_toml(home, "ship-08")["ship"]["timezone"] == "Europe/Berlin"
    assert env.run("new", "ship-09", "--timezone", "America/New_York")[0] == 0
    assert _seed_ship_toml(home, "ship-09")["ship"]["timezone"] == "America/New_York"


def test_new_rejects_bad_input(env):
    assert env.run("new", "Ship_5")[0] == 1
    code, _, err = env.run("new", "ship-05", "--autostart", "main")
    assert code == 1 and "NAME:CWD" in err
    code, _, err = env.run("new", "ship-05", "--tools", "nope")
    assert code == 2 and "Unknown tools: nope" in err
    for zone in ("../../etc/shadow", "Mars/Olympus_Mons"):
        code, _, err = env.run("new", "ship-05", "--timezone", zone)
        assert code == 1 and "IANA zone name" in err
    assert not env.store.load().has_ship("ship-05")


# --- adopt and scan ----------------------------------------------------------


@pytest.fixture
def stranger(tmp_path):
    key = hostkey.generate("ship-07")
    return " ".join(key.public.split()[:2]), key.fingerprint


def test_adopt_with_fingerprint(env, stranger):
    key, fp = stranger
    env.app.keyscan = lambda host: key
    env.ship.status_override["ship-07"] = RUNNING
    code, out, err = env.run("adopt", "ship-07", "--host", "192.0.2.17", "--fingerprint", fp)
    assert code == 0, err
    ship = env.store.load().ship("ship-07")
    assert ship.host_key.startswith(key) and ship.names == ("192.0.2.17",)
    assert ship.enrolled and "Adopted ship-07" in out


def test_adopt_refuses_wrong_or_unchecked_fingerprint(env, stranger):
    key, _ = stranger
    env.app.keyscan = lambda host: key
    code, _, err = env.run("adopt", "ship-07", "--fingerprint", "SHA256:nope")
    assert code == 1 and "not the fingerprint" in err
    code, _, err = env.run("adopt", "ship-07")
    assert code == 1 and "--fingerprint" in err
    env.app.stdin_isatty = lambda: True
    env.app.confirm = lambda prompt: "no"
    assert env.run("adopt", "ship-07")[0] == 1
    assert not env.store.load().has_ship("ship-07")


def test_adopt_checks_the_ships_own_name(env, stranger):
    key, fp = stranger
    env.app.keyscan = lambda host: key
    original = env.ship.status

    def lying(ship):
        doc = original(ship)
        doc["ship"]["name"] = "ship-01"
        return doc

    env.ship.status = lying
    code, _, err = env.run("adopt", "ship-07", "--fingerprint", fp)
    assert code == 1 and "says it is ship-01" in err


def test_adopt_replace_repins(env, stranger):
    key, fp = stranger
    env.app.keyscan = lambda host: key
    assert env.run("adopt", "ship-01", "--fingerprint", fp)[0] == 1  # needs --replace
    code, out, _ = env.run("adopt", "ship-01", "--fingerprint", fp, "--replace")
    assert code == 0 and "Re-pinned ship-01" in out
    assert env.store.load().ship("ship-01").host_key.startswith(key)


def test_scan(env):
    inv = env.store.load()
    env.app.browse = lambda timeout: [
        Announcement("ship-01", "ship-01.local", 22, ("192.0.2.11",),
                     {"name": "ship-01", "id": inv.ship("ship-01").id}),
        Announcement("ship-09", "ship-09.local", 22, ("192.0.2.19",), {"name": "ship-09"}),
    ]
    code, out, _ = env.run("scan", "--timeout", "0")
    assert code == 0
    lines = out.splitlines()
    assert "known" in lines[1]
    assert "ship-fleet adopt ship-09 --host ship-09.local" in lines[2]
    env.app.browse = lambda timeout: []
    assert "No Ships" in env.run("scan")[1]


# --- ls and status -----------------------------------------------------------


def test_ls(env):
    env.ship.down.add("ship-02")
    code, out, _ = env.run("ls")
    assert code == 0
    header, one, two = out.splitlines()
    assert header.split()[:4] == ["SHIP", "TAGS", "STATE", "AGENTS"]
    assert one.startswith("ship-01") and "running" in one and "1 waiting · 1 working" in one
    assert "OK" in one and "Missing" in one
    assert two.startswith("ship-02") and "unreachable" in two

    code, out, _ = env.run("ls", "--json", "--tag", "work")
    data = json.loads(out)
    assert [d["name"] for d in data] == ["ship-01"] and data[0]["state"] == "running"

    env.ship.down.add("ship-01")
    code, out, _ = env.run("ls", "--offline")
    assert "running" in out.splitlines()[1]  # from the cache written above


def test_ls_flags_wrong_account(env):
    doc = copy.deepcopy(RUNNING)
    doc["auth"]["claude"]["account"] = "you@example.com"
    env.ship.status_override["ship-01"] = doc
    out = env.run("ls", "--tag", "work")[1]
    assert "Wrong account" in out.splitlines()[1]


def test_status(env):
    code, out, _ = env.run("status", "ship-01")
    assert code == 0
    assert "Sign-ins:" in out and "tag expects work@example.com" in out
    assert "builder" in out and "bypassPermissions" in out
    assert "herdr 0.9.5 (running); last resume 2026-01-01T00:00:00Z (2 resumed)" in out
    assert "Tooling 0.1.0 (0123456789ab); provisioned ok" in out
    assert "3 OS updates pending" in out
    env.ship.down.add("ship-01")
    code, out, _ = env.run("status", "ship-01")
    assert code == 1 and "unreachable" in out


def test_status_wait_enrols(env):
    with env.store.edit() as ed:
        ed.set_field("ship-01", "driver", "proxmox")
    code, out, _ = env.run("status", "ship-01", "--wait", "--json")
    assert code == 0 and json.loads(out)["state"] == "running"
    assert env.store.load().ship("ship-01").enrolled


# --- login and logout --------------------------------------------------------


def test_login(env):
    code, out, err = env.run("login", "ship-01", "claude")
    assert code == 0, err
    assert out.strip() == "ship-01 claude: signed in (claude work@example.com)."
    argv = env.argvs[0]
    assert "-tt" in argv and all(CLAUDE_CODE not in a for a in argv)


def test_login_wrong_account(env):
    env.ship.claude_account = "you@example.com"
    code, out, _ = env.run("login", "--tag", "work", "claude")
    assert code == 1
    assert "WRONG ACCOUNT: claude is you@example.com" in out
    assert "ship-fleet logout ship-01 claude" in out


def test_login_arguments(env):
    assert env.run("login", "claude")[0] == 1
    assert env.run("login", "ship-01", "ship-02", "--tag", "work")[0] == 1
    with pytest.raises(SystemExit):  # argparse: names, then --tag, then more words
        env.run("login", "ship-01", "--tag", "work", "claude")
    code, _, err = env.run("login", "ship-01", "agy")
    assert code == 2 and "Unknown sign-in service" in err


def test_logout(env):
    code, out, _ = env.run("logout", "ship-01", "github")
    assert code == 0 and "signed out of GitHub" in out
    assert env.ship.calls[-1][1][-1] == "gh auth logout --hostname github.com"


# --- set, tag, tools ---------------------------------------------------------


def test_set(env):
    code, out, err = env.run("set", "ship-01", "remote_control=off",
                             "default_mode=bypassPermissions")
    assert code == 0, err
    sent = env.ship.calls[-1][1]
    assert sent[3:] == ["set", "claude.remote_control=off", "claude.default_mode=bypassPermissions",
                        "claude.allow_bypass=true", "--json"]
    assert env.store.load().ship("ship-01").claude == {
        "remote_control": "off", "default_mode": "bypassPermissions", "allow_bypass": True}
    assert env.run("set", "ship-01", "colour=blue")[0] == 1
    assert env.run("set", "ship-01", "remote_control=maybe")[0] == 1
    assert env.run("set", "ship-01", "remote_control")[0] == 1


def test_tag(env):
    code, out, _ = env.run("tag", "ship-01")
    assert out.strip() == "work"
    code, out, _ = env.run("tag", "ship-01", "+gpu", "-work")
    assert code == 0 and env.store.load().ship("ship-01").tags == ("gpu",)
    assert env.ship.calls[-1][1][3:] == ["set", "ship.tags=gpu", "--json"]
    calls = len(env.ship.calls)
    env.run("tag", "ship-01", "personal", "--local")
    assert len(env.ship.calls) == calls
    assert env.run("tag", "ship-01", "+Bad")[0] == 1
    env.run("tag", "ship-01", "none", "--local")
    assert env.store.load().ship("ship-01").tags == ()


def test_tools(env):
    code, out, _ = env.run("tools", "ls")
    assert code == 0 and "gh-axi" in out and "orchestrator" in out
    code, out, _ = env.run("tools", "add", "ship-01", "tasks-axi")
    assert code == 0
    assert env.ship.calls[-1][1][3:] == ["tools", "add", "tasks-axi", "--json"]
    assert env.store.load().ship("ship-01").tools == ("gh-axi", "ship-axi", "tasks-axi")
    env.run("tools", "remove", "ship-01", "gh-axi")
    assert env.store.load().ship("ship-01").tools == ("ship-axi", "tasks-axi")
    assert env.run("tools", "add", "ship-01", "nope")[0] == 2
    assert env.run("tools", "add", "ship-01")[0] == 1


# --- ssh and logs ------------------------------------------------------------


def test_ssh(env, monkeypatch):
    seen = []
    monkeypatch.setattr("os.execvp", lambda file, argv: seen.append(argv))
    env.run("ssh", "ship-01")
    argv = seen[-1]
    assert argv[argv.index("-l") + 1] == "agent" and "-i" not in argv and "-tt" in argv
    env.run("ssh", "ship-01", "--as", "fleet", "--", "ls", "-la")
    argv = seen[-1]
    assert argv[argv.index("-l") + 1] == "fleet" and "-i" in argv and argv[-1] == "ls -la"
    env.run("ssh", "ship-01", "uptime")
    assert seen[-1][-1] == "uptime"


def test_ssh_dials_the_first_name_that_resolves(env, monkeypatch):
    seen = []
    monkeypatch.setattr("os.execvp", lambda file, argv: seen.append(argv))
    env.app.resolves = lambda host: host == "ship-02.example.net"
    env.run("ssh", "ship-02")
    argv = seen[-1]
    assert argv[argv.index("--") + 1] == "ship-02.example.net"
    assert "HostKeyAlias=ship-02" in argv  # still pinned to the Ship's own key
    env.app.resolves = lambda host: False  # nothing resolves: try the first, as before
    env.run("ssh", "ship-02")
    assert seen[-1][seen[-1].index("--") + 1] == "ship-02.local"


def test_logs(env):
    code, out, _ = env.run("logs", "ship-01", "herdr", "--lines", "2")
    assert code == 0 and out.splitlines() == ["line one", "line two"]
    assert env.ship.calls[-1][1][3:] == ["logs", "herdr", "--lines", "2", "--json"]


# --- phase 4: images, destroy, snapshots ---------------------------------------

IMG = "26.04-20261102-3f9a1c2e"


def add_image(env, *, published=None, file=None, sha="ab" * 32):
    entry = {"created": "2026-11-02T10:00:00Z", "sha256": sha,
             "ubuntu": {"release": "26.04", "build": "20261016"}, "tooling": "0.1.0",
             "commit": "unknown", "recipes": ["chrome-devtools-axi"]}
    if file:
        entry["file"] = str(file)
    with env.store.edit() as ed:
        ed.add_image(IMG, entry)
        if published:
            ed.set_image_field(IMG, "published", published)


def test_new_with_on_and_the_stock_template(env):
    assert env.run("new", "ship-04", "--on", "proxmox")[0] == 0
    assert env.driver.images == [None]
    assert env.store.load().ship("ship-04").image == "stock"


def test_new_uses_the_pinned_image(env):
    add_image(env, published={"proxmox": {"template_vmid": 9181}})
    with env.store.edit() as ed:
        ed.set_default("image", IMG)
    code, out, err = env.run("new", "ship-04", "--on", "proxmox")
    assert code == 0, err
    (image,) = env.driver.images
    assert image.id == IMG and image.ref == {"template_vmid": 9181}
    assert env.store.load().ship("ship-04").image == IMG


def test_new_refuses_an_image_not_published_to_the_driver(env, home):
    add_image(env, published={"manual": {"template_vmid": 9050}})
    code, out, err = env.run("new", "ship-04", "--on", "proxmox", "--image", IMG)
    assert code != 0 and "not published to the proxmox driver" in err
    assert not env.store.load().has_ship("ship-04") and env.driver.created == []
    assert not list((home / "data" / "seeds").glob("*.iso"))


def test_destroy_asks_for_the_name(env):
    env.run("new", "ship-04", "--on", "proxmox")
    env.app.stdin_isatty = lambda: True
    env.app.confirm = lambda prompt: "ship-4"
    code, out, err = env.run("destroy", "ship-04")
    assert code != 0 and "Not destroyed" in err and env.driver.destroyed == []
    env.app.confirm = lambda prompt: "ship-04"
    code, out, err = env.run("destroy", "ship-04")
    assert code == 0, err
    assert env.driver.destroyed == ["ship-04"]
    assert not env.store.load().has_ship("ship-04")


def test_destroy_without_a_terminal_needs_yes(env):
    code, out, err = env.run("destroy", "ship-01")
    assert code != 0 and "--yes" in err
    assert env.store.load().has_ship("ship-01")


def test_destroy_manual_prints_steps_and_keeps_the_entry_until_forget(env):
    code, out, err = env.run("destroy", "ship-01", "--yes")
    assert code == 0 and "destroy ship-01 --forget" in out
    assert env.store.load().has_ship("ship-01")
    assert env.run("destroy", "ship-01", "--yes", "--forget")[0] == 0
    assert not env.store.load().has_ship("ship-01")


def test_destroy_runs_the_before_destroy_hook_first(env):
    env.run("new", "ship-04", "--on", "proxmox")
    seen = []
    env.app.before_destroy = lambda inv, ship: seen.append((ship.name, env.driver.destroyed[:]))
    assert env.run("destroy", "ship-04", "--yes")[0] == 0
    assert seen == [("ship-04", [])]


def test_snapshot_commands(env):
    env.run("new", "ship-04", "--on", "proxmox")
    code, out, err = env.run("snapshot", "take", "ship-04", "before-x", "--note", "why")
    assert code == 0 and env.driver.snaps[-1] == ("take", "ship-04", "before-x", "why")
    assert "sign-ins" in err  # the warning
    assert env.run("snapshot", "take", "ship-04")[0] == 0
    assert env.driver.snaps[-1][2].startswith("snap-")
    code, out, err = env.run("snapshot", "ls", "ship-04")
    assert "before" in out and "a note" in out
    code, out, err = env.run("snapshot", "rollback", "ship-04", "before")
    assert code != 0 and "--yes" in err
    assert env.run("snapshot", "rollback", "ship-04", "before", "--yes")[0] == 0
    assert env.driver.snaps[-1] == ("rollback", "ship-04", "before")
    assert env.run("snapshot", "rm", "ship-04", "before")[0] == 0
    assert env.run("snapshot", "rm", "ship-04")[0] != 0


def test_snapshot_on_a_driver_without_them(env):
    code, out, err = env.run("snapshot", "ls", "ship-01")
    assert code != 0 and "cannot list snapshots" in err  # manual: it cannot know
    code, out, err = env.run("snapshot", "take", "ship-01", "s1")
    assert code == 0 and "qm snapshot" not in out  # no [drivers.proxmox] here: generic text


def test_image_ls_publish_use_rm(env, tmp_path):
    qcow = tmp_path / f"{IMG}.qcow2"
    qcow.write_bytes(b"QCOW")
    import hashlib

    add_image(env, file=qcow, sha=hashlib.sha256(b"QCOW").hexdigest())
    code, out, err = env.run("image", "ls")
    assert IMG in out and "stock template" in out
    # Not published yet: cannot be pinned.
    code, out, err = env.run("image", "use", IMG)
    assert code != 0 and "not published to the manual driver" in err
    code, out, err = env.run("image", "publish", IMG, "--driver", "proxmox")
    assert code == 0, err
    assert env.driver.published[0].path == qcow
    assert env.store.load().images[IMG]["published"] == {"proxmox": {"template_vmid": 9181}}
    with env.store.edit() as ed:
        ed.set_default("driver", "proxmox")
    assert env.run("image", "use", IMG)[0] == 0
    assert env.store.load().pinned_image == IMG
    code, out, err = env.run("image", "rm", IMG)
    assert code != 0 and "pinned" in err
    assert env.run("image", "use", "stock")[0] == 0
    assert "image" not in env.store.load().defaults
    code, out, err = env.run("image", "rm", IMG)
    assert code == 0, err
    assert env.driver.removed_images == [(IMG, {"template_vmid": 9181})]
    assert not qcow.exists() and IMG not in env.store.load().images


def test_image_publish_refuses_a_changed_file(env, tmp_path):
    qcow = tmp_path / f"{IMG}.qcow2"
    qcow.write_bytes(b"CHANGED")
    add_image(env, file=qcow, sha="00" * 32)
    code, out, err = env.run("image", "publish", IMG, "--driver", "proxmox")
    assert code != 0 and "does not match" in err and env.driver.published == []


def test_image_publish_by_hand(env):
    add_image(env)
    code, out, err = env.run("image", "publish", IMG, "--driver", "manual",
                             "--template-vmid", "9050")
    assert code == 0, err
    assert env.store.load().images[IMG]["published"] == {"manual": {"template_vmid": 9050}}


def test_image_build_records_the_image(env, tmp_path):
    from fleetledger.images import Built

    def builder(repo, *, recipes, backend, note):
        assert recipes == ["chrome-devtools-axi"] and backend == "qemu"
        entry = {"created": "2026-11-02T10:00:00Z", "sha256": "cd" * 32,
                 "ubuntu": {"release": "26.04", "build": "20261016"}, "tooling": "0.1.0",
                 "commit": "unknown", "dirty": True, "recipes": recipes}
        return Built(IMG, tmp_path / "x.qcow2", {}, entry)

    env.app.image_builder = builder
    code, out, err = env.run("image", "build", "--recipes", "chrome-devtools-axi",
                             "--backend", "qemu")
    assert code == 0, err
    assert env.store.load().images[IMG]["sha256"] == "cd" * 32


# --- phase 5: the overlay ----------------------------------------------------------

TS_KEY = "tskey-auth-kExample2CNTRL-SECRETSECRETSECRET0123"


class Keys:
    def __init__(self):
        self.asked = []

    def key(self, ship):
        self.asked.append(ship.name)
        return TS_KEY


def with_overlay(env, **extra):
    with env.store.edit() as ed:
        ed.doc["overlay"] = {"provider": "tailscale", "tags": ["tag:ship"], **extra}
    env.app.overlay_keys = Keys()


def test_overlay_join_hands_the_key_over_stdin_only(env):
    with_overlay(env)
    code, out, err = env.run("overlay", "join", "ship-01")
    assert code == 0, err
    assert env.ship.inputs == [("ship-01", (TS_KEY + "\n").encode())]
    argv = [c for name, c in env.ship.calls if "overlay" in c]
    assert argv and all(TS_KEY not in " ".join(c) for _, c in env.ship.calls)
    join = next(c for c in argv if "join" in c)
    assert join[join.index("--hostname") + 1] == "ship-01" and "tag:ship" in join
    # The overlay name goes first: it works off the LAN, and fails fast where unknown.
    assert env.store.load().ship("ship-01").names == ("ship-01.example-tailnet.ts.net",
                                                      "ship-01.local")
    assert TS_KEY not in out + err
    assert TS_KEY not in env.store.path.read_text()


def test_overlay_join_by_tag_and_with_a_login_server(env):
    with_overlay(env, login_server="https://hs.example.com")
    code, out, err = env.run("overlay", "join", "--tag", "work")
    assert code == 0, err
    joins = [c for _, c in env.ship.calls if "join" in c]
    assert len(joins) == 1 and "--login-server" in joins[0]  # only ship-01 is tagged work
    assert env.app.overlay_keys.asked == ["ship-01"]


def test_overlay_join_failure_keeps_names(env):
    with_overlay(env)
    env.ship.overlay_fails = True
    code, out, err = env.run("overlay", "join", "ship-01")
    assert code == 1 and "not joined" in out and TS_KEY not in out + err
    assert env.store.load().ship("ship-01").names == ("ship-01.local",)


def test_overlay_leave_drops_the_overlay_name(env):
    with_overlay(env)
    env.run("overlay", "join", "ship-01")
    code, out, err = env.run("overlay", "leave", "ship-01")
    assert code == 0, err
    assert env.store.load().ship("ship-01").names == ("ship-01.local",)


def test_overlay_needs_its_section(env):
    code, out, err = env.run("overlay", "join", "ship-01")
    assert code != 0 and "[overlay]" in err


def test_overlay_policy_lets_your_devices_in_and_ships_nowhere(env):
    with_overlay(env)
    code, out, err = env.run("overlay", "policy")
    doc = json.loads(out)
    assert doc["acls"] == [{"action": "accept", "src": ["autogroup:member"],
                            "dst": ["tag:ship:22"]}]
    assert not any("tag:ship" in rule["src"] for rule in doc["acls"])  # Ships open nothing
    assert doc["ssh"] == []  # no Tailscale SSH
    # Only admins may mint keys for the Ships' tag.
    assert doc["tagOwners"] == {"tag:ship": ["autogroup:admin"]}
    assert doc["tests"] == [{"src": "tag:ship", "deny": ["tag:ship:22"]}]


def test_overlay_join_skips_an_overlay_name_the_inventory_cannot_hold(env):
    with_overlay(env)
    real = env.ship.overlay_call

    def odd_name(ship, args):
        code, out, err = real(ship, args)
        env.ship.overlay[ship.name]["name"] = "ship_01.hs.example.com"
        return code, out.replace("ship-01.example-tailnet.ts.net", "ship_01.hs.example.com"), err

    env.ship.overlay_call = odd_name
    code, out, err = env.run("overlay", "join", "ship-01")
    assert code == 0 and "not a usable host name" in out
    assert env.store.load().ship("ship-01").names == ("ship-01.local",)


def test_overlay_status(env):
    with_overlay(env)
    env.run("overlay", "join", "ship-01")
    code, out, err = env.run("overlay", "status")
    assert "ship-01.example-tailnet.ts.net" in out and "running" in out
    assert "ship-02" in out and "not joined" in out


def test_destroy_leaves_the_overlay_first(env):
    with_overlay(env)
    env.run("new", "ship-04", "--on", "proxmox")
    env.run("overlay", "join", "ship-04")
    env.ship.calls.clear()
    assert env.run("destroy", "ship-04", "--yes")[0] == 0
    leave = [i for i, (_, c) in enumerate(env.ship.calls) if "leave" in c]
    assert leave and env.driver.destroyed == ["ship-04"]


def test_destroy_goes_on_if_the_ship_cannot_leave(env):
    with_overlay(env)
    env.run("new", "ship-04", "--on", "proxmox")
    env.ship.down.add("ship-04")
    code, out, err = env.run("destroy", "ship-04", "--yes")
    assert code == 0 and "admin console" in err and env.driver.destroyed == ["ship-04"]


def test_set_ssh_listen(env):
    code, out, err = env.run("set", "ship-01", "ssh_listen=overlay")
    assert code == 0, err
    assert ["sudo", "-n", "/usr/local/bin/ship-ctl", "set", "network.ssh_listen=overlay",
            "--json"] in [c for _, c in env.ship.calls]
    assert env.run("set", "ship-01", "ssh_listen=lan")[0] != 0
