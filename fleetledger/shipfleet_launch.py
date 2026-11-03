"""Start ship-fleet so that the current folder can never shadow the package.

ship-fleet is installed editable: the `fleetledger` package is found through an
import hook that Python consults *after* sys.path. An empty PYTHONPATH entry (a
leading or trailing ':', or '::') or the script's own folder puts the current
directory on sys.path, and from a ShipShape checkout's root the outer project
folder `fleetledger/` (no __init__.py) is then taken as a namespace package
called fleetledger: "cannot import name '__version__' from 'fleetledger'
(unknown location)". This launcher drops such entries before importing anything
from fleetledger. It is the target of the ship-fleet command.
"""

from __future__ import annotations

import os
import sys

PACKAGE = "fleetledger"


def _shadows(entry: str) -> bool:
    """An entry whose `fleetledger` folder is not a real package would shadow it."""
    folder = os.path.join(entry or os.curdir, PACKAGE)
    return os.path.isdir(folder) and not os.path.isfile(os.path.join(folder, "__init__.py"))


def _holds_package(entry: str) -> bool:
    return os.path.isfile(os.path.join(entry, PACKAGE, "__init__.py"))


def clean_sys_path(path: list[str], cwd: str) -> list[str]:
    """sys.path without the current directory, empty entries and shadowing folders.

    One exception: an absolute entry for the current folder that holds the real
    package stays (a compat-mode editable install puts the project folder on
    sys.path, and you may be standing in it)."""
    here = os.path.realpath(cwd)
    kept = []
    for entry in path:
        if entry in ("", os.curdir):
            continue
        try:
            if os.path.realpath(entry) == here and not (
                    os.path.isabs(entry) and _holds_package(entry)):
                continue
        except (OSError, ValueError):
            pass
        if _shadows(entry):
            continue
        kept.append(entry)
    return kept


def forget_namespace_package() -> None:
    """If a shadowing namespace package was already imported, forget it."""
    module = sys.modules.get(PACKAGE)
    if module is not None and getattr(module, "__file__", None) is None:
        for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + ".")]:
            del sys.modules[name]


def main(argv: list[str] | None = None) -> int:
    sys.path[:] = clean_sys_path(sys.path, os.getcwd())
    forget_namespace_package()
    from fleetledger.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
