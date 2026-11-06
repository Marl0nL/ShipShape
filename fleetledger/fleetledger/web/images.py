"""Baked images, on Settings (Images): use one for new Ships, publish it, remove it.

Every action is the CLI's own `ship-fleet image` command:
- Use (`image use ID|stock`) only edits the inventory: it runs straight away
  (runs.run_now) and its answer is the flash. Pinning an image not published to
  the default driver is refused by the CLI, with its hint.
- Publish (`image publish ID`, or `--template-vmid N` to record a template made
  by hand) and Remove (`image rm ID [--keep-file]`) talk to the hypervisor: they
  are jobs (web.jobs), shown in the Images section. A by-hand driver's steps show
  on the job's card (jobs.RecordingApp, _outcome.html). Remove needs the dialog's
  tick box; the CLI refuses to remove the pinned image.
Building an image takes a while and needs local tools: it stays a command to copy.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from fleetledger.errors import FleetError

IMAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BACK = "/settings#images"


def _back() -> RedirectResponse:
    return RedirectResponse(BACK, status_code=303)


def _flash(request: Request, kind: str, text: str) -> None:
    from fleetledger.web.app import session_of, ui_of

    ui_of(request).flash(session_of(request), kind, text)


async def _image_field(request: Request) -> tuple[dict[str, list[str]], str | None]:
    from fleetledger.web.forms import form, one

    fields = await form(request)
    image = one(fields, "image")
    return fields, image if IMAGE_ID.match(image) else None


async def use(request: Request) -> Response:
    from fleetledger.web.app import ui_of
    from fleetledger.web.runs import run_now

    _fields, image = await _image_field(request)
    if image is None:
        _flash(request, "bad", "Choose an image (or the stock template).")
        return _back()
    if ui_of(request).jobs.busy([], [_lock(image)]):
        _flash(request, "bad", f"{image} is being published or removed; wait for it to finish.")
        return _back()
    code, lines = await run_in_threadpool(run_now, ui_of(request).app, ["image", "use", image])
    _flash(request, "ok" if code == 0 else "bad", " ".join(lines) or "Done.")
    return _back()


def _lock(image: str) -> str:
    """The job lock for one image: one Publish or Remove of it at a time, so a second
    click cannot upload it twice (orphaning a template) or forget it mid-upload."""
    return f"image {image}"


def _start(request: Request, image: str, argv: list[str], title: str) -> None:
    from fleetledger.web import jobs
    from fleetledger.web.app import ui_of

    ui = ui_of(request)

    def run(job: Any) -> int:
        return jobs.run_cli(jobs.RecordingApp(ui.app, job), argv, job)

    try:
        ui.jobs.start("image", [], run, title=title, locks=[_lock(image)])
    except FleetError as exc:
        _flash(request, "bad", exc.message)
        return
    _flash(request, "ok", f"{title}: started; it shows under Images.")


async def publish(request: Request) -> Response:
    from fleetledger.web.forms import one

    fields, image = await _image_field(request)
    vmid = one(fields, "template_vmid")
    if image is None:
        _flash(request, "bad", "Choose an image.")
    elif vmid and not (vmid.isascii() and vmid.isdigit()):
        _flash(request, "bad", "A template VM id is a number, such as 9181.")
    elif vmid:
        _start(request, image, ["image", "publish", image, "--template-vmid", vmid],
               f"Record template {vmid} for {image}")
    else:
        _start(request, image, ["image", "publish", image], f"Publish {image}")
    return _back()


async def remove(request: Request) -> Response:
    from fleetledger.web.forms import one

    fields, image = await _image_field(request)
    if image is None:
        _flash(request, "bad", "Choose an image.")
    elif one(fields, "confirm") != "yes":
        _flash(request, "bad", "Tick the box to confirm.")
    else:
        keep = one(fields, "keep_file") == "yes"
        _start(request, image, ["image", "rm", image, *(["--keep-file"] if keep else [])],
               f"Remove {image}")
    return _back()


def section_context(ui: Any, inv: Any) -> dict[str, Any]:
    """What Settings' Images section needs: the images, and their recent jobs."""
    from fleetledger import images

    return {"images": images.listing(inv),
            "default_driver": inv.defaults.get("driver", "manual"),
            "image_jobs": [j for j in ui.jobs.shown() if j.kind == "image"][:3]}


def routes() -> list[Route]:
    return [
        Route("/settings/images/use", use, methods=["POST"]),
        Route("/settings/images/publish", publish, methods=["POST"]),
        Route("/settings/images/remove", remove, methods=["POST"]),
    ]
