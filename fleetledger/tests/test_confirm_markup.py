"""Every confirm tick box in the web UI's templates is wired to the dialog (app.js).

The server checks these fields (test_web.py, test_websetup.py); app.js hides a
box marked data-confirm-field and ticks it only after the person confirms in the
dialog, for forms marked data-confirm. A box without the marker would stay a
plain tick box; one whose form lacks data-confirm would be hidden with nothing
to tick it, so the action could never be confirmed.
"""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "fleetledger" / "web" / "templates"
CONFIRM_NAMES = ("confirm", "matches", "changed_ok")
FORM = re.compile(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", re.S)
# An <input> tag, allowing Jinja tags such as {% if x %} checked{% endif %} inside it.
TAG_PART = r"(?:[^>{]|\{[^}]*\})*"
BOX = re.compile(rf"<input\b{TAG_PART}type=\"checkbox\"{TAG_PART}>")


def boxes():
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for form in FORM.finditer(text):
            for box in BOX.findall(form.group("body")):
                name = re.search(r'name="([^"]+)"', box).group(1)
                yield path.name, form.group("attrs"), name, box


def test_every_confirm_box_is_wired_to_the_dialog():
    found = list(boxes())
    confirm = [(f, attrs, box) for f, attrs, name, box in found if name in CONFIRM_NAMES]
    assert len(confirm) >= 7  # fleet: 1 (in a loop), ship: 2, setup: 4
    for template, attrs, box in confirm:
        assert "data-confirm-field" in box, f"{template}: {box}"
        assert "data-confirm=" in attrs, f"{template}: its form has no data-confirm"


def test_only_confirm_boxes_are_hidden():
    for template, _attrs, name, box in boxes():
        if "data-confirm-field" in box:
            assert name in CONFIRM_NAMES, f"{template}: {name} is a choice, not a confirmation"
