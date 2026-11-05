"""Settings ▸ Images: use, publish (or record a template VM id) and remove baked
images, each through `ship-fleet image`, with a fake driver."""

import hashlib
import subprocess

import pytest
from fleetledger.drivers.base import Outcome
from test_cli import IMG, add_image
from test_web import wait_for, web  # noqa: F401 - the web UI fixture

from fleetledger import firstrun


class FakeDriver:
    id = "proxmox"

    def __init__(self):
        self.published, self.removed = [], []

    def publish_image(self, image):
        self.published.append(image.id)
        return Outcome(True, [f"Published {image.id} as template 9181."],
                       driver_ref={"template_vmid": 9181})

    def remove_image(self, image):
        self.removed.append((image.id, dict(image.ref)))
        return Outcome(True, [f"Removed template {image.ref['template_vmid']}."])


@pytest.fixture
def imaged(web, tmp_path):  # noqa: F811
    from fleetledger.drivers import get_driver

    qcow = tmp_path / f"{IMG}.qcow2"
    qcow.write_bytes(b"QCOW")
    add_image(web, file=qcow, sha=hashlib.sha256(b"QCOW").hexdigest())
    fake = FakeDriver()
    web.app.driver_factory = lambda inv, d: fake if d == "proxmox" else get_driver(inv, d)
    web.fake, web.qcow = fake, qcow
    web.sign_in()
    return web


def _settings(web):  # noqa: F811
    return web.client.get("/settings").text


def _job(web):  # noqa: F811
    wait_for(lambda: web.app_ui.jobs.recent() and web.app_ui.jobs.recent()[0].done)
    return web.app_ui.jobs.recent()[0]


def test_the_images_section(imaged, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("GET /settings ran a command")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(firstrun, "everything", refuse)
    page = _settings(imaged)
    section = page[page.index('id="images"'):page.index('id="activity"')]
    assert IMG in section and "20261016" in section and "chrome-devtools-axi" in section
    assert 'action="/settings/images/use"' in section
    assert "Publish to manual" in section  # not published to the default driver yet
    assert 'action="/settings/images/remove"' in section and 'name="keep_file"' in section
    assert 'data-copy="ship-fleet image build"' in section


def test_use_is_refused_until_published_to_the_default_driver(imaged):
    imaged.post("/settings/images/use", {"image": IMG})
    page = _settings(imaged)
    assert "not published to the manual driver" in page
    assert f"ship-fleet image publish {IMG}" in page  # the CLI's hint
    assert imaged.store.load().pinned_image == "stock"


def test_publish_runs_as_a_job_then_use_pins_it(imaged):
    with imaged.store.edit() as ed:
        ed.set_default("driver", "proxmox")
    imaged.post("/settings/images/publish", {"image": IMG})
    job = _job(imaged)
    assert job.ok and job.kind == "image" and imaged.fake.published == [IMG]
    assert imaged.store.load().images[IMG]["published"] == {"proxmox": {"template_vmid": 9181}}
    assert "Published" in imaged.client.get(f"/jobs/{job.id}").text
    imaged.post("/settings/images/use", {"image": IMG})
    assert imaged.store.load().pinned_image == IMG
    page = _settings(imaged)
    assert "Use the stock template for new Ships" in page


def test_the_manual_driver_shows_the_steps(imaged):
    imaged.post("/settings/images/publish", {"image": IMG})
    job = _job(imaged)
    assert job.result["outcome"].done is False
    assert "Do this on your hypervisor" in imaged.client.get(f"/jobs/{job.id}").text


def test_recording_a_template_vm_id(imaged):
    imaged.post("/settings/images/publish", {"image": IMG, "template_vmid": "9182"})
    assert _job(imaged).ok
    assert imaged.store.load().images[IMG]["published"] == {"manual": {"template_vmid": 9182}}
    imaged.post("/settings/images/publish", {"image": IMG, "template_vmid": "92a"})
    assert len(imaged.app_ui.jobs.recent()) == 1  # not a number: refused


def test_remove_needs_the_tick_refuses_the_pinned_image_and_can_keep_the_file(imaged):
    with imaged.store.edit() as ed:
        ed.set_image_field(IMG, "published", {"proxmox": {"template_vmid": 9181}})
    imaged.post("/settings/images/remove", {"image": IMG})
    assert imaged.app_ui.jobs.recent() == []  # no tick: refused
    with imaged.store.edit() as ed:
        ed.set_default("image", IMG)
    imaged.post("/settings/images/remove", {"image": IMG, "confirm": "yes"})
    job = _job(imaged)
    assert not job.ok and any("pinned" in line for line in job.snapshot())
    with imaged.store.edit() as ed:
        ed.set_default("image", None)
    imaged.post("/settings/images/remove", {"image": IMG, "confirm": "yes",
                                            "keep_file": "yes"})
    wait_for(lambda: len(imaged.app_ui.jobs.recent()) == 2
             and imaged.app_ui.jobs.recent()[0].done)
    assert imaged.app_ui.jobs.recent()[0].ok
    assert imaged.fake.removed == [(IMG, {"template_vmid": 9181})]
    assert IMG not in imaged.store.load().images and imaged.qcow.exists()  # kept


@pytest.mark.parametrize("route", ["use", "publish", "remove"])
def test_bad_image_ids_are_refused(imaged, route):
    imaged.post(f"/settings/images/{route}", {"image": "../etc", "confirm": "yes"})
    assert imaged.app_ui.jobs.recent() == []
    assert imaged.store.load().pinned_image == "stock"
