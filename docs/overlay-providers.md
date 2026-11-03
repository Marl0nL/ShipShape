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
| `remove(ship)` | Take a destroyed Ship's device off the overlay, or return the steps to do it by hand. Must never remove a device it cannot tie to the Ship (for Tailscale: the Ship's tag *and* its name). |
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
  key itself. The client's secret is stored on your computer (0600).

OAuth scopes, by the names in Tailscale's current "Trust credentials" reference
(checked against it, not from memory; re-check if Tailscale renames them):

| Level | Scopes | What FleetLedger does with them |
|---|---|---|
| Default | `auth_keys` | mint one single-use, pre-approved, tagged key per join (10-minute expiry by default) |
| | `devices:core`, with the tag `tag:ship` | find a destroyed Ship's device and remove it; a credential with this scope must have tags, and only reaches devices with them |
| | `policy_file:read`, which also needs `devices:core:read` and `devices:posture_attributes:read` | read and validate the policy, for `overlay check` |
| Opt-in (`api.manage_policy = true`) | `policy_file`, which also needs `devices:posture_attributes` | merge the Ships' rules into the policy, after showing a diff and asking |

The opt-in level means **the stored secret can change your tailnet's access
rules**. Leave it off unless you want FleetLedger to keep the rules in place for
you; at the default level you paste them once and `overlay check` verifies they
are still right.

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
