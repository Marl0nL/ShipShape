#!/usr/bin/env bash
# treehouse recipe (run as agent): pinned release binary.
set -euo pipefail
# shellcheck disable=SC1091 # sourced from this folder at run time
. "$(dirname "$0")/lib/release-binary.sh"
install_release_binary treehouse v3.1.8 \
  "https://github.com/kunchenguid/treehouse/releases/download/{version}/treehouse-{version}-linux-{arch}.tar.gz" \
  bd6227d909ba8046263ae5d4e072ccbcf43010cac9d7289b1262126510edfeed \
  3d1c02c8ab13049709ace66be51d99b72fe7b6d0b73d0d0efd90f226e1aa87d1
