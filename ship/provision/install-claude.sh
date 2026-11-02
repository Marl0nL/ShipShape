#!/usr/bin/env bash
# Install Claude Code with the native installer, as the agent user, after checking
# Anthropic's signed release manifest. ship-provision runs this once; afterwards
# Claude Code updates itself (~/.local/bin/claude is never replaced by ShipShape).
#
# usage: install-claude.sh MANIFEST_BASE CHANNEL KEY_FINGERPRINT MINIMUM_VERSION PLATFORM
#   PLATFORM is linux-x64 or linux-arm64.
set -euo pipefail

base=$1 channel=$2 fingerprint=$3 minimum=$4 platform=$5
key_url=https://downloads.claude.ai/keys/claude-code.asc

die() { echo "install-claude: $*" >&2; exit 1; }
fetch() { curl -fsSL --proto '=https' --tlsv1.2 --retry 3 "$@"; }

umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"
export GNUPGHOME="$work/gnupg"
mkdir -m 0700 "$GNUPGHOME"

version=$(fetch "$base/$channel")
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "the $channel channel gave an unexpected version"
lowest=$(printf '%s\n%s\n' "$minimum" "$version" | sort -V | head -n1)
[ "$lowest" = "$minimum" ] || die "the $channel channel gives $version, below the minimum $minimum"

fetch "$key_url" | gpg --batch --dearmor >key.gpg
primaries=$(gpg --batch --show-keys --with-colons key.gpg |
  awk -F: '$1 == "pub" { p = 1; next } $1 == "fpr" && p { print $10; p = 0 }')
[ "$primaries" = "$fingerprint" ] || die "the release signing key is not the pinned key"

fetch -o manifest.json "$base/$version/manifest.json"
fetch -o manifest.json.sig "$base/$version/manifest.json.sig"
gpgv --keyring "$work/key.gpg" manifest.json.sig manifest.json || die "manifest signature check failed"
checksum=$(jq -r --arg p "$platform" '.platforms[$p].checksum // empty' manifest.json)
[[ $checksum =~ ^[0-9a-f]{64}$ ]] || die "no checksum for $platform in the manifest"

fetch -o claude "$base/$version/$platform/claude"
echo "$checksum  claude" | sha256sum -c --quiet - || die "binary checksum mismatch"
chmod 0700 claude
./claude install "$version"
"$HOME/.local/bin/claude" --version
