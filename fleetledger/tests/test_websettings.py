"""The fleet's Settings page (/settings): Health, where Ships run, access, images and
the activity log. Uses test_websetup's fixtures: a scratch home and a fake repo."""

import subprocess

import pytest
from test_websetup import _key, chosen, partway, settings_page, ws  # noqa: F401 - fixtures

from fleetledger import firstrun


def test_settings_needs_a_fleet_and_the_nav_says_so(ws):  # noqa: F811
    response = ws.get("/settings")
    assert response.status_code in (302, 303) or "Set up FleetLedger" in response.text
    assert '<a href="/setup">Setup</a>' in ws.get("/setup").text  # no fleet yet: Setup
    ws.post("/setup/fleet/new", {"your_key": "~/.ssh/id_ed25519.pub"})
    text = ws.get("/setup").text
    assert '<a href="/settings">Settings</a>' in text and "Audit log</a>" not in text
    assert '<a href="/settings#access">Settings</a>' in text  # setup step 1 points there
    assert 'action="/setup/owner"' not in text  # the forms moved


def test_a_plain_get_runs_nothing_and_contacts_nothing(chosen, monkeypatch):  # noqa: F811
    def refuse(*args, **kwargs):
        raise AssertionError("GET /settings ran a command or a check")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(firstrun, "everything", refuse)
    text = settings_page(chosen)
    for anchor in ("health", "where", "access", "images", "activity"):
        assert f'id="{anchor}"' in text and f'href="#{anchor}"' in text
    assert "Not checked yet." in text
    assert 'action="/setup/owner"' in text and 'name="back" value="settings"' in text
    assert "Run setup again" in text


def test_health_checks_only_when_asked_and_shows_each_item(chosen, monkeypatch):  # noqa: F811
    calls = []

    def fake(app, *, offline=False):
        calls.append(offline)
        return [
            firstrun.Item("computer", "ssh-keygen", "ok", "OpenSSH 9.6"),
            firstrun.Item("fleet", "inventory", "ok", "fine", "ship-fleet init"),  # no fix
            firstrun.Item("ships", "ship-07", "warn", "not enrolled yet",
                          "ship-fleet status ship-07 --wait"),
            firstrun.Item("overlay", "overlay check", "skip", "no overlay"),
            firstrun.Item("fleet", "certificate authority", "missing", "none yet",
                          "ship-fleet ca init"),
            firstrun.Item("hypervisor", "Proxmox API", "fail", "cannot connect",
                          "ship-fleet setup"),
            firstrun.Item("overlay", "overlay", "info", "not set up"),
        ]

    monkeypatch.setattr(firstrun, "everything", fake)
    settings_page(chosen)
    assert calls == []
    response = chosen.post("/settings/health")
    assert response.headers["location"] == "/settings#health" and calls == [False]
    text = settings_page(chosen)
    assert "Last checked" in text and "Not checked yet." not in text
    # Each state as an icon and a word; the fix as a command to copy and a place to go.
    assert 'class="sev-tag lvl-warning"' in text and ">missing</span>" in text
    assert 'class="sev-tag lvl-critical"' in text and ">failed</span>" in text
    assert 'class="sev-tag lvl-notice"' in text and ">info</span>" in text
    assert 'data-copy="ship-fleet ca init"' in text
    # warn: the warning triangle and the word; skip: a muted pill, no icon.
    warn_row = text[text.index("ship-07"):]
    warn_row = warn_row[:warn_row.index("</tr>")]
    assert 'class="sev-tag lvl-warning"' in warn_row and ">warning</span>" in warn_row
    assert 'data-copy="ship-fleet status ship-07 --wait"' in warn_row
    skip_row = text[text.index("overlay check"):]
    skip_row = skip_row[:skip_row.index("</tr>")]
    assert '<span class="pill muted">skipped</span>' in skip_row and "sev-tag" not in skip_row
    assert 'data-copy="ship-fleet init"' not in text  # an ok item shows no fix
    assert '<a href="/settings#access">Access</a>' in text
    assert '<a href="/setup#hypervisor">Setup</a>' in text


def test_a_failed_check_says_why(chosen, monkeypatch):  # noqa: F811
    from fleetledger.errors import FleetError

    def broken(app, *, offline=False):
        raise FleetError("The inventory cannot be read.")

    monkeypatch.setattr(firstrun, "everything", broken)
    chosen.post("/settings/health")
    assert "The inventory cannot be read." in settings_page(chosen)


def test_access_forms_posted_from_settings_return_there(chosen, tmp_path):  # noqa: F811
    _key(tmp_path / "lap")
    with chosen.store.edit() as ed:
        ed.set_machine("lap", {"public_key": (tmp_path / "lap.pub").read_text().strip(),
                               "added": "2026-01-01T00:00:00Z", "scope": ["all"],
                               "logins": ["agent"]})
    page = settings_page(chosen)
    access = page[page.index('id="access"'):page.index('id="images"')]
    forms = [f for f in access.split("<form")[1:] if 'action="/setup/' in f.split(">")[0]]
    assert forms and all('name="back" value="settings"' in f for f in forms)
    for path, data in (("/setup/owner", {"owner": "sam"}),
                       ("/setup/machine", {"machine": "lap", "logins": "agent"}),
                       ("/setup/fleet/ca", {}),
                       ("/setup/fleet/adopt", {})):
        response = chosen.post(path, {**data, "back": "settings"})
        assert response.headers["location"] == "/settings#access", path
        assert chosen.post(path, data).headers["location"].startswith("/setup"), path


@pytest.mark.parametrize("with_ca", [False, True])
def test_settings_with_and_without_a_ca_and_machines(chosen, tmp_path, with_ca):  # noqa: F811
    if not with_ca:  # a new fleet makes its CA; one made before CAs existed has none
        with chosen.store.edit() as ed:
            ed.doc.pop("ca")
    else:
        _key(tmp_path / "lap")
        with chosen.store.edit() as ed:
            ed.set_machine("lap", {"public_key": (tmp_path / "lap.pub").read_text().strip(),
                                   "added": "2026-01-01T00:00:00Z", "scope": ["all"],
                                   "logins": ["agent"]})
    text = settings_page(chosen)
    assert ('action="/setup/fleet/ca"' in text) is not with_ca  # Make it, until there is one
    assert ("Trusted machines" in text) is with_ca
    assert ('name="machine" value="lap"' in text) is with_ca
    assert ("The fleet has a certificate authority." in text) is with_ca


def test_the_audit_log_is_in_settings_and_still_at_its_own_address(chosen):  # noqa: F811
    from fleetledger.concierge import audit

    audit.record("ship-01", "login", "claude", "ok", "work@example.com")
    assert "work@example.com" in settings_page(chosen)
    assert "work@example.com" in chosen.get("/audit").text


def test_where_ships_run_on_proxmox(chosen):  # noqa: F811
    with chosen.store.edit() as ed:
        ed.doc.setdefault("drivers", {})["proxmox"] = {
            "api_url": "https://pve.example.invalid:8006/api2/json", "node": "pve",
            "pool": "shipshape", "template_vmid": 9000, "seed_storage": "seeds",
            "disk_storage": "local-lvm", "bridge": "vmbr0", "token_id": "shipshape@pve!build",
            "token_file": "~/.config/fleetledger/proxmox-token",
            "tls_fingerprint": ":".join(["AB"] * 32), "vmid_range": [9100, 9199]}
        ed.set_default("driver", "proxmox")
    page = settings_page(chosen)
    where = page[page.index('id="where"'):page.index('id="access"')]
    assert "https://pve.example.invalid:8006/api2/json" in where
    assert "9100 to 9199" in where and "[9100" not in where
    assert "proxmox-token" not in where and "AB:AB" not in where  # no secrets' whereabouts
