# Runbook: reach Ships from outside your LAN

Attach herdr to a Ship from your laptop on a phone hotspot, with no router ports
forwarded. Ships and your own devices join an overlay network (Tailscale, or a
Headscale server you run), and an access rule lets your devices reach the Ships
on port 22 and nothing else.

What does not change: SSH with the Ship's pinned host key is still the only way
in, as `agent` with your key, as your own login (`ship-fleet owner`), or as
`fleet` with FleetLedger's. The overlay adds a
route, not a new way to log in. Tailscale SSH stays off.

## How keys are handled

Each Ship joins with a **one-off auth key**: single use, pre-approved, tagged
`tag:ship`, expiring in minutes. FleetLedger gets it after the Ship has booted,
either from you (pasted, not shown) or minted through Tailscale's API, and hands
it to the Ship over SSH on stdin. On the Ship it exists only in a root-only file
under `/run` while `tailscale up` runs. Keys never go into seeds, cloud metadata,
command lines, logs, `ships.toml`, the audit log or the status document.

## The quick way: the setup page, or `ship-fleet overlay setup`

`ship-fleet setup`'s **Reach Ships from anywhere** step does this for Tailscale
with an OAuth client ([web-ui.md](../web-ui.md#first-run-the-setup-page)). For
pasted keys or Headscale, or to choose everything in a terminal:

```sh
ship-fleet overlay setup    # asks: provider, key source, OAuth client and level
ship-fleet overlay check    # verifies it, now and whenever you like
```

`setup` asks for the provider (Tailscale, or your own Headscale), the key
source (keys you paste, or an OAuth client) and, for OAuth, how much the stored
secret may do: `keys` (the default), `devices` or `policy`
([the choice](../guide/your-own-fleet.md#choosing-how-ships-get-overlay-keys);
scopes in [overlay-providers.md](../overlay-providers.md)). It checks the
client's scopes with Tailscale before saving anything, writes `[overlay]` to
`ships.toml` and the secret to your OS keyring (or a 0600 file), and prints the
access rules to add to your policy unless it adds them itself. `check` verifies
the setup, the policy, the tailnet's Ship devices against the inventory (from
the `devices` level) and each Ship's overlay; `--probe` also tries, from each
Ship, to open a connection to this computer and to another Ship, which must
fail ([5. Check it](#5-check-it-acceptance)).

The steps below are what `setup` does, by hand, and the laptop side.
[../guide/your-own-fleet.md](../guide/your-own-fleet.md) explains the choices.

## 1. The tailnet (once)

In the Tailscale admin console (or on your Headscale server):

1. **Access rules.** Print the template and paste it into the access controls
   (policy file):

   ```sh
   ship-fleet overlay policy
   ```

   It allows exactly one thing: your devices (`autogroup:member`) to Ships
   (`tag:ship`) on port 22. Nothing allows `tag:ship` as a source, so **a Ship
   can open no connection to your laptop or to another Ship**. The `tests` block
   makes the console refuse a later edit that would let Ships reach each other.
   If others share your tailnet, replace `autogroup:member` with your own user or
   a group.
2. **Tag owner.** The template makes `tag:ship` owned by `autogroup:admin`, so
   only admins can mint keys for it.
3. **Key source**, one of:
   - **Paste (default).** For each join, make a key under Settings > Keys >
     Generate auth key: *not* reusable, *not* ephemeral (a Ship that reboots must
     stay), *pre-approved*, tags `tag:ship`, expiry 1 hour or less. Headscale:
     `headscale preauthkeys create --user <user> --expiration 1h --tags tag:ship`.
   - **API.** Make an OAuth client (Settings > Trust credentials) with the tag
     `tag:ship` and the scopes of the level you want; the default needs only
     `auth_keys`, so the stored secret can do nothing but mint `tag:ship` keys.
     The levels (device removal, policy management) and their exact scope names
     are in [../overlay-providers.md](../overlay-providers.md). Put its secret in a
     file only you can read:

     ```sh
     ( umask 077; cat > ~/.config/fleetledger/tailscale-oauth )   # paste, then Ctrl-D
     ```

     FleetLedger then mints a single-use, pre-approved, tagged key that expires in
     10 minutes for each join.

## 2. FleetLedger (once)

Add to `~/.config/fleetledger/ships.toml`:

```toml
[overlay]
provider = "tailscale"
tags = ["tag:ship"]
# login_server = "https://headscale.example.com"   # Headscale only
key_source = "prompt"                              # or "tailscale-api":

# [overlay.api]
# tailnet = "-"                                    # your default tailnet
# oauth_client_id = "kExampleClient1"
# oauth_secret_file = "~/.config/fleetledger/tailscale-oauth"
```

## 3. Join Ships

With `[overlay]` set up, a new Ship joins on its own once it enrols (`ship-fleet
new`, and the web UI; `--no-overlay` or the setup page's tick opts one out), and
its host certificate is re-signed with its overlay name. A join key that must be
pasted needs a terminal, so from the web UI that takes the API key source. For
Ships made before, or opted out:

```sh
ship-fleet overlay join ship-01          # or several names, or --tag work
ship-fleet overlay status
```

or **Join the overlay** on the Ship's page.

On the Ship this installs `tailscale` from its pinned, signature-checked apt repo
(it is not installed or running on Ships that never join), starts `tailscaled`
with log upload to Tailscale turned off, and joins as `ship-01` with the tag.
FleetLedger then puts the Ship's overlay name (for example
`ship-01.example-tailnet.ts.net`) first in its list of names. It works from the
LAN and off it, and a computer that is not on the overlay fails to resolve it at
once and moves on to the LAN name.

To make a Ship refuse SSH logins from the LAN altogether (only the overlay and
loopback are accepted):

```sh
ship-fleet set ship-01 ssh_listen=overlay
```

It is refused until the Ship has joined, and `overlay leave` turns it back to
`any` first, so the Ship can never be locked out by these commands. **The
computer FleetLedger runs on must be on the overlay too** before you do this:
otherwise its own logins come from the LAN and are refused ("refused
FleetLedger's admin key"). If the overlay itself breaks, the Proxmox console
still works, and `sudo ship-ctl set network.ssh_listen=any` there opens the LAN
again.

**With the LAN guard on** (`lan_guard=true`), the Ship also refuses new
outgoing connections to `100.64.0.0/10`, the overlay's own address range, and to
your LAN. That matches the access rules (Ships open nothing), and replies to
connections your devices start are let through. The one effect: the Ship cannot
start a direct WireGuard path to a device on your LAN, so Tailscale may relay that
traffic through its servers (still end-to-end encrypted) instead.

## 4. Your laptop

1. Install Tailscale on the laptop and sign in to the same tailnet. With MagicDNS
   on, Ships are reachable by name.
2. Trust the Ships' host keys on the laptop. The easy way is
   `ship-fleet trust laptop --key laptop.pub` on the computer that runs
   FleetLedger ([trust-machines.md](trust-machines.md)): the laptop gets a
   certificate and the Ships' CA line, and needs no known-hosts lines or agent
   key (skip to step 4). By hand: FleetLedger knows the host keys; copy the
   lines for the Ships you want from its known-hosts file (on the computer that
   runs FleetLedger):

   ```sh
   cat ~/.config/fleetledger/known_hosts          # "<ship> ssh-ed25519 AAAA..."
   ```

   On the laptop, put each line into `~/.ssh/known_hosts` with the Ship's overlay
   name in front, and add an SSH config entry:

   ```
   # ~/.ssh/known_hosts
   ship-01.example-tailnet.ts.net ssh-ed25519 AAAA...

   # ~/.ssh/config
   Host ship-01.example-tailnet.ts.net
       User agent
       ForwardAgent no
       StrictHostKeyChecking yes
   ```

3. Your key (by hand only): the laptop's key must be one of the Ship's agent keys
   (`fleet.agent_keys` in `ships.toml`, put on the Ship by its seed).
4. Attach:

   ```sh
   herdr --remote agent@ship-01.<your tailnet>.ts.net   # the overlay name
   ```

   A herdr-core Ship (`runtime = "core"`) is not reached with the herdr TUI. With
   the trust bundle installed, `hc` and the herdr desktop app use its SSH config
   entry, overlay name included:

   ```sh
   hc machine add ship-01 --name ship-01   # once; the bundle's install.sh does it
   hc attach ship-01/main                  # or hc, for the picker
   ```

   In the desktop app: Settings > Machines > Add machine, then pick the Ship. Its
   telemetry is on by default and can be turned off in its settings. By hand,
   without the bundle, add the Ship with `hc machine add agent@<ship overlay name>
   --name ship-01`. See the trust runbook's
   [herdr-core Ships](trust-machines.md#herdr-core-ships).

## 5. Check it (acceptance)

`ship-fleet overlay check --probe` covers the tailnet side and the "a Ship
cannot reach the laptop" probe (run it on the laptop, with the laptop on the
overlay). The probe passes only when the attempt times out, which is what a
policy block looks like; a refused connection fails it, since only a packet the
policy let through gets refused. By hand:

- On a phone hotspot (not your LAN), `herdr --remote agent@<ship overlay name>`
  (`hc attach <ship>/<agent>` for a herdr-core Ship) attaches, and `ssh` to the
  Ship succeeds with no host-key prompt.
- Your router has no forwarded ports for any of this.
- From a Ship, a connection to your laptop fails:
  `ship-fleet ssh ship-01 -- timeout 5 bash -c 'echo > /dev/tcp/<laptop overlay address>/22'`
  must **time out** (the access rules give Ships no destinations, and Tailscale
  drops what the rules block). "Connection refused" is a failure too: it means
  the packet got through the policy and the laptop simply has nothing on port 22.
- With `ssh_listen=overlay`, `ssh agent@ship-01.local` from the LAN is refused,
  while the overlay name works.

## Leaving, and destroying

```sh
ship-fleet overlay leave ship-01
```

turns `ssh_listen` back to `any`, logs the Ship out of the tailnet, and stops and
disables `tailscaled`. `ship-fleet destroy` does this first by itself, then
prints the exact admin-console step to remove the Ship's device (its tailnet
name and node id). If you turned device removal on (`remove_devices`, level 2 or
3), it removes the device itself instead: the one `tag:ship` device with the node
id the Ship joined as (Ships joined before that was recorded: its recorded
tailnet name or live address). It never removes a device by hostname alone,
never an untagged one, and if several match it removes none and says so. On
Headscale it prints the server-side steps.

## Threats this adds, and what limits them

| Threat | What limits it |
|---|---|
| Someone on the overlay reaches a Ship | Only SSH is allowed in, and only from your devices (access rules); logins still need your key and the pinned host key |
| A compromised Ship reaches your laptop or other Ships over the overlay | The access rules give `tag:ship` no destinations at all; a policy test guards it |
| A leaked join key | Single use, pre-approved only for `tag:ship`, expires in minutes; never stored |
| A leaked OAuth client secret (it sits in your OS keyring, encrypted at rest, or a 0600 file; either way any program running as you can read it while you are logged in) | At the default level it can only mint `tag:ship` keys (which only get port 22 from your devices). If you turned on device removal it can also remove or re-tag **any** device in the tailnet, yours included, not only Ships (Tailscale does not limit `devices:core` by tag; an admin repairs it in the console); with policy management it can also change the access rules. Revoke it in the admin console; pasting keys stores no secret at all |
| Tailscale's coordination service | It can add nodes to your tailnet, but not log in to a Ship: SSH keys and pinned host keys are separate from it. Headscale removes this dependency |
