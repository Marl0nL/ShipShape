#!/usr/bin/env bash
# Builds every image the ShipShape layout draws, into ../img, and its anchors into ../sprites.js.
# An hour or more of CPU: the ships, the close-ups and the map are path-traced in Blender.
#
#   BLENDER=/path/to/blender art/build.sh
#
# Needs: Blender 4.2+ (BLENDER, default `blender` on PATH); python3 with numpy, scipy and pillow; curl.
# Downloads (once, into $SHIPSHAPE_ART, default ~/.local/share/shipshape-art): Natural Earth 10m land,
# minor islands, rivers and lakes (public domain), and Mapzen/Tilezen terrarium elevation tiles
# (zoom 6, AWS open data). Work files stay there too.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
blender="${BLENDER:-blender}"
work="${SHIPSHAPE_ART:-$HOME/.local/share/shipshape-art}"
img="$here/../img"
command -v "$blender" >/dev/null || { echo "build: Blender not found; set BLENDER" >&2; exit 2; }
mkdir -p "$work/geo/terr" "$work/stage" "$work/out/close" "$img"

echo "==> map data"
ne=https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson
for f in land minor_islands rivers_lake_centerlines lakes; do
  case "$f" in land) out=land ;; minor_islands) out=minor ;; rivers_lake_centerlines) out=rivers ;; lakes) out=lakes ;; esac
  [ -s "$work/geo/$out.geojson" ] || curl -sfL -o "$work/geo/$out.geojson" "$ne/ne_10m_$f.geojson"
done
for x in $(seq 30 38); do
  for y in $(seq 22 26); do
    t="$work/geo/terr/$x-$y.png"
    [ -s "$t" ] || curl -sf -o "$t" "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/6/$x/$y.png"
  done
done

cd "$here"
echo "==> the Mediterranean"
python3 medmap.py "$work/geo" "$work/stage"
"$blender" -b -P blend_med.py -- "$work/stage" "$work/out" 100 >/dev/null

echo "==> ships (every class, sail count and heading, and their lamps)"
./batch_ships.sh "$blender" "$work/out"
echo "==> close-ups"
# shellcheck disable=SC2046
SHIP_PX=200 "$blender" -b -P blend_ship.py -- "$work/out/close" $(python3 -c '
from shipspec import SHIPS, SAILS
print(" ".join(f"{c}-{t}{n}-1" for c in SHIPS for n in range(SAILS[c] + 1) for t in ("", "L")))') >/dev/null
echo "==> compass"
"$blender" -b -P blend_compass.py -- "$img/compass.png" >/dev/null

echo "==> sea, cloud, pennants, gulls, smoke"
python3 render_world.py sea clouds flags >/dev/null
python3 extras.py "$img" >/dev/null
python3 creatures.py "$img" >/dev/null
python3 sheets.py "$img"

echo "==> finishing"
SHIPSHAPE_IMG="$img" python3 post.py ships close map
