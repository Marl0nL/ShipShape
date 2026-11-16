#!/usr/bin/env bash
# Installs the Fleet panel (herdr/mods/fleet) into Herdr, and only it: no layout, no theme, and it
# never docks the panel into a layout. Open it as a tab afterwards:
#
#   herdr/install-fleet.sh       into ${HERDR_APP_HOME:-~/.config/herdr-app}/mods/fleet
#   herdr-app do tab.new '{"name":"Fleet","mod":"fleet"}'
#
# Run it again to update. It never writes the panel's settings.json (see mods/fleet/README.md).
#
# Herdr docks a new mod into the layout on screen if it sees main.js before a mod.json saying
# "place": "none", so mod.json goes in first; and model.js before main.js, which imports it.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
home="${HERDR_APP_HOME:-$HOME/.config/herdr-app}"
dest="$home/mods/fleet"

mkdir -p "$dest"
for f in mod.json model.js main.js README.md settings.example.json; do
  cp "$here/mods/fleet/$f" "$dest/$f"
done

echo "installed the Fleet panel into $dest"
if [ ! -e "$dest/settings.json" ]; then
  echo "next: say where FleetLedger is in $dest/settings.json (copy settings.example.json beside it)"
fi
echo "open it: herdr-app do tab.new '{\"name\":\"Fleet\",\"mod\":\"fleet\"}'"
