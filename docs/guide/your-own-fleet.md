# Set up your own fleet

A guide for anyone running ShipShape for themselves: from nothing to Ships you
can reach from anywhere. Each step links to the runbook with the details.

## What you need

- A computer to run FleetLedger on (Linux or macOS with Python 3.12+, pipx and
  OpenSSH). It holds the inventory, your fleet's admin key and any API
  credentials; Ships never depend on it to keep running.
- A hypervisor. Proxmox VE is built in; anything else works with the manual
  driver, which prints the steps.
- Accounts for what your agents use: Claude, and optionally Google Cloud and
  GitHub. Each Ship signs in to them itself; FleetLedger never holds the tokens.
- Optionally, an overlay network for access off your LAN: a Tailscale account,
  or your own Headscale server.

## The steps

1. **Hypervisor (once).** [runbooks/proxmox-setup.md](../runbooks/proxmox-setup.md):
   a pool, a seed storage, a scoped API token and a template. Section 4 adds
   snapshots and baked images, if you want them.
2. **FleetLedger (once).** `fleetledger/install.sh`, then `ship-fleet init`
   ([runbooks/create-ship.md](../runbooks/create-ship.md)). Put your tags and the
   accounts each tag should use in `~/.config/fleetledger/ships.toml`.
3. **A baked image (optional).** `ship-fleet image build`, `image publish`,
   `image use` ([runbooks/images.md](../runbooks/images.md)): new Ships then need
   a few minutes rather than ten.
4. **A Ship.** `ship-fleet new ship-01 --tag work`, then
   `ship-fleet login ship-01 all` ([runbooks/sign-in.md](../runbooks/sign-in.md)).
5. **Your other machines (optional).** `ship-fleet trust laptop`
   ([runbooks/trust-machines.md](../runbooks/trust-machines.md)) gives a machine a
   short-lived certificate to attach to Ships with herdr.
6. **Access off the LAN (optional).** `ship-fleet overlay setup`, then
   `ship-fleet overlay check` ([runbooks/remote-access.md](../runbooks/remote-access.md)).
   The rest of this guide is about the one real choice in it.

## Choosing how Ships get overlay keys

Every Ship joins the overlay with a single-use, pre-approved, tagged key that
expires in minutes, handed over SSH after boot. The choice is who makes those
keys, and how much else a stored credential may do. `ship-fleet overlay setup`
offers these four:

| | **Paste** | **OAuth: keys** (default) | **OAuth: keys + devices** | **OAuth: keys + devices + policy** |
|---|---|---|---|---|
| Making a Ship's key | you, in the admin console, each time | FleetLedger | FleetLedger | FleetLedger |
| Stored on your computer | nothing | an OAuth secret, in your OS keyring (or a 0600 file) | the same | the same |
| Scopes | none | `auth_keys` | + `devices:core` | + `policy_file` |
| If the secret leaks, someone can | (nothing to leak) | mint `tag:ship` keys: add devices that your rules let reach nothing | also remove or re-tag **any** device in your tailnet, your laptop included: `devices:core` is tailnet-wide, its tags only bind keys. Recovery: an admin re-adds or re-tags them in the console | also rewrite your access rules |
| Destroying a Ship | prints the step to remove its device | prints the step | removes its device | removes its device |
| Access rules | you paste them once; `check --policy FILE` checks an export | the same | the same | FleetLedger adds them (diff, then your yes); `check` reads them itself |
| Choose it if | you want no credential stored | you want hands-off keys (the usual choice) | you destroy Ships often and accept the wider scope | you want FleetLedger to keep the rules in place, and will guard the secret like an admin login |

Where the secret lives: `setup` uses your OS keyring when a secure, unlocked one
is there (GNOME Keyring or KWallet on Linux, Keychain on macOS), and a 0600 file
otherwise or if you prefer. The keyring keeps it encrypted at rest; while it is
unlocked, though, any program running as you can read it, agents on this computer
included. It protects the secret on a lost or copied disk, not from your own
processes. If the keyring is locked or gone, FleetLedger says so and how to
unlock it or switch to the file; it never quietly reads a file instead.

Whatever you pick, revoke the client in the admin console if your computer is
lost, and run `ship-fleet overlay check` now and then: it tells you if the
rules drift.

Headscale has one mode: you paste keys made on your server (`headscale
preauthkeys create`), and FleetLedger prints the server-side steps it cannot do.

## The rules that keep Ships contained

Whichever way you go, the tailnet policy needs four things, and `ship-fleet
overlay check` verifies each:

1. `tag:ship` is owned by admins only, so nobody else can make a device look like
   a Ship.
2. Your devices may reach `tag:ship` on port 22.
3. No rule has `tag:ship`, or `*`, as its source. Tailscale's starter policy
   allows everything to reach everything, which would let a Ship reach your
   laptop: replace it.
4. A policy test that a Ship cannot reach another Ship, so a later edit that
   would allow it is refused.

`ship-fleet overlay policy` prints exactly these pieces.

## Other overlays

The overlay sits behind a small interface; NetBird, ZeroTier or WireGuard can be
added ([overlay-providers.md](../overlay-providers.md)).
