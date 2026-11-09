"""FleetLedger on its own VM (appliance.py, selfupdate.py): the seed, the [fleet] self
guard, the tailnet rules, doctor's checks, and self-update.

The checkout is the fake one from conftest, made a git repository with an "origin"
that is a bare repository here (renamed to an example https address once fetched);
systemd and Proxmox are fakes. Nothing reaches a network or a real hypervisor.
"""

from __future__ import annotations

import io
import json
import stat
import subprocess

import pytest
import yaml
from fleetledger.drivers.proxmox import NotOurs, ProxmoxDriver
from fleetledger.drivers.proxmox_api import ProxmoxError
from fleetledger.errors import ConfigError, FleetError
from fleetledger.seed import iso
from test_service import FakeSystem, units  # noqa: F401 - the unit folder fixture

from fleetledger import appliance, cli, firstrun, ssh

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com"]
ORIGIN = "https://example.com/you/ShipShape.git"
PX = {"api_url": "https://pve.example.com:8006/api2/json", "node": "pve", "pool": "shipshape",
      "template_vmid": 9000, "seed_storage": "local", "disk_storage": "local-lvm",
      "bridge": "vmbr0", "token_id": "fleet@pve!x", "token_file": "~/token",
      "tls_fingerprint": ":".join(["AB"] * 32), "vmid_range": [9100, 9199]}


def git(root, *args):
    return subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def checkout(fake_repo, tmp_path):
    """The fake checkout, committed and pushed to an origin, as a real one would be."""
    root = fake_repo.root
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git(root, "remote", "add", "origin", str(bare))
    git(root, "push", "-q", "origin", "main")
    git(root, "fetch", "-q", "origin")
    git(root, "remote", "set-url", "origin", ORIGIN)
    return Checkout(fake_repo, bare)


class Checkout:
    """The fake checkout (a frozen Repo), and the bare repository standing for origin."""

    def __init__(self, repo, bare):
        self.repo, self.bare = repo, bare

    def __getattr__(self, name):
        return getattr(self.repo, name)


def app_for(store, repo, system=None):
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store, repo_factory=lambda: repo,
                  stdin_isatty=lambda: False)
    if system is not None:
        app.system = system
    return app, out


def with_proxmox(store):
    with store.edit() as ed:
        ed.set_driver("proxmox", dict(PX))


# --- the seed --------------------------------------------------------------------------------


def test_the_seed_makes_a_locked_down_vm_and_records_it_as_self(store, home, checkout):
    with_proxmox(store)
    app, out = app_for(store, checkout)
    assert cli.main(["appliance", "seed", "--vmid", "120"], app) == 0, out.getvalue()
    path = home / "data" / "seeds" / "appliance-fleetledger.iso"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    label, files = iso.read(path.read_bytes())
    assert label == "CIDATA" and set(files) == {"user-data", "meta-data", "network-config"}
    doc = yaml.safe_load(files["user-data"])
    # Your keys only, no passwords, root off: FleetLedger's user has no sudo; the system's
    # admin does. Its own host key, pinned here.
    assert [u["name"] for u in doc["users"]] == ["fleetledger", "vmadmin"]
    user, admin = doc["users"]
    assert "sudo" not in user and user["lock_passwd"] is True and admin["lock_passwd"] is True
    you = (home / "keys" / "you.pub").read_text().strip()
    assert user["ssh_authorized_keys"] == admin["ssh_authorized_keys"] == [you]
    assert doc["ssh_pwauth"] is False and doc["disable_root"] is True
    entry = store.load().doc["fleet"]["self"]
    assert entry["name"] == "fleetledger" and entry["vmid"] == 120
    assert entry["names"] == ["fleetledger.local"]
    assert doc["ssh_keys"]["ed25519_public"].split()[:2] == entry["host_key"].split()
    # The only secret in it is its own host key.
    assert files["user-data"].count("PRIVATE KEY-----") == 2  # BEGIN and END of one key
    # This very commit, installed; the service ready but not started; a reboot drains.
    commit = git(checkout.root, "rev-parse", "HEAD")
    text = files["user-data"]
    assert f"checkout --detach {commit}" in text and ORIGIN in text
    assert "service install --port 8765 --no-start" in text
    assert ["loginctl", "enable-linger", "fleetledger"] in doc["runcmd"]
    assert doc["swap"] == {"size": 0}
    drop_in = {f["path"]: f["content"] for f in doc["write_files"]}
    assert drop_in["/etc/systemd/system/user@.service.d/fleetledger-drain.conf"] == \
        "[Service]\nTimeoutStopSec=45min\n"
    assert "AllowUsers fleetledger vmadmin" in \
        drop_in["/etc/ssh/sshd_config.d/10-fleetledger.conf"]
    assert "libnss-mdns" in doc["packages"]
    # The steps: the pinned Ubuntu image checked by its sha256, and no pool.
    said = out.getvalue()
    sha = checkout.versions["ubuntu"]["images"]["amd64"]["sha256"]
    assert f"echo '{sha}  /var/tmp/" in said and "| sha256sum -c" in said
    create = next(line for line in said.splitlines() if line.startswith("qm create 120 "))
    assert "--pool" not in create and "--cpu host" in create and "--onboot 1" in create
    assert "local:iso/appliance-fleetledger.iso,media=cdrom" in create
    assert f"fleetledger,fleetledger.local {entry['host_key']}" in said


@pytest.mark.parametrize("argv, why", [
    (["--vmid", "9150"], "in the Ships' range"),
    (["--vmid", "120", "--name", "ship-01"], "is a Ship's name"),
    (["--vmid", "120", "--repo", "git@example.com:you/ShipShape.git"], "over https"),
])
def test_the_seed_refuses_what_would_clash(store, home, checkout, argv, why):
    with_proxmox(store)
    app, out = app_for(store, checkout)
    assert cli.main(["appliance", "seed", *argv], app) == 1
    assert why in out.getvalue()
    assert "self" not in store.load().doc["fleet"]


def test_the_seed_wants_a_pushed_clean_checkout(store, home, checkout):
    (checkout.root / "fleetledger").mkdir(exist_ok=True)
    (checkout.root / "fleetledger" / "new.py").write_text("x = 1\n")
    git(checkout.root, "add", "-A")
    app, out = app_for(store, checkout)
    assert cli.main(["appliance", "seed", "--vmid", "120"], app) == 1
    assert "uncommitted changes" in out.getvalue()
    git(checkout.root, "commit", "-qm", "local only")
    app, out = app_for(store, checkout)
    assert cli.main(["appliance", "seed", "--vmid", "120"], app) == 1
    assert "No remote branch has commit" in out.getvalue()


def test_one_vm_at_a_time_and_forget(store, home, checkout):
    app, out = app_for(store, checkout)
    assert cli.main(["appliance", "seed", "--vmid", "120"], app) == 0
    app, out = app_for(store, checkout)
    assert cli.main(["appliance", "seed", "--vmid", "121"], app) == 1
    assert "recorded already" in out.getvalue()
    app, out = app_for(store, checkout)
    assert cli.main(["appliance", "show"], app) == 0 and "VM id 120" in out.getvalue()
    assert cli.main(["appliance", "forget"], app_for(store, checkout)[0]) == 0
    assert "self" not in store.load().doc["fleet"]


# --- the guard ------------------------------------------------------------------------------


def _self(store, vmid=120, key=None):
    key = key or "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHNlbGZzZWxmc2VsZnNlbGZzZWxmc2VsZnNlbGZzZWxm"
    with store.edit() as ed:
        appliance_entry = {"name": "fleetledger", "vmid": vmid, "names": ["fleetledger.local"],
                           "host_key": key}
        import tomlkit

        table = tomlkit.inline_table()
        table.update(appliance_entry)
        ed.doc["fleet"]["self"] = table


def test_no_ship_may_be_fleetledgers_own_vm(store):
    _self(store)
    host_key = store.load().ship("ship-01").host_key
    for change in ({"name": "fleetledger"}, {"vmid": 120}, {"host_key": "self"}):
        entry = {"id": "0b6e3e51-79f8-4e2a-8a77-5a7b2d1c3f10", "driver": "proxmox",
                 "names": ["x.local"], "host_key": host_key.replace("Gk", "Gl")}
        name = change.get("name", "ship-09")
        if "vmid" in change:
            entry["driver_ref"] = {"vmid": 120}
        if "host_key" in change:
            entry["host_key"] = store.load().doc["fleet"]["self"]["host_key"]
        with pytest.raises(ConfigError, match=r"FleetLedger's own VM \(\[fleet\] self\)"):
            with store.edit() as ed:
                ed.add_ship(name, entry)


class FakeAPI:
    def __init__(self, visible=()):
        self.calls = []
        self.visible = set(visible)

    def get(self, path, **kw):
        self.calls.append(("get", path, kw))
        if path == "/cluster/nextid":
            if kw.get("vmid") in (9100,):
                raise ProxmoxError("in use", status=400)
            return kw.get("vmid", 9100)
        vmid = int(path.split("/")[4])
        if vmid in self.visible:
            return {"name": "fleetledger"}
        raise ProxmoxError("forbidden", status=403)

    def post(self, *a, **kw):
        self.calls.append(("post", a, kw))

    def delete(self, *a, **kw):
        self.calls.append(("delete", a, kw))


def test_the_proxmox_driver_never_touches_fleetledgers_own_vm():
    from fleetledger.inventory import Ship

    api = FakeAPI(visible={120})  # even were it in the pool by mistake
    driver = ProxmoxDriver({**PX, "vmid_range": [9100, 9102]}, api=api, protected=120)
    ship = Ship(name="fleetledger", id="x", driver="proxmox", names=("a",), host_key="k",
                driver_ref={"vmid": 120})
    for call in (lambda: driver.destroy(ship), lambda: driver.stop(ship),
                 lambda: driver.reboot(ship), lambda: driver.resize(ship, {"cores": 4}),
                 lambda: driver.snapshot(ship, "before"), lambda: driver.check_name(120, "x")):
        with pytest.raises(FleetError, match="FleetLedger's own VM"):
            call()
    assert api.calls == []  # refused before anything was asked
    driver = ProxmoxDriver({**PX, "vmid_range": [9100, 9102]}, api=FakeAPI(), protected=9101)
    assert driver.allocate_vmid() == 9102  # 9100 taken, 9101 its own: skipped
    assert not isinstance(FleetError("x"), NotOurs)


def test_the_manual_driver_prints_no_step_for_it(store):
    from fleetledger.drivers import get_driver

    with_proxmox(store)
    _self(store, vmid=120)
    driver = get_driver(store.load(), "manual")
    from fleetledger.inventory import Ship

    ship = Ship(name="other", id="x", driver="manual", names=("a",), host_key="k",
                driver_ref={"vmid": 120})
    with pytest.raises(FleetError, match="FleetLedger's own VM"):
        driver.destroy(ship)
    assert get_driver(store.load(), "proxmox").protected == 120


def test_its_pin_is_used_for_migrate_push_and_never_weakened(store, home):
    _self(store)
    inv = store.load()
    text = ssh.known_hosts_text(inv)
    assert f"{appliance.ALIAS} {inv.doc['fleet']['self']['host_key']}" in text
    from fleetledger import migrate

    pinned = appliance.push_options(inv, "fleetledger@fleetledger.local")
    assert f"HostKeyAlias={appliance.ALIAS}" in pinned
    assert appliance.push_options(inv, "you@desktop.example") == []
    argv = migrate.ssh_argv("fleetledger.local", "x", [*pinned, "UserKnownHostsFile=/dev/null"])
    options = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
    first = next(o for o in options if o.startswith("UserKnownHostsFile="))
    assert first == pinned[0]  # ssh keeps the first: the pin wins over a later -o


# --- the tailnet rules ------------------------------------------------------------------------


def test_the_overlay_policy_gains_the_vms_two_rules_and_stays_clean(store, home):
    from fleetledger.overlay import providers, rules

    base = json.dumps(providers.policy_doc(["tag:ship"]))
    doc = json.loads(appliance.policy_with_self(base))
    assert doc["tagOwners"]["tag:fleetledger"] == ["autogroup:admin"]
    assert {"action": "accept", "src": ["tag:fleetledger"], "dst": ["tag:ship:22"]} in doc["acls"]
    assert {"action": "accept", "src": ["autogroup:member"],
            "dst": ["tag:fleetledger:22", "tag:fleetledger:443"]} in doc["acls"]
    assert {"src": "tag:ship", "deny": ["tag:fleetledger:22", "tag:fleetledger:443"]} in \
        doc["tests"]
    findings = {f.name: f.ok for f in rules.findings(doc, ["tag:ship"])}
    assert findings["Ships cannot start connections"] is True
    assert findings["your devices reach Ships on port 22"] is True


# --- doctor ------------------------------------------------------------------------------------


def test_doctor_checks_the_token_cannot_see_it(store):
    _self(store)
    inv = store.load()
    ok = firstrun.self_hidden(FakeAPI(), PX, inv)
    assert [(i.state, "out of the token's reach" in i.detail) for i in ok] == [("ok", True)]
    bad = firstrun.self_hidden(FakeAPI(visible={120}), PX, inv)
    assert bad[0].state == "fail" and "in the pool" in bad[0].detail


def test_doctor_on_the_vm_says_whether_image_builds_get_kvm(store, monkeypatch):
    _self(store)
    monkeypatch.setattr(firstrun.socket if hasattr(firstrun, "socket") else __import__("socket"),
                        "gethostname", lambda: "fleetledger")
    monkeypatch.setattr(firstrun.os, "access", lambda path, mode: False)
    items = firstrun.appliance_items(store.load())
    assert items[0].state == "info" and "no KVM here" in items[0].detail
    monkeypatch.setattr(firstrun.os, "access", lambda path, mode: True)
    assert firstrun.appliance_items(store.load())[0].state == "ok"


# --- self-update -------------------------------------------------------------------------------


def _advance(checkout, tmp_path, name, change=None):
    """A newer commit on origin (from another clone)."""
    other = tmp_path / f"other-{name}"
    subprocess.run(["git", "clone", "-q", str(checkout.bare), str(other)], check=True)
    target = other / (change or "ship/files/new.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(name + "\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", name)
    git(other, "push", "-q", "origin", "main")
    return git(other, "rev-parse", "HEAD")


@pytest.fixture
def fetchable(checkout):
    git(checkout.root, "remote", "set-url", "origin", str(checkout.bare))
    return checkout


def test_self_update_moves_ahead_and_restarts_the_service(store, home, fetchable, tmp_path,
                                                          units):  # noqa: F811
    target = _advance(fetchable, tmp_path, "newer")
    installs = []
    system = FakeSystem(active=True)
    units.mkdir(parents=True)
    (units / "fleetledger.service").write_text("# Written by ship-fleet service install\n")
    app, out = app_for(store, fetchable, system)
    app.run_install = lambda root: installs.append(root) or 0
    assert cli.main(["self-update"], app) == 0, out.getvalue()
    assert git(fetchable.root, "rev-parse", "HEAD") == target
    assert installs == []  # its install did not change
    assert ["systemctl", "--user", "--no-block", "restart", "fleetledger.service"] in system.calls
    assert "No Ship changed" in out.getvalue()


def test_self_update_reinstalls_when_the_install_changed(store, home, fetchable, tmp_path):
    _advance(fetchable, tmp_path, "deps", "fleetledger/pyproject.toml")
    installs = []
    app, out = app_for(store, fetchable)
    app.run_install = lambda root: installs.append(root) or 0
    assert cli.main(["self-update", "--no-restart"], app) == 0, out.getvalue()
    assert installs == [str(fetchable.root)]


def test_self_update_never_goes_back_or_drops_your_changes(store, home, fetchable, tmp_path):
    first = git(fetchable.root, "rev-parse", "HEAD")
    _advance(fetchable, tmp_path, "newer")
    app, out = app_for(store, fetchable)
    app.run_install = lambda root: 0
    assert cli.main(["self-update", "--no-restart"], app) == 0
    app, out = app_for(store, fetchable)
    assert cli.main(["self-update", "--to", first, "--no-restart"], app) == 1
    assert "is not ahead" in out.getvalue() and "--force" in out.getvalue()
    app, out = app_for(store, fetchable)
    app.run_install = lambda root: 0
    assert cli.main(["self-update", "--to", first, "--force", "--no-restart"], app) == 0
    assert git(fetchable.root, "rev-parse", "HEAD") == first
    (fetchable.root / "versions.toml").write_text("changed by hand\n")
    app, out = app_for(store, fetchable)
    assert cli.main(["self-update"], app) == 1 and "uncommitted changes" in out.getvalue()
    assert (fetchable.root / "versions.toml").read_text() == "changed by hand\n"



# --- images built on the desktop, for a VM without nested virtualisation ----------------------


def _fake_builder(manifest):
    import hashlib

    from fleetledger.images import Built

    def build(repo, *, recipes=None, backend="virt-customize", note=print, out_dir=None):
        out = out_dir / f"{manifest['id']}.qcow2"
        out.write_bytes(b"a baked image")
        doc = dict(manifest, sha256=hashlib.sha256(b"a baked image").hexdigest(), size_bytes=13)
        out.with_suffix(".json").write_text(json.dumps(doc))
        return Built(doc["id"], out, doc, {})

    return build


def test_an_image_built_on_a_retired_desktop_is_imported_on_the_vm(store, home, checkout,
                                                                    tmp_path, monkeypatch):
    from fleetledger.store import TomlFileStore

    from fleetledger import handover

    manifest = json.loads((checkout.root / "contracts" / "examples" / "image-manifest" /
                           "baked.json").read_text())
    # The desktop handed its fleet on: it acts on nothing, but may build an image file.
    handover.write(store, handover.Marker("retired", handover.now(), "fleetledger", 1))
    app, out = app_for(store, checkout)
    app.image_builder = _fake_builder(manifest)
    assert cli.main(["image", "build", "--out", str(tmp_path / "out")], app) == 0, out.getvalue()
    built = tmp_path / "out" / f"{manifest['id']}.qcow2"
    assert built.is_file() and "ship-fleet image import" in out.getvalue()
    app, out = app_for(store, checkout)
    assert cli.main(["image", "ls"], app) == 1 and "retired" in out.getvalue()  # still refused

    # On the VM (another FleetLedger home): checked, then recorded as if built there.
    vm = tmp_path / "vm"
    for d in ("config", "data"):
        (vm / d).mkdir(parents=True)
    monkeypatch.setenv("FLEETLEDGER_DATA_DIR", str(vm / "data"))
    (vm / "config" / "ships.toml").write_text(store.path.read_text())
    vm_store = TomlFileStore(vm / "config" / "ships.toml")
    app, out = app_for(vm_store, checkout)
    assert cli.main(["image", "import", str(built)], app) == 0, out.getvalue()
    entry = vm_store.load().images[manifest["id"]]
    assert entry["commit"] == manifest["commit"]
    assert (vm / "data" / "images" / f"{manifest['id']}.qcow2").read_bytes() == b"a baked image"
    built.write_bytes(b"tampered")
    app, out = app_for(vm_store, checkout)
    assert cli.main(["image", "import", str(built)], app) == 1
    assert "does not match its manifest's sha256" in out.getvalue()
