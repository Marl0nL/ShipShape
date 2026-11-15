# ShipShape for Herdr

A Herdr layout that shows the fleet as a sea chart of the Mediterranean. Every Herdr space is a
ship (in ShipShape terms one Ship, one VM), its agents are the crew, and the chart sails each one
out on a voyage while its agents work. Beside it sit the terminals of the selected ship, a muster
roll of every agent, and a close-up "deck view" of any ship.

It is a Herdr *layout* plus one *mod*, laid out the way Herdr shares layouts:

| Path | What it is |
|---|---|
| `layouts/shipshape.json` | the window: the helm bar, the chart and muster roll, the console (terminals, or the watch) |
| `themes/shipshape.json` | a dusk-sea theme (ink, brass, cream) |
| `mods/shipshape/` | the mod: every panel in the layout, in roles (`main.js`) |
| `mods/shipshape/art/` | the art pipeline: Blender and numpy scripts that render every image the chart draws |
| `install.sh` | copies the layout, theme and mod into Herdr's folder |

## What it shows

| In Herdr | On the chart |
|---|---|
| A space | A ship. Its class goes by the agents aboard: dinghy (0), sloop (1-2), brig (3-4), frigate (5-6), galleon (7+) |
| Agents working | One sail set for each; the ship sails a sea lane out to its own port and works the waters off it |
| An agent blocked | The ship heaves to where she is under a red signal; fog closes in, and after four minutes a squall |
| An agent finished unseen | The ship ties up at her port with a green lamp; the town strings up lights and gulls gather |
| Nothing in flight | The ship sails home and anchors |
| A Ship's first ShipShape tag | Her home harbour. Port Ledger (Heraklion, Crete) is home to this computer and untagged ships; each tag gets a harbour of its own (Valletta, Limassol, Palma, ...) |
| The agent's terminal title | Her goal, written on her port |

The wind is a steady westerly across the whole chart: sails are rendered trimmed and heeled for each
of eight headings, ships go fastest on a reach and beat to windward, pennants, cloud and smoke go
downwind. The chart keeps the clock: dawn, day, dusk and night, lanterns and windows lit after dark,
the lighthouse beam sweeping.

- **Chart:** scroll to zoom about the pointer, drag to move; the buttons at the bottom right zoom,
  frame the whole fleet, or go home. Click a ship to select it (and go to whichever agent needs you);
  double-click, or keep zooming in on her, to go aboard. Click open sea to let her go.
- **Deck view:** the ship up close. Each set sail carries the name and task of a working agent (click
  to go to them); the wheel is the helmsman's; her papers show where she is bound, her crew and, for a
  Ship FleetLedger knows, her vitals. Scroll out or press Chart to go back.
- **Console:** the selected ship's terminals; with no ship selected, *the watch*: every agent's last
  word, ship by ship.
- **Muster roll:** every agent, the ones that need you first; `alt-up`/`alt-down` follow its order.
- **Helm bar:** the shown ship and its tabs, the fleet's tally, the hour, and window controls.

Commands, for `keys.json` or `herdr-app do`: `shipshape.aboard`, `shipshape.chart`,
`shipshape.watch`, `shipshape.ship`, `shipshape.fleet`, `shipshape.home`, and `shipshape.dawn`,
`.noon`, `.dusk`, `.night`, `.clock`.

## How it reads ShipShape

- **Tags and vitals:** `ship-fleet ls --json --offline` (FleetLedger's last known status, no SSH),
  every five minutes. A space on another machine is matched to the Ship of the same name. Without
  FleetLedger every ship simply sails from Port Ledger.
- **Papers:** the deck view's Papers button opens a tab on this computer running `ship-fleet ui`.
- **The helmsman** is meant to be the Ship's primary agent (`ship-ctl agent primary`). Herdr can't
  read the agent record, so for now the wheel goes to an agent named `main` (or ending `-main`),
  else a pinned one, else the first aboard. A label set by the Ship (`hc label`) would make this exact.

## Install

The rendered images are not in the repository (about 100 MB); build them once, then install.

```sh
BLENDER=/path/to/blender herdr/mods/shipshape/art/build.sh   # an hour or more of CPU
# quit Herdr, then:
herdr/install.sh                                             # into ~/.config/herdr-app
herdr-app do layout.switch '{"name":"shipshape"}'            # or ctrl-shift-l
```

`build.sh` needs Blender 4.2 or later, python3 with numpy, scipy and pillow, and curl. `install.sh`
also fetches the font, IM Fell English (SIL Open Font License), from the Google Fonts repository.

## The art pipeline

Everything is rendered from scripts; nothing is drawn by hand.

| Script | Makes |
|---|---|
| `shipspec.py` | the ships' measurements, the wind, sail trim and heel, shared by the rest |
| `blend_ship.py` | a ship in Blender (hull, rig, sails trimmed to the wind, deck, crew), path-traced; `batch_ships.sh` runs every class, sail count and heading, and the lamps-only passes |
| `medmap.py`, `blend_med.py` | the Mediterranean: coastlines, real relief and depths, rivers, towns, sea lanes; then built and rendered in Blender (`blend_isle.py` holds the shared scenery pieces) |
| `blend_compass.py` | the brass compass |
| `render_world.py`, `extras.py`, `creatures.py`, `sheets.py` | the sea, its swell and glitter, cloud, pennants, gulls, smoke, the lighthouse beam |
| `post.py` | foam and wakes laid under the ships, lamps turned into light, and every sprite's anchors into `../sprites.js` |
| `render3d.py` | shared camera and finishing helpers (and the first numpy ship renderer) |

Map data, downloaded by `build.sh`: [Natural Earth](https://www.naturalearthdata.com/) 10m land,
minor islands, rivers and lakes (public domain), and the
[Terrain Tiles](https://registry.opendata.aws/terrain-tiles/) on AWS Open Data (Mapzen/Tilezen:
SRTM, GMTED, ETOPO1 and other sources; see their attribution notes).

## Limits worth knowing

- **Herdr reloads a mod on every file saved into its folder.** Hundreds of images at once exhaust its
  script runtime ("out of memory" in `herdr-app status`) until Herdr is restarted. Quit Herdr before
  `install.sh`; when developing, write images elsewhere and copy them in at once.
- **Only native animation.** A mod can move, resize and fade elements (`transition`, `loop`, `spin`)
  but not step through frames or tilt an image. Two poses cross-faded show as a ghost, so ships sway
  and heave as one image rather than roll. `loop` always swings back, so anything that should travel
  one way (cloud, swell) is moved by a transition and put back a whole period at each turn
  (`drift()` in `chart.js`).
- **Tiles show seams** when moved by fractions of a pixel; repeating layers are drawn as one seamless
  image (`sheets.py`).
- `herdr-app check` loads the mod without its roles' props, so a pass says little; check
  `herdr-app status` for errors and look at `herdr-app shot`.
