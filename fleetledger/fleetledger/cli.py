"""ship-fleet command line.

Placeholder until the Fleet track builds W10. The command name, the package name
and --version are fixed here so the Ship track and docs can rely on them.
"""

from __future__ import annotations

import argparse
import sys

from fleetledger import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ship-fleet",
        description="Find, show and manage ShipShape Ships.",
    )
    parser.add_argument("--version", action="version", version=f"ship-fleet {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print("ship-fleet: no commands yet; the Fleet track is building them (docs/tracks.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
