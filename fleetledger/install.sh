#!/usr/bin/env bash
# Install or update ship-fleet from this checkout with pipx.
#
# Editable on purpose: FleetLedger reads contracts/, versions.toml and the
# Ship catalogue from the checkout, and packs ship/ into tooling bundles, so
# `git pull` is all an update needs (rerun this script if dependencies or the
# command's entry point change).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v pipx >/dev/null 2>&1; then
  echo "install.sh: pipx not found. Install it first (e.g. apt install pipx), then rerun." >&2
  exit 2
fi
for tool in ssh ssh-keygen ssh-keyscan; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "install.sh: $tool not found; FleetLedger uses your system's OpenSSH." >&2
    exit 2
  fi
done

# An empty PYTHONPATH entry (leading or trailing ':', or '::') means "the current
# folder". ship-fleet's launcher guards against it, but other Python tools in a
# ShipShape checkout would still trip over it: say so.
case ":${PYTHONPATH-}:" in
  *::*)
    if [ -n "${PYTHONPATH-}" ]; then
      echo "install.sh: warning: PYTHONPATH has an empty entry ('$PYTHONPATH'), which Python" >&2
      echo "  reads as the current folder. Remove the stray ':' (for example in ~/.bashrc)." >&2
    fi
    ;;
esac

pipx install --force --editable "$here"
echo
echo "Installed: $(command -v ship-fleet || echo "ship-fleet (add pipx's bin folder to PATH)")"
echo "Next: ship-fleet setup"
