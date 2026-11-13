# Runbook: move a Ship from herdr to herdr-core

How to move one live Ship's agents from herdr's server to herdr-core (`hc`).
Every agent keeps its conversation and its permission mode; if anything goes wrong
the Ship puts itself back on herdr, with its agents resumed there.

**Classic herdr is deprecated.** The next ShipShape release removes it, so move every
Ship still on it now. Such a Ship shows attention `runtime-deprecated` (in `ship-fleet
status`, `ship-fleet update` and the web UI) with the command for it. The switch is
**one-way**: a switch from herdr-core back to herdr is refused, and changes nothing.

The command is `ship-fleet runtime SHIP core` (on the Ship: `sudo ship-ctl
runtime switch core`). What it does, step by step, is in
[`contracts/ship-ctl.md`](../../contracts/ship-ctl.md), "`ship-ctl runtime switch`".

Move one Ship at a time, a trial Ship first.

## 1. Before you start

1. **The Ship is up to date.** It needs tooling with the switch, and herdr-core
   installed (both come with `ship-fleet update ship-01`). For core, Claude Code must
   be 2.1.295 or newer; the switch checks and says so.
2. **Its agents are idle.** Look at `ship-fleet status ship-01`: every agent should
   be `idle` or `done`. The switch refuses an agent that is working, waiting at a
   question or permission prompt, or in a state the runtime cannot tell, and says
   which. `--force` moves it anyway, interrupting it with Escape (its turn ends, or
   its question is dismissed), as `ship-fleet agent restart --force` does.
3. **Nothing else runs in herdr** (moving to core): stopping herdr's server ends
   every pane, so a shell someone left open in a herdr pane closes, and a non-Claude
   agent there makes the switch refuse. Claude agents started by hand are fine: the
   Ship records them, and they move too.
4. **No agent runs in bypassPermissions** where the Ship no longer allows it
   (`allow_bypass = false` set after it started): it could only come back in
   another mode, so the switch refuses until you restart it in a mode of your
   choice.

## 2. Switch

```sh
ship-fleet runtime ship-01 core
```

It takes about a minute per agent. You see, for example:

```
ship-01: moved from herdr to core; 2/2 agents running in their modes
```

What happened on the Ship:

- each agent was stopped gracefully (`/exit`) and its session's mode pinned;
- herdr's workspaces for those agents were closed, then `herdr.service` stopped and
  its boot link removed; ship.toml says `runtime = "core"`; `ship-core.service` was
  enabled and started;
- each agent was started on core with `--resume <its session>` in its mode, in a
  terminal labelled for the herdr desktop app (space `shipshape`, a tab per agent);
- the Ship checked each one is back exactly once, in its own conversation and mode.
  FleetLedger then checks the same from the status document.

Remote Control comes back by itself: the switch does not change the managed
settings, and each resumed session reconnects to its own Remote Control session.
Check one in the Claude app if you rely on it.

The status document records the switch (`agent_record.last_switch`), and
`ship-fleet status ship-01 --json` shows it.

## 3. On each laptop

A laptop that attaches to the Ship with herdr's TUI (`herdr --remote ship-01`) keeps
a herdr machine entry for it, and connecting with it would start a herdr server on
the Ship again. Replace that entry:

1. On the FleetLedger computer, make the machine's bundle again:
   `ship-fleet trust laptop` (it reuses the recorded key; see
   [trust-machines.md](trust-machines.md)).
2. Copy it to the laptop and run its `install.sh`, as when you first trusted it.

Until you do, do not open the herdr TUI against that Ship. Its own resume is off and
the agents' workspaces are closed, so it would only start an empty server, but it is
noise. Attach to core agents with the herdr desktop app, or with `hc attach
ship-01/main` (`hc` alone for the picker) once the laptop knows the Ship as an `hc`
machine.

## 4. If it fails

The Ship rolls back by itself and says why:

```
ship-01: the switch to core failed and was rolled back to herdr; 2/2 agents running in their modes
  the switch to core failed and was rolled back to herdr: main did not start on core: ...
```

Every agent is back on the old runtime, in its own conversation and mode; ship.toml
and the boot unit are as they were. Fix what it names (often: update Claude Code or
the Ship's tooling) and run the switch again.

**`the rollback did not finish cleanly`** (attention `runtime-switch-failed`): the
message says what is left. Two cases:

- **An agent is still on the new runtime** (it would not exit there, or its start
  there had not come up yet). It is not started on the old runtime, so it never runs
  twice, and the Ship keeps it out of the old runtime until the next boot. Attach to
  it and exit it (`/exit`), then reboot the Ship: the boot starts only the runtime
  ship.toml names and resumes every recorded agent on it, in its mode.
- **The old runtime did not start again** (its unit failed). Fix what the message
  names, then switch to the runtime ship.toml names:
  `ship-fleet runtime ship-01 herdr` (if it was going back to herdr: allowed because
  the Ship is still on herdr). That starts it, puts the boot units right and resumes
  the agents waiting for it (outcome `repaired`).

**Attention `runtime-switch-unfinished`**: ship-ctl was stopped part way (the Ship
lost power, say). Switch to the runtime ship.toml names (the attention message names
it): that starts it if it is not running, puts the boot units right, and resumes the
agents the switch had stopped, in their modes (outcome `repaired`); it records the
unfinished switch as `interrupted`. Then, if you still want to move the Ship, run the
switch again.

## 5. No way back

Once a Ship runs herdr-core it stays there: `ship-fleet runtime ship-01 herdr` (and
`ship-ctl runtime switch herdr` on the Ship) is refused, and nothing on the Ship
changes:

```
ship-01: not switched: classic herdr is deprecated and the next ShipShape release removes it, so the switch to herdr-core is one-way; nothing changed
```

If something does not work on herdr-core, report it rather than moving back.

The one exception is a switch from herdr that did not finish: ship-ctl was stopped part
way, or its rollback failed before ship.toml went back to herdr. Then
`ship-fleet runtime ship-01 herdr` is allowed, and finishes the way back.

## Do not

- Edit ship.toml's `runtime` by hand on a live Ship. The Ship then starts no agent
  that ran under the other runtime until the next boot (so none starts twice), and
  the switch refuses until ship.toml is put back. Use the switch.
- Move several Ships at once. One at a time, and look at each one after.
