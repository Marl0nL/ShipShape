# Runbook: SSH certificates and attaching from other machines

## Set up or update

```sh
# 1. On FleetLedger's VM:
ship-fleet self-update
# 2. On a computer that runs Herdr (later, to update: shipshape-setup):
ssh fleetledger@fleetledger.example.invalid ship-fleet machine-setup --installer | sh -s -- --herdr-client --fleetledger fleetledger@fleetledger.example.invalid
```

The first runs on the VM, as ever: it updates FleetLedger, brings its own state up to
date (the trusted machines' bundle keys) and restarts it. The second runs the installer,
`shipshape-setup`, that FleetLedger's VM has (the version it runs; from a ShipShape
checkout, `./shipshape-setup --herdr-client --fleetledger fleetledger@HOST` is the same).
It installs a copy of itself, so later `shipshape-setup` with nothing after it updates
everything and asks nothing; nothing updates it unattended. It:

- pins the VM's host key: one `~/.ssh/known_hosts` already has is used; otherwise it shows
  the fingerprint and asks you to confirm it (or `--host-key-fingerprint SHA256:...`);
- finds which trusted machine this computer is by its keys. If none, and your ssh session
  on the VM has a shell, it shows the `ship-fleet trust` it would run there (a name from
  this computer's host name, `--scope all`, `--logins both`, a new key of its own in
  `~/.ssh/shipshape`) and runs it after one yes (`--yes`; `--name`, `--scope`, `--logins`
  to choose). With no key that logs in there, it asks to be paired by code and waits for
  your approval ([Pairing by code](#pairing-by-code)); with pairing off (or `--no-pairing`),
  it says the one step to take on a computer that has a key (the Fleet panel's Machines ▸
  Trust a machine, or one `ssh ... ship-fleet trust` line), and stops: a computer never
  trusts itself;
- installs the refresher and its timer (below), the Desk and the Fleet panel into Herdr,
  and fills in the panel's `settings.json` where you have not; and opens the Fleet tab
  (`--no-open` not to).

A computer with no key that logs in to the VM runs the installer from a ShipShape checkout
instead; while pairing is on (`ship-fleet pair on`, once), it shows a code, which you approve
on a device that has a shell there (the Fleet panel's Machines page, or `ssh ... ship-fleet
pair approve`, typing that code), and it then finishes by itself (see [Pairing by
code](#pairing-by-code)). No Ship needs
updating for any of this. The rest of this page is how it works, and the steps by hand.

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

## Pairing by code

For a computer with no key that logs in to FleetLedger's VM (a new laptop): nobody copies a
key or a line by hand. Once, on the VM (or from the Fleet panel's Machines page, Turn
pairing on):

```sh
ship-fleet pair on
```

Then, on the new computer, from a ShipShape checkout:

```sh
./shipshape-setup --herdr-client --fleetledger fleetledger@HOST
```

It pins the VM's host key (shown for you to confirm, or `--host-key-fingerprint`), makes its
own key (`~/.ssh/shipshape/id_ed25519`), asks FleetLedger to trust it, and shows a six-digit
code, its key's fingerprint and the VM's host key's. On a device that has a shell on the VM,
approve it: the Fleet panel's Machines page lists it under Waiting to be paired (Approve…
opens a terminal), or

```sh
ssh fleetledger@HOST ship-fleet pair approve
```

Type the code the new computer shows (FleetLedger never shows it, so it cannot be copied
from there), check the key and host key it shows against the new computer's, and choose
its name, scope and logins (the name it gave itself, `all` and `both` by default). That runs
`ship-fleet trust NAME --key ...`, with all of trust's rules, and the installer, waiting,
finishes as a trusted run does: the refresher through its own key, the Desk and the Fleet
panel from the checkout. Refuse it instead with `ship-fleet pair refuse ID` (or the panel's
Refuse); a request nobody answers lapses after 10 minutes, and Ctrl-C on the new computer
withdraws it. Nothing of a refused, withdrawn or lapsed request is kept.

Being on the same network is never an approval. While pairing is on, anybody who reaches
the VM's ssh may ask (with the installer's well-known pairing key, whose line may run only
`ship-fleet machine-pair`), but a request grants nothing, at most 4 wait at once, and none
is trusted until you type its code. Somebody who keeps asking can use those bounds up (the installer then says FleetLedger is busy, and falls back to the step by hand): `ship-fleet pair off`, then `on`, starts them afresh. `ship-fleet pair off` closes it again; `ship-fleet
doctor` checks its line (`fleet.pairing`). See [docs/security.md](../security.md#pairing-by-code)
and `contracts/ship-fleet.md` ("ship-fleet pair").

A phone is out of scope: it runs no installer, so it cannot ask. Its browser pairs with the
web UI instead (`ship-fleet ui link`, or the Fleet panel's Pair a phone's browser).

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

With FleetLedger on its own VM, trusting a machine is: run `trust` once there, paste one
line on the machine. From then on the machine keeps itself current. (`shipshape-setup`,
above, does all of this for you from a computer whose ssh has a shell on the VM.)

1. On the machine you want to attach from, make a key just for this (no passphrase, so
   its timer can use it; the key may fetch only its own bundle on the VM):

   ```sh
   mkdir -p ~/.ssh/shipshape && ssh-keygen -t ed25519 -N '' -f ~/.ssh/shipshape/id_ed25519
   ```

2. On the FleetLedger VM, with its **public** half (the file, or the pasted
   `ssh-ed25519 AAAA...` line):

   ```sh
   ship-fleet trust laptop --key 'ssh-ed25519 AAAA...' --scope tag:work   # or --scope all, ship:ship-01
   ```

3. `trust` prints one line starting `sh -c '`. Paste it in a terminal on the laptop. It
   finds the laptop's key under `~/.ssh` by its `.pub` file (wherever you made it),
   pins the VM's host key, fetches the laptop's bundle with that key, and installs the
   refresher at `~/.ssh/shipshape/refresh`. The refresher records the name the laptop
   is trusted under and its key's path in `~/.ssh/shipshape/machine.conf`, writes the
   certificate, `known_hosts` and SSH config, adds herdr-core Ships to `hc`, and leaves a
   timer that runs it at login and hourly (a systemd user timer on Linux, a launchd
   agent on macOS). Add ` --no-timer` at the end of the line for no timer.

Every refresh renews the certificate once it is past half its life, adds new Ships and
removes destroyed ones (from `hc` too, but only the machines the refresher added
itself). It applies the bundle as data only and never replaces itself; see
[A machine that refreshes itself](#a-machine-that-refreshes-itself) below. To remove
the timer: `~/.ssh/shipshape/refresh remove-timer`. To update the refresher, run
`shipshape-setup` (or paste the line again: `ship-fleet trust laptop` prints it). With
Herdr on the machine and no Fleet panel in it yet, the pasted line ends by naming the one
command left: `shipshape-setup --herdr-client`, from a checkout (a key that may only fetch
its bundle gets the mods from there, and the panel then shows the Ships from
`fleet.json`).

Your main machine (whose own key logs in to the VM with a shell) gets the same line
from `ship-fleet trust desktop --key ~/.ssh/id_ed25519.pub` and runs the same
refresher, which asks for its bundle by name. Its timer runs without your ssh-agent, so
a key with a passphrase works only when you run the refresher yourself (or the Fleet
panel does, with your agent); give the timer a key it can open.

Without the refresher (FleetLedger on your desktop, or you would rather not), the
bundle works as before:

- `--scope` decides which Ships the certificate opens. You can repeat it.
- `--logins` decides what it may log in as there: `agent` (attach to the agent,
  `herdr --remote`), `owner` (your own login, with sudo) or `both`, the default for
  a new machine (one of yours). A shared machine that should only reach agents:
  `--logins agent`.
  Scopes and logins become certificate principals (`agent:all`, `agent:tag:work`,
  `owner:ship:ship-01`, ...), and each Ship accepts for each login only those
  matching that login and its own name and tags. An owner-only machine's SSH config
  logs in as you, and its herdr saved machines are left out.
- `--hours` sets the lifetime; the default comes from `[ca] user_cert_hours`.
- Certificates never log in as `fleet`. They allow a terminal and nothing else:
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
each herdr Ship herdr does not know yet. Ships that run herdr-core get hc instead
(see [herdr-core Ships](#herdr-core-ships) below).

If the machine is the FleetLedger computer itself, `ship-fleet trust
--this-computer` does all of this in place, with your own key (the first of
`fleet.agent_keys` whose private half is here) and scope `all`. The web setup
does it after the first Ship, and the Ship page offers it. Once this computer is
trusted, every Ship that enrols refreshes its bundle (a new certificate, the new
Ship in its SSH config and in herdr). Which machine entry is this computer is
kept in `~/.config/fleetledger/this-computer.json`. `ship-fleet trust
--this-computer` shows what `install.sh` added and skipped, and its closing
"can attach" line names `hc` or `herdr --remote` only when that program is
installed here; without it, it says what to install (hc:
<https://herdr.dev/core/>) and to run the command again.

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

### herdr-core Ships

A Ship with `runtime = "core"` in its ship.toml runs its agents in herdr-core,
and the herdr TUI (`herdr --remote`) cannot attach to it. FleetLedger knows a
Ship's runtime from its last status (`herdr.runtime`), and every attach command
it shows follows it. A Ship with no status yet (one just made: every new Ship runs
herdr-core) counts as herdr-core; an adopted Ship's status is kept from the moment
it is adopted. Nothing about SSH changes: the same certificate (`clear,
permit-pty`), CA line and `Host` block work for herdr-core.

For each herdr-core Ship, `install.sh`:

- removes its herdr saved machine, if herdr is installed and has one (the entry
  whose label or SSH target is the Ship's name, removed by its profile id). A
  herdr TUI that lists a herdr-core Ship starts a stray herdr server on it over
  SSH, which could try to resume sessions herdr-core is running;
- runs `hc machine add <ship> --name <ship>` if `hc` is installed here and does
  not have the Ship yet. Without `hc` it says so and skips it. It never installs
  anything, here or on the Ship (no `--install`).

A herdr-only bundle is exactly as before. Run the bundle again after a Ship
changes runtime (download it again, or `ship-fleet trust <machine>`; this
computer's is rewritten in its next renewal, or with `ship-fleet trust
--this-computer`): it is safe to run any number of times. `--no-herdr` skips both
herdr and hc.

On the laptop, two clients reach a herdr-core Ship:

- **`hc` in a terminal** (herdr-core, installed the way its docs say:
  <https://herdr.dev/core/>; the Ship's version may differ, as long as both speak
  the same protocol generation):

  ```sh
  hc ls                             # every machine's terminals: ship-01/t3 main idle
  hc attach ship-01/main            # one agent (detach: ctrl+\)
  hc                                # the picker
  ```

- **The herdr desktop app**: Settings > Machines > Add machine, and pick the Ship
  (the app reads `~/.ssh/config` and its `Include`, so the bundle's entry is
  there). If it offers to install or update herdr-core on the Ship, accepting is
  fine: it copies its own herdr-core to the agent's `~/.local/bin` there and
  restarts the core from it, terminals and agents kept. The Ship keeps that newer
  copy, also across a reboot (herdr-core never moves back), and its status says so
  (`core-unpinned`, a notice). That copy is Herdr's, not checked against the Ship's
  pinned checksum ([security.md](../security.md)). The app's telemetry is on by default; turn it off
  in its settings (Privacy) if you want. It shows the Ship's agents in one space,
  `shipshape`, a tab each.

## 4. Renew

A certificate lapses at the end of its lifetime, and sshd then refuses it. In its last
day `ship-fleet attention`, the web UI's Needs attention and `ship-fleet doctor` (Health)
flag it as `machine-cert-expiring`, and once it has lapsed as `machine-cert-lapsed`;
`ship-fleet ca show` gives each machine's state, and its row under Trusted machines a
badge. A machine with the refresher (section 3) renews itself; nothing renews any
other machine. To
renew one by hand, run `ship-fleet trust laptop` again on the FleetLedger computer; it reuses the
recorded key. To
send only the new certificate, use:

```sh
ship-fleet trust laptop --print-cert | ssh laptop 'cat > ~/.ssh/shipshape/laptop-cert.pub'
```

Or, from the web UI (on this computer, or on the laptop itself through the remote web
UI, [remote-ui.md](remote-ui.md)): **Download bundle** in the machine's row under
Settings ▸ Trusted machines. It runs `ship-fleet trust laptop` and hands you the bundle
as `shipshape-laptop.tar.gz` (from a paired device, only for the machine it runs on); on
the laptop,
`tar xzf shipshape-laptop.tar.gz && sh shipshape-laptop/install.sh`. The bundle holds
no secret (a certificate for the laptop's key, the CA's public line, SSH config and
`install.sh`), and a renewal keeps the private key path the last bundle named.

Each Ship's `HostName` in a bundle is the first of its names that resolves on the
computer FleetLedger runs on. For a machine elsewhere, `--hostnames overlay` writes the
overlay name of each Ship on the overlay instead (the remote web UI's Download bundle
does that).

Without the refresher, re-run `install.sh` after adding Ships, so the new Ships get
`Host` blocks and herdr entries.

To change a machine's scope, run `ship-fleet trust laptop --scope <new scope>`; its
logins, `ship-fleet trust laptop --logins agent` (or the setup page's Trusted
machines). Narrowing logins works like narrowing a scope, below: a certificate it
holds for more is revoked on every Ship at once.
A certificate names the Ships it opens, so narrowing the scope (for example
from `all` to `tag:work`) would otherwise leave the old certificate opening
every Ship until it lapsed. FleetLedger records each certificate it issues
(`issued` in `ships.toml`) and puts the serials of any that are wider than the
new scope into the revocation list, which it sends to every Ship straight away.
A Ship it cannot reach gets the list at the next `ship-fleet ca sign`; until
then the old certificate still works there. Install the new certificate on the
machine afterwards (the bundle or `--print-cert`).

### A machine that refreshes itself

`ship-fleet machine-bundle laptop`, run on the FleetLedger computer, prints the
laptop's own bundle as JSON (contracts/ship-fleet.md): its certificate, the CA lines,
the Ships its scope reaches with their names, tags and runtime, and FleetLedger's own
host key. It signs a new certificate only once the current one is past half its life,
and nothing while the vault is locked (it then returns the current certificate while
it lasts, and says why it is not new). It is the one command a key of the
laptop's may run on FleetLedger's own VM, so the laptop can keep itself current
(the laptop's refresher, installed by the line `trust` prints, applies it).

On FleetLedger's own VM, `ship-fleet trust laptop --key laptop.pub` also gives the
laptop's key one line in the `fleetledger` user's `~/.ssh/authorized_keys`, in a
marked block of FleetLedger's own, forced to `ship-fleet machine-bundle laptop` with
`restrict` (no terminal, no forwarding). From the laptop, `ssh -i
~/.ssh/shipshape/id_ed25519 fleetledger@<the VM>` then returns its bundle as JSON,
whatever command it asks for, and nothing else.

The refresher (`~/.ssh/shipshape/refresh`) asks for that bundle with `ssh -F none`, the
key and host key in `machine.conf`, and nothing from `~/.ssh/config`. It checks every
value against a tight pattern and refuses the whole bundle if one is out of place. It
writes only its own files in `~/.ssh/shipshape` (the SSH config from its own template),
and changes `hc` only to add herdr-core Ships and remove the ones it added. When
FleetLedger cannot be reached, refuses the laptop (untrusted, or idle 30 days), or
cannot sign a certificate that is due (its vault locked), the refresher keeps the last
good files, exits 1 and says why in one sentence, also in
`~/.ssh/shipshape/state.json` (for the Fleet panel). `fleet.json` beside it is
the last bundle it applied, for the Fleet panel and the Desk. The bundle also carries
the refresher itself, base64: only the pasted line ever decodes it, so a FleetLedger
that was taken over cannot run code on the laptop through a refresh (it can still sign
for any name it likes, since it is the CA). Contract: `contracts/ship-fleet.md`, "The
refresher on a trusted machine". `untrust` removes the line. Your own
lines in that file are never changed; a machine whose key is one of yours (it has a
line of its own there) gets no restricted line, so that key keeps your line's shell
and asks for its bundle by name (`ship-fleet machine-bundle NAME`). `ship-fleet
doctor` checks the block (`bundle keys`) and `ship-fleet trust --sync-keys` rewrites
it. On a desktop FleetLedger writes no line: there `authorized_keys` belongs to your
own login.

A machine that has asked is watched: `ship-fleet attention` says
`machine-refresh-stopped` when it has not asked for 4 days. Running the command by
hand counts as that machine asking, so to look at what it prints, run it for
FleetLedger's own computer (`ship-fleet ca show` marks it), which is never watched. A machine that has not
asked for 30 days is refused (`machine-refresh-refused`) until you let it again:

```sh
ship-fleet trust laptop --allow-again    # it may ask again; signs nothing
ship-fleet trust laptop --idle-limit off # it stays trusted until untrusted
ship-fleet trust laptop --idle-limit on  # the default: refused after 30 days idle
```

A machine that has never asked (one trusted before this existed, say) starts its 30
days at its first ask. FleetLedger's own computer has no idle limit. `ship-fleet ca
show` says when each machine last asked.

`ship-fleet trust laptop --looks on` adds the fleet's Desk looks to the laptop's bundle, so
its refresher writes them into Herdr's folder there, and its restricted key then reads them
too. The laptop must run refresher 2 first: [share-desk-looks.md](share-desk-looks.md).

## 5. Revoke

```sh
ship-fleet untrust laptop
```

This marks the machine revoked in `ships.toml`, adds its key to the revocation
list, and pushes the list to every Ship. It cannot be undone, so it asks you to
type the machine's name; where nobody can be asked (a script), add `--yes`.

A machine whose key is one of your keys for Ships (`fleet.agent_keys`), such as
the old computer after a move, is different: the revocation list applies to
every login, so untrusting it locks that key out of every Ship, as the agent and
as you. `untrust` says so and offers to stop renewing it instead: press Enter and
it stays trusted, and its certificate lapses by itself, since nothing renews a
machine other than this computer unless you run `ship-fleet trust <machine>` or
Download bundle. To revoke it all the same, type its name after the warning, or
add `--yes --revoke-my-key`. The web UI's Untrust asks for its name the same way.

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

## Logins of their own (upgrading)

Before owner logins had certificates of their own, a machine's `agent:*`
certificate also opened your own login. The first `ship-fleet trust`, `untrust`
or `ca sign` (or the web UI starting) after upgrading records `logins = ["agent",
"owner"]` for every machine trusted before, so each keeps what it had, and says so
once. Their next certificate carries both principals; until a machine renews, a
Ship already updated (`ship-fleet update`) lets its current certificate log in as
the agent only. This computer renews within a day by itself; for another machine,
run `ship-fleet trust NAME` on the FleetLedger computer (or Download bundle), then
`sh install.sh` from the new bundle on that machine.

**Update every Ship before relying on "the agent only".** A Ship still running the
previous tooling lets any `agent:*` certificate log in as you, whatever the machine's
logins say, because its owner login took the agent's certificates then. So:
`ship-fleet update` every Ship first, then `ship-fleet trust NAME --logins agent`.
Narrowing a machine trusted before the upgrade revokes the certificates it holds
(the migration records them as having opened both logins, which they did), on
every Ship it reaches; one it cannot reach is named, and gets the list at the next
`ship-fleet ca sign`.

**Going back to the previous FleetLedger** needs a hand edit of ships.toml: it does
not know `logins` or `owner:*` principals, so delete every `logins = [...]` line
under `[machine.*]` and every `issued` entry naming `owner:*`. Ships need nothing:
the previous FleetLedger signs `agent:*` only, which an updated Ship takes for the
agent's login only.
