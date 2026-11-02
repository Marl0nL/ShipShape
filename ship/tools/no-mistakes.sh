#!/usr/bin/env bash
# no-mistakes recipe (run as agent): pinned release binary.
set -euo pipefail
# shellcheck disable=SC1091 # sourced from this folder at run time
. "$(dirname "$0")/lib/release-binary.sh"
install_release_binary no-mistakes v1.106.1 \
  "https://github.com/kunchenguid/no-mistakes/releases/download/{version}/no-mistakes-{version}-linux-{arch}.tar.gz" \
  fbf131356a278623a509b6b50ecb39c5375b1a82e7dcfc48e42d10e761416d1e \
  8529f37d3b11a213abe9162851e84208e9c12e9d8ebf830e037f05e00558a656
