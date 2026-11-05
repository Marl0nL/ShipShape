# Overlay providers

FleetLedger reaches Ships off the LAN through an overlay network
([runbooks/remote-access.md](runbooks/remote-access.md)). The overlay sits behind
one small interface, so it can be swapped or extended. Tailscale and Headscale are
built in; NetBird, ZeroTier or plain WireGuard can be added the same way.

Whatever the provider, two things never change:

- **SSH stays the only way in.** The overlay adds a route to sshd. Logins still
  need the pinned host key and your key; no overlay-native login (Tailscale SSH
  and similar) is ever turned on.
- **Join keys never sit anywhere.** A key reaches the Ship only on stdin over SSH,
  after boot (`ship-ctl overlay join`, `contracts/ship-ctl.md`): never in a seed,
  cloud metadata, a command line, a log or the inventory.

## The interface

`fleetledger/fleetledger/overlay/providers.py`, class `OverlayProvider`:

| Method | What it does |
|---|---|
| `keys()` | Where one-off join keys come from: an object with `key(ship) -> str`. |
| `join_args(ship)` | The arguments for `ship-ctl overlay join` (hostname, tags, server). Never the key. |
| `policy()` | The access rules for the Ships, as text: your devices may reach the Ships' tag on port 22, and nothing allows the Ships' tag as a source. |
| `remove(ship, live)` | Take a destroyed Ship's device off the overlay, or return the steps to do it by hand. Must never remove a device it cannot tie to the Ship by identity: for Tailscale, a Ship tag *and* the node id recorded at join (or, for Ships joined before node ids were recorded, the tailnet name recorded at join or the Ship's live overlay address); never the hostname alone. `live` is the Ship's overlay status just before it left, when it could be read. |
| `check(ships)` | Checks of the overlay itself, for `ship-fleet overlay check`. |

Joining, leaving and status are the same for every provider: FleetLedger calls
`ship-ctl overlay join|leave|status` on the Ship and passes the key on stdin.

Providers are listed in `PROVIDERS` in the same file, keyed by
`ships.toml` `[overlay] provider` (`contracts/ships.toml.schema.json`).

## The built-in providers

### Tailscale (`provider = "tailscale"`)

Tailscale's hosted service: nothing to run yourself, no open ports. Keys come from
one of two sources (`key_source`):

- **`prompt`**: you make a single-use, pre-approved, `tag:ship` key in the admin
  console for each Ship and paste it. FleetLedger stores no credential at all.
  Destroying a Ship prints how to remove its device.
- **`tailscale-api`**: FleetLedger signs in with an OAuth client and mints each
  key itself. The client's secret is stored on your computer: `overlay setup`
  puts it in the OS keyring when a usable one is there, else in a 0600 file
  (`overlay.api.secret_store`; absent means `file`).

OAuth scopes, by the names in Tailscale's current "Trust credentials" reference
(checked against it, not from memory; re-check if Tailscale renames them):

Three levels, each adding to the one before; pick one in `ship-fleet overlay
setup`:

| Level | Scopes on the OAuth client | What the stored secret can do | Settings |
|---|---|---|---|
| **1. Keys only** (default) | `auth_keys` | mint single-use, pre-approved `tag:ship` keys that expire in 10 minutes; nothing else | (the defaults) |
| **2. Keys and device removal** | + `devices:core` (Tailscale makes you choose tags, here `tag:ship`) | also remove devices. **This scope is tailnet-wide**: the tags only bind the keys the client makes, not which devices it can see, remove or re-tag, so a leaked secret could remove or re-tag *any* device (Tailscale issue 10702 asks for tag-limited clients). FleetLedger itself only removes a device it can tie to the Ship by its recorded node id (or, for older Ships, its recorded tailnet name or live address) | `api.remove_devices = true` |
| **3. Keys, device removal and the policy** | + `policy_file`, which also needs `devices:core:read` and `devices:posture_attributes` | also read and **change your tailnet's access rules**. FleetLedger only adds the Ships' rules, after showing a diff and asking, and `overlay check` reads the policy itself | `api.remove_devices = true`, `api.manage_policy = true` |

At levels 1 and 2, destroy prints the exact admin-console step where needed, and
`overlay check` checks the access rules from a policy file you export from the
admin console (`--policy FILE`). Level 3 is the most convenient and gives the
secret the most power: treat it like an admin login.

### Headscale (`provider = "headscale"`)

Your own Headscale server (`login_server`, required). The Ship's tailscale client
joins it. Keys are pasted (`headscale preauthkeys create --user <user>
--expiration 1h --tags tag:ship`); FleetLedger has no access to the server, so
removing a device and checking the policy are steps it prints for you. Headscale
reads the same policy format.

## Adding a provider

1. **FleetLedger side.** Subclass `OverlayProvider` in
   `fleetledger/fleetledger/overlay/providers.py` (or a module next to it), add it
   to `PROVIDERS`, and add its name to `overlay.provider` in
   `contracts/ships.toml.schema.json` (a contract change, in its own commit). Its
   `remove()` must only ever remove a device it can tie to the Ship.
2. **Ship side.** `ship-ctl overlay join|leave|status`
   (`ship/lib/shipctl/overlay.py`) drives the tailscale client today. A provider
   with another client needs its own implementation there, chosen by a new
   `--provider` argument to `ship-ctl overlay join`; that argument is a change to
   `contracts/ship-ctl.md`. Keep the same rules: the key from stdin into a
   root-only file under `/run` that is removed straight after, the client
   installed only on join from a pinned, signature-checked source, and nothing
   listening or logging to a third party until the Ship joins.
3. **Status.** The status document's `overlay` object (`ship-status.schema.json`)
   has a `provider` field; add the new name there too.
4. **Tests** must pin the security properties: the key only on stdin, never in
   argv, results, logs or files that outlive the join; no device removed unless
   it is the Ship's; the policy gives Ships no destinations.
5. **Docs**: a section in this file and in the remote-access runbook.
