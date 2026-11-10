# Toolkits contract: your own recipes, profiles, sign-ins and skills

A toolkit is a folder of yours (often a checkout of a private repo) laid out like
ShipShape's own catalogue. It lets you bring tools ShipShape will never name, because
the repo is public. FleetLedger reads it from the path local config gives, checks it,
and lists its tools and profiles beside the catalogue's.

Today FleetLedger lists and checks toolkits; it does **not** send them to Ships yet.
`ship-fleet tools add`, `ship-fleet new --tools/--profile` and seeds refuse toolkit
tools and profiles with that reason.

Schema: `toolkit.schema.json` (for `toolkit.toml`). Examples: `examples/toolkit/`.

## Layout

```
mykit/
  toolkit.toml            schema, id, version, description, instructions, files
  tools/<id>.toml         recipes: tool-recipe.schema.json
  tools/*.sh              recipe scripts (kind = "script"), relative to tools/
  profiles/<id>.toml      profiles: profile.schema.json
  signins/<id>.toml       sign-ins, in FleetLedger's provider format
  skills/<name>/SKILL.md  Agent Skills
  instructions/*.md       standing instructions for the Ship's agents
  files/                  vendored files (release archives), pinned in toolkit.toml
```

```toml
# toolkit.toml
schema = 1
id = "mykit"
version = "1.4.0"
description = "Tools I use on my Ships"
instructions = ["instructions/mykit.md"]

[files]
"files/example-cli-1.2.0.tar.gz" = "<its sha256>"
```

## Adding one

In local, git-ignored `ships.toml` (`ships.toml.schema.json`, `toolkit`):

```toml
[toolkit.mykit]
path = "~/toolkits/mykit"
allow_root = false          # recipes may not run as root unless true
only_tags = ["work"]        # only Ships with one of these tags (enforced once toolkits reach Ships)

[toolkit.mykit.options]
site = "docs.example.com"   # values its sign-ins and profiles use
```

```sh
ship-fleet toolkit add mykit ~/toolkits/mykit   # checks it, then writes [toolkit.mykit]
ship-fleet toolkit ls                           # every toolkit, its state and contents
ship-fleet toolkit check mykit                  # check an added toolkit again
ship-fleet toolkit check ~/toolkits/mykit       # check a folder: usable in the toolkit's own CI
ship-fleet toolkit show mykit                   # its tools, profiles, sign-ins, skills
```

A toolkit with any problem does not load at all; `tools ls`, `toolkit ls` and the web
UI say so, and the catalogue and other toolkits work as before.

## Ids

- Tools and profiles are named `<toolkit>/<id>` (`mykit/example-cli`), so a later
  catalogue tool can never collide with one of yours. The `toolId` and `profileId`
  patterns in `ship.toml`, `ships.toml`, the status document and profiles allow one
  `toolkit/` prefix; a recipe's or profile's own `id` (its file name) never has one.
- The CLI takes a toolkit tool's or profile's bare id when only one toolkit has it.
- A toolkit's own ids may not reuse a catalogue tool's or profile's id.
- Sign-in ids stay flat (they are status keys, `^[a-z][a-z0-9_]{0,31}$`) and must be
  unique across FleetLedger's own sign-ins and every toolkit loaded.

## Rules on top of the catalogue's (`tools.md`)

- Everything a recipe or profile points at resolves **inside** the toolkit: `script`
  (relative to `tools/`), `setup` (relative to `profiles/`), `skill_dir` (relative to
  the toolkit's top, with a `SKILL.md`), and `instructions`.
- No symlinks anywhere in the folder (a top-level `.git` is ignored).
- Every file under `files/` is listed in `toolkit.toml` `[files]` with its SHA-256,
  and matches it.
- npm versions (`version`, `also_npm`) are one exact version: no ranges or tags.
- No apt repositories: `apt` entries and apt packages are plain package names.
- `default = true` and `bake = true` are refused: a toolkit tool lands only where you
  add it, and baked images hold catalogue tools only.
- `run_as = "root"` is refused unless ships.toml says `allow_root = true` for it.
- `needs` names a sign-in FleetLedger knows or one of the toolkit's own.
- A profile lists catalogue tools and its own toolkit's (bare or `mykit/...`), not
  another toolkit's.
- A sign-in file has a `[signin]` table, and its `part_of` names one of the toolkit's
  own sign-ins. Sign-in files are checked by FleetLedger's provider loader until the
  shared sign-in contract lands.

## Moving FleetLedger

`ships.toml` moves with `ship-fleet migrate`; the toolkit folders are yours and do not.
The plan names each one ("bring this folder to the new computer"), and `migrate wipe`
leaves a toolkit folder even when it sits inside FleetLedger's own folders.
