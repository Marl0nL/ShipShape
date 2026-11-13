"""pytest fixtures for the Ship track. The harness lives in ship_harness.py, so tests
import helpers from there (a module named `conftest` is ambiguous: fleetledger/tests
has one too)."""

import pytest
import ship_harness
from ship_harness import clean_env, ship  # noqa: F401  (fixtures)


@pytest.fixture(scope="session", autouse=True)
def _bundle_home(tmp_path_factory):
    """The compiled bundles make_bundle copies are built under pytest's temporary
    folder (each xdist worker its own), so even a killed run leaves none behind."""
    ship_harness.use_bundle_home(tmp_path_factory.mktemp("bundles"))


@pytest.fixture(autouse=True)
def _no_pane_env(clean_env):  # noqa: F811  (uses the fixture)
    """Every ship test runs without the HERDR_*, HC_* and SHIPSHAPE_* settings of
    the pane that started pytest."""
