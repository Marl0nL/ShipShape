"""GitHub repos for a Ship's agents (github.py)."""

from __future__ import annotations

import pytest
from fleetledger.errors import FleetError

from fleetledger import github


@pytest.mark.parametrize("given,repo", [
    ("someone/agent-setup", "someone/agent-setup"),
    ("https://github.com/someone/agent-setup.git", "someone/agent-setup"),
    (" someone/agent.setup_2 ", "someone/agent.setup_2"),
])
def test_names_are_checked_and_tidied(given, repo):
    assert github.check_name(given) == repo


@pytest.mark.parametrize("bad", ["someone", "someone/", "/x", "a/b/c", "some one/x",
                                 "someone/..", "-x/y", "someone/x;rm"])
def test_bad_names_are_refused(bad):
    with pytest.raises(FleetError, match="not a GitHub repo name"):
        github.check_name(bad)


def test_what_happens_is_said_plainly():
    assert "clones it into ~/work/agent-setup as it starts" in github.what_happens(
        "someone/agent-setup", "public")
    assert "once you sign it in to GitHub" in github.what_happens("someone/x", "not-public")
    assert "Could not ask GitHub" in github.what_happens("someone/x", "unknown")
