"""Settings ▸ Access ▸ Certificates and Trusted machines (web.trust), and a Ship's
Renew: each a job running the CLI's own `ca sign`, `trust` or `untrust`, with the
test CA (test_ca's throwaway keys) and the fake Ships, through the gate."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from fl_harness import keygen
from fleetledger.web import dashboard, jobs, trust
from test_ca import _with_ca, cakeys  # noqa: F401 - fixture
from test_cli import ShipSSH, result
from test_web import wait_for, web  # noqa: F401 - the web UI fixture

from fleetledger import ca

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture
def caweb(web, cakeys, tmp_path):  # noqa: F811
    """The web UI on a fleet with a CA; its Ships take a trust document."""
    web.app.signer = ca.Signer(agent_keys=lambda: set())
    _with_ca(web.store, cakeys)
    web.trusted = []
    side = web.ship
    original = side.__call__

    class Side:
        calls = side.calls
        down = side.down

        def __call__(self, ship, command):
            if ship.name in side.down:
                return original(ship, command)
            if command[:4] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "ssh-trust"]:
                return 0, json.dumps(result("ssh-trust", changed=["/etc/ssh/x"])), ""
            if command[:4] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "version"]:
                v = result("version", data={"tooling": "0.1.0", "commit": "0123456789ab",
                                            "contracts": {"ship-status": 1, "ship-toml": 1,
                                                          "ship-ctl-result": 1, "bundle": 1,
                                                          "seed": 1, "runtime": 1,
                                                          "ssh-trust": 1}})
                return 0, json.dumps(v), ""
            return original(ship, command)

    class RecordingSSH(ShipSSH):
        def run(self, ship, command, *, input=None, timeout=60):
            if command[3:4] == ["ssh-trust"]:
                web.trusted.append((ship.name, json.loads(input)))
            return super().run(ship, command, input=input, timeout=timeout)

        def probe_certificate(self, ship, known_hosts, timeout=30):
            return None

    shared = Side()
    web.app.ssh_factory = lambda inv: RecordingSSH(inv, shared)
    web.fleet.poll()
    web.sign_in()
    web.tmp = tmp_path
    return web


def _key(caweb, name):
    keygen(caweb.tmp / name, name)
    return (caweb.tmp / f"{name}.pub").read_text().strip()


def _finished(web, count=1):  # noqa: F811
    wait_for(lambda: len(web.app_ui.jobs.recent()) >= count
             and all(j.done for j in web.app_ui.jobs.recent()))
    return web.app_ui.jobs.recent()[0]


def _settings(web):  # noqa: F811
    return web.client.get("/settings").text


def _trust(caweb, name, key, **extra):
    data = {"machine": name, "key": key, "scope": "all", "logins": "both", **extra}
    return caweb.post("/settings/machines/trust", data)


def test_settings_shows_the_cas_each_ships_certificate_and_the_machines(caweb, monkeypatch):
    with caweb.store.edit() as ed:
        ed.set_machine("lap", {"public_key": _key(caweb, "lap"), "added": "2026-01-01T00:00:00Z",
                               "scope": ["tag:work"], "logins": ["agent"]})

    def refuse(*args, **kwargs):
        raise AssertionError("GET /settings ran a command")

    monkeypatch.setattr(subprocess, "run", refuse)
    page = _settings(caweb)
    inv = caweb.store.load()
    cfg = ca.config(inv)
    assert ca.fingerprint(cfg.host_public) in page and ca.fingerprint(cfg.user_public) in page
    access = page[page.index('id="certificates"'):page.index('id="images"')]
    # The example status is signed by another CA: each Ship needs a new certificate.
    assert access.count("signed by another CA") == 2
    assert 'action="/ships/ship-01/certs/renew"' in access
    assert 'action="/settings/certs/renew"' in access and 'name="force"' in access
    machines = access[access.index('id="machines"'):]
    assert ca.fingerprint(ca.machine(inv, "lap").public_key) in machines
    assert 'action="/settings/machines/untrust"' in machines
    assert 'action="/settings/machines/scope"' in machines
    assert 'action="/settings/machines/trust"' in machines
    assert "ssh-ed25519 AAAA" not in machines.replace('placeholder="ssh-ed25519 AAAA', "")


def test_without_a_ca_there_is_no_certificates_section(web):  # noqa: F811
    web.sign_in()
    page = _settings(web)
    assert 'id="certificates"' not in page and 'id="machines"' not in page
    assert 'action="/setup/fleet/ca"' in page
    web.post("/settings/certs/renew")
    assert "no certificate authority yet" in _settings(web)
    assert web.app_ui.jobs.recent() == []


def test_trusting_a_machine_writes_its_bundle_and_shows_its_folder(caweb):
    key = _key(caweb, "laptop")
    response = _trust(caweb, "laptop", key + "\n", scope="tag:work", logins="agent",
                      hours="12")
    assert response.headers["location"] == "/settings?said=machines#machines"
    job = _finished(caweb)
    assert job.ok, job.snapshot()
    m = ca.machine(caweb.store.load(), "laptop")
    assert m.scope == ("tag:work",) and m.logins == ("agent",) and m.cert_hours == 12
    folder = ca.bundle_dir("laptop")
    assert (folder / "install.sh").is_file() and (folder / "laptop-cert.pub").is_file()
    log = "\n".join(job.snapshot())
    assert f"Bundle: {folder}" in log and "sh install.sh" in log
    page = _settings(caweb)
    assert "bundle <code>" in page and "/trust/laptop" in page


@pytest.mark.parametrize("pasted", [
    "{pub_path}",                                   # a path: never read
    "~/.ssh/id_ed25519.pub",
    "{private}",                                    # a private key
    "{key}\n{key}",                                 # two lines
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ you@host",  # another type
    "ssh-ed25519-cert-v01@openssh.com AAAAC3Nz you",    # a certificate
    "",
])
def test_the_key_field_takes_one_ed25519_public_key_line_only(caweb, pasted):
    key = _key(caweb, "other")
    pasted = pasted.format(pub_path=caweb.tmp / "other.pub", key=key,
                           private=(caweb.tmp / "other").read_text())
    _trust(caweb, "other", pasted)
    assert caweb.app_ui.jobs.recent() == []  # refused before the CLI could read a file
    assert ca.machine(caweb.store.load(), "other") is None
    assert "Never a private key" in _settings(caweb)


def test_a_path_in_the_key_field_is_not_read(caweb, monkeypatch):
    _key(caweb, "other")
    path = caweb.tmp / "other.pub"
    read = []
    original = type(path).read_text

    def spy(self, *args, **kwargs):
        if self == path:
            read.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", spy)
    _trust(caweb, "other", str(path))
    assert read == [] and caweb.app_ui.jobs.recent() == []


@pytest.mark.parametrize("field, value, said", [
    ("machine", "Laptop!", "a-z, 0-9 and -"),
    ("scope", ["all", "tag:work"], "not both"),
    ("scope", ["tag:Nope!"], "Tick the Ships"),
    ("logins", "root", "log in as"),
    ("hours", "0", "Hours"),
    ("hours", "12h", "Hours"),
])
def test_trust_refuses_bad_fields(caweb, field, value, said):
    _trust(caweb, "laptop", _key(caweb, "laptop"), **{field: value})
    assert caweb.app_ui.jobs.recent() == []
    assert said in _settings(caweb)


def test_trusting_a_name_already_trusted_is_refused(caweb):
    _trust(caweb, "laptop", _key(caweb, "laptop"))
    _finished(caweb)
    _trust(caweb, "laptop", _key(caweb, "other"))
    assert len(caweb.app_ui.jobs.recent()) == 1 and "trusted already" in _settings(caweb)


def test_changing_a_machines_ships_needs_the_tick(caweb):
    _trust(caweb, "laptop", _key(caweb, "laptop"))
    _finished(caweb)
    caweb.post("/settings/machines/scope", {"machine": "laptop", "scope": "tag:work"})
    assert "Tick the box" in _settings(caweb) and len(caweb.app_ui.jobs.recent()) == 1
    caweb.post("/settings/machines/scope", {"machine": "laptop", "scope": "all",
                                            "confirm": "yes"})
    assert "No change." in _settings(caweb)
    caweb.post("/settings/machines/scope", {"machine": "laptop", "scope": "tag:work",
                                            "confirm": "yes"})
    job = _finished(caweb, 2)
    assert job.ok, job.snapshot()
    assert ca.machine(caweb.store.load(), "laptop").scope == ("tag:work",)
    caweb.post("/settings/machines/scope", {"machine": "nobody", "scope": "all",
                                            "confirm": "yes"})
    assert "No trusted machine called nobody" in _settings(caweb)


def test_untrust_revokes_and_pushes_the_list_and_names_ships_not_updated(caweb):
    key = _key(caweb, "laptop")
    _trust(caweb, "laptop", key)
    _finished(caweb)
    caweb.post("/settings/machines/untrust", {"machine": "laptop"})
    assert "Tick the box" in _settings(caweb)
    assert not ca.machine(caweb.store.load(), "laptop").revoked
    caweb.ship.down.add("ship-02")
    caweb.trusted.clear()
    caweb.post("/settings/machines/untrust", {"machine": "laptop", "confirm": "yes"})
    job = _finished(caweb, 2)
    assert job.kind == "trust" and job.title == "Untrust laptop"
    assert ca.machine(caweb.store.load(), "laptop").revoked
    krl = __import__("base64").b64decode(dict(caweb.trusted)["ship-01"]["revoked_keys"])
    assert ca.krl_revokes(krl, key)
    assert "ship-02" not in dict(caweb.trusted)
    assert not job.ok and "Not updated yet: ship-02" in "\n".join(job.snapshot())
    page = _settings(caweb)
    assert "Untrusted: laptop" in page
    machines = page[page.index('id="machines"'):]
    assert 'name="machine" value="laptop"' not in machines  # no row, no Untrust
    caweb.post("/settings/machines/untrust", {"machine": "laptop", "confirm": "yes"})
    assert "No trusted machine called laptop" in _settings(caweb)


def test_renew_one_ship_from_its_row_its_menu_or_an_alert(caweb):
    assert caweb.post("/ships/ship-01/certs/renew", {"from": "settings"}).headers[
        "location"] == "/settings?said=certificates#certificates"
    job = _finished(caweb)
    assert job.ok, job.snapshot()
    assert job.kind == "certs" and job.ships == ["ship-01"]
    assert [name for name, _ in caweb.trusted] == ["ship-01"]
    assert caweb.post("/ships/ship-01/certs/renew", {"from": "fleet"}).headers[
        "location"] == "/"
    _finished(caweb, 2)
    assert caweb.post("/ships/ship-01/certs/renew").headers["location"] == "/ships/ship-01"
    assert caweb.post("/ships/nope/certs/renew").status_code == 404


def test_renew_all_and_re_issue_even_if_current(caweb, monkeypatch):
    argvs = []
    monkeypatch.setattr(jobs, "run_cli", lambda app, argv, job: argvs.append(argv) or 0)
    caweb.post("/settings/certs/renew")
    job = _finished(caweb)
    assert job.ships == ["ship-01", "ship-02"] and job.title == "Renew certificates"
    caweb.post("/settings/certs/renew", {"force": "yes"})
    _finished(caweb, 2)
    assert argvs == [["ca", "sign"], ["ca", "sign", "--force"]]


def test_renewing_this_computer_runs_trust_this_computer(caweb, monkeypatch):
    argvs = []
    monkeypatch.setattr(jobs, "run_cli", lambda app, argv, job: argvs.append(argv) or 0)
    page = _settings(caweb)
    assert "Let this computer use ssh" in page  # not trusted yet
    caweb.post("/settings/machines/renew-here")
    _finished(caweb)
    assert argvs == [["trust", "--this-computer"]]


def test_certificate_alerts_have_a_renew_button(caweb):
    for code in ("host-cert-expired", "host-cert-expiring", "host-cert-missing",
                 "ssh-trust-behind"):
        action = dashboard.action_for(code, "ship-01", None, None)
        assert action.kind == "post" and action.value == "/ships/ship-01/certs/renew"
        assert action.label == "Renew" and action.title == "ship-fleet ca sign ship-01"
        assert action.confirm is None  # renewing changes no access
    page = caweb.client.get("/").text
    assert 'action="/ships/ship-01/certs/renew"' in page  # host-cert-missing: another CA
    assert "ship-fleet ca sign ship-01" in page  # as the tooltip


def test_the_ship_menu_offers_renew_only_with_a_ca(caweb, web):  # noqa: F811
    page = caweb.client.get("/ships/ship-01").text
    assert "Renew certificate</button>" in page
    with web.store.edit() as ed:
        ed.doc.pop("ca")
    caweb.fleet.poll()
    page = caweb.client.get("/ships/ship-01").text
    assert "Renew certificate" not in page


def test_public_key_line():
    line = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKk you@laptop"
    assert trust.public_key_line(f"  {line}\n\n") == line
    assert trust.public_key_line("ssh-ed25519 AAAA\nssh-ed25519 BBBB") is None
    assert trust.public_key_line("~/.ssh/id_ed25519.pub") is None


def _block(page, anchor, end):
    return page[page.index(f'id="{anchor}"'):page.index(end)]


def test_a_refusal_shows_in_the_block_it_came_from(caweb):
    response = _trust(caweb, "laptop", "~/.ssh/id_ed25519.pub")
    page = caweb.client.get(response.headers["location"]).text
    machines = _block(page, "machines", 'id="images"')
    assert "Never a private key" in machines  # where the anchor scrolls, not the top
    assert page.index("Never a private key") > page.index('id="machines"')
    caweb.post("/settings/certs/renew")
    page = caweb.client.get("/settings?said=certificates").text
    certs = _block(page, "certificates", 'id="machines"')
    assert "Renew certificates: started" in certs and 'id="job-' in certs


def test_a_finished_certificate_or_machine_job_redraws_the_page(caweb, monkeypatch):
    monkeypatch.setattr(jobs, "run_cli", lambda app, argv, job: 0)
    caweb.post("/settings/machines/renew-here")
    job = _finished(caweb)
    response = caweb.client.get(f"/jobs/{job.id}?v=0")
    assert response.headers.get("HX-Refresh") == "true"  # the tables show the change


def test_a_narrowing_that_does_not_reach_every_ship_fails(caweb):
    _trust(caweb, "laptop", _key(caweb, "laptop"))
    _finished(caweb)
    caweb.ship.down.add("ship-02")
    caweb.post("/settings/machines/scope", {"machine": "laptop", "scope": "ship:ship-01",
                                            "confirm": "yes"})
    job = _finished(caweb, 2)
    log = "\n".join(job.snapshot())
    assert not job.ok and "NOT YET REVOKED on ship-02" in log  # never a green card
    assert ca.machine(caweb.store.load(), "laptop").scope == ("ship:ship-01",)  # recorded


def test_saving_the_same_ships_in_another_order_changes_nothing(caweb):
    with caweb.store.edit() as ed:
        ed.set_machine("lap", {"public_key": _key(caweb, "lap"), "added": "2026-01-01T00:00:00Z",
                               "scope": ["ship:ship-01", "tag:work"], "logins": ["agent"]})
    caweb.post("/settings/machines/scope", {"machine": "lap", "scope": ["tag:work",
                                                                       "ship:ship-01"],
                                            "confirm": "yes"})
    assert "No change." in _settings(caweb) and caweb.app_ui.jobs.recent() == []


def test_the_state_column_and_where_renew_is_offered(caweb):
    from fleetledger import status

    inv = caweb.store.load()
    ca.build_krl(inv)  # cached, as after any push: Ships are judged against it
    doc = status.read_cache("ship-01")
    doc["ssh"]["revoked_keys_sha256"] = "0" * 64
    status.write_cache("ship-01", doc)
    status.cache_path("ship-02").unlink()
    page = _settings(caweb)
    certs = _block(page, "certificates", 'id="machines"')
    row1 = certs[certs.index('href="/ships/ship-01"'):]
    row1 = row1[:row1.index("</tr>")]
    row2 = certs[certs.index('href="/ships/ship-02"'):]
    row2 = row2[:row2.index("</tr>")]
    assert "trust behind" in row1 and "/ships/ship-01/certs/renew" in row1
    assert "not seen yet" in row2 and "certs/renew" not in row2  # nothing to renew yet
    old = status.read_cache("ship-01")
    old.pop("ssh")
    status.write_cache("ship-01", old)
    row1 = _block(_settings(caweb), "certificates", 'id="machines"')
    row1 = row1[row1.index('href="/ships/ship-01"'):]
    row1 = row1[:row1.index("</tr>")]
    assert "update its tooling first" in row1 and "certs/renew" not in row1


def test_the_more_menu_hides_renew_for_a_ship_that_does_not_answer(caweb):
    caweb.ship.down.add("ship-01")
    caweb.fleet.poll()
    page = caweb.client.get("/ships/ship-01").text
    assert "Renew certificate</button>" not in page


def test_a_fleet_level_job_card_has_no_stray_separator(caweb, monkeypatch):
    monkeypatch.setattr(jobs, "run_cli", lambda app, argv, job: 0)
    caweb.post("/settings/machines/renew-here")
    job = _finished(caweb)
    header = caweb.client.get(f"/jobs/{job.id}?v=0").text.split("</header>")[0]
    assert "Renew this computer" in header and " · " not in header


def test_a_ships_renew_needs_a_ca(web):  # noqa: F811
    web.sign_in()
    web.post("/ships/ship-01/certs/renew")
    assert web.app_ui.jobs.recent() == []
    assert "no certificate authority yet" in web.client.get("/ships/ship-01").text


def test_this_computers_ships_are_not_changed_from_its_row(caweb):
    from fleetledger import paths, trustcmd

    with caweb.store.edit() as ed:
        ed.set_machine("here", {"public_key": _key(caweb, "here"), "added": "2026-01-01T00:00:00Z",
                                "scope": ["all"], "logins": ["agent", "owner"]})
    (paths.config_dir() / trustcmd.THIS_COMPUTER).write_text(
        json.dumps({"machine": "here", "identity": "~/.ssh/id_ed25519"}))
    page = _settings(caweb)
    row = page[page.index("(this computer)"):]
    assert 'action="/settings/machines/scope"' not in row[:row.index("</tr>")]
    caweb.post("/settings/machines/scope", {"machine": "here", "scope": "tag:work",
                                            "confirm": "yes"})
    assert "reaches every Ship" in _settings(caweb) and caweb.app_ui.jobs.recent() == []
