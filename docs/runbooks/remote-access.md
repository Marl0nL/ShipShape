# Runbook: reach Ships from outside your LAN

Attach herdr to a Ship from your laptop on a phone hotspot, with no router ports
forwarded. Ships and your own devices join an overlay network (Tailscale, or a
Headscale server you run), and an access rule lets your devices reach the Ships
on port 22 and nothing else.

What does not change: SSH with the Ship's pinned host key is still the only way
in, as `agent` with your key or `fleet` with FleetLedger's. The overlay adds a
route, not a new way to log in. Tailscale SSH stays off.

## How keys are handled

Each Ship joins with a **one-off auth key**: single use, pre-approved, tagged
`tag:ship`, expiring in minutes. FleetLedger gets it after the Ship has booted,
either from you (pasted, not shown) or minted through Tailscale's API, and hands
it to the Ship over SSH on stdin. On the Ship it exists only in a root-only file
under `/run` while `tailscale up` runs. Keys never go into seeds, cloud metadata,
command lines, logs, `ships.toml`, the audit log or the status document.

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

```sh
ship-fleet overlay join ship-01          # or several names, or --tag work
ship-fleet overlay status
```

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

**With the LAN guard on** (`lan_guard=true`, phase 2), the Ship also refuses new
outgoing connections to `100.64.0.0/10`, the overlay's own address range, and to
your LAN. That matches the access rules (Ships open nothing), and replies to
connections your devices start are let through. The one effect: the Ship cannot
start a direct WireGuard path to a device on your LAN, so Tailscale may relay that
traffic through its servers (still end-to-end encrypted) instead.

## 4. Your laptop

1. Install Tailscale on the laptop and sign in to the same tailnet. With MagicDNS
   on, Ships are reachable by name.
2. Trust the Ships' host keys on the laptop. FleetLedger knows them; copy the
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

   (Phase 3's `ship-fleet trust <machine>` does this for you with SSH
   certificates; this is the manual way.)
3. Your key: the laptop's key must be one of the Ship's agent keys
   (`fleet.agent_keys` in `ships.toml`, put on the Ship by its seed).
4. Attach:

   ```sh
   herdr --remote agent@ship-01.<your tailnet>.ts.net   # the overlay name
   ```

## 5. Check it (acceptance)

- On a phone hotspot (not your LAN), `herdr --remote agent@<ship overlay name>`
  attaches, and `ssh` to the Ship succeeds with no host-key prompt.
- Your router has no forwarded ports for any of this.
- From a Ship, a connection to your laptop fails:
  `ship-fleet ssh ship-01 -- timeout 5 bash -c 'echo > /dev/tcp/<laptop overlay address>/22'`
  must fail (the access rules give Ships no destinations).
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
| A leaked OAuth client secret | At the default level it can only mint `tag:ship` keys (which only get port 22 from your devices). If you turned on device removal it can also remove **any** device in the tailnet, not only Ships (Tailscale does not limit `devices:core` by tag); with policy management it can also change the access rules. Revoke it in the admin console; pasting keys stores no secret at all |
| Tailscale's coordination service | It can add nodes to your tailnet, but not log in to a Ship: SSH keys and pinned host keys are separate from it. Headscale removes this dependency |
