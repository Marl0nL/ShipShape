"""ship-fleet (W10): every command, against a fake Ship behind a real SSH object."""

import copy
import io
import json
import re
import stat
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from fleetledger.concierge import engine
from fleetledger.drivers import get_driver
from fleetledger.drivers.base import Outcome
from fleetledger.errors import FleetError
from fleetledger.scan import Announcement
from fleetledger.seed import hostkey, iso
from fleetledger.ssh import SSH, Result, Unreachable

from fleetledger import __version__, cli

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parents[1] / "contracts" / "examples"
RUNNING = json.loads((EXAMPLES / "ship-status" / "running.json").read_text())
RUNNING_CORE = json.loads((EXAMPLES / "ship-status" / "running-core.json").read_text())
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
        if command[:1] == ["timeout"]:  # overlay check --probe: /dev/tcp to an address
            return getattr(self, "probe_rc", 124), "", getattr(self, "probe_err", "")
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
                   "tags": tags, "ssh_listen": "any", "node_id": f"n{ship.name.replace('-', '')}"}
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

    def start(self, ship):
        self.power = [*getattr(self, "power", []), ("start", ship.name)]
        return Outcome(done=True, messages=[f"Started {ship.name}."])

    def stop(self, ship):
        self.power = [*getattr(self, "power", []), ("stop", ship.name)]
        return Outcome(done=True, messages=[f"Shut down {ship.name}."])

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


def test_new_proxmox_says_what_the_first_boot_is_doing(env, monkeypatch):
    """A first boot is minutes of not answering, then of refusing the admin key until
    cloud-init has set it up: each is said in words, with a line now and then, and
    none of it ends the wait."""
    ticks = iter(range(0, 10**6, 20))  # each look at the clock: 20 s on
    env.app.clock = lambda: next(ticks)
    polls = []
    real = type(env.ship).__call__

    def booting(self, ship, command):
        if command[-2:] == ["status", "--json"]:
            polls.append(command)
            if len(polls) <= 6:
                raise Unreachable(f"Cannot reach {ship.name}: connection refused")
            if len(polls) == 7:
                raise FleetError(f"{ship.name} (192.0.2.44) refused FleetLedger's admin key.")
        return real(self, ship, command)

    monkeypatch.setattr(type(env.ship), "__call__", booting)
    code, out, err = env.run("new", "ship-12", "--driver", "proxmox")
    assert code == 0, err
    assert env.store.load().ship("ship-12").enrolled
    assert "ship-12: not answering yet: still booting" in err
    assert re.search(r"ship-12: still waiting, \d+ min so far", err)
    assert ("refused FleetLedger's admin key. (it may still be starting; still trying)"
            in err)
    assert "ship-12: unreachable" not in err and "ship-12: error" not in err


def test_resize_through_the_driver_and_recorded(env):
    sized = []
    env.driver.resize = lambda ship, size: sized.append(size) or Outcome(
        done=True, messages=["Set 8 cores."])
    rebooted = []
    env.driver.reboot = lambda ship: rebooted.append(ship.name) or Outcome(
        done=True, messages=["Rebooted."])
    env.run("new", "ship-16", "--driver", "proxmox")
    code, out, err = env.run("resize", "ship-16", "--cores", "8", "--restart")
    assert code == 0, err
    assert sized == [{"cores": 8}] and rebooted == ["ship-16"]
    assert env.store.load().ship("ship-16").size["cores"] == 8
    assert env.run("resize", "ship-16", "--cores", "0")[0] == 1  # refused before the driver
    assert env.run("resize", "ship-16")[0] == 1
    assert sized == [{"cores": 8}]
    code, _, _ = env.run("resize", "ship-01", "--disk-gb", "80", "--record-only")
    assert code == 0 and env.store.load().ship("ship-01").size["disk_gb"] == 80
    code, out, _ = env.run("resize", "ship-16", "--disk-gb", "60")
    assert code == 0  # the disk, then the Ship's filesystem with it
    assert any(c[3:4] == ["grow-root"] for name, c in env.ship.calls if name == "ship-16")


def test_a_new_ship_can_start_its_first_agent_in_a_github_repo(env, home):
    asked = []
    env.app.github_visibility = lambda repo: asked.append(repo) or "not-public"
    code, out, err = env.run("new", "ship-19", "--repo", "someone/agent-setup")
    assert code == 0, err
    assert asked == ["someone/agent-setup"] and "once you sign it in to GitHub" in out
    assert _seed_ship_toml(home, "ship-19")["autostart"] == [
        {"name": "main", "cwd": "~/work/agent-setup", "repo": "someone/agent-setup"}]
    code, _, _ = env.run("new", "ship-20", "--autostart", "lead:~/x:plan", "--repo",
                         "someone/agent-setup")
    assert code == 0
    assert _seed_ship_toml(home, "ship-20")["autostart"] == [
        {"name": "lead", "cwd": "~/work/agent-setup", "mode": "plan",
         "repo": "someone/agent-setup"}]
    assert env.run("new", "ship-21", "--repo", "not a repo")[0] == 1


def test_an_agent_in_a_repo_can_be_added_later(env):
    code, _, err = env.run("set", "ship-01", "autostart.add=docs:github:someone/agent-setup")
    assert code == 0, err
    assert ["set", "autostart.add=docs:github:someone/agent-setup", "--json"] in [
        c[3:] for _, c in env.ship.calls]
    calls = len(env.ship.calls)
    assert env.run("set", "ship-01", "autostart.add=docs:github:not/a/repo")[0] == 1
    assert len(env.ship.calls) == calls


@pytest.mark.parametrize("args", [["--memory-mb", "2048"], ["--cores", "100"],
                                  ["--disk-gb", "8000"]])
def test_a_size_the_contract_refuses_never_reaches_the_hypervisor(env, args):
    sized = []
    env.driver.resize = lambda ship, size: sized.append(size) or Outcome(done=True)
    env.run("new", "ship-23", "--driver", "proxmox")
    code, _, err = env.run("resize", "ship-23", *args)
    assert code == 1 and " to " in err  # says the limits
    assert sized == [] and env.store.load().ship("ship-23").size == {}


def test_a_new_ship_outside_the_size_contract_is_refused_plainly(env):
    code, _, err = env.run("new", "ship-26", "--memory-mb", "2048")
    assert code == 1 and "memory mb: 4096 to 262144" in err
    assert "does not match" not in err and not env.store.load().has_ship("ship-26")


def test_a_ship_for_an_ecdsa_or_security_key(env, home):
    at = "@" + "openssh.com"  # a key type's suffix (written apart: not an address)
    key = f"sk-ssh-ed25519{at} AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29t you\n"
    you = Path(env.store.load().doc["fleet"]["agent_keys"][0])
    you.write_text(key)
    code, _, err = env.run("new", "ship-24")
    assert code == 0, err
    assert _seed_ship_toml(home, "ship-24")["owner"]["keys"] == [key.strip()]
    you.write_text("ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTY= you\n")
    assert env.run("new", "ship-25")[0] == 0


def test_offline_views_leave_out_sign_ins_a_ship_does_not_use(env):
    doc = copy.deepcopy(RUNNING)
    doc["attention"].append({"code": "signin-missing", "subject": "gcloud",
                             "message": "Google Cloud is not signed in."})
    env.ship.status_override["ship-01"] = doc
    env.run("ls")  # fetched and cached
    env.run("signins", "ship-01", "--off", "gcloud")
    inv = env.store.load()
    from fleetledger import status

    view = status.cached_view(inv, inv.ship("ship-01"), "none")
    assert not [a for a in view.attention if a.get("subject") == "gcloud"]


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


def test_adopt_accepts_a_newer_ships_unknown_status_block(env, stranger):
    """Forward compatibility, as status.fetch: a block this FleetLedger does not know
    is dropped; the rest is still checked."""
    key, fp = stranger
    env.app.keyscan = lambda host: key
    env.ship.status_override["ship-07"] = {**RUNNING, "from_the_future": {"x": 1}}
    code, _, err = env.run("adopt", "ship-07", "--host", "192.0.2.17", "--fingerprint", fp)
    assert code == 0, err
    assert env.store.load().has_ship("ship-07")


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


def test_status_says_tmp_waits_for_its_reboot(env):
    assert "/tmp in memory" not in env.run("status", "ship-01")[1]
    doc = copy.deepcopy(RUNNING)
    doc["host"]["tmp"] = {"fs": "tmpfs", "next_boot": "disk"}
    env.ship.status_override["ship-01"] = doc
    assert "/tmp in memory until the next reboot" in env.run("status", "ship-01")[1]


def test_status_shows_recorded_agents_that_are_not_running(env):
    doc = copy.deepcopy(RUNNING)
    stamp = "2026-01-02T00:00:00Z"
    doc["agent_record"] = {"runtime": "herdr", "agents": [
        {"name": "main", "cwd": "/home/agent/work/main", "mode": "auto", "autostart": True,
         "state": "running", "relaunch_failures": 0, "created_at": stamp, "updated_at": stamp},
        {"name": "helper", "cwd": "/home/agent/work/side", "mode": "plan", "autostart": False,
         "state": "failed", "relaunch_failures": 3, "last_error": "agent_not_ready",
         "created_at": stamp, "updated_at": stamp}],
        "resume": {"at": stamp, "method": "ship-ctl", "resumed": 2, "failed": 1}}
    env.ship.status_override["ship-01"] = doc
    code, out, _ = env.run("status", "ship-01")
    assert code == 0
    assert "  helper       failed   plan  (not running)" in out
    assert "last resume 2026-01-02T00:00:00Z (2 resumed, 1 failed)" in out


def test_status_of_a_herdr_ship_has_no_terminal_column(env):
    """herdr Ships print exactly as before: no address, no waiting reason."""
    code, out, _ = env.run("status", "ship-01")
    assert "  main         blocked  auto\n" in out
    assert "herdr-core" not in out and "w1:p2" not in out


def test_status_of_a_core_ship_shows_the_runtime_addresses_and_what_agents_wait_for(env):
    env.ship.status_override["ship-01"] = copy.deepcopy(RUNNING_CORE)
    code, out, _ = env.run("status", "ship-01")
    assert code == 0
    lines = out.splitlines()
    [main] = [x for x in lines if x.startswith("  main ")]
    [builder] = [x for x in lines if x.startswith("  builder ")]
    assert main.split() == ["main", "blocked", "auto", "t3", "waits", "for", "approval:",
                            "npm", "test"]
    assert builder.split() == ["builder", "working", "bypassPermissions", "t4"]
    assert "herdr-core 0.1.19 (core running); last resume 2026-01-01T00:00:00Z (2 resumed)" in out
    assert "\nherdr 0.1.19" not in out


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


def test_a_sign_in_turned_off_is_signed_out_and_never_offered(env):
    code, out, err = env.run("signins", "ship-01", "--off", "gcloud")
    assert code == 0, err
    assert "gcloud auth revoke" in env.ship.calls[-1][1][-1]  # signed out on the Ship
    assert env.store.load().ship("ship-01").signins == ("claude", "github")
    assert env.run("signins", "ship-01")[1].split() == ["claude", "on", "gcloud", "off",
                                                        "github", "on"]
    calls = len(env.ship.calls)
    code, _, err = env.run("login", "ship-01", "gcloud")
    assert code == 1 and "does not use Google Cloud" in err and "--on gcloud" in err
    assert len(env.ship.calls) == calls and env.argvs == []  # nothing started
    code, out, err = env.run("login", "--tag", "work", "gcloud")
    assert code == 0, err
    assert env.argvs == []  # by tag: skipped where it is off, nothing started
    _, out, _ = env.run("ls")
    assert "off" in out
    assert env.run("signins", "ship-01", "--on", "gcloud")[0] == 0
    assert env.store.load().ship("ship-01").signins is None  # all again: absent


def test_turning_off_a_sign_in_that_was_never_made_signs_nothing_out(env):
    doc = copy.deepcopy(RUNNING)
    for key in ("gcloud", "gcloud_adc"):
        doc["auth"][key].update(state="missing", account=None)
    env.ship.status_override["ship-01"] = doc
    code, out, _ = env.run("signins", "ship-01", "--off", "gcloud")
    assert code == 0 and "Google Cloud was not signed in there" in out
    assert not any("gcloud auth revoke" in c[-1] for _, c in env.ship.calls)


def test_a_new_ship_can_use_fewer_sign_ins(env):
    assert env.run("new", "ship-17", "--signins", "claude")[0] == 0
    assert env.store.load().ship("ship-17").signins == ("claude",)
    assert env.run("new", "ship-18", "--signins", "claude,agy")[0] == 1


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


def test_set_adds_an_agent_and_starts_it(env):
    code, out, err = env.run("set", "ship-01", "autostart.add=main:~/work",
                             "autostart.add=docs:~/docs:plan")
    assert code == 0, err
    verbs = [c[3:] for _, c in env.ship.calls[-2:]]
    assert verbs[0] == ["set", "autostart.add=main:~/work", "autostart.add=docs:~/docs:plan",
                        "--json"]
    assert verbs[1][0] == "autostart"  # started now (once Claude is signed in)
    assert env.run("set", "ship-01", "autostart.remove=docs")[0] == 0
    assert env.ship.calls[-1][1][3:] == ["set", "autostart.remove=docs", "--json"]
    for bad in ("autostart.add=Main:~/work", "autostart.add=main:work",
                "autostart.add=main", "autostart.remove=../x"):
        calls = len(env.ship.calls)
        assert env.run("set", "ship-01", bad)[0] == 1
        assert len(env.ship.calls) == calls  # refused before reaching the Ship


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


def test_your_own_login_on_ships(env, monkeypatch, home):
    assert env.run("owner")[1].strip() == "human"  # the default
    assert env.run("new", "ship-22")[0] == 0
    owner = _seed_ship_toml(home, "ship-22")["owner"]
    assert owner["name"] == "human" and owner["keys"][0].startswith("ssh-ed25519 ")
    code, out, err = env.run("owner", "sam")
    assert code == 0, err
    sent = [c[3:] for name, c in env.ship.calls if name == "ship-01" and c[3:4] == ["set"]]
    assert sent[-1][1:3] == ["owner.name=sam", f"owner.keys={owner['keys'][0]}"]
    assert env.store.load().owner == "sam" and "ssh sam@ship-01" in out
    for bad in ("agent", "fleet", "root", "nobody", "Sam", "a b"):
        assert env.run("owner", bad)[0] == 1
    assert env.store.load().owner == "sam"
    seen = []
    monkeypatch.setattr("os.execvp", lambda file, argv: seen.append(argv))
    env.run("ssh", "ship-01", "--as", "owner")
    assert seen[-1][seen[-1].index("-l") + 1] == "sam" and "-i" not in seen[-1]


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


def test_destroy_drops_the_cached_status(env):
    from fleetledger import status

    env.run("new", "ship-04", "--on", "proxmox")
    status.write_cache("ship-04", {"attention": []})
    assert status.cache_path("ship-04").exists()
    assert env.run("destroy", "ship-04", "--yes")[0] == 0
    assert not status.cache_path("ship-04").exists()


def test_destroy_drops_the_ships_history(env):
    import time

    from fleetledger import history

    store, now = history.History(), time.time()
    store.add([history.Sample("ship-02", now, True, 5.0, 10.0, 20.0, 1, 0, 0),
               history.Sample("ship-01", now, True, 5.0, 10.0, 20.0, 1, 0, 0)])
    assert env.run("destroy", "ship-02", "--yes", "--forget")[0] == 0
    left = store.since(0)
    assert "ship-02" not in left and "ship-01" in left  # only its own charts go


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


def test_a_new_ship_joins_the_overlay_once_it_enrols(env):
    with_overlay(env)
    code, out, err = env.run("new", "ship-13", "--driver", "proxmox")
    assert code == 0, err
    joins = [c for name, c in env.ship.calls if name == "ship-13" and "join" in c]
    assert len(joins) == 1
    ship = env.store.load().ship("ship-13")
    assert ship.names[0] == "ship-13.example-tailnet.ts.net"  # the preferred name
    assert "on the overlay as" in out


def test_a_new_ship_can_stay_off_the_overlay(env):
    with_overlay(env)
    code, out, err = env.run("new", "ship-14", "--driver", "proxmox", "--no-overlay")
    assert code == 0, err
    assert not [c for name, c in env.ship.calls if name == "ship-14" and "join" in c]


def test_a_failed_join_does_not_fail_the_new_ship(env):
    with_overlay(env)
    env.ship.overlay_fails = True
    code, out, err = env.run("new", "ship-15", "--driver", "proxmox")
    assert code == 0, err
    assert env.store.load().ship("ship-15").enrolled
    assert "not on the overlay yet" in out and "ship-fleet overlay join ship-15" in out


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


class FakeTailnet:
    def __init__(self, devices, log=None, fail=False):
        self._devices, self.deleted, self.log, self.fail = devices, [], log, fail

    def devices(self):
        if self.log is not None:
            self.log.append("devices")
        if self.fail:
            from fleetledger.errors import FleetError

            raise FleetError("Tailscale API: listing devices failed (503).")
        return self._devices

    def delete_device(self, device_id):
        self.deleted.append(device_id)


def test_destroy_removes_the_ships_device_from_the_tailnet(env):
    with_overlay(env, key_source="tailscale-api",
                 api={"oauth_client_id": "kExampleClient1", "oauth_secret_file": "/unused",
                      "remove_devices": True})
    env.run("new", "ship-04", "--on", "proxmox")
    env.run("overlay", "join", "ship-04")
    assert env.store.load().ship("ship-04").overlay_node == "nship04"  # recorded at join
    order = []
    tailnet = FakeTailnet([{"nodeId": "nship04", "name": "ship-04.example-tailnet.ts.net",
                            "hostname": "ship-04", "tags": ["tag:ship"]}], log=order)
    env.app.overlay_api = lambda api: tailnet
    real = env.ship.overlay_call

    def logged(ship, args):
        order.append(args[0])
        return real(ship, args)

    env.ship.overlay_call = logged
    code, out, err = env.run("destroy", "ship-04", "--yes")
    assert code == 0, err
    assert order.index("leave") < order.index("devices")  # left first, then removed
    assert tailnet.deleted == ["nship04"] and "matched by node id" in err


def test_destroy_removes_the_device_even_if_the_ship_is_unreachable(env):
    with_overlay(env, key_source="tailscale-api",
                 api={"oauth_client_id": "kExampleClient1", "oauth_secret_file": "/unused",
                      "remove_devices": True})
    env.run("new", "ship-04", "--on", "proxmox")
    env.run("overlay", "join", "ship-04")
    env.ship.down.add("ship-04")
    tailnet = FakeTailnet([{"nodeId": "nship04", "name": "ship-04.example-tailnet.ts.net",
                            "hostname": "ship-04", "tags": ["tag:ship"]}])
    env.app.overlay_api = lambda api: tailnet
    code, out, err = env.run("destroy", "ship-04", "--yes")
    assert code == 0 and tailnet.deleted == ["nship04"]  # the node id recorded at join
    assert env.driver.destroyed == ["ship-04"]


def test_destroy_goes_on_when_the_tailnet_cannot_be_reached(env):
    with_overlay(env, key_source="tailscale-api",
                 api={"oauth_client_id": "kExampleClient1", "oauth_secret_file": "/unused",
                      "remove_devices": True})
    env.run("new", "ship-04", "--on", "proxmox")
    env.app.overlay_api = lambda api: FakeTailnet([], fail=True)
    code, out, err = env.run("destroy", "ship-04", "--yes")
    assert code == 0 and env.driver.destroyed == ["ship-04"]
    assert "could not remove its overlay device" in err and "admin console" in err


def test_overlay_leave_forgets_the_node_id(env):
    with_overlay(env)
    env.run("overlay", "join", "ship-01")
    assert env.store.load().ship("ship-01").overlay_node == "nship01"
    env.run("overlay", "leave", "ship-01")
    assert env.store.load().ship("ship-01").overlay_node is None


def test_destroy_by_default_prints_the_admin_console_step(env):
    """API keys, device removal not turned on: no API call, the exact step instead."""
    with_overlay(env, key_source="tailscale-api",
                 api={"oauth_client_id": "kExampleClient1", "oauth_secret_file": "/unused"})
    env.run("new", "ship-04", "--on", "proxmox")
    env.run("overlay", "join", "ship-04")
    env.app.overlay_api = lambda api: (_ for _ in ()).throw(AssertionError("no API call"))
    code, out, err = env.run("destroy", "ship-04", "--yes")
    assert code == 0 and env.driver.destroyed == ["ship-04"]
    assert "Machines > ship-04.example-tailnet.ts.net, node id nship04" in err


def test_destroy_with_pasted_keys_says_how_to_remove_the_device(env):
    with_overlay(env)
    env.run("new", "ship-04", "--on", "proxmox")
    code, out, err = env.run("destroy", "ship-04", "--yes")
    assert code == 0 and "admin console > Machines" in err


def test_resolving_a_name_is_capped(monkeypatch):
    """An mDNS name nobody answers must not stall `ship-fleet ssh` or `new`."""
    import time as _time

    def slow(host, port):
        _time.sleep(5)
        return []

    monkeypatch.setattr("socket.getaddrinfo", slow)
    started = _time.monotonic()
    assert cli._resolves("ship-01.local", timeout=0.2) is False
    assert _time.monotonic() - started < 2
    assert cli._resolves("192.0.2.44") is True  # an address needs no lookup
    assert cli._resolves("2001:db8::44") is True


# --- overlay setup and check --------------------------------------------------------------

import json as _json  # noqa: E402

from fleetledger.overlay.providers import policy_doc  # noqa: E402

OAUTH_SECRET = "tskey-client-kExample9CNTRL-SETUPSECRET0123456789"
GOOD_POLICY = _json.dumps(policy_doc(["tag:ship"]), indent=2)


class FakeTailscale:
    def __init__(self, policy=GOOD_POLICY, scopes=("auth_keys", "devices:core",
                                                   "policy_file:read", "policy_file"),
                 devices=(), problems=()):
        self.policy, self.granted_scopes = policy, frozenset(scopes)
        self._devices, self.problems = list(devices), list(problems)
        self.set_calls, self.validated, self.secrets_seen = [], [], []
        self.granted = frozenset()
        self.used = []  # which device and policy calls were made

    def __call__(self, api_cfg, secret=None):  # the app's overlay_api factory
        if secret is not None:
            self.secrets_seen.append(secret())
        return self

    def token(self):
        self.granted = self.granted_scopes
        return "token"

    def devices(self):
        self.used.append("devices")
        return self._devices

    def delete_device(self, device_id):
        pass

    def policy_text(self):
        self.used.append("policy")
        return self.policy, '"etag-7"'

    def validate(self, text):
        self.validated.append(text)
        return list(self.problems)

    def set_policy(self, text, etag):
        self.set_calls.append((text, etag))


def interactive(env, answers, secret=OAUTH_SECRET, tailnet=None):
    queue = list(answers)
    env.app.stdin_isatty = lambda: True
    env.app.confirm = lambda prompt: queue.pop(0)
    env.app.ask_secret = lambda prompt: secret
    env.app.overlay_api = tailnet
    return queue


def written(env, home):
    text = env.store.path.read_text()
    secret_file = home / "config" / "tailscale-oauth"
    return text, secret_file


def test_setup_paste(env, home):
    interactive(env, ["tailscale", "paste"])
    code, out, err = env.run("overlay", "setup")
    assert code == 0, err
    overlay = env.store.load().overlay
    assert overlay == {"provider": "tailscale", "tags": ["tag:ship"], "key_source": "prompt"}
    assert '"tagOwners"' in out and "Settings > Keys" in out
    assert not (home / "config" / "tailscale-oauth").exists()


def test_setup_headscale(env):
    interactive(env, ["headscale", "https://hs.example.com"])
    assert env.run("overlay", "setup")[0] == 0
    overlay = env.store.load().overlay
    assert overlay["provider"] == "headscale" and overlay["login_server"] == "https://hs.example.com"
    interactive(env, ["headscale", "http://plain.example.com"])
    code, out, err = env.run("overlay", "setup", "--force")
    assert code != 0 and "https://" in err


LEVEL_SCOPES = {
    "keys": ("auth_keys",),
    "devices": ("auth_keys", "devices:core"),
    "policy": ("auth_keys", "devices:core", "policy_file"),
}


def level_answers(level, apply="yes"):
    answers = ["", "", level]  # provider tailscale, key source oauth, the level
    if level == "policy":
        answers.append("yes")  # yes, the secret may change access rules
    answers.append("kExampleClient1")
    if level == "policy":
        answers.append(apply)  # apply the merged policy
    return answers


def test_setup_oauth_default_is_keys_only(env, home):
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["keys"])
    interactive(env, ["", "", "", "kExampleClient1"], tailnet=tailnet)  # level: default
    code, out, err = env.run("overlay", "setup")
    assert code == 0, err
    api = env.store.load().overlay["api"]
    assert api["remove_devices"] is False and api["manage_policy"] is False
    assert tailnet.used == []  # no device or policy call at this level
    ships_toml, secret_file = written(env, home)
    assert secret_file.read_text().strip() == OAUTH_SECRET
    assert oct(secret_file.stat().st_mode & 0o777) == "0o600"
    # The secret is only ever in its file: not in ships.toml, not in any output.
    assert OAUTH_SECRET not in ships_toml + out + err
    assert tailnet.secrets_seen == [OAUTH_SECRET]  # checked in memory before saving
    assert '"tagOwners"' in out and "check --policy FILE" in out
    assert "EVERY device in the tailnet" in out  # the trade-offs were shown


@pytest.mark.parametrize("level", ["keys", "devices", "policy"])
def test_each_level_needs_exactly_its_scopes(env, home, level):
    scopes = LEVEL_SCOPES[level]
    for missing in scopes:
        tailnet = FakeTailscale(scopes=[s for s in scopes if s != missing], policy="{}")
        interactive(env, level_answers(level), tailnet=tailnet)
        code, out, err = env.run("overlay", "setup", "--force")
        assert code != 0 and missing in err and "Nothing was saved" in err
        assert env.store.load().overlay is None
        assert not (home / "config" / "tailscale-oauth").exists()
    tailnet = FakeTailscale(scopes=scopes, policy="{}")
    interactive(env, level_answers(level), tailnet=tailnet)
    code, out, err = env.run("overlay", "setup", "--force")
    assert code == 0, err
    api = env.store.load().overlay["api"]
    assert api["remove_devices"] is (level != "keys")
    assert api["manage_policy"] is (level == "policy")


@pytest.mark.parametrize("extra", ["devices:core", "policy_file", "devices:core:read",
                                   "policy_file:read"])
def test_setup_notes_a_client_wider_than_its_level(env, extra):
    tailnet = FakeTailscale(scopes=("auth_keys", extra))
    interactive(env, ["", "", "keys", "kExampleClient1"], tailnet=tailnet)
    code, out, err = env.run("overlay", "setup")
    assert code == 0 and "more than this level uses" in err


def test_setup_policy_level_shows_a_diff_and_asks(env):
    tailnet = FakeTailscale(policy='{\n  // mine\n  "acls": [],\n}\n')
    interactive(env, level_answers("policy"), tailnet=tailnet)
    code, out, err = env.run("overlay", "setup")
    assert code == 0, err
    assert "can change your tailnet's access rules" in out
    assert "+++ tailnet policy (with the Ships' rules)" in out
    ((text, etag),) = tailnet.set_calls
    assert etag == '"etag-7"' and "// mine" in text  # merged, not overwritten
    assert tailnet.validated == [text]  # validated before it was sent
    assert env.store.load().overlay["api"]["manage_policy"] is True


def test_setup_policy_level_declined_falls_back_to_devices(env):
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["devices"], policy="{}")
    interactive(env, ["", "", "policy", "no", "kExampleClient1"], tailnet=tailnet)
    assert env.run("overlay", "setup")[0] == 0
    api = env.store.load().overlay["api"]
    assert tailnet.set_calls == [] and api["manage_policy"] is False
    assert api["remove_devices"] is True


def test_setup_policy_level_apply_declined_changes_nothing(env):
    tailnet = FakeTailscale(policy="{}")
    interactive(env, level_answers("policy", apply="no"), tailnet=tailnet)
    code, out, err = env.run("overlay", "setup")
    assert code == 0 and tailnet.set_calls == [] and "Not changed" in out


def test_setup_policy_level_refused_by_tailscale_changes_nothing(env):
    tailnet = FakeTailscale(policy="{}", problems=["test(s) failed"])
    interactive(env, level_answers("policy")[:-1], tailnet=tailnet)
    code, out, err = env.run("overlay", "setup")
    assert code != 0 and "refuses" in err and tailnet.set_calls == []


def test_setup_needs_a_terminal_and_force_to_replace(env):
    code, out, err = env.run("overlay", "setup")
    assert code != 0 and "terminal" in err
    with_overlay(env)
    interactive(env, ["tailscale", "paste"])
    code, out, err = env.run("overlay", "setup")
    assert code != 0 and "--force" in err


def oauth_overlay(env, home, tailnet, level="policy"):
    secret = home / "config" / "tailscale-oauth"
    secret.write_text(OAUTH_SECRET + "\n")
    secret.chmod(0o600)
    with_overlay(env, key_source="tailscale-api",
                 api={"oauth_client_id": "kExampleClient1", "oauth_secret_file": str(secret),
                      "remove_devices": level != "keys", "manage_policy": level == "policy"})
    env.app.overlay_api = tailnet


def test_check_at_the_keys_level_uses_no_device_or_policy_calls(env, home, tmp_path):
    tailnet = FakeTailscale()
    oauth_overlay(env, home, tailnet, level="keys")
    code, out, err = env.run("overlay", "check")
    assert code == 0 and tailnet.used == [] and "--policy FILE" in out
    exported = tmp_path / "policy.hujson"
    exported.write_text(GOOD_POLICY)
    code, out, err = env.run("overlay", "check", "--policy", str(exported))
    assert code == 0 and tailnet.used == [] and "Ships cannot start connections" in out


def test_check_a_good_tailnet(env, home):
    oauth_overlay(env, home, FakeTailscale())
    code, out, err = env.run("overlay", "check")
    assert code == 0, out
    assert "FAIL" not in out and "OK" in out


def test_check_the_starter_allow_all_policy_fails(env, home):
    allow_all = '{"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]}'
    oauth_overlay(env, home, FakeTailscale(policy=allow_all))
    code, out, err = env.run("overlay", "check")
    assert code == 1 and "Ships cannot start connections" in out and "FAIL" in out


def test_check_runs_the_ship_tests_through_tailscale(env, home):
    oauth_overlay(env, home, FakeTailscale(problems=["tag:ship can reach tag:ship:22"]))
    code, out, err = env.run("overlay", "check")
    assert code == 1 and "Tailscale runs the Ship tests" in out


def test_check_flags_devices_not_in_the_inventory(env, home):
    oauth_overlay(env, home, FakeTailscale(devices=[
        {"name": "old-ship.example-tailnet.ts.net", "hostname": "old-ship", "tags": ["tag:ship"]},
        {"name": "laptop.example-tailnet.ts.net", "hostname": "laptop", "tags": []}]))
    code, out, err = env.run("overlay", "check")
    assert "tailnet devices" in out and "old-ship" in out and "laptop" not in out
    assert any(line.startswith("WARN") and "tailnet devices" in line for line in out.splitlines())


def test_check_with_pasted_keys_reads_an_exported_policy(env, tmp_path):
    with_overlay(env)
    exported = tmp_path / "policy.hujson"
    exported.write_text("// exported\n" + GOOD_POLICY)
    code, out, err = env.run("overlay", "check")
    assert code == 0 and "SKIP" in out and "--policy FILE" in out
    code, out, err = env.run("overlay", "check", "--policy", str(exported))
    assert code == 0 and "your devices reach Ships on port 22" in out


@pytest.mark.parametrize("rc, stderr, state", [
    (124, "", "OK"),  # timed out: the policy dropped it
    (0, "", "FAIL"),  # connected
    (1, "bash: connect: Connection refused", "FAIL"),  # reset: the policy let it through
    (1, "bash: connect: Network is unreachable", "SKIP"),  # cannot tell
])
def test_check_probe_tells_blocked_from_allowed(env, home, rc, stderr, state):
    import re as _re

    oauth_overlay(env, home, FakeTailscale())
    env.run("overlay", "join", "ship-01")
    env.app.local_overlay_ips = lambda: ["192.0.2.99"]
    env.ship.probe_rc, env.ship.probe_err = rc, stderr
    code, out, err = env.run("overlay", "check", "--probe")
    probes = [c for _, c in env.ship.calls if c[:1] == ["timeout"]]
    assert probes and probes[-1][-1] == "192.0.2.99"
    assert _re.search(rf"^{state}\s+ship-01 cannot reach this computer", out, _re.M), out
    assert code == (1 if state == "FAIL" else 0)


def test_check_probe_that_cannot_run_is_not_a_pass(env, home):
    import re as _re

    oauth_overlay(env, home, FakeTailscale())
    env.run("overlay", "join", "ship-01")
    env.app.local_overlay_ips = lambda: ["192.0.2.99"]
    real = env.ship.__class__.__call__

    def unreachable(self, ship, command):
        if command[:1] == ["timeout"]:
            raise Unreachable("Cannot reach ship-01")
        return real(self, ship, command)

    env.ship.__class__.__call__ = unreachable
    try:
        code, out, err = env.run("overlay", "check", "--probe")
    finally:
        env.ship.__class__.__call__ = real
    assert _re.search(r"^SKIP\s+ship-01 cannot reach this computer", out, _re.M), out


def test_setup_then_check_finds_the_secret_where_it_was_written(env, home, monkeypatch):
    """FLEETLEDGER_CONFIG_DIR (set by the fixture) moves the secret; ships.toml must say so."""
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["keys"])
    interactive(env, ["", "", "", "kExampleClient1"], tailnet=tailnet)
    assert env.run("overlay", "setup")[0] == 0
    recorded = env.store.load().overlay["api"]["oauth_secret_file"]
    from fleetledger import paths as fl_paths

    assert fl_paths.expand(recorded) == home / "config" / "tailscale-oauth"
    code, out, err = env.run("overlay", "check")
    assert any(line.startswith("OK") and "OAuth secret" in line
               for line in out.splitlines()), out



def test_setup_force_replaces_and_says_the_old_secret_is_unused(env, home):
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["keys"])
    interactive(env, ["", "", "", "kExampleClient1"], tailnet=tailnet)
    assert env.run("overlay", "setup")[0] == 0
    interactive(env, ["tailscale", "paste"])
    code, out, err = env.run("overlay", "setup", "--force")
    assert code == 0, err
    assert env.store.load().overlay["key_source"] == "prompt"
    assert "no longer used" in out and "tailscale-oauth" in out


def test_setup_at_the_devices_level_tries_the_device_list(env):
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["devices"])
    interactive(env, level_answers("devices"), tailnet=tailnet)
    assert env.run("overlay", "setup")[0] == 0
    assert tailnet.used == ["devices"]  # checked by use; never the policy at this level


def test_setup_shows_the_tag_owner_before_asking_for_the_client(env):
    seen = []
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["keys"])
    interactive(env, ["", "", "", "kExampleClient1"], tailnet=tailnet)
    real = env.app.confirm

    def spy(prompt):
        seen.append((prompt, env.app.out.getvalue()))
        return real(prompt)

    env.app.confirm = spy
    env.run("overlay", "setup")
    before_client = next(out for prompt, out in seen if "client id" in prompt)
    assert '"tag:ship": ["autogroup:admin"]' in before_client


def test_check_fails_on_a_bad_secret_file_or_an_unreadable_policy(env, home):
    import re as _re

    class Unreadable(FakeTailscale):
        def policy_text(self):
            from fleetledger.errors import FleetError

            raise FleetError("Tailscale API: reading the policy failed (403).")

    oauth_overlay(env, home, Unreadable())
    (home / "config" / "tailscale-oauth").chmod(0o644)
    code, out, err = env.run("overlay", "check")
    assert code == 1
    assert _re.search(r"^FAIL\s+OAuth secret", out, _re.M)
    assert _re.search(r"^FAIL\s+read the policy", out, _re.M)


@pytest.mark.parametrize("expiry, state", [(600, "OK"), (3600, "OK"), (7200, "WARN")])
def test_check_wants_keys_that_expire_within_an_hour(env, home, expiry, state):
    import re as _re

    oauth_overlay(env, home, FakeTailscale())
    with env.store.edit() as ed:
        ed.doc["overlay"]["api"]["key_expiry_s"] = expiry
    code, out, err = env.run("overlay", "check")
    assert _re.search(rf"^{state}\s+join keys expire", out, _re.M), out


def test_check_matches_stale_devices_by_hostname_too(env, home):
    oauth_overlay(env, home, FakeTailscale(devices=[
        {"name": "ship-01-1.example-tailnet.ts.net", "hostname": "ship-01", "tags": ["tag:ship"]}]))
    code, out, err = env.run("overlay", "check")
    assert any(line.startswith("OK") and "tailnet devices" in line for line in out.splitlines())


# --- the OAuth secret in the OS keyring ---------------------------------------------------


def secure_keyring(monkeypatch, locked=False):
    from fleetledger.overlay import secret_store
    from test_secret_store import fake_backend

    backend = fake_backend(locked=locked)
    monkeypatch.setattr(secret_store, "get_keyring", lambda: backend)
    return backend


def test_setup_keeps_the_secret_in_the_keyring_by_default(env, home, monkeypatch):
    backend = secure_keyring(monkeypatch)
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["keys"])
    interactive(env, ["", "", "", "kExampleClient1", ""], tailnet=tailnet)  # store: default
    code, out, err = env.run("overlay", "setup")
    assert code == 0, err
    api = env.store.load().overlay["api"]
    assert api["secret_store"] == "keyring"
    assert api["keyring_item"] == "tailscale-oauth:kExampleClient1"
    assert "oauth_secret_file" not in api
    assert backend.items == {("fleetledger", "tailscale-oauth:kExampleClient1"): OAUTH_SECRET}
    assert not (home / "config" / "tailscale-oauth").exists()
    assert OAUTH_SECRET not in env.store.path.read_text() + out + err
    assert "any program running as you" in out  # the honest trade-off
    code, out, err = env.run("overlay", "check")
    assert any(line.startswith("OK") and "OAuth secret" in line and "keyring" in line
               for line in out.splitlines()), out


def test_setup_can_choose_the_file_even_with_a_keyring(env, home, monkeypatch):
    backend = secure_keyring(monkeypatch)
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["keys"])
    interactive(env, ["", "", "", "kExampleClient1", "file"], tailnet=tailnet)
    assert env.run("overlay", "setup")[0] == 0
    assert env.store.load().overlay["api"]["secret_store"] == "file"
    assert backend.items == {} and (home / "config" / "tailscale-oauth").exists()


def test_setup_uses_the_file_when_the_keyring_is_locked(env, home, monkeypatch):
    secure_keyring(monkeypatch, locked=True)
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["keys"])
    interactive(env, ["", "", "", "kExampleClient1"], tailnet=tailnet)  # no store question
    code, out, err = env.run("overlay", "setup")
    assert code == 0 and "cannot be used here" in out
    assert env.store.load().overlay["api"]["secret_store"] == "file"


def test_check_fails_plainly_on_a_locked_keyring(env, home, monkeypatch):
    import re as _re

    secure_keyring(monkeypatch)
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["keys"])
    interactive(env, ["", "", "", "kExampleClient1", ""], tailnet=tailnet)
    assert env.run("overlay", "setup")[0] == 0
    secure_keyring(monkeypatch, locked=True)
    code, out, err = env.run("overlay", "check")
    assert code == 1
    assert _re.search(r"^FAIL\s+OAuth secret .*locked.*Unlock it", out, _re.M), out


def test_setup_reuses_a_secret_already_in_the_keyring(env, home, monkeypatch):
    """The secret was stored by hand under the stable names: setup offers to reuse it,
    checks it with Tailscale, never asks for it again and never prints it."""
    backend = secure_keyring(monkeypatch)
    backend.items[("fleetledger", "tailscale-oauth:kExampleClient1")] = OAUTH_SECRET
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["keys"])
    asked = []
    interactive(env, ["", "", "", "kExampleClient1", ""], tailnet=tailnet)  # reuse: yes
    env.app.ask_secret = lambda prompt: asked.append(prompt) or "never-asked"
    code, out, err = env.run("overlay", "setup")
    assert code == 0, err
    assert asked == []  # the secret prompt never came
    assert tailnet.secrets_seen == [OAUTH_SECRET]  # checked with Tailscale first
    api = env.store.load().overlay["api"]
    assert api["secret_store"] == "keyring"
    assert api["keyring_item"] == "tailscale-oauth:kExampleClient1"
    assert backend.items == {("fleetledger", "tailscale-oauth:kExampleClient1"): OAUTH_SECRET}
    assert backend.sets == []  # reused as it was, never written again
    assert OAUTH_SECRET not in out + err + env.store.path.read_text()


@pytest.mark.parametrize("stored", ["two\nlines", "x" * 513])
def test_an_implausible_keyring_item_is_not_reused(env, monkeypatch, stored):
    backend = secure_keyring(monkeypatch)
    backend.items[("fleetledger", "tailscale-oauth:kExampleClient1")] = stored
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["keys"])
    interactive(env, ["", "", "", "kExampleClient1", ""], tailnet=tailnet)  # store: keyring
    code, out, err = env.run("overlay", "setup")
    assert code == 0, err
    assert "plausible" in err
    assert tailnet.secrets_seen == [OAUTH_SECRET]  # asked for, and used, a new one


def test_setup_with_an_existing_keyring_secret_can_take_a_new_one(env, monkeypatch):
    backend = secure_keyring(monkeypatch)
    backend.items[("fleetledger", "tailscale-oauth:kExampleClient1")] = "tskey-client-OLD000000"
    tailnet = FakeTailscale(scopes=LEVEL_SCOPES["keys"])
    interactive(env, ["", "", "", "kExampleClient1", "no", ""], tailnet=tailnet)
    assert env.run("overlay", "setup")[0] == 0
    assert tailnet.secrets_seen == [OAUTH_SECRET]
    assert backend.items[("fleetledger", "tailscale-oauth:kExampleClient1")] == OAUTH_SECRET


def test_a_reused_secret_that_tailscale_refuses_saves_nothing(env, monkeypatch):
    backend = secure_keyring(monkeypatch)
    backend.items[("fleetledger", "tailscale-oauth:kExampleClient1")] = OAUTH_SECRET
    tailnet = FakeTailscale(scopes=())  # the stored secret's client lacks auth_keys
    interactive(env, ["", "", "", "kExampleClient1", ""], tailnet=tailnet)
    code, out, err = env.run("overlay", "setup")
    assert code != 0 and "auth_keys" in err and env.store.load().overlay is None


def test_the_keyring_names_are_stable():
    from fleetledger.overlay import secret_store

    assert secret_store.SERVICE == "fleetledger"
    assert secret_store.oauth_item("kExampleClient1") == "tailscale-oauth:kExampleClient1"
    assert "kExampleClient1" not in secret_store.SERVICE


# --- ship-fleet doctor ---------------------------------------------------------------------


def _no_real_commands(monkeypatch):
    """Any command doctor runs without going through app.run_cmd fails the test."""
    from fleetledger import firstrun

    def refuse(argv, **kw):
        raise AssertionError(f"ran a real command: {argv}")

    monkeypatch.setattr(firstrun, "_run", refuse)


def doctor_tools(env, monkeypatch, *, agent=True, loaded=True):
    """Fake ssh-keygen -lf and ssh-add -l (nothing real is asked)."""
    import subprocess as _sp

    if agent:
        monkeypatch.setenv("SSH_AUTH_SOCK", "/nonexistent/agent.sock")
    else:
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    env.commands = []
    _no_real_commands(monkeypatch)

    def run(argv, **kw):
        env.commands.append(list(argv[:2]))
        if argv[:2] == ["ssh-keygen", "-lf"]:
            return _sp.CompletedProcess(argv, 0, "256 SHA256:adminFPR fleet-admin (ED25519)\n", "")
        if argv[:2] == ["ssh-add", "-l"]:
            line = "256 SHA256:adminFPR fleet-admin (ED25519)\n" if loaded else ""
            return _sp.CompletedProcess(argv, 0 if loaded else 1, line, "")
        raise AssertionError(argv)

    env.app.run_cmd = run


def doctor_rows(out):
    import re as _re

    return {m.group(2).strip(): m.group(1) for m in
            _re.finditer(r"^(ok|info|WARN|MISSING|FAIL|-)\s+\S+\s+(.+?)\s{2,}", out, _re.M)}


def test_doctor_on_a_fresh_computer_says_run_setup(env, tmp_path, monkeypatch):
    from fleetledger.store import TomlFileStore

    env.app.store_factory = lambda: TomlFileStore(tmp_path / "none" / "ships.toml")
    code, out, err = env.run("doctor")
    assert code == 1
    assert doctor_rows(out)["inventory"] == "MISSING" and "ship-fleet setup" in out


def config_snapshot(folder):
    """Every file under folder: name, bytes and mtime (doctor must change none)."""
    return {p: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_doctor_reports_each_piece_and_the_next_command(env, home, monkeypatch):
    doctor_tools(env, monkeypatch, loaded=False)
    backend = secure_keyring(monkeypatch)
    before = config_snapshot(home)
    code, out, err = env.run("doctor", "--offline")
    rows = doctor_rows(out)
    assert rows["inventory"] == "ok" and rows["admin key"] == "ok"
    assert rows["ssh-agent"] == "MISSING" and "ssh-add " in out
    assert rows["certificate authority"] == "MISSING" and "ship-fleet ca init" in out
    assert rows["driver"] == "ok"  # manual
    assert rows["overlay"] == "-"
    assert code == 1
    assert config_snapshot(home) == before  # read-only: no file written, touched or added
    assert backend.sets == []  # nothing stored in the keyring
    assert env.commands == [["ssh-add", "-l"], ["ssh-keygen", "-lf"]]  # and nothing else run


def test_doctor_with_the_admin_key_loaded(env, monkeypatch):
    doctor_tools(env, monkeypatch, loaded=True)
    code, out, err = env.run("doctor", "--offline")
    assert doctor_rows(out)["ssh-agent"] == "ok"


def test_doctor_without_an_agent(env, monkeypatch):
    doctor_tools(env, monkeypatch, agent=False)
    code, out, err = env.run("doctor", "--offline")
    assert doctor_rows(out)["ssh-agent"] == "MISSING" and 'eval "$(ssh-agent)"' in out


@pytest.mark.parametrize("value", [":/x", "/x:", "/a::/b"])
def test_doctor_flags_empty_pythonpath_entries(env, monkeypatch, value):
    doctor_tools(env, monkeypatch)
    monkeypatch.setenv("PYTHONPATH", value)
    code, out, err = env.run("doctor", "--offline")
    assert doctor_rows(out)["PYTHONPATH"] == "WARN" and "stray ':'" in out


def test_doctor_is_quiet_about_a_clean_pythonpath(env, monkeypatch):
    doctor_tools(env, monkeypatch)
    monkeypatch.setenv("PYTHONPATH", "/a:/b")
    code, out, err = env.run("doctor", "--offline")
    assert "PYTHONPATH" not in doctor_rows(out)


def test_doctor_offline_never_contacts_proxmox(env, home, monkeypatch):
    doctor_tools(env, monkeypatch)
    proxmox_inventory(env, home)
    env.app.proxmox_api = lambda config: (_ for _ in ()).throw(AssertionError("network"))
    code, out, err = env.run("doctor", "--offline")
    assert "Traceback" not in err
    assert doctor_rows(out)["Proxmox API"] == "-" and doctor_rows(out)["Proxmox token"] == "ok"


def test_doctor_does_not_take_another_key_for_the_admin_key(env, monkeypatch):
    import subprocess as _sp

    doctor_tools(env, monkeypatch)

    def run(argv, **kw):
        if argv[:2] == ["ssh-keygen", "-lf"]:
            return _sp.CompletedProcess(argv, 0, "256 SHA256:adminFPR x (ED25519)\n", "")
        return _sp.CompletedProcess(argv, 0, "256 SHA256:otherKEY you (ED25519)\n", "")

    env.app.run_cmd = run
    code, out, err = env.run("doctor", "--offline")
    assert doctor_rows(out)["ssh-agent"] == "MISSING" and "\n  ssh-add " in out


def test_doctor_says_an_agent_that_cannot_be_reached(env, monkeypatch):
    import subprocess as _sp

    doctor_tools(env, monkeypatch)

    def run(argv, **kw):
        if argv[:2] == ["ssh-add", "-l"]:
            return _sp.CompletedProcess(argv, 2, "", "Could not open a connection")
        return _sp.CompletedProcess(argv, 0, "256 SHA256:adminFPR x (ED25519)\n", "")

    env.app.run_cmd = run
    code, out, err = env.run("doctor", "--offline")
    assert doctor_rows(out)["ssh-agent"] == "FAIL" and "does not answer" in out


def test_doctor_names_missing_ca_and_agent_key_files(env, home, monkeypatch):
    doctor_tools(env, monkeypatch)
    with env.store.edit() as ed:
        ed.set_ca({"host_key": str(home / "keys" / "gone_host_ca"),
                   "user_key": str(home / "keys" / "gone_user_ca")})
    (home / "keys" / "you.pub").unlink()
    code, out, err = env.run("doctor", "--offline")
    rows = doctor_rows(out)
    assert rows["certificate authority"] == "FAIL" and "gone_host_ca" in out
    assert rows["your keys for Ships"] == "FAIL" and "you.pub" in out
    assert code == 1


def test_doctor_lists_each_next_command_once(env, monkeypatch):
    from fleetledger import firstrun

    items = [firstrun.Item("a", "one", "missing", "x", "ship-fleet setup"),
             firstrun.Item("b", "two", "fail", "y", "ship-fleet setup")]
    monkeypatch.setattr(firstrun, "everything", lambda app, offline=False: items)
    code, out, err = env.run("doctor")
    assert out.count("\n  ship-fleet setup") == 1 and code == 1


def test_doctor_passes_a_set_up_computer_with_no_ships_yet(env, home, monkeypatch):
    """Ships are the next step, not a failure: exit 0, with ship-fleet new in Next."""
    from conftest import keygen

    doctor_tools(env, monkeypatch, loaded=True)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    keygen(home / "keys" / "host_ca", "host-ca")
    keygen(home / "keys" / "user_ca", "user-ca")
    with env.store.edit() as ed:
        for ship in env.store.load().ships():
            ed.remove_ship(ship.name)
        ed.set_ca({"host_key": str(home / "keys" / "host_ca"),
                   "user_key": str(home / "keys" / "user_ca")})
    code, out, err = env.run("doctor", "--offline")
    assert code == 0, out
    assert doctor_rows(out)["Ships"] == "info" and "ship-fleet new ship-01" in out
    assert "Everything is in place." in out


class FakeProxmoxAPI:
    def __init__(self, template=True, fail=False):
        self.template, self.fail = template, fail

    def get(self, path, **params):
        from fleetledger.drivers.proxmox_api import ProxmoxError

        if self.fail:
            raise ProxmoxError("Cannot reach the Proxmox API at pve.example.com: refused")
        if path == "/version":
            return {"version": "9.2.11"}
        if path.endswith("/config"):
            return {"name": "shipshape-base", "template": 1} if self.template else {"name": "x"}
        raise AssertionError(path)


def proxmox_inventory(env, home):
    token = home / "config" / "proxmox-token"
    token.write_text("00000000-test\n")
    token.chmod(0o600)
    with env.store.edit() as ed:
        ed.doc["defaults"]["driver"] = "proxmox"
        ed.doc["drivers"] = {"proxmox": {
            "api_url": "https://pve.example.com:8006/api2/json", "node": "pve",
            "pool": "shipshape", "template_vmid": 9000, "seed_storage": "shipshape-seeds",
            "disk_storage": "local-lvm", "bridge": "vmbr0", "token_id": "shipshape@pve!build",
            "token_file": str(token), "tls_fingerprint": ":".join(["AB"] * 32)}}


@pytest.mark.parametrize("api, template_state, api_state", [
    (FakeProxmoxAPI(), "ok", "ok"),
    (FakeProxmoxAPI(template=False), "FAIL", "ok"),
    (FakeProxmoxAPI(fail=True), None, "FAIL"),
])
def test_doctor_checks_proxmox(env, home, monkeypatch, api, template_state, api_state):
    doctor_tools(env, monkeypatch)
    proxmox_inventory(env, home)
    env.app.proxmox_api = lambda config: api
    code, out, err = env.run("doctor")
    rows = doctor_rows(out)
    assert rows["Proxmox token"] == "ok" and rows["Proxmox API"] == api_state
    assert rows.get("template") == template_state
    assert "00000000-test" not in out + err  # the token is never shown


def test_doctor_reports_missing_openssh_tools_instead_of_crashing(env, monkeypatch):
    def run(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setenv("SSH_AUTH_SOCK", "/nonexistent/agent.sock")
    env.app.run_cmd = run
    code, out, err = env.run("doctor", "--offline")
    assert code == 1 and "Traceback" not in err
    assert doctor_rows(out)["ssh-agent"] == "FAIL" and "does not answer" in out
    assert 'eval "$(ssh-agent)"' not in out  # would start a second agent


def test_doctor_reports_a_hung_agent_instead_of_crashing(env, monkeypatch):
    import subprocess as _sp

    def run(argv, **kw):
        if argv[:2] == ["ssh-add", "-l"]:
            raise _sp.TimeoutExpired(argv, 10)
        return _sp.CompletedProcess(argv, 0, "256 SHA256:adminFPR x (ED25519)\n", "")

    monkeypatch.setenv("SSH_AUTH_SOCK", "/nonexistent/agent.sock")
    env.app.run_cmd = run
    code, out, err = env.run("doctor", "--offline")
    assert code == 1 and doctor_rows(out)["ssh-agent"] == "FAIL"


def test_doctor_says_when_the_admin_fingerprint_cannot_be_read(env, monkeypatch):
    import subprocess as _sp

    def run(argv, **kw):
        if argv[:2] == ["ssh-keygen", "-lf"]:
            return _sp.CompletedProcess(argv, 1, "", "not a key")
        return _sp.CompletedProcess(argv, 0, "256 SHA256:adminFPR x (ED25519)\n", "")

    monkeypatch.setenv("SSH_AUTH_SOCK", "/nonexistent/agent.sock")
    env.app.run_cmd = run
    code, out, err = env.run("doctor", "--offline")
    assert doctor_rows(out)["ssh-agent"] == "FAIL" and "ssh-keygen -lf" in out


def test_doctor_reports_an_unreadable_token_file_instead_of_crashing(env, home, monkeypatch):
    doctor_tools(env, monkeypatch)
    proxmox_inventory(env, home)
    token = home / "config" / "proxmox-token"
    token.unlink()
    token.mkdir(mode=0o700)  # a folder where the file should be
    code, out, err = env.run("doctor", "--offline")
    assert code == 1 and "Traceback" not in err
    assert doctor_rows(out)["Proxmox token"] == "FAIL" and "Cannot read" in out


def keyring_overlay(env, item="tailscale-oauth:kExampleClient1"):
    with env.store.edit() as ed:
        ed.set_overlay({"provider": "tailscale", "tags": ["tag:ship"],
                        "key_source": "tailscale-api",
                        "api": {"tailnet": "-", "oauth_client_id": "kExampleClient1",
                                "secret_store": "keyring", "keyring_item": item}})


@pytest.mark.parametrize("state, row", [("ok", "ok"), ("locked", "FAIL"), ("absent", "FAIL")])
def test_doctor_checks_the_oauth_secret_in_the_keyring(env, monkeypatch, state, row):
    doctor_tools(env, monkeypatch)
    backend = secure_keyring(monkeypatch, locked=state == "locked")
    if state == "ok":
        backend.items[("fleetledger", "tailscale-oauth:kExampleClient1")] = OAUTH_SECRET
    keyring_overlay(env)
    code, out, err = env.run("doctor", "--offline")
    assert doctor_rows(out)["OAuth secret"] == row
    if state == "locked":
        assert "Unlock it" in out
    assert OAUTH_SECRET not in out + err
    assert backend.sets == []


@pytest.mark.parametrize("state, row", [("ok", "ok"), ("open", "FAIL"), ("absent", "FAIL")])
def test_doctor_checks_the_oauth_secret_file(env, home, monkeypatch, state, row):
    doctor_tools(env, monkeypatch)
    secret = home / "config" / "tailscale-oauth"
    if state != "absent":
        secret.write_text(OAUTH_SECRET + "\n")
        secret.chmod(0o600 if state == "ok" else 0o644)
    with env.store.edit() as ed:
        ed.set_overlay({"provider": "tailscale", "tags": ["tag:ship"],
                        "key_source": "tailscale-api",
                        "api": {"tailnet": "-", "oauth_client_id": "kExampleClient1",
                                "secret_store": "file", "oauth_secret_file": str(secret)}})
    code, out, err = env.run("doctor", "--offline")
    assert doctor_rows(out)["OAuth secret"] == row
    assert OAUTH_SECRET not in out + err


def test_doctor_lists_ships_still_enrolling(env, monkeypatch):
    doctor_tools(env, monkeypatch)
    with env.store.edit() as ed:
        ed.set_field("ship-01", "enrolled", None)  # still being made
    code, out, err = env.run("doctor", "--offline")
    assert "ship-fleet status ship-02 --wait" not in out  # enrolled: not flagged
    assert "ship-fleet status ship-01 --wait" in out


def test_ship_fleet_attention_agrees_on_not_enrolled(env):
    with env.store.edit() as ed:
        ed.set_field("ship-02", "enrolled", None)
    env.ship.down.add("ship-02")
    code, out, _ = env.run("attention")
    assert "not-enrolled" in out and "Not enrolled yet" in out
