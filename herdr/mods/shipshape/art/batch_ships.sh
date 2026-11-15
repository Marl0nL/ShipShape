#!/usr/bin/env bash
# Renders every ship sprite with Blender, four processes side by side: each class with each number of
# sails set, on each heading, and its lamps-only pass (lit as she heels).
#   art/batch_ships.sh /path/to/blender /path/to/out
set -euo pipefail
blender="$1"
out="$2"
cd "$(dirname "$0")"
jobs="$(mktemp -d)"
trap 'rm -rf "$jobs"' EXIT
python3 - > "$jobs/all" <<'P'
from shipspec import SHIPS, SAILS, HEADINGS
jobs = [f"{c}-{t}{n}-{k}" for c in SHIPS for n in range(SAILS[c] + 1) for k in range(HEADINGS) for t in ("", "L")]
print("\n".join(jobs))
P
split -n l/4 -d "$jobs/all" "$jobs/part."
for f in "$jobs"/part.*; do
  mapfile -t list < "$f"
  SHIP_THREADS=4 "$blender" -b -P blend_ship.py -- "$out" "${list[@]}" > "$f.log" 2>&1 &
done
wait
if grep -l "Error\|Traceback" "$jobs"/part.*.log; then
  echo "batch_ships: a render failed (logs above)" >&2
  exit 1
fi
