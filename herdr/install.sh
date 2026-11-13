#!/usr/bin/env bash
# Installs the ShipShape layout into Herdr: the layout, its theme and its mod, and the font it uses.
# Build the art first (mods/shipshape/art/build.sh). Quit Herdr before installing: it reloads a mod on
# every file saved into its folder, and hundreds of new images at once can exhaust it.
#
#   herdr/install.sh            into ${HERDR_APP_HOME:-~/.config/herdr-app}
#   herdr-app do layout.switch '{"name":"shipshape"}'   (or ctrl-shift-l)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
home="${HERDR_APP_HOME:-$HOME/.config/herdr-app}"
mod="$here/mods/shipshape"

if ! ls "$mod"/img/*.png >/dev/null 2>&1; then
  echo "install: no art in $mod/img; run mods/shipshape/art/build.sh first" >&2
  exit 2
fi
if pgrep -x herdr-app >/dev/null; then
  echo "install: Herdr is running; quit it first (it reloads the mod on every file copied in)" >&2
  exit 2
fi

# IM Fell English (SIL Open Font License), from the Google Fonts repository.
fonts="$mod/fonts"
mkdir -p "$fonts"
for f in IMFeENrm28P IMFeENit28P; do
  [ -s "$fonts/$f.ttf" ] || curl -sfL -o "$fonts/$f.ttf" "https://raw.githubusercontent.com/google/fonts/main/ofl/imfellenglish/$f.ttf"
done
[ -s "$fonts/OFL.txt" ] || curl -sfL -o "$fonts/OFL.txt" "https://raw.githubusercontent.com/google/fonts/main/ofl/imfellenglish/OFL.txt"

mkdir -p "$home/layouts" "$home/themes" "$home/mods"
cp "$here/layouts/shipshape.json" "$here/layouts/shipshape.svg" "$home/layouts/"
cp "$here/themes/shipshape.json" "$home/themes/"
rm -rf "$home/mods/shipshape.new"
mkdir -p "$home/mods/shipshape.new"
(cd "$mod" && tar --exclude=__pycache__ --exclude=./art -cf - .) | (cd "$home/mods/shipshape.new" && tar -xf -)
rm -rf "$home/mods/shipshape"
mv "$home/mods/shipshape.new" "$home/mods/shipshape"
echo "installed into $home; start Herdr and switch to the layout: herdr-app do layout.switch '{\"name\":\"shipshape\"}'"
