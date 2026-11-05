"""The New Ship form mapped to `ship-fleet new` (web/newship.py): every option, and the
same checks `new` makes, so nothing starts on bad input."""

from __future__ import annotations

import pytest
from fleetledger.errors import FleetError
from fleetledger.web import newship
from test_cli import env  # noqa: F401  (a fixture: the CLI's store, fake repo and Ship)

BASE = {"name": ["ship-30"], "agent_on": [], "overlay": []}


def form(**extra):
    out = {k: list(v) for k, v in BASE.items()}
    for key, value in extra.items():
        out[key] = value if isinstance(value, list) else [value]
    return out


def mapped(env, advanced=True, **extra):  # noqa: F811
    inv = env.store.load()
    name, argv = newship.argv(form(**extra), env.app, inv, advanced=advanced)
    return name, argv


@pytest.mark.parametrize("fields,expected", [
    ({}, ["new", "ship-30", "--no-overlay"]),
    ({"tag": "work, lab"}, ["--tag", "work", "--tag", "lab"]),
    ({"cores": "8", "memory_gb": "16", "disk_gb": "80"},
     ["--cores", "8", "--disk-gb", "80", "--memory-mb", "16384"]),
    ({"signins_shown": "yes", "signin": ["claude", "github"]}, ["--signins", "claude,github"]),
    ({"agent_on": "yes", "agent": "lead", "cwd": "~/work/x"}, ["--autostart", "lead:~/work/x"]),
    ({"agent_on": "yes", "agent": "lead", "cwd": "~/w", "agent_mode": "plan"},
     ["--autostart", "lead:~/w:plan"]),
    ({"agent_on": "yes", "repo": "someone/agent-setup"}, ["--repo", "someone/agent-setup"]),
    ({"agent_on": "yes", "prompt": "Get going."}, ["--autostart-prompt", "Get going."]),
    ({"overlay": "yes"}, []),  # ticked: no --no-overlay
    ({"image": "stock"}, []),  # the pinned one: nothing to say
    ({"hosts": "ship-30.example.net\n192.0.2.30"},
     ["--host", "ship-30.example.net", "--host", "192.0.2.30"]),
    ({"mode": "plan"}, ["--mode", "plan"]),
    ({"allow_bypass": "yes"}, ["--allow-bypass"]),
    ({"agent_sudo": "yes"}, ["--agent-sudo"]),
    ({"timezone": "Europe/Berlin"}, ["--timezone", "Europe/Berlin"]),
    ({"profile": "orchestrator"}, ["--profile", "orchestrator"]),
])
def test_each_option_maps_to_its_argument(env, fields, expected, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TZ", "UTC")
    name, argv = mapped(env, **fields)
    assert name == "ship-30"
    joined = " ".join(argv)
    assert " ".join(expected) in joined, argv
    if fields == {"overlay": "yes"}:
        assert "--no-overlay" not in argv


def test_tools_tick_boxes_map_to_tools_and_no_default_tools(env):  # noqa: F811
    nf = newship.choices(env.app, env.store.load())
    defaults = [t for t, _, on in nf.tools if on]
    extras = [t for t, _, on in nf.tools if not on]
    assert defaults and extras  # the catalogue has both
    _, argv = mapped(env, tools_shown="yes", tool=defaults)
    assert "--tools" not in argv  # exactly the defaults
    _, argv = mapped(env, tools_shown="yes", tool=[*defaults, extras[0]])
    assert argv[argv.index("--tools") + 1] == extras[0]
    _, argv = mapped(env, tools_shown="yes", tool=defaults[1:])
    assert "--no-default-tools" in argv
    assert argv[argv.index("--tools") + 1] == ",".join(defaults[1:])


def test_the_driver_choice_is_offered_only_when_proxmox_is_set_up(env):  # noqa: F811
    assert newship.choices(env.app, env.store.load()).drivers == ["manual"]
    with pytest.raises(FleetError, match="Where it runs"):
        mapped(env, driver="proxmox")


@pytest.mark.parametrize("fields,message", [
    ({"name": "Bad Name"}, "Ship name"),
    ({"name": "ship-01"}, "already in the inventory"),
    ({"tag": "Work!"}, "is not a tag"),
    ({"cores": "0"}, "cores: 1 to"),
    ({"memory_gb": "1"}, "memory mb"),
    ({"agent_on": "yes", "agent": "Lead"}, "a name of a-z"),
    ({"agent_on": "yes", "repo": "not a repo"}, "not a GitHub repo name"),
    ({"timezone": "Mars/Base"}, "Time zone"),
    ({"hosts": "-bad host"}, "not a host name"),
    ({"image": "nope"}, "Base image"),
    ({"vmid": "12"}, "VM id"),
    ({"mode": "yolo"}, "permission mode"),
    ({"tools_shown": "yes", "tool": ["not-a-tool"]}, "Tools"),
    ({"profile": "nope"}, "Profiles"),
])
def test_bad_input_is_refused_before_anything_starts(env, fields, message):  # noqa: F811
    with pytest.raises(FleetError, match=message):
        mapped(env, **fields)


def test_setup_leaves_out_advanced(env):  # noqa: F811
    _, argv = mapped(env, advanced=False, mode="plan", agent_sudo="yes", hosts="x.example")
    assert "--mode" not in argv and "--agent-sudo" not in argv and "--host" not in argv


def test_the_name_defaults_to_the_next_free_one(env):  # noqa: F811
    inv = env.store.load()
    assert newship.next_name(inv) == "ship-03"  # ship-01 and ship-02 are taken
    name, argv = newship.argv({"overlay": []}, env.app, inv)
    assert name == "ship-03" and argv[:2] == ["new", "ship-03"]


def test_every_argv_the_form_makes_parses_as_ship_fleet_new(env):  # noqa: F811
    from fleetledger import cli

    _, argv = mapped(env, tag="work lab", cores="8", memory_gb="16", disk_gb="80",
                     signins_shown="yes", signin=["claude"], agent_on="yes", agent="lead",
                     cwd="~/w", agent_mode="plan", repo="someone/agent-setup",
                     prompt="Go.", hosts="ship-30.example.net", mode="plan",
                     allow_bypass="yes", agent_sudo="yes", timezone="Europe/Berlin",
                     profile="orchestrator")
    args = cli.build_parser().parse_args(argv)
    assert args.name == "ship-30" and args.tag == ["work", "lab"] and args.cores == 8
    assert args.autostart == ["lead:~/w:plan"] and args.repo == "someone/agent-setup"


def test_the_image_is_checked_against_the_driver_chosen_in_the_form(env):  # noqa: F811
    """As `new --driver D --image X` checks it, not against the default driver."""
    entry = {"created": "2026-11-02T10:00:00Z", "sha256": "ab" * 32,
             "ubuntu": {"release": "26.04", "build": "20261016"}, "tooling": "0.1.0",
             "commit": "unknown", "recipes": []}
    with env.store.edit() as ed:
        ed.doc.setdefault("drivers", {})["proxmox"] = {
            "api_url": "https://pve.example.invalid:8006/api2/json", "node": "pve",
            "pool": "shipshape", "template_vmid": 9000, "seed_storage": "seeds",
            "disk_storage": "local-lvm", "bridge": "vmbr0", "token_id": "shipshape@pve!x",
            "token_file": "~/.config/fleetledger/proxmox-token",
            "tls_fingerprint": ":".join(["AB"] * 32)}
        for image, published in (("baked-1", {"proxmox": {"template_vmid": 9181}}),
                                 ("hand-1", {"manual": {"template_vmid": 9050}})):
            ed.add_image(image, dict(entry))
            ed.set_image_field(image, "published", published)
    nf = newship.choices(env.app, env.store.load())
    assert nf.drivers == ["manual", "proxmox"] and nf.images == ["stock", "hand-1"]
    _, argv = mapped(env, driver="proxmox", image="baked-1")
    assert argv[argv.index("--image") + 1] == "baked-1"
    with pytest.raises(FleetError, match="Base image"):
        mapped(env, driver="proxmox", image="hand-1")
    with pytest.raises(FleetError, match="Base image"):
        mapped(env, image="baked-1")  # the default driver, manual: not published there
