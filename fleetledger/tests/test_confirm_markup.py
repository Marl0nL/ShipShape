"""Every confirm tick box in the web UI's templates is wired to the dialog (app.js).

The server checks these fields (test_web.py, test_websetup.py); app.js hides a
box marked data-confirm-field and ticks it only after the person confirms in the
dialog, for forms marked data-confirm. A box without the marker would stay a
plain tick box; one whose form lacks data-confirm would be hidden with nothing
to tick it, so the action could never be confirmed.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "fleetledger" / "web"
TEMPLATES = WEB / "templates"
CONFIRM_NAMES = ("confirm", "matches", "changed_ok")
FORM = re.compile(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", re.S)
# An <input> tag, allowing Jinja tags such as {% if x %} checked{% endif %} inside it.
TAG_PART = r"(?:[^>{]|\{[^}]*\})*"
BOX = re.compile(rf"<input\b{TAG_PART}type=\"checkbox\"{TAG_PART}>")


def confirm_forms():
    """(template, field names) for every form the dialog handles."""
    for path in sorted(TEMPLATES.glob("*.html")):
        for form in FORM.finditer(path.read_text(encoding="utf-8")):
            if re.search(r"data-confirm(-when)?=", form.group("attrs")):
                yield path.name, set(re.findall(r'\bname="([^"]+)"', form.group("body")))


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
    assert len(confirm) >= 7  # fleet 1 (in a loop), _macros 1, _ship_overview 1, setup 4
    for template, attrs, box in confirm:
        assert "data-confirm-field" in box, f"{template}: {box}"
        assert "data-confirm=" in attrs, f"{template}: its form has no data-confirm"


def test_only_confirm_boxes_are_hidden():
    for template, _attrs, name, box in boxes():
        if "data-confirm-field" in box:
            assert name in CONFIRM_NAMES, f"{template}: {name} is a choice, not a confirmation"


def test_app_js_reads_no_form_property_a_field_could_shadow():
    """In a browser a field shadows the form property of its name (the certificate
    form's "matches" box hides form.matches), so app.js reads forms only through
    Element.prototype / HTMLFormElement.prototype. Guard both sides: no `form.x`
    in app.js's code, and no field in a confirm form named after a property it
    could still reach."""
    code = re.sub(r"//[^\n]*", "", (WEB / "static" / "app.js").read_text(encoding="utf-8"))
    used = set(re.findall(r"\bform\.([A-Za-z_]\w*)", code))
    assert used == set(), f"app.js reads form properties directly: {sorted(used)}"
    risky = {"elements", "dataset", "id", "action", "method", "submit", "requestSubmit",
             "querySelector", "querySelectorAll", "hasAttribute", "getAttribute",
             "setAttribute", "removeAttribute", "documentElement"}
    for template, names in confirm_forms():
        assert not names & risky, f"{template}: a field shadows {sorted(names & risky)}"
