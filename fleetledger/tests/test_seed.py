"""Seed generator (W3): ship.toml, bundle, cloud-init files and the ISO."""

import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest
import yaml
from fleetledger.catalogue import Catalogue
from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import Ship
from fleetledger.seed import Autostart, SeedOptions, cloudinit, hostkey, iso, make_seed, shiptoml
from fleetledger.seed import bundle as bundle_mod

from fleetledger import schemas

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
CREATED = "2026-01-01T00:00:00Z"


def contract_block(heading: str, lang: str) -> str:
    """The first fenced block of a language after a heading in contracts/seed.md."""
    text = (CONTRACTS / "seed.md").read_text()
    section = text.split(heading, 1)[1]
    match = re.search(rf"```{lang}\n(.*?)```", section, re.S)
    assert match, heading
    return match.group(1)


# --- ship.toml --------------------------------------------------------------


def test_ship_toml_from_inventory(store, fake_repo):
    inv = store.load()
    cat = Catalogue.from_repo(fake_repo)
    doc = shiptoml.build(inv, inv.ship("ship-01"), cat)
    assert doc["ship"] == {
        "name": "ship-01",
        "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "tags": ["work"],
    }
    assert doc["claude"] == {
        "remote_control": "on",
        "default_mode": "auto",
        "session_prefix": "ship-01",
    }
    # No tools anywhere in the inventory: the catalogue's defaults.
    assert doc["tools"] == {"install": ["gh-axi", "ship-axi"], "profiles": ["orchestrator"]}
    assert doc["profile_options"] == {
        "orchestrator": {"repo_url": "https://example.com/orchestrator.git"}
    }
    assert "autostart" not in doc and "agent" not in doc and "network" not in doc

    text = shiptoml.render(doc)
    assert tomllib.loads(text) == doc
    assert schemas.is_valid("ship.toml", tomllib.loads(text))


def test_a_new_ships_ship_toml_says_it_runs_herdr_core(store, fake_repo):
    """Written out, not left to the default: a ship.toml without `runtime` means classic
    herdr, as it has for every Ship made before herdr-core, and keeps meaning that."""
    inv = store.load()
    doc = shiptoml.build(inv, inv.ship("ship-01"), Catalogue.from_repo(fake_repo))
    assert doc["runtime"] == "core"
    text = shiptoml.render(doc)
    assert 'schema = 1\nruntime = "core"\n' in text  # top level, before any table
    assert tomllib.loads(text)["runtime"] == "core"
    schema = json.loads((CONTRACTS / "ship.toml.schema.json").read_text())
    assert schema["properties"]["runtime"]["default"] == "herdr"  # unset: unchanged


def test_a_freshly_seeded_ship_starts_herdr_core_at_boot_not_herdr(store, fake_repo, tmp_path):
    """The seed's ship.toml, as cloud-init writes it, through a sandbox first boot:
    ship-core.service is enabled at boot and herdr.service is not."""
    if shutil.which("systemctl") is None:
        pytest.skip("needs systemctl")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ship" / "tests"))
    from ship_harness import provision

    inv = store.load()
    seed = make_seed(inv, inv.ship("ship-01"), fake_repo)
    user = yaml.safe_load(iso.read(seed.path.read_bytes())[1]["user-data"])
    written = next(f for f in user["write_files"] if f["path"] == "/etc/shipshape/ship.toml")
    root = tmp_path / "root"
    (root / "etc/shipshape").mkdir(parents=True)
    (root / "etc/shipshape/ship.toml").write_text(written["content"])
    run = provision("--first-boot", "--root", str(root))
    assert run.returncode == 0, run.stdout + run.stderr
    wants = root / "etc/systemd/user/default.target.wants"
    assert sorted(p.name for p in wants.iterdir()) == ["ship-core.service"]
    assert (root / "etc/systemd/user/herdr.service").exists()  # installed, not started


def test_ship_toml_has_no_timezone_unless_given(store, fake_repo):
    inv = store.load()
    doc = shiptoml.build(inv, inv.ship("ship-01"), Catalogue.from_repo(fake_repo))
    assert "timezone" not in doc["ship"]  # the Ship then stays on UTC


@pytest.mark.parametrize("env,link,file,expected", [
    ({"TZ": "Europe/Berlin"}, "/usr/share/zoneinfo/Asia/Tokyo", None, "Europe/Berlin"),
    ({"TZ": ":America/New_York"}, None, None, "America/New_York"),
    ({"TZ": "/usr/share/zoneinfo/Asia/Tokyo"}, None, None, "Asia/Tokyo"),
    ({}, "/usr/share/zoneinfo/Asia/Tokyo", "Europe/Berlin", "Asia/Tokyo"),
    ({}, "/var/db/timezone/zoneinfo/America/Argentina/Buenos_Aires", None,
     "America/Argentina/Buenos_Aires"),  # macOS
    ({}, None, "Europe/Berlin\n", "Europe/Berlin"),
    ({"TZ": "EST+5"}, "../../etc/shadow", "not a zone", "UTC"),  # nothing usable
    ({}, None, None, "UTC"),
])
def test_local_timezone(tmp_path, env, link, file, expected):
    localtime = tmp_path / "localtime"
    if link:
        localtime.symlink_to(link)
    timezone_file = tmp_path / "timezone"
    if file:
        timezone_file.write_text(file)
    known = {"Europe/Berlin", "America/New_York", "Asia/Tokyo", "America/Argentina/Buenos_Aires"}
    assert shiptoml.local_timezone(env, localtime, timezone_file, known) == expected


def test_local_timezone_without_tzdata_trusts_the_name_pattern(tmp_path):
    assert shiptoml.local_timezone({"TZ": "Europe/Berlin"}, tmp_path / "x", tmp_path / "y",
                                   known=set()) == "Europe/Berlin"


def test_ship_toml_options_and_overrides(store, fake_repo):
    with store.edit() as ed:
        ed.set_field("ship-02", "tools", ["tasks-axi"])
    inv = store.load()
    opts = SeedOptions(
        autostart=[
            Autostart("main", "~/work/main", "auto", "Get ready."),
            Autostart("grinder", "~/work/grinder", "bypassPermissions"),
        ],
        agent_sudo=True,
        lan_guard=False,
        lan_guard_allow=["192.0.2.10/32"],
        timezone="America/New_York",
    )
    doc = shiptoml.build(inv, inv.ship("ship-02"), Catalogue.from_repo(fake_repo), opts)
    assert doc["ship"]["tags"] == ["personal"]
    assert doc["ship"]["timezone"] == "America/New_York"
    assert 'timezone = "America/New_York"' in shiptoml.render(doc)
    assert doc["claude"]["allow_bypass"] is True
    assert doc["tools"]["install"] == ["tasks-axi"]
    assert doc["agent"] == {"sudo": True}
    assert doc["network"] == {"lan_guard": False, "lan_guard_allow": ["192.0.2.10/32"]}
    assert doc["autostart"][0] == {
        "name": "main",
        "cwd": "~/work/main",
        "mode": "auto",
        "prompt": "Get ready.",
    }
    parsed = tomllib.loads(shiptoml.render(doc))
    assert parsed == doc


def test_ship_toml_tags_and_profiles_fall_back_to_defaults(store, fake_repo):
    with store.edit() as ed:
        ed.set_field("ship-01", "tags", None)
        ed.set_field("ship-01", "profiles", None)
        ed.doc["defaults"]["tags"] = ["work"]
        ed.doc["defaults"]["profiles"] = ["orchestrator"]
        ed.set_field("ship-02", "tags", [])  # an explicit empty list means no tags
    inv = store.load()
    cat = Catalogue.from_repo(fake_repo)
    one = shiptoml.build(inv, inv.ship("ship-01"), cat)
    assert one["ship"]["tags"] == ["work"]
    assert one["tools"]["profiles"] == ["orchestrator"]
    two = shiptoml.build(inv, inv.ship("ship-02"), cat)
    assert two["ship"]["tags"] == []
    assert inv.expected_account(inv.ship("ship-02"), "claude").accounts == frozenset()


def test_ship_toml_keeps_session_prefix(store, fake_repo):
    with store.edit() as ed:
        ed.set_nested("ship-01", "claude", "session_prefix", "work-01")
    inv = store.load()
    doc = shiptoml.build(inv, inv.ship("ship-01"), Catalogue.from_repo(fake_repo))
    assert doc["claude"]["session_prefix"] == "work-01"


def test_ship_toml_rejects_bypass_without_allow(store, fake_repo):
    inv = store.load()
    opts = SeedOptions(autostart=[Autostart("a", "~/work/a", "bypassPermissions")])
    with pytest.raises(ConfigError, match="ship.toml for ship-01"):
        shiptoml.build(inv, inv.ship("ship-01"), Catalogue.from_repo(fake_repo), opts)


def test_ship_toml_missing_profile_option(store, fake_repo):
    with store.edit() as ed:
        del ed.doc["profile_options"]
    inv = store.load()
    with pytest.raises(ConfigError, match="repo_url"):
        shiptoml.build(inv, inv.ship("ship-01"), Catalogue.from_repo(fake_repo))


def test_ship_toml_unknown_tool(store, fake_repo):
    with store.edit() as ed:
        ed.doc["defaults"]["tools"] = ["nope"]
    inv = store.load()
    with pytest.raises(FleetError, match="Unknown tools: nope"):
        shiptoml.build(inv, inv.ship("ship-01"), Catalogue.from_repo(fake_repo))


# --- bundle -----------------------------------------------------------------


def members(data: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: m for m in tar.getmembers()}


def test_bundle_contents(fake_repo):
    b = bundle_mod.pack(fake_repo, created_at=CREATED)
    names = members(b.data)
    files = {n for n, m in names.items() if m.isfile()}
    assert files == {
        "bundle.json",
        "versions.toml",
        "bin/ship-ctl",
        "provision/ship-provision",
        "files/herdr.service",
        "tools/gh-axi.toml",
        "tools/ship-axi.toml",
        "tools/tasks-axi.toml",
        "profiles/orchestrator.toml",
    }
    assert names["provision/ship-provision"].mode == 0o755
    assert names["files/herdr.service"].mode == 0o644
    assert names["bin"].isdir() and names["bin"].mode == 0o755
    assert all(m.uid == 0 and m.uname == "root" for m in names.values())
    assert all(not n.startswith("/") and ".." not in n.split("/") for n in names)

    with tarfile.open(fileobj=io.BytesIO(b.data), mode="r:gz") as tar:
        manifest = json.load(tar.extractfile("bundle.json"))
        versions = tar.extractfile("versions.toml").read()
    assert manifest == b.manifest
    assert manifest["commit"] == "unknown" and manifest["dirty"] is False
    assert schemas.is_valid("bundle-manifest", manifest)
    assert versions == fake_repo.versions_path.read_bytes()
    assert b.release == "0.1.0-unknown"
    assert b.checksum_line == f"{hashlib.sha256(b.data).hexdigest()}  bundle.tar.gz\n"


def test_bundle_is_reproducible(fake_repo):
    one = bundle_mod.pack(fake_repo, created_at=CREATED)
    two = bundle_mod.pack(fake_repo, created_at=CREATED)
    assert one.data == two.data


def test_bundle_records_git_commit_and_dirty(fake_repo):
    root = fake_repo.root

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "x")
    clean = bundle_mod.pack(fake_repo, created_at=CREATED)
    assert re.fullmatch(r"[0-9a-f]{40}", clean.manifest["commit"])
    assert clean.manifest["dirty"] is False
    assert clean.release == f"0.1.0-{clean.manifest['commit'][:12]}"

    (root / "ship" / "files" / "herdr.service").write_text("changed\n")
    assert bundle_mod.pack(fake_repo, created_at=CREATED).manifest["dirty"] is True

    # Ignored files never reach a bundle.
    (root / ".gitignore").write_text("*.local.toml\n")
    (root / "ship" / "secret.local.toml").write_text("x = 1\n")
    assert "secret.local.toml" not in members(bundle_mod.pack(fake_repo).data)


def test_bundle_needs_ship_track_entry_points(fake_repo):
    (fake_repo.ship_dir / "bin" / "ship-ctl").chmod(0o644)
    with pytest.raises(FleetError, match="ship/bin/ship-ctl is missing or not executable"):
        bundle_mod.pack(fake_repo)


def test_bundle_refuses_symlinks(fake_repo):
    (fake_repo.ship_dir / "files" / "link").symlink_to("/etc/hostname")
    with pytest.raises(FleetError, match="symlink"):
        bundle_mod.pack(fake_repo)


def test_bundle_size_limit(fake_repo, monkeypatch):
    monkeypatch.setattr(bundle_mod, "MAX_BYTES", 100)
    with pytest.raises(FleetError, match="5 MiB"):
        bundle_mod.pack(fake_repo)


def test_bootstrap_unpacks_bundle(fake_repo, tmp_path):
    """Run the contract's bootstrap script (paths moved under tmp) on a real bundle."""
    (fake_repo.ship_dir / "provision" / "ship-provision").write_text(
        "#!/usr/bin/env bash\necho \"provisioned $1\" > \"$(dirname \"$0\")/../../../ran\"\n"
    )
    b = bundle_mod.pack(fake_repo, created_at=CREATED)
    root = tmp_path / "shipfs"
    state = root / "var/lib/shipshape"
    state.mkdir(parents=True)
    (state / "bundle.tar.gz").write_bytes(b.data)
    (state / "bundle.sha256").write_text(b.checksum_line)
    script = cloudinit.BOOTSTRAP
    for prefix in ("/var/lib/shipshape", "/var/log/shipshape", "/opt/shipshape"):
        script = script.replace(prefix, str(root) + prefix)

    subprocess.run(["bash", "-c", script], check=True)
    current = root / "opt/shipshape/current"
    assert os.readlink(current).endswith("/releases/0.1.0-unknown")
    assert (current / "bin" / "ship-ctl").stat().st_mode & stat.S_IXUSR
    assert (root / "opt/shipshape/ran").read_text() == "provisioned --first-boot\n"


# --- cloud-init files -------------------------------------------------------


def test_bootstrap_matches_contract():
    assert cloudinit.BOOTSTRAP == contract_block("### Bootstrap (the runcmd)", "bash")


def test_network_config_matches_contract():
    expected = yaml.safe_load(contract_block("### network-config", "yaml"))
    assert yaml.safe_load(cloudinit.network_config()) == expected


def test_meta_data():
    iid = cloudinit.new_instance_id("ship-01")
    assert re.fullmatch(r"ship-01-[0-9a-f]{8}", iid)
    assert cloudinit.new_instance_id("ship-01") != iid
    assert yaml.safe_load(cloudinit.meta_data("ship-01", iid)) == {
        "instance-id": iid,
        "local-hostname": "ship-01",
    }


def test_user_data_shape(fake_repo):
    key = hostkey.generate("ship-01")
    b = bundle_mod.pack(fake_repo, created_at=CREATED)
    text = cloudinit.user_data(
        "ship-01",
        key,
        ["ssh-ed25519 AAAAadmin fleet-admin"],
        ["ssh-ed25519 AAAAyou you", "ssh-ed25519 AAAAlaptop laptop"],
        'schema = 1\n[ship]\nname = "ship-01"\n',
        b,
    )
    assert text.startswith("#cloud-config\n")
    doc = yaml.safe_load(text)
    assert tuple(doc) == cloudinit.USER_DATA_KEYS

    # Every value must equal the contract's, apart from the per-Ship parts.
    expected = yaml.safe_load(contract_block("### user-data", "yaml"))
    expected["ssh_keys"] = {"ed25519_private": key.private, "ed25519_public": key.public}
    expected["users"][0]["ssh_authorized_keys"] = ["ssh-ed25519 AAAAadmin fleet-admin"]
    expected["users"][1]["ssh_authorized_keys"] = [
        "ssh-ed25519 AAAAyou you",
        "ssh-ed25519 AAAAlaptop laptop",
    ]
    expected["write_files"][0]["content"] = 'schema = 1\n[ship]\nname = "ship-01"\n'
    expected["write_files"][1]["content"] = base64.b64encode(b.data).decode()
    expected["write_files"][2]["content"] = b.checksum_line
    expected["runcmd"] = [["bash", "-c", cloudinit.BOOTSTRAP]]
    assert doc == expected

    # Spelled out, because these are the ones that matter most.
    assert doc["ssh_pwauth"] is False and doc["disable_root"] is True
    assert doc["preserve_hostname"] is False and doc["hostname"] == "ship-01"
    assert "ssh_genkeytypes" not in doc and doc["ssh_deletekeys"] is True
    fleet, agent = doc["users"]
    assert fleet["lock_passwd"] is True and agent["lock_passwd"] is True
    assert fleet["sudo"] == "ALL=(ALL:ALL) NOPASSWD: ALL" and "sudo" not in agent
    files = {f["path"]: f for f in doc["write_files"]}
    assert files["/etc/shipshape/ship.toml"]["permissions"] == "0644"
    assert files["/var/lib/shipshape/bundle.tar.gz"]["permissions"] == "0600"
    assert base64.b64decode(files["/var/lib/shipshape/bundle.tar.gz"]["content"]) == b.data


def test_public_key_files(tmp_path):
    good = tmp_path / "k.pub"
    good.write_text("# mine\nssh-ed25519 AAAA one\n\necdsa-sha2-nistp256 AAAA two\n")
    assert cloudinit.read_public_keys(good, "k") == [
        "ssh-ed25519 AAAA one",
        "ecdsa-sha2-nistp256 AAAA two",
    ]
    private = tmp_path / "k"
    private.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nxx\n")
    with pytest.raises(ConfigError, match="private key"):
        cloudinit.read_public_keys(private, "k")
    junk = tmp_path / "junk.pub"
    junk.write_text("hello\n")
    with pytest.raises(ConfigError, match="does not hold"):
        cloudinit.read_public_keys(junk, "k")


# --- host keys and the ISO --------------------------------------------------


def test_host_key_fingerprint_matches_ssh_keygen(tmp_path):
    key = hostkey.generate("ship-01")
    assert key.public.startswith("ssh-ed25519 ") and key.public.endswith(" ship-01")
    assert "PRIVATE" not in repr(key)
    pub = tmp_path / "k.pub"
    pub.write_text(key.public + "\n")
    out = subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True)
    assert out.stdout.split()[1] == key.fingerprint


def test_iso_round_trip():
    files = {"user-data": "#cloud-config\n", "meta-data": "a: 1\n", "network-config": "v: 2\n"}
    label, read = iso.read(iso.build(files))
    assert label == "CIDATA"
    assert read == files
    with pytest.raises(ValueError):
        iso.build({"vendor-data": "x"})


def test_make_seed(store, fake_repo, home, tmp_path):
    inv = store.load()
    seed = make_seed(inv, inv.ship("ship-01"), fake_repo)

    assert seed.path.parent == home / "data" / "seeds"
    assert stat.S_IMODE(seed.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(seed.path.stat().st_mode) == 0o600
    assert seed.path.name == f"{seed.instance_id}.iso"
    # The seeds folder holds the ISO and nothing else (no stray key files).
    assert list(seed.path.parent.iterdir()) == [seed.path]

    label, files = iso.read(seed.path.read_bytes())
    assert label == "CIDATA"
    assert set(files) == {"user-data", "meta-data", "network-config"}
    user = yaml.safe_load(files["user-data"])
    meta = yaml.safe_load(files["meta-data"])
    assert meta["instance-id"] == seed.instance_id
    assert user["hostname"] == "ship-01"
    assert user["ssh_keys"]["ed25519_public"] == seed.host_public_key
    assert seed.host_fingerprint == hostkey.fingerprint(seed.host_public_key)

    # The private key in the seed is the pair of the pinned public key.
    priv = tmp_path / "priv"
    priv.write_text(user["ssh_keys"]["ed25519_private"])
    priv.chmod(0o600)
    derived = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(priv)], capture_output=True, text=True, check=True
    ).stdout.split()[:2]
    assert derived == seed.host_public_key.split()[:2]

    admin_pub = (home / "keys" / "fleet_admin.pub").read_text().strip()
    you_pub = (home / "keys" / "you.pub").read_text().strip()
    assert user["users"][0]["ssh_authorized_keys"] == [admin_pub]
    assert user["users"][1]["ssh_authorized_keys"] == [you_pub]

    written = [f for f in user["write_files"] if f["path"] == "/etc/shipshape/ship.toml"][0]
    assert written["content"] == seed.ship_toml
    assert schemas.is_valid("ship.toml", tomllib.loads(seed.ship_toml))

    # A second seed for the same Ship gets a new instance id and host key.
    again = make_seed(inv, inv.ship("ship-01"), fake_repo)
    assert again.instance_id != seed.instance_id
    assert again.host_public_key != seed.host_public_key


def test_make_seed_refuses_private_agent_key(store, fake_repo, home):
    with store.edit() as ed:
        ed.doc["fleet"]["agent_keys"] = [str(home / "keys" / "you")]
    inv = store.load()
    with pytest.raises(ConfigError, match="private key"):
        make_seed(inv, inv.ship("ship-01"), fake_repo)
    assert not any((home / "data").rglob("*.iso"))


def test_make_seed_for_new_ship(store, fake_repo):
    ship = Ship(
        name="ship-09",
        id="9a1e9f1c-0d36-4b0e-9a55-1c2b3d4e5f60",
        driver="manual",
        names=("ship-09.local",),
        host_key="ssh-ed25519 AAAA placeholder",
        tags=("work",),
    )
    seed = make_seed(store.load(), ship, fake_repo)
    assert tomllib.loads(seed.ship_toml)["ship"]["name"] == "ship-09"
