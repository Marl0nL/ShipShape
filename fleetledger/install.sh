#!/usr/bin/env bash
# Install or update ship-fleet from this checkout with pipx.
#
# Editable on purpose: FleetLedger reads contracts/, versions.toml and the
# Ship catalogue from the checkout, and packs ship/ into tooling bundles, so
# `git pull` is all an update needs (rerun this script if dependencies change).
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

pipx install --force --editable "$here"
echo
echo "Installed: $(command -v ship-fleet || echo "ship-fleet (add pipx's bin folder to PATH)")"
echo "Next: ship-fleet init"
