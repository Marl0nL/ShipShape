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
expires in minutes, handed over SSH after boot. The choice is who makes those keys.

| | **Pasted keys** | **OAuth client, default level** | **OAuth client, manage level** |
|---|---|---|---|
| Making a Ship's key | you, in the admin console, each time | FleetLedger, automatically | FleetLedger, automatically |
| Stored on your computer | nothing | an OAuth secret (0600) | an OAuth secret (0600) |
| What that secret can do | (none) | mint `tag:ship` keys and read the policy; with device removal on (`devices:core`), also remove **any** device in the tailnet | the same, and change the policy |
| Access rules | you paste them once; `check --policy` verifies an export | you paste them once; `check` verifies them | FleetLedger adds them (diff, then your yes); `check` verifies them |
| Destroying a Ship | prints how to remove its device | removes its device (or prints the steps, if you turned device removal off) | the same |
| Choose it if | you would rather store no credential, or have few Ships | you want it hands-off (the usual choice) | you also want FleetLedger to keep the rules in place |

How to think about the risk: a stolen pasted key is worth nothing after a few
minutes. A stolen default-level secret lets someone add `tag:ship` devices (which
the rules let *your* devices reach, but which cannot reach anything). If device
removal is on, it can also remove devices, and not only Ships: Tailscale's
`devices:core` scope covers the whole tailnet (the tags a client must have only
bind the keys it makes). That is disruptive, not a way into your machines; if you
would rather it could not happen, answer no to device removal in `overlay setup`
(`api.remove_devices = false`) and leave that scope out. A stolen manage-level
secret can rewrite your access rules, so it is as sensitive as an admin login.
Whatever you pick, revoke the client in the admin console if your computer is
lost, and `ship-fleet overlay check` will tell you if the rules drift.

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
