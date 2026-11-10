"""prompts: the questions FleetLedger asks in a terminal."""

from types import SimpleNamespace

import pytest
from fleetledger.errors import FleetError

from fleetledger import prompts


def app(*answers, tty=True, secret=None):
    queue = list(answers)
    notes = []
    return SimpleNamespace(confirm=lambda prompt: queue.pop(0), note=notes.append,
                           stdin_isatty=lambda: tty, ask_secret=secret, notes=notes)


def test_ask_strips_and_falls_back_to_the_default():
    assert prompts.ask(app("  hello "), "Q: ") == "hello"
    assert prompts.ask(app("   "), "Q: ", "dflt") == "dflt"


def test_choose_asks_again_until_it_is_a_choice():
    a = app("maybe", "YES")
    assert prompts.choose(a, "Go", ("yes", "no"), "no") == "yes"
    assert a.notes == ["Please answer one of: yes, no."]
    assert prompts.choose(app(""), "Go", ("yes", "no"), "no") == "no"


def test_secret_is_stripped_unless_asked_not_to():
    a = app(secret=lambda prompt: " pass phrase ")
    assert prompts.secret(a, "S: ") == "pass phrase"
    assert prompts.secret(a, "S: ", strip=False) == " pass phrase "


def test_typed_needs_a_terminal_and_the_word():
    with pytest.raises(FleetError, match="^no tty$"):
        prompts.typed(app(tty=False), "Q: ", "yes", no_terminal="no tty", refused="no")
    with pytest.raises(FleetError, match="^refused$"):
        prompts.typed(app("nope"), "Q: ", "yes", no_terminal="x", refused="refused")
    prompts.typed(app(" Yes "), "Q: ", "yes", no_terminal="x", refused="y")  # any case


def test_typed_takes_a_name_only_as_it_is_spelt():
    with pytest.raises(FleetError, match="Not destroyed"):
        prompts.typed(app("SHIP-01"), "Q: ", "ship-01", exact=True, no_terminal="x",
                      refused="Not destroyed")
    prompts.typed(app(" ship-01 "), "Q: ", "ship-01", exact=True, no_terminal="x",
                  refused="Not destroyed")
