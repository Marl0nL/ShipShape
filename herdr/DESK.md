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
| `mods/desk/` | the desk itself, in parts (`props.part`): `head`, `assistant`, `manager`, `coder`, `crew`, `none`, plus the window's `bar` and `brand` corner, and the `dashboard` tab |
| `mods/desk/bin/desk` | the agents' CLI (one Python 3 file, no dependencies), linked as `~/.local/bin/desk` |
| `mods/desk-spaces/`, `mods/desk-agents/` | clones of Herdr's shipped `spaces` and `agents` sidebar mods, changed where marked `Desk:` (tag colours; the agents list's hierarchy view) |
| `mods/desk/links.js` | task cards' branch, PR and Diff menu, and the checks on what a board names before the desk shows or opens it |
| `mods/desk/dash.js`, `dashboard.js` | the Dashboard: its logic (no gpui or herdr imports, tested in `tests/desk/`) and its view |
| `mods/desk/lookfolder.js` | your own looks, from a folder of data in Herdr's folder (`desk-looks/`, below) |
| `desk-looks.example/` | a placeholder looks folder to start from (nothing installs it) |
| `mods/desk/shiptags.js` | each Ship's FleetLedger tags (below); `fleet-model.js` beside it is a link to the Fleet panel's `mods/fleet/model.js`, whose checks it reads with |
| `install-desk.sh` | copies all of it into Herdr's folder (with the Fleet panel, `install-fleet.sh`) and links the CLI |

```
root (row; terminal_opacity 0.88; link_handlers: fleet, mods/fleet/README.md)
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
   logo. Your looks folder (`desk-looks/`) adds presets and palettes and sets tag looks of its
   own, over the popup's.

The head part then moves the layout to match, by clicks rather than by rewriting the layout file
(which would throw away the person's dragged sizes): `layout.select` for `desk-body` and
`main-stack`, `layout.resize` for the desk's width (each kind keeps the width last given it), and
`app.theme` for the palette.

## The Dashboard

The bar's **Dashboard** button (or `desk.dashboard`) opens one tab, or selects it when it is open,
in a space on this computer so it stays when a Ship goes. It lists every agent on every machine
Herdr reaches, from Herdr's live state alone (`agents()`, `spaces()`, `panes()`, `terminals()`,
`machines()` and `desk.tagIndex`): no file is read and nothing runs for it.

- **Needs you:** blocked agents, the longest blocked first, then those done while nobody looked.
- **In progress** and **Idle** (folded): by machine, this computer first, each agent nested under
  the one it reports to (or "of <manager>" when that one is in another section). A row shows where
  it runs, how long it has been in its state (its tab's `working_since` or `changed_at`: Herdr
  times tabs, not agents, so two agents sharing a tab share a clock), what it reports (`terminals()`'s `report`, when that carries a
  message) and its branch (`panes()`).
- **Recent:** status changes this Herdr has seen since it started, in memory only (50 kept, the
  latest 15 shown with the boards' findings and crews' reports).
- **Machines:** each machine's agents, or "unreachable since 10:05" when `machines()` says so.
  That machine's agents stay, greyed and dated (the last ones seen when Herdr drops them), and
  their Go and Peek are off.
- Filter chips by tag and machine. **Go** focuses the agent (`pane.focus`); **Peek** shows its
  terminal over the Dashboard (`terminal.peek`), and warns before you peek at a crew.

What agents keep comes on top, and only while the Dashboard is on show:

- **Boards:** one `sh` per machine every 15 s, through Herdr's connection (`exec` with `on`), that
  prints every board its agents' `desk.file` labels name (else the desk's default place), at
  most 60 a machine and 256 KiB each. For a firstmate crew with no board yet, the same batch
  prints firstmate's `state/<id>.meta` (kind, branch, worktree) and the last lines of its
  `state/<id>.status`. Each path and id is checked first and goes to `sh` only as an argument; a
  marker made for each read separates the files, so a board cannot pass for the next one. A
  machine `machines()` does not report `ready` is not asked, so reads never pile up against one
  that is down. Rows then show each board's summary and next-steps tally; a manager's blocked
  tasks go in Needs you (even while the manager is idle); findings and crews' reports go in
  Recent.
- **The fleet:** the Fleet panel publishes `fleet.summary` after each of its reads. The Dashboard
  shows "Fleet: 2 need you ›" (it opens the Fleet tab) and, for a Ship Herdr cannot reach,
  FleetLedger's last word on its agents, dated ("FleetLedger (8m ago): 2 working, 1 blocked"),
  with **Open in Fleet ›**. It asks FleetLedger nothing itself.

It redraws every three seconds while on show and does nothing while hidden: no read, no redraw.

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

- **Tags:** a space on another machine takes the tags of the Ship of the same name, from
  `~/.ssh/shipshape/fleet.json`, which a trusted machine's self-refresh keeps current
  (`contracts/machine-bundle.schema.json`; read every 30 s, so a change shows within that). A
  computer with no `fleet.json` that runs FleetLedger itself is asked instead,
  `ship-fleet ls --json --offline` every five minutes (FleetLedger's last known status, no SSH).
  Both are checked with the Fleet panel's own reader: a malformed `fleet.json` is ignored, a
  Ship with a name outside `[a-z0-9-]` is left out, and so is a tag outside the contract's
  pattern. When an agent is on another machine and there are no Ship tags, the head says so in a
  quiet line ("No Ship tags"; the reason in its tooltip); "Some Ship tags left out" when the local
  `ship-fleet` answered but `fleet.json` was ignored or some of its entries were refused (an older
  or newer FleetLedger).
  Agents on this computer have only their own `desk.tags`.
- **The Fleet panel:** the top bar has a **Fleet** button when `mods/fleet` is installed; it opens
  the Fleet tab, or selects it if one is open (`herdr/mods/fleet/README.md`).
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
| `desk.lookFolder` | the desk (head and Look popup) | your looks folder as checked (`{looks, problem}`); each part checks it again |
| `desk.sample` | `desk.sample-*` commands | a sample board in place of the focused agent |
| `fleet.summary` | the Fleet panel, after each read | the Ships as FleetLedger last saw them, their listed agents, how many items need you: read by the Dashboard |
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
- The tag looks here are neutral presets with no band or logo. Your own, with a band and a logo,
  go in `~/.config/herdr-app/desk-looks/` (`mods/desk/README.md`, "Your own looks";
  `contracts/desk-looks.md`), outside the repository; keep them out of it (`docs/hygiene.md`).
