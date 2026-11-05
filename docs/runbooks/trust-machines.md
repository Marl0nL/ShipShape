# Runbook: SSH certificates and attaching from other machines

From phase 3 FleetLedger is the fleet's SSH certificate authority (CA). That
gives you two things:

- **Ships prove who they are with certificates.** Each Ship's host certificate
  lists every name it can be reached by. A machine that trusts the CA connects
  to any Ship with no host-key prompt and no per-Ship known_hosts entry.
- **Your machines attach with short-lived certificates.** `ship-fleet trust`
  gives a machine (a second desktop, a laptop) a certificate for the `agent`
  user on the Ships you choose. It also writes that machine's SSH config, and
  registers each Ship as a herdr saved machine, so one herdr window shows every
  Ship's agents. A certificate lapses by itself; `ship-fleet untrust` revokes
  it sooner.

FleetLedger itself keeps checking each Ship against its pinned host key, as in
phase 1. Certificates are for everything else.

## 1. Make the CA (once)

```sh
ship-fleet ca init
ssh-add ~/.ssh/shipshape_host_ca_ed25519 ~/.ssh/shipshape_user_ca_ed25519
```

This makes two ed25519 keys, each with its own passphrase: a **host CA**, which
signs Ships, and a **user CA**, which signs your machines. Both private keys
stay on this computer. With the keys loaded in ssh-agent, FleetLedger signs
through the agent (`ssh-keygen -U`) and never reads the key files; without
them, ssh-keygen asks for the passphrase each time. The `[ca]` section of
`ships.toml` records where they are, how long host certificates last (90 days)
and the default user certificate lifetime (168 hours).

## 2. Certify the Ships (migration from pinned keys)

```sh
ship-fleet ca sign              # every Ship; or: ship-fleet ca sign ship-01, --tag work
```

For each Ship, FleetLedger:

1. connects over the pinned-key connection it already trusts;
2. signs the host key **from the inventory**, never a key read off the network,
   for every name in `names` plus the bare Ship name;
3. installs the certificate, the user CA key and the revocation list with
   `ship-ctl ssh-trust`. The Ship refuses a certificate for any key but its
   own, checks the new sshd configuration with `sshd -t`, and rolls back if
   sshd would refuse it;
4. opens a fresh connection that trusts **only** the CA, never the pinned key,
   and reports success only if that works.

So there is no moment where anything trusts a key it has not verified. New Ships
are certified automatically when they enrol, once a CA exists.

Run it again any time: it renews certificates within 30 days of expiry, re-signs
a Ship whose names changed, and pushes newer revocation lists. `ship-fleet ls`
and `status` flag `host-cert-missing`, `host-cert-expiring` and
`ssh-trust-behind`; `ship-fleet ca show` lists every Ship's certificate and
every trusted machine.

A Ship whose tooling predates phase 3 is skipped with a message. Update its
tooling first.

## 3. Trust a machine

On the machine you want to attach from, make a key just for this:

```sh
mkdir -p ~/.ssh/shipshape && ssh-keygen -t ed25519 -f ~/.ssh/shipshape/id_ed25519
```

Copy its **public** half (`id_ed25519.pub`) to the FleetLedger computer, then:

```sh
ship-fleet trust laptop --key laptop.pub --scope tag:work       # or --scope all, ship:ship-01
```

- `--scope` decides which Ships the certificate opens. You can repeat it.
  Scopes become certificate principals (`agent:all`, `agent:tag:work`,
  `agent:ship:ship-01`), and each Ship accepts only those matching its own name
  and tags.
- `--hours` sets the lifetime; the default comes from `[ca] user_cert_hours`.
- Certificates log in as `agent` only. They allow a terminal and nothing else:
  no agent, port or X11 forwarding.

The bundle lands in `~/.local/share/fleetledger/trust/laptop/` (mode 0600).
Copy that folder to the machine and run:

```sh
sh install.sh            # or: sh install.sh --no-herdr
```

It installs into `~/.ssh/shipshape/`:

- the certificate;
- a `known_hosts` with one `@cert-authority` line, limited to the Ships' names
  (plus `@revoked` lines for host keys FleetLedger replaced);
- an SSH config with one `Host` block per Ship: `HostName` the first of the
  Ship's names that resolves on the FleetLedger computer (a `.local` name with no
  mDNS there falls through to its overlay name or recorded address), `User
  agent`, the certificate, `ForwardAgent no`, and certificate-only host keys.

It puts `Include ~/.ssh/shipshape/config` at the top of `~/.ssh/config`. Then
it runs `herdr machine add <ship> --label <ship> --remote-session default` for
each Ship herdr does not know yet.

If the machine is the FleetLedger computer itself, `ship-fleet trust
--this-computer` does all of this in place, with your own key (the first of
`fleet.agent_keys` whose private half is here) and scope `all`. The web setup
does it after the first Ship, and the Ship page offers it. Once this computer is
trusted, every Ship that enrols refreshes its bundle (a new certificate, the new
Ship in its SSH config and in herdr). Which machine entry is this computer is
kept in `~/.config/fleetledger/this-computer.json`.

Its certificate is valid for 7 days, like any machine's; FleetLedger renews it (and
rewrites its bundle) in its last day whenever it runs: the web UI's poll, or
`ship-fleet status`. The Ship page shows until when. Plain ssh needs a key you can
open: FleetLedger prefers one of yours that it does not look after. If your only key
is one FleetLedger made (its passphrase is only in your keyring), plain ssh works
while your own ssh-agent holds it, and FleetLedger loads it there; with no ssh-agent
of yours running it says so, and `ship-fleet ssh` still works. A `~/.ssh/config`
that is a symbolic link (to a dotfiles folder, say) is written through the link. (`ship-fleet trust desktop
--key ~/.ssh/shipshape/id_ed25519.pub --install` still works for another key.)

Then, on that machine:

```sh
ssh ship-01 true                  # no host-key prompt
herdr --remote ship-01            # one Ship
herdr                             # every saved Ship in the sidebar
```

Never turn on `ForwardAgent` for Ships, even though herdr's docs suggest it for
git: agents on the Ship could then use your keys while you are attached. Ships
use their own GitHub sign-in.

## 4. Renew

A certificate lapses at the end of its lifetime, and sshd then refuses it. To
renew it, run `ship-fleet trust laptop` again; it reuses the recorded key. To
send only the new certificate, use:

```sh
ship-fleet trust laptop --print-cert | ssh laptop 'cat > ~/.ssh/shipshape/laptop-cert.pub'
```

Re-run `install.sh` after adding Ships, so the new Ships get `Host` blocks and
herdr entries.

To change a machine's scope, run `ship-fleet trust laptop --scope <new scope>`.
A certificate names the Ships it opens, so narrowing the scope (for example
from `all` to `tag:work`) would otherwise leave the old certificate opening
every Ship until it lapsed. FleetLedger records each certificate it issues
(`issued` in `ships.toml`) and puts the serials of any that are wider than the
new scope into the revocation list, which it sends to every Ship straight away.
A Ship it cannot reach gets the list at the next `ship-fleet ca sign`; until
then the old certificate still works there. Install the new certificate on the
machine afterwards (the bundle or `--print-cert`).

## 5. Revoke

```sh
ship-fleet untrust laptop
```

This marks the machine revoked in `ships.toml`, adds its key to the revocation
list, and pushes the list to every Ship.

Expiry and revocation stop **new** connections. sshd checks a certificate only
when a connection opens. herdr keeps its SSH connection to each saved machine
open (and idle for up to 10 minutes), so a client that is already attached
stays attached until that connection closes. To cut a machine off at once,
also end the agent's SSH sessions on the Ships in its scope, for example
`ship-fleet ssh ship-01 --as fleet -- sudo pkill -u agent -x sshd-session`. That
ends every attached session, yours included; agents and herdr keep running. A Ship that is unreachable at that
moment keeps accepting the machine's current certificate until it lapses, or
until you run `ship-fleet ca sign` once the Ship is back; FleetLedger lists
which Ships still need it. A revoked machine is never certified again under
that name. To trust it again, give it a new key and a new name.

## Re-keying a Ship

`ship-fleet adopt <ship> --replace` re-pins a rebuilt Ship. When a CA exists,
the old host key also goes into `[ca] revoked_host_keys`, so trusted machines
refuse it after their next `install.sh`. Then run `ship-fleet ca sign <ship>`.

## Checks (phase 3 acceptance)

Checked Ship to Ship on the test pool: one Ship played the trusted machine,
herdr 0.9.5 and OpenSSH 10.2.

- From a second machine, `herdr --remote ship-01` works with no host-key prompt.
- A lapsed certificate cannot connect. Test it with `ship-fleet trust laptop
  --hours 1`, wait an hour, then run `ssh ship-01 true`.
- After `ship-fleet untrust laptop`, its certificate is refused for every new
  connection.

`fleetledger/tests/test_ca_sshd.py` runs the same checks against a real sshd.
