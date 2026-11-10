#!/usr/bin/env bash
# Installs example-cli from the vendored archive (a placeholder for tests).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -m 0755 -D "$here/files/example-cli.txt" "$HOME/.local/bin/example-cli"
