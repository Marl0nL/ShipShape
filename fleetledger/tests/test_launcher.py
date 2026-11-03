"""ship-fleet's launcher: the current folder and empty PYTHONPATH entries can never
shadow the fleetledger package (shipfleet_launch.py explains the failure)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import shipfleet_launch

PROJECT = Path(__file__).resolve().parents[1]  # fleetledger/ (the outer project folder)
REPO = PROJECT.parent


@pytest.fixture
def editable_site(tmp_path):
    """A sitecustomize that finds fleetledger and the launcher the way an editable
    install does: through a hook consulted after sys.path, not through sys.path."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(textwrap.dedent(f"""
        import importlib.machinery, importlib.util, sys
        PROJECT = {str(PROJECT)!r}

        class EditableFinder:
            @classmethod
            def find_spec(cls, name, path=None, target=None):
                if name == "fleetledger":
                    return importlib.util.spec_from_file_location(
                        name, PROJECT + "/fleetledger/__init__.py",
                        submodule_search_locations=[PROJECT + "/fleetledger"])
                if name == "shipfleet_launch":
                    return importlib.util.spec_from_file_location(
                        name, PROJECT + "/shipfleet_launch.py")
                return None

        sys.meta_path.append(EditableFinder)
    """))
    return site


def run(code: str, site: Path, pythonpath: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH")}
    env["PYTHONPATH"] = pythonpath.format(site=site)
    return subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("pythonpath", ["{site}:", ":{site}", "{site}::/nonexistent"])
def test_the_failure_and_the_fix(editable_site, pythonpath):
    """From the checkout root with an empty PYTHONPATH entry, a plain import picks up
    the outer fleetledger/ folder as a namespace package; the launcher does not."""
    plain = run("from fleetledger import __version__", editable_site, pythonpath)
    assert plain.returncode != 0 and "unknown location" in plain.stderr
    launched = run("import shipfleet_launch, sys; sys.exit(shipfleet_launch.main(['--version']))",
                   editable_site, pythonpath)
    assert launched.returncode == 0, launched.stderr
    assert launched.stdout.startswith("ship-fleet ")


def test_clean_sys_path_drops_the_cwd_empty_and_shadowing_entries(tmp_path):
    shadow = tmp_path / "checkout"
    (shadow / "fleetledger").mkdir(parents=True)  # a project folder, no __init__.py
    real = tmp_path / "site-packages"
    (real / "fleetledger").mkdir(parents=True)
    (real / "fleetledger" / "__init__.py").write_text("")
    cwd = tmp_path / "work"
    cwd.mkdir()
    path = ["", ".", str(cwd), str(shadow), str(real), "/usr/lib/python3"]
    assert shipfleet_launch.clean_sys_path(path, str(cwd)) == [str(real), "/usr/lib/python3"]


def test_the_current_folder_stays_when_it_holds_the_real_package(tmp_path):
    """A compat-mode editable install puts the project folder on sys.path; standing
    in it must not hide the package. A relative '' or '.' still goes."""
    project = tmp_path / "project"
    (project / "fleetledger").mkdir(parents=True)
    (project / "fleetledger" / "__init__.py").write_text("")
    path = ["", ".", str(project), "/usr/lib/python3"]
    assert shipfleet_launch.clean_sys_path(path, str(project)) == [str(project),
                                                                   "/usr/lib/python3"]


@pytest.fixture
def saved_modules():
    """Put every fleetledger module back afterwards: the tests below remove them."""
    saved = {n: m for n, m in sys.modules.items()
             if n == "fleetledger" or n.startswith("fleetledger.")}
    yield
    for name in [n for n in sys.modules if n == "fleetledger" or n.startswith("fleetledger.")]:
        del sys.modules[name]
    sys.modules.update(saved)


def test_a_namespace_package_already_imported_is_forgotten(saved_modules):
    import types

    sys.modules["fleetledger"] = types.ModuleType("fleetledger")  # no __file__: a namespace
    sys.modules["fleetledger.cli"] = types.ModuleType("fleetledger.cli")
    shipfleet_launch.forget_namespace_package()
    assert "fleetledger" not in sys.modules and "fleetledger.cli" not in sys.modules


def test_main_forgets_an_imported_namespace_package(saved_modules):
    """main() itself drops a namespace package imported before it ran."""
    import types

    sys.modules["fleetledger"] = types.ModuleType("fleetledger")
    with pytest.raises(SystemExit) as exc:
        shipfleet_launch.main(["--version"])
    assert exc.value.code == 0


def test_the_real_package_is_kept(monkeypatch):
    import fleetledger

    shipfleet_launch.forget_namespace_package()
    assert sys.modules["fleetledger"] is fleetledger


def test_the_command_points_at_the_launcher():
    text = (PROJECT / "pyproject.toml").read_text()
    assert 'ship-fleet = "shipfleet_launch:main"' in text
    assert 'py-modules = ["shipfleet_launch"]' in text


@pytest.mark.parametrize("value, warned", [(":/x", True), ("/x:", True), ("/a::/b", True),
                                           ("/a:/b", False), ("", False)])
def test_install_sh_warns_about_empty_pythonpath_entries(tmp_path, value, warned):
    """Run only install.sh's PYTHONPATH check (pipx is replaced by a stub)."""
    stub = tmp_path / "bin"
    stub.mkdir()
    for tool in ("pipx", "ssh", "ssh-keygen", "ssh-keyscan"):
        (stub / tool).write_text("#!/bin/sh\nexit 0\n")
        (stub / tool).chmod(0o755)
    env = {"PATH": f"{stub}:/usr/bin:/bin", "PYTHONPATH": value, "HOME": str(tmp_path)}
    out = subprocess.run(["bash", str(PROJECT / "install.sh")], env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert ("empty entry" in out.stderr) is warned
    assert "ship-fleet setup" in out.stdout
