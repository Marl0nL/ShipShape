"""Settings ▸ Images: use, publish (or record a template VM id) and remove baked
images, each through `ship-fleet image`, with a fake driver."""

import hashlib
import subprocess
import threading

import pytest
from fleetledger.drivers.base import Outcome
from test_cli import IMG, add_image
from test_web import wait_for, web  # noqa: F401 - the web UI fixture

from fleetledger import firstrun, images


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
    row = section[section.index(f"<code>{IMG}</code>"):]
    row = row[:row.index("</tr>")]
    cells = [c.split(">", 1)[1].split("<", 1)[0].strip() for c in row.split("<td")[1:6]]
    assert cells == ["20261016", "0.1.0", "chrome-devtools-axi", "-", "0"]
    assert ">kept</td>" in row
    # Not published to the default driver (manual): Use would be refused, so not offered.
    assert "Use for new Ships" not in section
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
    assert job.result["outcome"].done is False and job.result["by_hand"] is True
    card = imaged.client.get(f"/jobs/{job.id}").text
    assert "Do this on your hypervisor" in card
    assert '<span class="pill muted">by hand</span>' in card


def test_the_listing_is_the_data_image_ls_prints(imaged):
    with imaged.store.edit() as ed:
        ed.set_image_field(IMG, "published", {"proxmox": {"template_vmid": 9181}})
        ed.set_image_field(IMG, "dirty", True)
        ed.set_default("image", IMG)
        ed.doc["ship"]["ship-01"]["image"] = IMG
    (row,) = images.listing(imaged.store.load())
    assert row == {"id": IMG, "ubuntu": "20261016", "tooling": "0.1.0", "dirty": True,
                   "recipes": ["chrome-devtools-axi"],
                   "published": {"proxmox": {"template_vmid": 9181}}, "ships": 1,
                   "file": True, "pinned": True}
    with imaged.store.edit() as ed:
        ed.set_image_field(IMG, "file", None)
        ed.set_default("image", None)
    (row,) = images.listing(imaged.store.load())
    assert row["file"] is False and row["pinned"] is False


class SlowDriver(FakeDriver):
    """A Publish that takes until the test lets it finish."""

    def __init__(self):
        super().__init__()
        self.go = threading.Event()

    def publish_image(self, image):
        self.go.wait(10)
        return super().publish_image(image)


def test_one_job_per_image_a_second_publish_or_remove_is_refused(imaged):
    from fleetledger.drivers import get_driver

    slow = SlowDriver()
    imaged.app.driver_factory = lambda inv, d: slow if d == "proxmox" else get_driver(inv, d)
    with imaged.store.edit() as ed:
        ed.set_default("driver", "proxmox")
    imaged.post("/settings/images/publish", {"image": IMG})
    imaged.post("/settings/images/publish", {"image": IMG})  # a double click
    assert f"already running on image {IMG}" in _settings(imaged)
    imaged.post("/settings/images/remove", {"image": IMG, "confirm": "yes"})
    assert f"already running on image {IMG}" in _settings(imaged)
    imaged.post("/settings/images/use", {"image": IMG})
    assert "being published or removed" in _settings(imaged)
    assert len(imaged.app_ui.jobs.recent()) == 1
    slow.go.set()
    job = _job(imaged)
    assert job.ok and slow.published == [IMG] and slow.removed == []
    assert job.locks == [f"image {IMG}"]
    imaged.post("/settings/images/remove", {"image": IMG, "confirm": "yes"})  # free again
    wait_for(lambda: len(imaged.app_ui.jobs.recent()) == 2
             and imaged.app_ui.jobs.recent()[0].done)
    assert imaged.app_ui.jobs.recent()[0].ok and slow.removed


def test_remove_on_the_manual_driver_reads_by_hand_with_the_next_step(imaged):
    with imaged.store.edit() as ed:
        ed.set_image_field(IMG, "published", {"manual": {"template_vmid": 9182}})
    imaged.post("/settings/images/remove", {"image": IMG, "confirm": "yes"})
    job = _job(imaged)
    assert not job.ok and job.result["by_hand"]  # the CLI says it is not finished
    card = imaged.client.get(f"/jobs/{job.id}").text
    assert '<span class="pill muted">by hand</span>' in card and ">failed<" not in card
    assert "Do this on your hypervisor" in card
    assert (f"Image {IMG} is still published to manual; remove it there, then run this "
            "again.") in card  # the command's own next step, on the card
    assert 'class="log bad"' not in card  # the next step is not shown as an error
    assert IMG in imaged.store.load().images


def test_remove_deletes_the_file_unless_kept(imaged):
    with imaged.store.edit() as ed:
        ed.set_image_field(IMG, "published", {"proxmox": {"template_vmid": 9181}})
    imaged.post("/settings/images/remove", {"image": IMG, "confirm": "yes"})
    assert _job(imaged).ok
    assert IMG not in imaged.store.load().images and not imaged.qcow.exists()


def test_recording_a_template_vm_id(imaged):
    imaged.post("/settings/images/publish", {"image": IMG, "template_vmid": "9182"})
    assert _job(imaged).ok
    assert imaged.store.load().images[IMG]["published"] == {"manual": {"template_vmid": 9182}}
    for bad in ("92a", "9²"):  # not a number, or not an ASCII one: refused
        imaged.post("/settings/images/publish", {"image": IMG, "template_vmid": bad})
    assert len(imaged.app_ui.jobs.recent()) == 1


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
