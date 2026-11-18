# Desk

A Herdr layout (`layouts/desk.json`, a clone of the default `main`) with one addition: a **desk**
on the right that follows the agent you focus and shows what that agent keeps for you.

| Agent kind | Its desk |
|---|---|
| Assistant | summary, cards, checklists, notes, links |
| Manager | a kanban of the tasks it hands out, each with the live status of the agent doing it (Peek shows that agent over the board), its branch and PR, and a Diff menu |
| Coder | branch, PR and CI, recent commits, its plan; Herdr's Changes panel below |
| Crew | a sheet over its terminal (what it's on, what it found, what's next, what its manager heard); its branch and PR on the desk |
| Untyped | buttons to set its kind, and sample boards to preview |

Each kind keeps the width you last gave it (a kanban wants room). `ctrl-shift-d` shows or hides
the desk.

Around it, the parts of `main` that said little are replaced:

- **The top bar** (the desk mod's `bar` part, instead of Herdr's tab strip) says where you are:
  `space › agent`, named by the agent's tab or session title rather than the program, with its tag
  colour. Tabs appear only when a space has more than one, named by what's in them. When any
  agent on any machine is blocked, **N need you** says how many: a click goes to the one blocked
  longest, the next click to the next (`desk.next-blocked` from a key; none on a machine Herdr
  cannot reach). A **Dashboard** button opens the Dashboard tab (below), and a **Fleet** button the Fleet panel's
  tab when that mod is installed (`install-desk.sh` installs it).
  New, Files, Changes and the window buttons are as before.
- **The sidebar's lists** are clones of Herdr's (`mods/desk-spaces`, `mods/desk-agents`). Both
  colour by tag: a tagged space's avatar takes its tag's colour, and each tagged agent gets a bar
  of it (a tag whose look has a band uses the band's colour). The agents list
  also has a **Who reports to whom** view, its default (the sort button switches to Priority or
  Grouped): each agent nests under the one supervising it, so a firstmate shows its secondmates
  and crews beneath it with tree lines, crews marked with the amber caution sign and named by
  task. "Supervising" is an agent's `desk.parent` label; else, for a firstmate crewmate or
  secondmate, the firstmate home its `firstmate.home` label names (the tag ends in a hash of that
  home's folder, matched against each agent's folder on the same machine); else the manager in its
  space; plus Herdr's own sub-agent parents. Secondmates go by their space's name and, like crews,
  wear their boss's tags. All of it comes from shared
  `desk.tagIndex` (`agents`, `spaces`, `parents`, `crews`), which the desk header publishes.
  Crews without tags of their own wear their boss's. The clones don't follow Herdr's updates to
  the originals; re-clone and re-apply the marked `Desk:` lines if you want them.
- **The corner** shows the look's brand (below).
- **Pane headers** show on hover only; the coder view hides Herdr's Changes panel outside git.

## Task cards: right now, Peek, branch, PR and Diff

Under a card's note, a live line says how long its agent has been in its state ("working 41m",
from its tab's `working_since` or `changed_at`) and, when Herdr's report for its terminal
carries one, what it says right now. A task in Doing whose agent has been idle (or done) for
ten minutes is marked **stale**; when Herdr has seen no change since it started, the card's own
`updated` decides. All of it is Herdr's live state: nothing is read for it.

Beside the agent on a card, **Peek** shows that agent's terminal over the board
(`terminal.peek`): read it or answer it, close the peek, and you are still on the kanban. For a
crew the button is amber and its tooltip warns that what you type steers the crew directly;
while the agent's machine is unreachable it is off and says so.

A manager's task card shows the work's branch and PR, taken from the first that answers: the
task's own `branch`, `pr`, `folder` and `base` (`desk task set ID --branch ... --pr ...`); the
live terminal of the agent on it (Herdr's `panes()`: its branch and folder); a PR address
(`/pull/N`, `/merge_requests/N`) in its link or note. **Diff ▾** offers **Open PR #N (files
changed)** in the browser and **Go to <agent> + Changes** (focus it, open Herdr's Changes
panel); each says what it does, or why it is off ("No PR yet", "<agent> isn't running here",
"ship-01 is unreachable"). A board is the agent's to write, so these are checked before they
are shown or opened (`links.js`): a branch must look like a git ref, a folder must be absolute
or `~/`, and only `https://` addresses open (a card's or task's `link` too: another scheme is
shown but not opened). A field that fails is left out, with a `?` saying so.

## Dashboard

One tab with every agent on every machine Herdr reaches: **Needs you** (blocked, the longest
first; then done), **In progress** and **Idle** by machine with crews nested under their
manager, **Recent** (status changes since Herdr started) and **Machines** (an unreachable one's
agents stay, greyed, with Go and Peek off). Each row says where the agent runs, how long it has
been in its state, what it reports and its branch; **Go** goes to it, **Peek** shows it over the
Dashboard. Filter by tag or machine.

While it is on show, and only then, it also reads every agent's board (one batch per machine
every 15 s, never from a machine Herdr does not report ready): rows get each board's summary and
next-steps tally, a manager's blocked tasks join Needs you, and findings join Recent. From the
Fleet panel's last read (shared `fleet.summary`) it shows "Fleet: N need you ›" and, for a Ship
Herdr cannot reach, FleetLedger's last word on its agents. `../../DESK.md`, "The Dashboard",
has the details.

## Crews

A crew is a worker another agent dispatched and supervises: a firstmate crewmate, say. Focus one
and the main area changes (the `main-stack` stack switches to `crew`): the crew's sheet fills the
top, and its terminal shrinks to a strip at the bottom (`crew-term`, 300 px; drag to resize) with
an amber edge, a faint caution triangle behind the text, and a strip saying whose terminal it is
and who supervises it. The point is that you talk to the manager, not the crew.

- A terminal labelled `firstmate.task` (firstmate's hc backend sets it) is a crew whatever it says,
  unless it labels itself `manager` (a secondmate). A tab named `fm-<id>` is guessed as one.
- Before the crew writes a board, the sheet shows what firstmate knows, read from firstmate's home
  on the crew's machine: the brief's intent (`data/<id>/intent.txt`), how it was sent
  (`state/<id>.meta`: ship or scout, branch, mode, worktree) and its reports
  (`state/<id>.status`), plus the harness's live status line. **Ask <manager> to set it up**
  types the request into the manager's prompt rather than the crew's.
- "crew of" is its `desk.parent` label or board's `crew.parent`, else the manager in its space.
- `https://` addresses in its findings and its reports are links (a PR shows as `⇄ #418`; the
  full address is the tooltip), as they are in a task's note and a board's notes. Nothing but
  `https://` opens.
- The triangle needs the layout's `terminal_opacity` below 1 (0.88): only the crew pane's frame
  has an image behind it, so other terminals look the same.

## How agents feed it

Each agent owns its labels and one board file; nothing writes the layout or this mod at run time.

- `desk.kind`: `assistant`, `manager` or `coder` (unlabelled agents are guessed from their tab or
  space name, else as a coder if they're in a git folder).
- `desk.tags`: comma-separated (`work,project-x`). Agents on a ShipShape Ship also get the Ship's
  FleetLedger tags, from this computer's `~/.ssh/shipshape/fleet.json` (kept by its self-refresh),
  else from a local `ship-fleet`; when neither has them the header says "No Ship tags", and why
  (`../../DESK.md`, "Where it touches ShipShape").
- `desk.file`: where its board is, on the agent's own machine.

`bin/desk` (linked as `~/.local/bin/desk`) sets these and writes the board; `AGENTS.md` is the
agents' guide. Boards on this computer refresh every 1.4 s, on other machines every 4 s, and only
for the desk on show.

An agent with no board gets a **Set this agent up** section on its desk. It says where the
agent runs, and for an agent on another machine (a Ship, say), **Put desk on <machine>** copies the
tool to `~/.local/bin/desk` and the guide to `~/.local/share/herdr-desk/AGENTS.md` there
(through Herdr's own connection; it won't overwrite some other `desk`). Then **Send to <agent>**
types the instructions into its prompt, or **Copy** puts them on the clipboard. The board stays on
the agent's machine, its `hc label` reaches Herdr from there, and Herdr reads the board back over
the same connection, so a VM agent needs nothing else.

You can also set an agent's kind and tags from the desk: click its kind in the header.

## Looks

The settings button on the desk's header opens the Look popup:

- **Presets:** Graphite, Slate ledger, Ember index, Linen, Moss.
- **Parts, mixed freely:** dark palette, light palette, finish (Soft, Outline, Ledger, Index cards,
  Glow), type, density. **Appearance** flips Herdr between dark and light (settings.json), so
  you can see the light palettes.
- **Looks by tag:** a tag can carry a preset. `work` wears Slate ledger and `personal` wears
  Moss, so the whole window changes the moment you focus one: palette, and, for a preset that
  has them, desk band and the window's top-left corner (logo and name). Pause tag looks to
  review your own.

The corner is the desk mod's `brand` part, standing in for Herdr's `workspace-chrome` (search and
the sidebar toggle work as before). A preset's `brand` in `looks.js` sets its logo, size and name;
presets without one show Herdr's. None of the presets here has one: add your own preset with a
`band` and a `brand` (a logo of yours in `brand/`) in your installed copy. Urbanist (SIL OFL,
`fonts/Urbanist-OFL.txt`) is in `fonts/`.

Palettes are Herdr themes (`themes/desk-slate.json`, `desk-ember`, `desk-linen`, plus Herdr's
own). They go on through Herdr's theme pick, never the layout file,
so switching agents never disturbs your sizes. Fonts change only the desk, unless you press "Use
… for the whole window". Presets, parts and default tag looks are in `looks.js`.

Commands (keys.json or `herdr-app do`): `desk.sample-assistant`, `desk.sample-manager`,
`desk.sample-coder`, `desk.sample-crew`, `desk.sample-off`, `desk.look-<preset>`, `desk.taglooks`, `desk.toggle`, `desk.looks`,
`desk.dashboard` (the Dashboard tab, or a new one), `desk.next-blocked` (go to the next blocked agent).

## Files

| File | |
|---|---|
| `main.js` | the header and controller (view, width, look), the Kind and Look popups |
| `views.js` | assistant, manager (kanban), coder, crew sheet and untyped bodies |
| `links.js` | a task's branch and PR, the Diff menu, and the checks on what a board names (no gpui or herdr imports) |
| `dashboard.js`, `dash.js` | the Dashboard tab: its view, and its logic (no gpui or herdr imports; tested in `herdr/tests/desk/`) |
| `model.js` | the focused agent, its kind, tags and board (read from its own machine) |
| `looks.js` | palettes, finishes, type, density, presets, tag looks |
| `ui.js` | cards, chips, labels and buttons, styled by the look |
| `bin/desk` | the agents' CLI |
| `samples/` | the preview boards |
| `brand/`, `fonts/`, `icons/` | the crew triangle (and any logos you add); Urbanist; the caution icon |

## Watch out

- Herdr docks a new mod into the layout on screen if it sees `main.js` before a `mod.json` saying
  `"place": "none"`. When copying this mod somewhere new, put `mod.json` in first.
- Don't use `layout.set` for anything that changes per agent: rewriting the layout file throws
  away your dragged sizes and stack choices (a file edit beats clicks).
- Herdr's own theme menu also sets the theme pick; the desk puts its look back on the next agent
  switch.
