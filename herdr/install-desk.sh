#!/usr/bin/env bash
# Installs the Desk layout into Herdr: the layout, its themes, its three mods, and the agents' `desk`
# CLI (linked as ~/.local/bin/desk). It has no art to build.
#
#   herdr/install-desk.sh       into ${HERDR_APP_HOME:-~/.config/herdr-app}
#   herdr-app do layout.switch '{"name":"desk"}'   (or ctrl-shift-l)
#
# Herdr docks a new mod into the layout on screen if it sees main.js before a mod.json saying
# "place": "none", so each mod's mod.json is copied in before the rest of it.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
home="${HERDR_APP_HOME:-$HOME/.config/herdr-app}"

mkdir -p "$home/layouts" "$home/themes" "$home/mods"
cp "$here/layouts/desk.json" "$here/layouts/desk.svg" "$home/layouts/"
cp "$here"/themes/desk-*.json "$home/themes/"
for m in desk desk-agents desk-spaces; do
  mkdir -p "$home/mods/$m"
  cp "$here/mods/$m/mod.json" "$home/mods/$m/mod.json"
  (cd "$here/mods/$m" && tar --exclude=__pycache__ -cf - .) | (cd "$home/mods/$m" && tar -xf -)
done

# The agents' CLI. Leave some other `desk` alone.
bin="$HOME/.local/bin"
mkdir -p "$bin"
if [ -e "$bin/desk" ] && [ "$(readlink -f "$bin/desk")" != "$(readlink -f "$home/mods/desk/bin/desk")" ]; then
  echo "install-desk: $bin/desk is something else; not replacing it" >&2
else
  ln -sfn "$home/mods/desk/bin/desk" "$bin/desk"
fi

echo "installed into $home; switch to it: herdr-app do layout.switch '{\"name\":\"desk\"}'"
echo "optional key: \"ctrl-shift-d\": \"layout.toggle:desk\" in keys.json's global section shows or hides the desk"
