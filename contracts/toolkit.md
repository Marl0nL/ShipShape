# Toolkits contract: your own recipes, profiles, sign-ins and skills

A toolkit is a folder of yours (often a checkout of a private repo) laid out like
ShipShape's own catalogue. It lets you bring tools ShipShape will never name, because
the repo is public. FleetLedger reads it from the path local config gives, checks it,
lists its tools and profiles beside the catalogue's, and sends it, as its own archive,
only to the Ships its `only_tags` allow ("On a Ship" below).

Schemas: `toolkit.schema.json` (for `toolkit.toml`), `toolkit-manifest.schema.json`
(the archive's `toolkit.json`). Examples: `examples/toolkit/`,
`examples/toolkit-manifest/`.

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
only_tags = ["work"]        # only Ships with one of these tags get it (none: every Ship)

[toolkit.mykit.options]
site = "docs.example.com"   # values its sign-ins and profiles use
```

```sh
ship-fleet toolkit add mykit ~/toolkits/mykit   # checks it, then writes [toolkit.mykit]
ship-fleet toolkit ls                           # every toolkit, its state and contents
ship-fleet toolkit check mykit                  # check an added toolkit again
ship-fleet toolkit check ~/toolkits/mykit       # check a folder: usable in the toolkit's own CI
ship-fleet toolkit show mykit                   # its tools, profiles, sign-ins, skills
ship-fleet toolkit push mykit [--tag work]      # send it to the Ships it may go to
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
- Sign-in files follow the sign-in contract (`signin.schema.json`), as FleetLedger's
  own do. Each has a `[signin]` table, and its `part_of` names one of the toolkit's own
  sign-ins. A loaded toolkit's sign-ins join the catalogue's sign-in registry, so a
  later toolkit's `needs` may name them; an id or status key already known stops the
  toolkit loading.

## On a Ship

### Which Ships get it

A toolkit goes only to a Ship with one of its `only_tags` (the Ship's tags in the
inventory); with no `only_tags`, to every Ship. FleetLedger sends it:

- with `ship-fleet toolkit push ID [--tag T] [--force]`, to every Ship (or every Ship
  tagged T) it may go to; the others are listed as skipped, with the reason;
- with `ship-fleet update`, after the tooling update, every toolkit that Ship may have;
- with `ship-fleet tools add`, the toolkit of a toolkit tool added, first;
- when a new Ship enrols (`ship-fleet new`, `status --wait`), every toolkit it may have.

`tools add`, `new --tools/--profile`, seeds and the web UI's Settings ▸ Tools refuse a
toolkit's tools and profiles on a Ship that may not have it, and say why. A Ship that
already has the same release is not sent it again. A toolkit that does not load is
not sent; `update` says so.

A Ship whose tooling predates toolkits (no `toolkit` in `ship-ctl version`
`contracts`) is told to update first (`ship-fleet update`, which then sends its
toolkits); nothing is sent to it before.

### The archive

FleetLedger packs each toolkit on its own: a gzip tar of the toolkit's files, as
checked (bar a top-level `.git` and the cache and editor files the tooling bundle
also leaves out), with `toolkit.json` at the top (`toolkit-manifest.schema.json`):

```json
{ "schema": "shipshape.toolkit/1", "id": "mykit", "version": "1.4.0",
  "content_sha256": "<64 hex>", "created_at": "2026-03-01T00:00:00Z" }
```

- **Content hash.** One line per file but `toolkit.json`, sorted by path (UTF-8
  bytes): `<sha256 of the file> <755|644> <path>\n`; `content_sha256` is the SHA-256
  of those lines. 755 is an executable file. A toolkit may not have a `toolkit.json`
  of its own at its top.
- **Reproducible**, like the tooling bundle: sorted entries, owner root, modes 0644,
  0755 and folders 0755, times from `created_at`. Regular files and folders only;
  file names have no control characters or backslash.
- **At most 100 MiB** packed (the tooling bundle's limit stays 5 MiB). The Ship also
  refuses one that unpacks to more than 512 MiB or has more than 20000 entries.
- FleetLedger keeps the last archive of each toolkit in its data folder
  (`toolkit-archives/`), and sends the same bytes again while the toolkit's files are
  unchanged; `ship-fleet migrate` leaves that folder (it is rebuilt).

It is copied, with a `.sha256` beside it, into `/home/fleet/incoming/`, applied, then
removed.

### `ship-ctl toolkit apply --archive PATH [--allow-root] [--force]`

1. Refuse an archive over 100 MiB, one that does not match its `.sha256` (if there),
   one with a link, device, absolute path or `..`, or one whose `toolkit.json` does
   not validate.
2. Unpack it into a new folder under `/opt/shipshape/toolkits/`, recompute the
   content hash and refuse it unless it matches; its `toolkit.toml` must have the
   same id and version; its recipes and profiles must follow their schemas; its
   sign-in files must load together with the bundle's and the other toolkits' (no
   id or status key twice).
3. Refuse a version lower than the one on the Ship (semver precedence: `1.3.0-rc.1`
   is lower than `1.3.0`) unless `--force` (`toolkit-rollback`). The same version
   with other files is applied.
4. Make it root's, folders 0755 and files 0644 or 0755, and move it to
   `/opt/shipshape/toolkits/<id>/<version>-<hash12>/`; switch that toolkit's
   `current` link to it (atomically); record `[toolkits] <id> = "<version>-<hash12>"`
   in ship.toml, and whether FleetLedger said `--allow-root` (ships.toml
   `allow_root = true`) in `/var/lib/shipshape/toolkits.json`. Older releases are
   pruned (three are kept).
5. Install the toolkit's tools and profiles ship.toml asks for, from the new release.

`data`: `{ "toolkit": { "id", "version", "release", "previous", "allow_root" },
"installed"?, "profiles"? }`. When a tool fails, the toolkit stays applied and the
command exits 1 with `tool-failed`.

### The allow-list

ship.toml `[toolkits]` (root's, written only by `toolkit apply` and `remove`; `ship-ctl
set --file` keeps it and refuses to change it) is the allow-list. ship-ctl reads a
toolkit's recipes, profiles and sign-ins only through it, from the release folder it
names, never the `current` link, and only when that folder (and the folders above
it) is a real folder owned by root and writable by root alone. So:

- a toolkit's files on disk without an entry run nothing: its tools fail with "not
  on this Ship", and its sign-ins are not the Ship's;
- `run_as = "root"` recipes run only if that very release was applied with
  `--allow-root`;
- a toolkit recipe whose own id is a catalogue recipe's, or whose `apt` entries are
  not plain package names, is refused, as FleetLedger refuses them;
- two toolkits' skill folders of one name: the first keeps `~/.claude/skills/<id>`;
- a recipe's `apt` packages are installed as root even without `allow_root` (as for
  catalogue recipes: packages from the repositories the Ship already has, whose
  maintainer scripts run as root), so list only packages you would install by hand.

A Ship that stops qualifying (its tags, or the toolkit's `only_tags`, change) keeps the
toolkit until it is removed there (`ship-ctl toolkit remove ID`); removing it for you
is later work.

Toolkit tools and profiles are `<toolkit>/<id>` in ship.toml. In a toolkit's profile a
bare tool id is the toolkit's own tool when it has one, else a catalogue tool.
Scripts run from the release folder (`tools/`, `profiles/`), and `skill_dir` is
relative to the toolkit's top. The toolkit's sign-in files join the Ship's sign-ins
(status `auth`, `signin check`, `notify-signin`), so a tool's `needs` on one is
released by the usual reaction when it turns ok. `ship-ctl status` lists
`toolkits`: each one's id, version, release and state (`missing` when its folder is
not there).

### `ship-ctl toolkit remove ID` and `toolkit list`

`remove` uninstalls the toolkit's tools ship.toml asks for (directly or through its
profiles): npm packages no other tool the Ship keeps uses, the skill folder, the MCP
server, the Claude plugin (each unless a tool the Ship keeps uses the same one), and
the recipe's own `uninstall` command; apt packages stay. Then it drops its tools, profiles and profile options from ship.toml, its
`[toolkits]` entry, its records, its lines in the agent's environment file, and
`/opt/shipshape/toolkits/<id>/`. `data`: `{ "removed", "tools", "uninstalled",
"profiles" }`.

`list`: `data.toolkits`, each with `id`, `version`, `release`, `allow_root`, `state`
and its `tools`.

## Moving FleetLedger

`ships.toml` moves with `ship-fleet migrate`; the toolkit folders are yours and do not.
The plan names each one ("bring this folder to the new computer"), and `migrate wipe`
leaves a toolkit folder even when it sits inside FleetLedger's own folders.
