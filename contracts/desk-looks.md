# Desk looks folder

Your own looks for the Herdr Desk (`herdr/mods/desk`), as a folder of data. The Desk merges it
over the looks it ships with (`herdr/mods/desk/looks.js`); a computer without the folder looks
exactly as it would without this contract. The folder is the unit that moves between
computers: today by hand, later through FleetLedger, unchanged.

## Layout

```
desk-looks/                      in Herdr's folder: ~/.config/herdr-app, or $HERDR_APP_HOME
  looks.json                     desk-looks.schema.json: palettes, presets, tag looks
  palettes/looks-<id>.json       desk-palette.schema.json: one Herdr theme per listed palette
  logos/<name>.png | <name>.jpg  the pictures presets name for the window's corner
```

Nothing else in the folder is read. Files are found by name from `looks.json`, never by
listing the folder: a palette `looks-<id>` is `palettes/looks-<id>.json`, a brand's `logo` is
`logos/<logo>`. Names are a-z, 0-9 and `-` (plus the extension), so no name is a path.

## Rules a schema cannot say

The Desk checks all of these (`herdr/mods/desk/lookfolder.js`), and so must anything that
stores or carries a folder:

- `looks.json` is `shipshape.desk-looks/1` and has no field the schema does not name; each
  palette file is a `desk-palette`, likewise.
- Ids are unique: palette ids, preset ids, and tags in `tag_looks`. A palette id starts
  `looks-`, so it never names a theme of Herdr's or the Desk's own.
- A preset's `palette` is a dark palette and its `light` a light one: one of the Desk's
  (`graphite`, `ink`, `moss`, `desk-slate`, `desk-ember`; `paper`, `desk-linen`) or one the
  folder lists with the matching `dark`. Its `finish`, `type` and `density` are the Desk's.
- A tag look's `preset` is a preset of the folder's or of the Desk's.
- A logo is the picture its name says, by its first bytes: PNG (`89 50 4E 47 0D 0A 1A 0A`)
  or JPEG (`FF D8 FF`).
- Limits: `looks.json` 64 KiB; each palette file 16 KiB; each logo 64 KiB; at most 16
  palettes, 32 presets, 64 tag looks and 16 logos; 512 KiB for the files read in all.
- A folder with one thing out of place is ignored whole: the Desk wears its own looks and
  says why in the Look popup.

## What the Desk does with it

- Palettes: each is written, from its checked values (never the file's own text), to Herdr's
  `themes/<id>.json`, only when that differs. Only `looks-*` themes are ever written there.
  A palette later dropped from the folder leaves its theme file behind, unused.
- Presets join the Desk's (one with a Desk preset's id replaces it) in the Look popup and the
  `desk.look-<preset>` commands.
- Tag looks win over the Look popup's choice for the same tag; tags the folder does not name
  keep the popup's.
- It reads the folder when the desk starts, when the Look popup opens and on the
  `desk.looks-reload` command; never on a timer.

## For carrying it

FleetLedger keeps one folder for the fleet (`ship-fleet looks publish`, `contracts/ship-fleet.md`,
"ship-fleet looks"), checked by these same rules on the way in; nothing carries it to a
computer from there yet.

Every key in `looks.json` and the palette files is a-z and `_`, and nothing nests more than
four deep, so the refresher's strict JSON reader (`fleetledger/fleetledger/refresher/refresh.sh`)
can read it as it is. The binary files are the logos alone.
