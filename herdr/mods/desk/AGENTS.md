# Your desk in Herdr (for agents)

The person you work for uses Herdr's **Desk** layout. Beside your terminal sits a panel, your desk,
that shows what you're doing in a form suited to your kind of work. You keep it current; the person
reads it instead of scrolling back through your output.

You own two things, and nothing else:

1. **Labels on your own terminal**: what kind of agent you are, and your tags.
2. **One board file**: a small JSON file on your own machine, written only by you.

Never edit Herdr's layouts, mods or themes (`~/.config/herdr-app`), or another agent's board.

## Start: say what you are

Run this once, early in a session:

```sh
desk init <kind> --name <board-name> --tags <tag,tag>
```

- **kind**: `assistant` (you don't write code: you plan, arrange and keep track; you get cards,
  checklists and notes), `manager` (you hand work to other agents and track it; you get a kanban),
  `coder` (you change code; you get your plan beside the branch, PR, CI and diff), or `crew`
  (another agent dispatched you to do one task and supervises you, e.g. a firstmate crewmate; you
  get a sheet over your terminal: what you're on, what you've found, what's next). A crew also
  passes `--parent <who sent you>` and `--task <your task id>`.
- **--name**: a lasting board name, so the board survives restarts (`assistant`, `release-lead`).
  Leave it out for a throwaway board named after your terminal.
- **--tags**: what you belong to: `work`, `personal`, a project. Tags group agents, and a tag can
  change the person's whole window (a `work` agent shows in the work look's colours), which helps them
  not to mix agents up. Use the tags you're told; if unsure, ask.

`desk init` writes the labels `desk.kind`, `desk.tags` and `desk.file` on your terminal
(`$HC_TERMINAL_ID`) and creates your board. It works the same on any machine Herdr is connected
to: your board stays on your machine and Herdr reads it from there.

If `desk` isn't on your machine (`command -v desk` finds nothing), ask the person to press
**Put desk on <your machine>** on your desk in Herdr: it copies the tool to `~/.local/bin/desk`
and this guide to `~/.local/share/herdr-desk/AGENTS.md`. It's one Python 3 file with no
dependencies. Until then you can set the labels yourself and write the file as described under
"The board file".

## Keep it current

Update the board whenever something the person would want to glance at changes: you start or
finish a step, you're blocked, you learn something they need to decide. Small, frequent updates
beat a big one at the end. Keep text short: titles of a few words, a sentence or two of body.

```sh
desk summary "Sorting out Thursday's conference trip"       # one line: what you're doing now

# cards (any kind): a fact, a decision, a warning
desk card add flights --title "Flights held" --body "Out Thu 7:05; hold ends 5pm" --tone warn
desk card set flights --tone good --body "Booked, ref ABC123"
desk card rm flights
#   --tone info | good | warn | bad    --link URL    --tags a,b    --top (put a new card first)

# checklists (assistants; a coder's plan)
desk list add Today "Reply to the landlord"
desk list check Today 1            # by number (from 1) or by the item's text
desk list uncheck Today 1
desk list rm Today 2               # one item;  desk list rm Today  removes the list

# tasks (managers: the kanban; coders may list theirs too)
desk task add login-bug --title "Fix login redirect" --status todo --agent fix-login
desk task move login-bug doing --note "fix-login has started"
desk task move login-bug review --note "PR #412, CI green" --link https://...
desk task rm login-bug
#   statuses: todo | doing | review | blocked | done
#   --agent: who is doing it: their tab name, space name, title or terminal (t7). The desk shows
#   that agent's live status on the card, and the person can click through to them.

# crews: what you've found (newest first) and your next steps, in order
desk finding add "Loop only happens with SSO on" --tone warn
desk finding rm 2                  # by number or text;  desk finding clear
desk next set "Write a failing test" "Carry return_to through state" "Open the PR"
desk next done 1                   # or: undo 1 / add "Report to firstmate" / rm 2 / clear

desk note "Prefers window seats. No checked bags."           # replaces the notes ( - reads stdin)
desk link add https://example.com/itinerary --title "Itinerary"
desk tags add project-x      # or: set work,project-x  /  rm personal
desk show                    # what's on the board;  desk show --json
desk clear                   # empty it, keeping kind and tags
```

Blocked on the person? Say so where they'll see it: a `bad` card, or the task in `blocked`
with a note saying exactly what you need from them.

## The board file

`desk` writes it for you. If you write it yourself, write the whole file to a temporary file in
the same folder and rename it over the old one, so Herdr never reads half a file. Default place:
`~/.local/state/herdr-desk/<name>.json`; its path goes in the `desk.file` label.

```json
{
  "version": 1,
  "kind": "manager",
  "title": "Release lead",
  "tags": ["work"],
  "updated": "2026-11-12T09:52:00Z",
  "summary": "Getting 2.4 out the door",
  "cards": [{ "id": "freeze", "title": "Code freeze Fri 3pm", "body": "...", "tone": "warn", "link": null, "tags": [] }],
  "lists": [{ "title": "Today", "items": [{ "text": "Confirm flights", "done": false }] }],
  "tasks": [{ "id": "login-bug", "title": "Fix login redirect", "status": "review", "note": "PR #412", "agent": "fix-login", "link": null, "updated": "..." }],
  "notes": "free text",
  "links": [{ "title": "Itinerary", "url": "https://..." }],
  "crew": { "parent": "firstmate", "task": "fm-fix-login-k3" },
  "findings": [{ "text": "Loop only with SSO on", "tone": "warn", "at": "2026-11-12T09:50:00Z" }],
  "next": [{ "text": "Open the PR", "done": false }]
}
```

Every section is optional; the desk draws what's there. Labels without `desk`:

```sh
hc label "$HC_TERMINAL_ID" desk.kind=assistant desk.tags=personal desk.file="$HOME/.local/state/herdr-desk/assistant.json"
```

## What the person sees

- **Assistant:** your summary, cards (two across when there's room), checklists, notes, links.
- **Manager:** your summary and cards, then To do / Doing / Review / Blocked / Done, each task
  with whoever is on it and their live status.
- **Coder:** your summary, the branch (ahead, behind, changed files), the PR and CI (from `gh`),
  recent commits, your plan and cards; Herdr's Changes panel (the diff) below.
- **Crew:** a sheet over your terminal, which is kept short and marked with a caution sign so the
  person doesn't type into it by mistake: your summary, findings and next steps, and what your
  manager has heard from you; your branch and PR beside it. Keep the summary to what you're doing
  this minute, add a finding when you learn something your manager would want, and rewrite
  `next` when the plan changes. A firstmate crewmate is shown as a crew even before it says so.
