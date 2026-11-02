#!/usr/bin/env bash
# ship-axi recipe (run as agent): install the `ship` skill into ~/.claude/skills/ship.
set -euo pipefail
src=$(cd "$(dirname "$0")/../skills/ship" && pwd)
dest=$HOME/.claude/skills/ship
if [ -d "$dest" ] && diff -rq -x README.md "$src" "$dest" >/dev/null 2>&1; then
  exit 0
fi
mkdir -p "$HOME/.claude/skills"
tmp=$(mktemp -d "$HOME/.claude/skills/.ship.XXXXXX")
cp -R "$src/." "$tmp/"
rm -f "$tmp/README.md"
chmod -R u=rwX,go=rX "$tmp"
rm -rf "$dest"
mv "$tmp" "$dest"
echo "installed the ship skill"
