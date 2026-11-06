""""This computer" becomes "this browser's machine" (piece 3 of the remote web UI).

A paired device says which trusted machine it runs on (devices.Device.machine); a
Ship's pages then show that machine's attach commands, never the FleetLedger
computer's, and offer its bundle: **Download bundle** renews its certificate with
`ship-fleet trust NAME` and answers with shipshape-NAME.tar.gz. "Let this computer use
ssh" is the FleetLedger computer's own, so it is neither shown nor done for a device.

The test CA (test_ca's throwaway keys) signs for real; Ships and Tailscale Serve are
simulated (test_webtrust's fake Ships, test_webremote's Serve headers).
"""

from __future__ import annotations

import html
import io
import json
import re
import shutil
import subprocess
import tarfile
import time

import pytest
from fleetledger.web import devices as devices_mod
from fleetledger.web import devicesweb, remote, trust
from fleetledger.web.devices import Devices
from starlette.testclient import TestClient
from test_ca import cakeys  # noqa: F401 - fixture
from test_web import ORIGIN, web  # noqa: F401 - the web UI fixture
from test_webremote import ON, REMOTE, REMOTE_ORIGIN, SERVE
from test_webtrust import _finished, _key, caweb  # noqa: F401 - fixture

from fleetledger import ca, cli, trustcmd

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
OVERLAY_NAME = "ship-01.example-tailnet.ts.net"


class Wall:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def here(caweb, monkeypatch):  # noqa: F811
    """caweb with the remote address on: a paired device (`env.remote`, its CSRF in
    `env.rcsrf`), the desktop's own browser still `env.client`, and a trusted machine
    `lap` (both logins, every Ship). ship-01 is on the overlay."""
    env = caweb
    remote.save(ON)
    env.wall = Wall()
    env.devices = Devices(wall=env.wall)
    env.gate.remote, env.gate.devices = ON, env.devices
    asgi = env.client.app
    env.remote = TestClient(asgi, base_url=REMOTE, headers=SERVE, follow_redirects=False)
    assert env.remote.get(f"/login?token={env.gate.new_token()}").status_code == 200
    env.rcsrf = re.search(r'name="csrf-token" content="([^"]+)"',
                          env.remote.get("/").text).group(1)
    [env.device] = env.devices.all()
    with env.store.edit() as ed:
        ed.set_machine("lap", {"public_key": _key(env, "lap"), "added": "2026-01-01T00:00:00Z",
                               "scope": ["all"], "logins": ["agent", "owner"]})
        ed.set_field("ship-01", "overlay_node", "n0000001")
        ed.set_field("ship-01", "names", [OVERLAY_NAME, *env.store.load().ship("ship-01").names])
    env.fleet.poll()

    def rpost(path, data=None, **kw):
        return env.remote.post(path, data={"csrf": env.rcsrf, **(data or {})},
                               headers=REMOTE_ORIGIN, **kw)

    def on(machine):
        devices_mod.change(env.devices, env.store.load(), env.devices.all()[0],
                           machine=machine)

    env.rpost, env.on = rpost, on
    return env


def access(client, ship="ship-01"):
    return client.get(f"/ships/{ship}/access").text


def bundle_files(response):
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        texts = {m.name: tar.extractfile(m).read().decode() for m in members.values()
                 if m.isfile()}
    return members, texts


# --- the desktop's own browser: as before ----------------------------------------------


def test_this_computers_browser_sees_this_computer_as_before(here):
    page = access(here.client)
    assert "Attach from this computer" in page and "<h2>This computer</h2>" in page
    assert 'action="/attach-here"' in page
    assert "This browser's machine" not in page
    assert "Attach: <code>ship-fleet ssh ship-01</code>" in here.client.get(
        "/ships/ship-01").text


# --- a paired device ---------------------------------------------------------------------


def test_a_device_with_no_machine_said_is_asked_and_never_offered_this_computers(here):
    page = access(here.remote)
    assert "<h2>This browser's machine</h2>" in page and "<h2>This computer</h2>" not in page
    assert 'action="/attach-here"' not in page and "Let this computer use ssh" not in page
    assert "Which trusted machine is browser on?" in page
    assert '<option value="lap">lap</option>' in page
    assert "herdr --remote ship-01" not in page
    assert "on the FleetLedger computer (ssh to it first)" in page
    bar = here.remote.get("/ships/ship-01").text
    assert "Attach from browser: <a href=\"/ships/ship-01/access\">set it up</a>" in bar
    assert "Nothing attaches from browser yet." in bar


def test_this_computers_own_setup_is_refused_from_a_device(here, monkeypatch):
    monkeypatch.setattr(trustcmd, "trust_this_computer",
                        lambda *a, **k: pytest.fail("trusted the FleetLedger computer"))
    response = here.rpost("/attach-here", {"ship": "ship-01", "from": "access"})
    assert response.status_code == 303
    assert "sets up the FleetLedger computer itself" in here.remote.get(
        response.headers["location"]).text
    response = here.rpost("/settings/machines/renew-here")
    assert "sets up the FleetLedger computer itself" in here.remote.get(
        response.headers["location"]).text
    assert here.app_ui.jobs.recent() == []
    machines = here.remote.get("/settings").text
    assert 'action="/settings/machines/renew-here"' not in machines
    assert "The FleetLedger computer" in machines


def test_saying_its_machine_shows_that_machines_commands(here):
    response = here.rpost("/settings/devices/machine",
                          {"device": "mine", "machine": "lap", "back": "ship-01"})
    assert response.headers["location"] == "/ships/ship-01/access"
    assert here.devices.all()[0].machine == "lap"
    page = access(here.remote)
    assert "<h2>Attach from lap</h2>" in page
    for line in ("herdr --remote ship-01", "ssh ship-01", f"ssh {here.store.load().owner}@ship-01"):
        assert f"<code>{line}</code>" in page
    assert "browser runs on lap, a trusted machine" in page
    assert 'action="/settings/machines/bundle"' in page and 'value="lap"' in page
    assert "tar xzf shipshape-lap.tar.gz &amp;&amp; sh shipshape-lap/install.sh" in page
    assert "Attach from lap: <code>herdr --remote ship-01</code>" in here.remote.get(
        "/ships/ship-01").text
    # The desktop's own browser is not affected.
    assert "Attach from this computer" in access(here.client)
    # And back to none.
    here.rpost("/settings/devices/machine", {"device": "mine", "machine": "", "back": "ship-01"})
    assert here.devices.all()[0].machine is None


def test_a_machine_that_may_log_in_only_as_you_gets_only_that(here):
    with here.store.edit() as ed:
        ed.set_machine_field("lap", "logins", ["owner"])
    here.fleet.poll()  # the pages read the poller's inventory
    here.on("lap")
    page = access(here.remote)
    owner = here.store.load().owner
    assert f"<code>ssh {owner}@ship-01</code>" in page
    assert "<code>herdr --remote ship-01</code>" not in page
    assert f"Attach from lap: <code>ssh {owner}@ship-01</code>" in here.remote.get(
        "/ships/ship-01").text


def test_a_machine_that_does_not_reach_the_ship_says_so(here):
    with here.store.edit() as ed:
        ed.set_machine_field("lap", "scope", ["tag:nothing-here"])
    here.fleet.poll()
    here.on("lap")
    page = access(here.remote)
    assert "It does not reach ship-01 yet" in page
    assert "<code>herdr --remote ship-01</code>" not in page


@pytest.mark.parametrize("machine, says", [("nope", "No trusted machine called 'nope'"),
                                           ("gone", "No trusted machine called 'gone'")])
def test_only_a_trusted_machine_can_be_said(here, machine, says):
    with here.store.edit() as ed:
        ed.set_machine("gone", {"public_key": _key(here, "gone"), "added": "2026-01-01T00:00:00Z",
                                "scope": ["all"], "revoked": "2026-01-02T00:00:00Z"})
    response = here.rpost("/settings/devices/machine", {"device": "mine", "machine": machine})
    assert says in html.unescape(here.remote.get(response.headers["location"]).text)
    assert here.devices.all()[0].machine is None


def test_this_computers_own_entry_is_never_a_devices_machine(here, monkeypatch):
    monkeypatch.setattr(trustcmd, "this_computer",
                        lambda: {"machine": "lap", "identity": "~/.ssh/id_ed25519"})
    here.on("lap")  # recorded, but it is the FleetLedger computer's
    assert devicesweb.machine_of(here.devices.all()[0], here.store.load()) is None
    assert "lap" not in devicesweb.machine_choices(here.store.load())
    assert "Which trusted machine is browser on?" in access(here.remote)


def test_settings_devices_says_and_changes_what_each_runs_on(here):
    page = here.remote.get("/settings").text
    section = page[page.index('id="devices"'):page.index('id="images"')]
    assert 'action="/settings/devices/machine"' in section
    assert '<option value="lap">lap</option>' in section
    response = here.rpost("/settings/devices/machine",
                          {"device": here.devices.all()[0].key, "machine": "lap"})
    assert response.headers["location"].startswith("/settings")
    assert "browser runs on lap" in here.remote.get(response.headers["location"]).text
    page = here.remote.get("/settings").text
    assert '<option value="lap" selected>lap</option>' in page
    assert "(this browser's machine)" in page


# --- Download bundle ---------------------------------------------------------------------


def test_download_bundle_renews_the_certificate_and_answers_with_the_bundle(here, tmp_path):
    before = ca.machine(here.store.load(), "lap").last_valid_before
    response = here.rpost("/settings/machines/bundle", {"machine": "lap"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/gzip"
    assert response.headers["content-disposition"] == \
        'attachment; filename="shipshape-lap.tar.gz"'
    assert response.headers["cache-control"] == "no-store"
    members, texts = bundle_files(response)
    top = "shipshape-lap"
    assert set(members) == {top, *(f"{top}/{f}" for f in (
        "config", "known_hosts", "install.sh", "bundle.json", "lap-cert.pub"))}
    assert members[top].isdir() and members[top].mode == 0o700
    for name, m in members.items():
        assert m.uid == 0 and m.uname == "" and (m.isdir() or m.mode == 0o600), name
    # A certificate for lap's key, from the fleet's user CA, newly issued.
    inv = here.store.load()
    lap = ca.machine(inv, "lap")
    assert lap.last_valid_before and lap.last_valid_before != before
    cert = tmp_path / "lap-cert.pub"
    cert.write_text(texts[f"{top}/lap-cert.pub"])
    listing = subprocess.run(["ssh-keygen", "-L", "-f", str(cert)], capture_output=True,
                             text=True, check=True).stdout
    assert ca.fingerprint(ca.config(inv).user_public) in listing
    assert json.loads(texts[f"{top}/bundle.json"])["machine"] == "lap"
    # From a device the Ship's HostName is its overlay name (what resolves on the
    # FleetLedger computer may not where the device is).
    config = texts[f"{top}/config"]
    assert re.search(rf"Host ship-01\n  HostName {re.escape(OVERLAY_NAME)}\n", config)
    assert "IdentityFile ~/.ssh/shipshape/id_ed25519" in config
    assert "PRIVATE KEY" not in "".join(texts.values())


def test_from_this_computer_the_hostnames_are_what_resolves_here(here):
    here.app.resolves = lambda name: name != OVERLAY_NAME
    response = here.post("/settings/machines/bundle", {"machine": "lap"})
    assert response.status_code == 200
    config = bundle_files(response)[1]["shipshape-lap/config"]
    assert f"HostName {OVERLAY_NAME}" not in config


def test_a_renewal_keeps_the_identity_its_last_bundle_named(here):
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: here.store,
                  ssh_factory=here.app.ssh_factory)
    app.signer = here.app.signer
    assert cli.main(["trust", "lap", "--identity", "~/.ssh/work_ed25519"], app) == 0, \
        out.getvalue()
    config = bundle_files(here.rpost("/settings/machines/bundle", {"machine": "lap"}))[1][
        "shipshape-lap/config"]
    assert "IdentityFile ~/.ssh/work_ed25519" in config


def test_a_device_paired_hours_ago_may_still_renew_its_machine(here):
    here.wall.now += devices_mod.FRESH + 60  # destroy and the like need a fresh pairing;
    assert here.rpost("/settings/machines/bundle", {"machine": "lap"}).status_code == 200


@pytest.mark.parametrize("machine, says", [
    ("nope", "No trusted machine called nope"),
    ("gone", "No trusted machine called gone"),
    ("../etc", "No trusted machine called ../etc"),
])
def test_download_bundle_refuses_anything_but_a_trusted_machine(here, machine, says):
    with here.store.edit() as ed:
        ed.set_machine("gone", {"public_key": _key(here, "gone"), "added": "2026-01-01T00:00:00Z",
                                "scope": ["all"], "revoked": "2026-01-02T00:00:00Z"})
    response = here.rpost("/settings/machines/bundle",
                          {"machine": machine, "from": "access", "ship": "ship-01"})
    assert response.status_code == 303
    assert response.headers["location"] == "/ships/ship-01/access"
    assert says in here.remote.get(response.headers["location"]).text


def test_download_bundle_refuses_this_computers_own_entry(here, monkeypatch):
    monkeypatch.setattr(trustcmd, "this_computer",
                        lambda: {"machine": "lap", "identity": "~/.ssh/id_ed25519"})
    response = here.post("/settings/machines/bundle", {"machine": "lap"})
    assert response.status_code == 303
    assert "renews its certificate itself" in here.client.get(
        response.headers["location"]).text


def test_a_failed_renewal_says_why_and_sends_no_file(here, monkeypatch):
    def fail(*a, **k):
        from fleetledger.errors import FleetError

        raise FleetError("the CA key is locked")

    monkeypatch.setattr(trustcmd, "_sign_machine", fail)
    response = here.rpost("/settings/machines/bundle", {"machine": "lap"})
    assert response.status_code == 303
    assert "not renewed: ship-fleet: the CA key is locked" in here.remote.get(
        response.headers["location"]).text


def test_trusted_machines_offer_a_bundle_for_each_but_this_computer(here, monkeypatch):
    monkeypatch.setattr(trustcmd, "this_computer",
                        lambda: {"machine": "desk", "identity": "~/.ssh/id_ed25519"})
    with here.store.edit() as ed:
        ed.set_machine("desk", {"public_key": _key(here, "desk"), "added": "2026-01-01T00:00:00Z",
                                "scope": ["all"], "logins": ["agent", "owner"]})
    page = here.client.get("/settings").text
    machines = page[page.index('id="machines"'):page.index('id="devices"')]
    forms = re.findall(r'action="/settings/machines/bundle"[^>]*>\s*<input[^>]*>\s*'
                       r'<input type="hidden" name="machine" value="([a-z0-9-]+)"', machines)
    assert forms == ["lap"]


# --- trusting the device's own machine from it ------------------------------------------


def test_trusting_a_machine_from_a_device_can_say_it_runs_on_it(here):
    page = here.remote.get("/settings").text
    assert "this browser (browser) runs on it" in page
    assert "runs on it" not in here.client.get("/settings").text  # only on a device
    key = _key(here, "phone-lap")
    here.rpost("/settings/machines/trust", {"machine": "work", "key": key, "scope": "all",
                                            "logins": "both", "mine": "yes"})
    job = _finished(here)
    assert job.ok, job.snapshot()
    assert here.devices.all()[0].machine == "work"
    assert "browser runs on work" in "\n".join(job.snapshot())
    config = (ca.bundle_dir("work") / "config").read_text()
    assert f"HostName {OVERLAY_NAME}" in config  # --hostnames=overlay: it is elsewhere


# --- the banner when its certificate is due ----------------------------------------------


def _valid_for(here, name, hours):
    until = ca.utc_now() + __import__("datetime").timedelta(hours=hours)
    with here.store.edit() as ed:
        ed.set_machine_field(name, "last_valid_before", ca.stamp(until))
    here.fleet.poll()


def test_a_banner_offers_the_bundle_when_this_browsers_machine_is_due(here):
    here.on("lap")
    _valid_for(here, "lap", 100)
    assert "machine-due" not in here.remote.get("/").text
    _valid_for(here, "lap", 3)
    page = here.remote.get("/").text
    assert "lap&#39;s certificate lapses at" in page or "lap's certificate lapses at" in page
    banner = page[page.index("machine-due"):page.index("</div>", page.index("machine-due"))]
    assert 'action="/settings/machines/bundle"' in banner and 'value="lap"' in banner
    _valid_for(here, "lap", -1)
    assert "certificate lapsed at" in here.remote.get("/").text
    assert here.rpost("/settings/machines/bundle", {"machine": "lap"}).status_code == 200
    assert "machine-due" not in here.remote.get("/").text  # renewed: gone at once
    assert "machine-due" not in here.client.get("/").text  # this computer renews itself


# --- the command line --------------------------------------------------------------------


def run(here, argv):
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: here.store)
    return cli.main(argv, app), out.getvalue()


def test_a_pairing_link_can_say_the_machine(here):
    code, said = run(here, ["ui", "link", "--name", "laptop", "--machine", "lap"])
    assert code == 0, said
    link = re.search(rf"{re.escape(REMOTE)}(/login\?token=\S+)", said).group(1)
    laptop = TestClient(here.client.app, base_url=REMOTE, headers=SERVE)
    assert laptop.get(link, follow_redirects=False).status_code == 200
    assert {(d.name, d.machine) for d in here.devices.all()} >= {("laptop", "lap")}
    code, said = run(here, ["ui", "link", "--machine", "nope"])
    assert code != 0 and "No trusted machine called 'nope'" in said


def test_ui_devices_changes_a_device(here):
    code, said = run(here, ["ui", "devices", "browser", "--machine", "lap", "--name", "laptop"])
    assert code == 0 and "laptop, on lap" in said, said
    code, said = run(here, ["ui", "devices"])
    assert "laptop  (" in said and "; on lap;" in said
    code, said = run(here, ["ui", "devices", "laptop", "--no-machine"])
    assert code == 0 and "on no machine said" in said
    code, said = run(here, ["ui", "devices", "laptop", "--machine", "lap", "--no-machine"])
    assert code != 0 and "not both" in said
    code, said = run(here, ["ui", "devices", "laptop", "--machine", "nope"])
    assert code != 0 and "No trusted machine" in said
    code, said = run(here, ["ui", "devices", "--machine", "lap"])
    assert code != 0 and "Name the device" in said
    code, said = run(here, ["ui", "devices", "laptop", "--name", "<b>"])
    assert code != 0 and "device name" in said


def test_trust_can_write_overlay_hostnames_for_a_machine_elsewhere(here):
    here.app.resolves = lambda name: name != OVERLAY_NAME
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: here.store,
                  ssh_factory=here.app.ssh_factory, resolves=here.app.resolves)
    app.signer = here.app.signer
    assert cli.main(["trust", "lap"], app) == 0, out.getvalue()
    assert f"HostName {OVERLAY_NAME}" not in (ca.bundle_dir("lap") / "config").read_text()
    assert cli.main(["trust", "lap", "--hostnames", "overlay"], app) == 0, out.getvalue()
    config = (ca.bundle_dir("lap") / "config").read_text()
    assert f"Host ship-01\n  HostName {OVERLAY_NAME}\n" in config
    # A Ship not on the overlay keeps the name that resolves here.
    assert re.search(r"Host ship-02\n  HostName (?!\S*example-tailnet)\S+\n", config)


def test_bundle_archive_holds_only_the_bundles_files(here):
    folder = ca.bundle_dir("lap")
    assert here.post("/settings/machines/bundle", {"machine": "lap"}).status_code == 200
    (folder / "stray-private-key").write_text("not for download")
    members, _ = bundle_files(type("R", (), {"content": trust.bundle_archive("lap")}))
    assert not any("stray" in m for m in members)


def test_the_gate_module_lists_bundle_download_as_an_everyday_action():
    assert not devices_mod.needs_fresh("/settings/machines/bundle")
    assert not devices_mod.needs_fresh("/settings/devices/machine")
