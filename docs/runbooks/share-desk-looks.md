# Runbook: one Desk look on all your computers

Your Desk looks (palettes, presets, tag looks and logos: the `desk-looks` folder of
`contracts/desk-looks.md`) can live on one computer, be kept by FleetLedger, and be written by
the refresher of each computer you opt in, so the Desk looks the same on all of them. Nothing
is sent to a computer you have not opted in, and nothing in the folder is ever run.

```
desktop                      FleetLedger                       laptop (opted in)
desk-looks/  --publish-->    looks/published.json  --bundle--> refresher --> desk-looks/
             (your shell)    (checked, kept)       (its key)   (checked)     (Herdr's folder)
```

## Before you start

- FleetLedger is updated to a version with `ship-fleet trust NAME --looks`.
- The computer that should get the looks is a trusted machine that refreshes itself: it has
  `~/.ssh/shipshape/refresh` ([trust-machines.md](trust-machines.md), "A machine that
  refreshes itself"). A computer with only a bundle installed by hand gets nothing.
- Herdr is installed there, so its folder exists (`~/.config/herdr-app`, or
  `$HERDR_APP_HOME` if you set that for the refresher too). The refresher never makes it.

## 1. Publish the looks, from the computer that has them

On the FleetLedger computer, or from a computer whose own key has a shell there:

```sh
ship-fleet looks publish ~/.config/herdr-app/desk-looks
COPYFILE_DISABLE=1 tar -cf - -C ~/.config/herdr-app/desk-looks . | ssh fleetledger@HOST ship-fleet looks publish -
ship-fleet looks show        # what is kept, and which machines it is sent to
```

(`COPYFILE_DISABLE=1` keeps macOS's `tar` from adding `._` files, which are refused.) A
folder with anything out of place is refused whole, saying why; `contracts/ship-fleet.md`,
"ship-fleet looks", has the rules.

## 2. On the other computer: refresher 2 first

```sh
~/.ssh/shipshape/refresh --version     # must say: ShipShape refresher 2 (or later)
```

If it says 1, or nothing, put the new refresher in place first: run `shipshape-setup` there
again (from a ShipShape checkout of this version), or paste the line `ship-fleet trust NAME`
prints on FleetLedger's VM again. Both install the refresher FleetLedger serves, after
checking its SHA-256; the refresher never replaces itself.

**Why first.** Every refresher refuses a bundle with a field it does not know, and refresher
1 does not know `looks`. FleetLedger cannot tell which refresher a computer runs (a refresh
asks for its bundle and nothing more), so it is you who check. If you opt in a computer that
still runs refresher 1, every refresh there fails and says, on stderr, in
`~/.ssh/shipshape/state.json` (`last_error`) and in the Fleet panel's header:

```
FleetLedger's bundle has a value out of place (an unknown field, looks); refused, and the files here are unchanged.
```

Its SSH config stays as it was, but its certificate is no longer renewed, so once it lapses
(7 days by default) that computer cannot attach. To mend it, update its refresher as above,
or turn the looks off for it (`ship-fleet trust NAME --looks off`).

## 3. Opt the computer in, on FleetLedger

```sh
ship-fleet trust laptop --looks on
```

It signs nothing and changes only `ships.toml`'s record of the machine (`looks = true`). It says
what to check on the laptop (step 2), and that **the laptop's restricted refresh key on
FleetLedger then also reads the fleet's looks**. That key still runs only
`ship-fleet machine-bundle laptop`, and still reads nothing it is sent, but its answer now
carries every file of the folder: palettes, presets and their labels, tag names, and logos.
Opt in only computers you would hand those to. `ship-fleet ca show` then says the laptop
"gets the fleet's looks", and `ship-fleet looks show` lists it under "sent to".

## 4. What you see on that computer

At its next refresh (its hourly timer, at login, the Fleet panel's refresh, or
`~/.ssh/shipshape/refresh` by hand):

```
Wrote the fleet's looks to ~/.config/herdr-app/desk-looks (the Desk wears them when its Look popup opens, or at herdr-app do desk.looks-reload).
```

The Desk reads the folder when it starts, when the Look popup opens, or on
`herdr-app do desk.looks-reload` (never on a timer), and wears the presets and tag looks as on
the first computer. A refresh with the same looks writes nothing.

## Changing, clearing and stopping

| You do, on FleetLedger | Each opted-in computer, at its next refresh |
|---|---|
| `ship-fleet looks publish` again, changed | replaces the folder it wrote with the new one |
| `ship-fleet looks clear` | removes the folder it wrote (`Removed the fleet's looks from ...`) |
| `ship-fleet trust laptop --looks off` | nothing: its bundle is as before, and the looks it has stay; delete `desk-looks` there by hand if you want them gone |
| (a kept folder FleetLedger can no longer use: `looks show` says the problem) | nothing: no looks are sent, the rest of the refresh works, and FleetLedger's audit line for each `machine-bundle` says `looks not sent: <why>` |

To change the looks, change them on the computer you publish from and publish again. A
computer whose fleet folder you edit keeps your edit, and gets no more of the fleet's (below).

## Your own looks folder is never touched

The refresher keeps the digest of each folder it writes in `~/.ssh/shipshape/looks-applied`.
Before it replaces or removes `desk-looks`, it computes the digest of what is there now (over
each file's name, size and bytes, as FleetLedger computes it) and goes on only when that is a
digest it wrote. So a `desk-looks` folder you made yourself, a link, or the fleet's folder
after you changed a byte or added a file is yours from then on: never replaced and never
removed. When FleetLedger sends looks, the refresher says so instead:

```
The fleet's looks were not written: ~/.config/herdr-app/desk-looks is a folder of your own (move it away to receive them).
```

(on stderr and in `state.json`'s `warning`; the refresh itself succeeds). To take the fleet's,
move yours aside (`mv ~/.config/herdr-app/desk-looks ~/desk-looks.mine`) and refresh.

## What the refresher checks before it writes anything

The bundle is refused whole, as for any other value out of place, when its `looks` has:

- a field it does not know; a file name that is not `looks.json`, `palettes/looks-<id>.json`
  or `logos/<name>.png|.jpg` (ids and names a-z, 0-9 and `-`); a name twice; no `looks.json`;
- a file over its limit (`looks.json` and each logo 64 KiB, each palette 16 KiB), more than 16
  palettes or 16 logos, or more than 512 KiB in all;
- data that is not base64 of exactly the size it says; a logo whose first bytes are not the
  picture its name says (PNG or JPEG); a JSON file the refresher's strict reader refuses, or a
  `looks.json` that is not `shipshape.desk-looks/1`;
- files that do not match the folder's digest.

The folder is then decoded in the refresher's own folder, copied into a new folder beside
`desk-looks` and checked again by its digest, and put in place by rename, so the Desk never
reads half a folder. Each file goes under its checked name; nothing else from the bundle
becomes a path, and nothing is run. `fleet.json` (the Fleet panel's copy of the bundle) never
holds the looks. A computer without `base64`, `sha256sum` (or `shasum`) or `od` leaves its
looks as they are and says so.
