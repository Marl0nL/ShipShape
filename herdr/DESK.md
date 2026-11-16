# The Desk: how it works today

The Desk is a Herdr layout for watching your agents. It is Herdr's default
`main` layout with one addition, a **desk** on the right that follows whichever agent you focus and
shows what that agent keeps for you. Agents fill their desks in themselves, on their own machines,
Ships included.

This document is the overview: what each piece is, how data moves, and where it touches ShipShape
and firstmate. The details live next to the code: [`mods/desk/README.md`](mods/desk/README.md)
for people, [`mods/desk/AGENTS.md`](mods/desk/AGENTS.md) for the agents that feed it.

This copy is a snapshot of a working install. `scripts/check` lints `install-desk.sh` and the
`desk` CLI, checks the mods' JavaScript parses and runs the CLI against a scratch board
(`tests/test_herdr_desk.py`); nothing drives a real Herdr. Herdr's own clones (below) don't
follow Herdr's updates.

## The pieces

| Path | What it is |
|---|---|
| `layouts/desk.json` | the window (a clone of Herdr's `main`; tree below) |
| `layouts/desk.svg` | its picture in Herdr's settings |
| `themes/desk-*.json` | palettes: Slate, Ember and Linen (the `work` tag wears Slate; `personal` wears Herdr's own Moss) |
| `mods/desk/` | the desk itself, in parts (`props.part`): `head`, `assistant`, `manager`, `coder`, `crew`, `none`, plus the window's `bar` and `brand` corner |
| `mods/desk/bin/desk` | the agents' CLI (one Python 3 file, no dependencies), linked as `~/.local/bin/desk` |
| `mods/desk-spaces/`, `mods/desk-agents/` | clones of Herdr's shipped `spaces` and `agents` sidebar mods, changed where marked `Desk:` (tag colours; the agents list's hierarchy view) |
| `install-desk.sh` | copies all of it into Herdr's folder and links the CLI |

```
root (row; terminal_opacity 0.88)
├─ sidebar (column, 268 px)
│  ├─ brand ─────────── desk {part: brand}      the look's logo and name
│  ├─ spaces ────────── desk-spaces             machines and spaces, tag-coloured
│  ├─ agents ────────── desk-agents             who reports to whom (default), or Priority / Grouped
│  └─ sidebar-footer ── workspace-chrome        Herdr's own
└─ work (column)
   ├─ tabs ──────────── desk {part: bar}        space › agent, tabs only when there are several
   ├─ work-body (row)
   │  ├─ main-stack (stack)                     switched per agent by the desk
   │  │  ├─ main ─────── the shown tab's terminals
   │  │  └─ crew (column)                       for a crew: its sheet over a short terminal
   │  │     ├─ crew-sheet ── desk {part: crew}
   │  │     └─ crew-term ─── the shown tab, 300 px, amber frame, caution triangle behind the text
   │  ├─ panel (hidden)  Herdr's Files / Changes
   │  └─ desk (column, 440 px; ctrl-shift-d)
   │     ├─ desk-head ── desk {part: head}      the agent, its kind, tags, look button
   │     └─ desk-body (stack)                   switched per agent by the desk
   │        ├─ desk-assistant / desk-manager / desk-none
   │        └─ desk-coder: desk {part: coder} over Herdr's Changes panel
   └─ status ─────────── workspace-status
```

## How it decides what to show

1. **Which agent.** The focused pane's agent (`focused()`), or a sample board while one is on
   (`desk.sample-*` commands).
2. **What kind.** Its `desk.kind` label (`assistant`, `manager`, `coder`, `crew`). Unlabelled, it
   is guessed: a terminal labelled `firstmate.task` is a crew (unless it says `manager`, as a
   secondmate does), a tab named `fm-<id>` is guessed a crew, else by its tab or space name, else a
   coder if it sits in a git folder.
3. **Which board.** Its `desk.file` label: a JSON file on the agent's own machine. The desk reads it
   there through Herdr's connection to that machine (`readTextFile(path, {on: machine})`), every
   1.4 s on this computer and every 4 s elsewhere, and only for the desk on show.
4. **Which look.** Its tags: its `desk.tags` label, plus, on a Ship, the Ship's FleetLedger tags.
   A tag can carry a look (`work` wears Slate ledger, `personal` wears Moss), which changes
   the whole window: its palette and, for a look that has them, the desk's band and the corner's
   logo.

The head part then moves the layout to match, by clicks rather than by rewriting the layout file
(which would throw away the person's dragged sizes): `layout.select` for `desk-body` and
`main-stack`, `layout.resize` for the desk's width (each kind keeps the width last given it), and
`app.theme` for the palette.

## How agents feed it

Each agent owns its terminal's labels and one board file, nothing else. `desk init <kind>` sets the
labels on `$HC_TERMINAL_ID` (`hc label`) and creates the board; `desk summary`, `desk card`,
`desk list`, `desk task`, `desk finding`, `desk next`, `desk note`, `desk link` keep it current.
The CLI writes the board atomically (temporary file, then rename), so the desk never reads half a
file. `mods/desk/AGENTS.md` is the agents' guide, and the schema.

An agent with no board gets a **Set this agent up** section on its desk:

- **Put desk on <machine>** copies the CLI to `~/.local/bin/desk` and the guide to
  `~/.local/share/herdr-desk/AGENTS.md` on that machine through Herdr's connection (it won't
  overwrite some other `desk`).
- **Send to <agent>** types the setup instructions into that agent's prompt, on the person's click;
  **Copy** puts them on the clipboard instead.

A Ship needs nothing else: the board stays on the Ship, its `hc label` reaches Herdr from there, and
Herdr reads the board back over the same connection.

## Where it touches ShipShape

- **Tags:** `ship-fleet ls --json --offline` every five minutes (FleetLedger's last known status,
  no SSH). A space on another machine takes the tags of the Ship of the same name. Without
  FleetLedger, agents have only their own `desk.tags`.
- **Ships as machines:** everything else reaches a Ship only as a Herdr machine (`hc` and
  `readTextFile` with `on`).

## Firstmate: managers, secondmates and crews

firstmate runs supervisors that dispatch crews. Its `herdr-desk` skill (in the firstmate repo) has a
firstmate or secondmate declare itself a manager when `desk` is on its machine and it runs in a
Herdr terminal, mirror its crews on its kanban, and write a `config/herdr-desk` flag; with the flag,
crew briefs tell each crew to `desk init crew --parent <its firstmate>` and keep its summary,
findings and next steps current.

Before a crew writes anything, the crew sheet shows what firstmate itself records, read from
firstmate's home on the crew's machine: `data/<id>/intent.txt` (the ask), `state/<id>.meta` (ship or
scout, branch, mode, worktree) and `state/<id>.status` (its reports), plus the harness's live status
line. The firstmate home is found from the crew's foreground command (`hc get`). **Ask <manager> to
set it up** types the request into the manager's prompt, not the crew's.

**Who reports to whom** (the sidebar's hierarchy, and the crew sheet's "crew of") is decided in
`bossOf()` in `mods/desk/model.js`, in order:

1. the agent's `desk.parent` label (or its board's `crew.parent`), matched on its own machine first;
2. the firstmate home its `firstmate.home` label names: firstmate's home tag ends in the first
   8 hex characters of sha256 of the home's folder, matched against each agent's folder on the same
   machine. This is what nests a secondmate, which has a space of its own, under its firstmate;
3. for a crew, the manager sharing its space (firstmate puts crews in its own space);
4. Herdr's own sub-agent parents (`terminals()[i].parent`).

The desk header publishes the result as shared state `desk.tagIndex`
(`{agents, spaces, parents, crews, names}`), which both sidebar clones read. Crews and secondmates
without tags of their own wear their boss's, and secondmates go by their space's name.

## Shared state

| Key | Written by | What |
|---|---|---|
| `desk.tagIndex` | desk header | tags and colours per agent and space, the hierarchy, crew tasks, names |
| `desk.look` | the Look popup | the person's own look (a preset, parts on top) |
| `desk.tagLooks`, `desk.tagLooksOff` | the Look popup | which tag wears which preset; paused or not |
| `desk.sample` | `desk.sample-*` commands | a sample board in place of the focused agent |
| `sidebar.width`, `sidebar.collapsed` | the brand corner's toggle | the sidebar fold |

## Editor support

Each mod has a `jsconfig.json` for editors. Its types come from `gpui-kit.d.ts`, which Herdr's
gpui-shell writes into each mod's folder every time it runs that mod, so it is not kept here
(`herdr/.gitignore`); nothing loads it at run time. Once the Desk is installed and has run, copy
the generated file next to the repository's copies to get completions while you edit them:

```sh
for m in desk desk-agents desk-spaces; do
  cp "${HERDR_APP_HOME:-$HOME/.config/herdr-app}/mods/$m/gpui-kit.d.ts" "herdr/mods/$m/"
done
```

## Watch out

- Herdr docks a new mod into the layout on screen if it sees `main.js` before a `mod.json` saying
  `"place": "none"`: `install-desk.sh` copies `mod.json` first.
- `layout.set` rewrites the layout file and loses the person's clicks; the desk uses clicks only.
- The crew triangle is the crew terminal block's pane-frame background image, seen through the
  terminal because the root's `terminal_opacity` is below 1. An image on the node itself does not
  show behind a terminal.
- The sidebar clones are copies of Herdr's mods at the version they were cloned from; re-clone and
  re-apply the `Desk:` lines to pick up Herdr's changes.
- The tag looks here are neutral presets with no band or logo. A live install can add presets of
  its own with a band and a logo; keep those out of this public repository (`docs/hygiene.md`).
