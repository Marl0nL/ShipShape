# The web UI (`ship-fleet ui`)

FleetLedger's setup, dashboard and login concierge, in your browser, on your
own computer only. The CLI stays complete: everything the UI does, a
`ship-fleet` command does too, and the UI runs the same code.

## First run: the setup page

```sh
ship-fleet setup              # opens the setup page (Ctrl-C to stop)
ship-fleet setup --no-browser # prints the link instead
ship-fleet setup --text       # the same steps in the terminal (no browser here)
```

`ship-fleet ui` on a computer that is not set up opens the same page, and it is
always under **Setup** in the menu. It starts with one choice:

- **New fleet (recommended).** FleetLedger makes every key it needs (its admin
  key, which logs in to Ships, and the host and user CA keys) and looks after
  them; see "Keys FleetLedger looks after" below. The only key question is which
  of your SSH keys may log in to Ships: your `~/.ssh/id_ed25519.pub` (or another
  found there) is proposed, or FleetLedger makes one for you.
- **Connect to an existing fleet or keys.** Use the admin and CA keys you
  already have, from another computer say, where they are. The page asks, in the
  open, **who looks after their passphrases**: FleetLedger (recommended: type each
  current passphrase once, FleetLedger re-encrypts the files with passphrases it
  keeps and loads them itself, so you are never asked again), or you (they keep
  their passphrases; you load the admin key with `ssh-add`, or are asked for its
  passphrase, on a desktop often in a pop-up, each time FleetLedger connects).
  The keys themselves stay the same either way.

Anything already on this computer is offered rather than asked again: a Proxmox
token secret already in the keyring or in `~/.config/fleetledger/proxmox-token`
(said up front; type its token id, which is filled in when the keyring names it,
and leave the secret empty), the saved Proxmox address and a certificate you
confirmed before, and a Tailscale OAuth client whose secret is in the keyring
(`fleetledger` / `tailscale-oauth:<client id>`) or in
`~/.config/fleetledger/tailscale-oauth` (whose secret names its client). Only
names are read to offer them; secrets are read only to check and use them, and
never shown.

Then, each step showing **done** or **next**:

1. **Where Ships run.** For Proxmox: the address; the server's certificate
   fingerprint, shown with the command to compare it on the host, then pinned
   when you confirm that it matches; the API token id and secret, checked through
   the pin before anything is stored. FleetLedger then finds the node, pool,
   storages, bridge and template the token can see and fills them in. Anything
   missing on the host comes back as the exact root commands, with a copy
   button and a **Check again**; nothing is saved until everything is there, and
   a last connection test runs before saving. A certificate that differs from
   the one you confirmed before is flagged, with both fingerprints, and needs its
   own confirmation. New Ships get VM ids 9100-9199 (ids already taken are skipped), away
   from the low ids Proxmox hands out to your own VMs; Advanced changes the range,
   or `any` for any free id. Or manual: FleetLedger writes each Ship's first-boot
   ISO and prints the steps.
2. **Reach Ships from anywhere (optional).** Links to the two Tailscale admin
   pages, the access rules with a copy button, then the OAuth client's id and
   secret. Before saving, FleetLedger checks the client signs in, can make a
   `tag:ship` key (made and deleted at once, which shows the rules are in
   place), and has the scopes of the level. **Skip for now** is fine.
3. **Your first Ship.** Name and tag; its size (cores, memory, disk, with the
   defaults shown); the sign-ins it uses; and a Claude agent to start on it (on by
   default: `main`, working in `~/work`, or in a GitHub repo you name, which the
   Ship clones first so the repo's CLAUDE.md, skills and settings apply; a private
   repo waits for the Ship's GitHub sign-in) with Remote Control, so once you sign the
   Ship in to Claude it shows in the Claude app under your account. With an
   overlay set up, the Ship joins it once it enrols (a tick to opt out) and its
   host certificate is signed with its overlay name. Progress shows live (seed,
   clone, start, then the first boot in words: not answering yet, cloud-init
   installing, a line a minute), and a first boot that outlasts the wait (30
   minutes) is "still starting", with `ship-fleet status <name> --wait`, not an
   error. Then FleetLedger trusts this computer with your own key (as `ship-fleet
   trust --this-computer` does: a short-lived certificate, an SSH config entry per
   Ship included from `~/.ssh/config`, herdr saved machines), so `ssh <ship>` and
   `herdr --remote <ship>` work here at once; **Sign it in** takes you to the
   concierge.

Each step has a collapsed **Advanced** section for the rest (where secrets are
kept, key file locations, other Proxmox storages, the VM ids FleetLedger may
use, the overlay level). Once there is a certificate authority, step 1 lists the **Trusted machines** and
what each may log in as: the agent only, you only (sudo), or both (a confirmation
says what changes; narrowing revokes its wider certificates at once).

A computer part-way through, from the terminal wizard
or an older setup, shows its finished steps as done and can change any of them
(manual to Proxmox, say); keys you chose passphrases for are offered, opt-in,
to FleetLedger's care. Nothing is deleted or rewritten without a click.

### Keys FleetLedger looks after

Each is an ordinary OpenSSH ed25519 key, encrypted with a random 256-bit
passphrase that FleetLedger keeps in your OS keyring (service `fleetledger`,
item `key-passphrase:<key file name>-<hash of its path>`), or, where there is no usable keyring, a
0600 file in `~/.config/fleetledger/key-passphrases/`. FleetLedger types the
passphrase itself, into `ssh-keygen` and `ssh-add`, through a terminal it
drives: never on a command line, in the environment, a log or on screen. When a
command needs the keys, FleetLedger loads them into your ssh-agent, or into one
of its own when none is running. That agent listens on `agent.sock` in
FleetLedger's private runtime folder (`$XDG_RUNTIME_DIR/fleetledger/`, or a
private `/tmp/fleetledger-<your uid>/` when that path is too long for a socket),
drops the keys after 8 hours (FleetLedger loads them again when it next needs
them) and keeps running after `ship-fleet` exits, until you log out. To stop it:
`pkill -u "$USER" -f 'ssh-agent -a .*fleetledger.*/agent.sock'`.
`ships.toml` lists them under `[fleet.managed_keys]`; `ship-fleet doctor`
checks each passphrase can be read. What this protects: a copy of the key files
(a backup, a lost disk) is useless without the keyring. What it does not: any
program running as you while the keyring is unlocked can read the passphrases,
as it can use ssh-agent.

On a GNOME desktop, the ssh-agent (`gcr-ssh-agent`, or the older gnome-keyring
one) lists every key in `~/.ssh` that has a `.pub`, whether it holds it or not,
and asks for the passphrase in a pop-up when one is used. So FleetLedger never
takes that list as "loaded": it adds its keys itself (and again every ten
minutes or each new command), and the setup page says when a key you look after
will bring those pop-ups.

## Every day

```sh
ship-fleet ui                 # opens your browser; Ctrl-C to stop
ship-fleet ui --no-browser    # prints the link instead
ship-fleet ui --port 8765     # a fixed port (default: any free one)
```

The link works once, for 15 minutes. It is printed in the terminal running
`ship-fleet ui`, and the browser is opened on it. If the browser shows a blank
or "not found" page (sandboxed browsers, such as snaps, cannot read the private
file FleetLedger opens them on), open the printed link instead. Press Enter in
that terminal for a new link (for a second browser, or after restarting the
browser). Stopping the UI ends every session and cancels any sign-in still open.

## What it shows

| Page | What is on it |
|---|---|
| **Fleet** (`/`) | The dashboard. Across the top, the fleet at a glance: Ships up, agents (working, waiting for you, on Remote Control), how many alerts at each level, sign-ins to fix, pending reboots and tooling updates (and OS packages), and overlay membership. Then **Needs attention**, then a card per Ship: state, tags, uptime, agents, each sign-in, CPU and memory over the last day as small charts with their current value, a disk gauge (marks at 80% and 90%), and its first note; a card links to its Ship. Then charts for the last 6 hours, 24 hours or 7 days (see "History and charts"). Filter by tag. Next to the filter, **Every Ship** (or **Every <tag> Ship**) opens the batch actions: sign in to a service, update, reboot. Updates, reboots, overlay joins and making a Ship run in the background as jobs; **Activity** lists the running ones and the last 20 finished, with their progress and outcome (including whether each Ship's agents came back in their modes), from any browser session. |
| **Ship** (`/ships/<name>`) | On every tab, under the name (with its tags, names, driver, overlay name and state, and when it was checked), a sticky action bar: the attach command for this computer with **Copy** (`herdr --remote <ship>` once this computer is trusted, else `ship-fleet ssh <ship>`), **Check now**, **Update** and **Reboot** (only while the Ship answers; otherwise a note to check its VM is on), and **More** (Join the overlay, for a Ship not on it). Under it, its jobs (an update, reboot or join started for it, from any page or session). Then the Overview: status, Needs attention (the Fleet page's alerts for this Ship, with their fixes), sign-ins with the account each tag expects (sign in, re-sign, sign out), agents grouped by herdr workspace with their state and mode, and whether they show in the Claude app (Remote Control on and Claude signed in) or why not; **Add an agent** (name, folder, mode: started now and on every boot, `ship-fleet set SHIP autostart.add=NAME:FOLDER`); herdr's last resume; the attach command that works from this computer (`herdr --remote <ship>` and `ssh <ship>` once this computer is trusted, else `ship-fleet ssh <ship>` and a button to trust it); **Join the overlay** for a Ship not on it yet; Claude Code, host and tooling, and its CPU, memory, disk and agents over the last 6 hours, 24 hours or 7 days. |
| Ship **Settings** | Remote Control, default mode, bypass, update channel, agent sudo, LAN guard and its extra allowed addresses, OS-update reboots and their time (`ship-fleet set`); the sign-ins it uses (claude, gcloud, github: an unticked one gets no prompt, no sign-in and no attention item, shows as off, and is signed out there; `ship-fleet signins`); its size (cores and memory with a restart through the hypervisor, a disk that only grows; `ship-fleet resize`); and tags. Update or reboot this Ship from its action bar. |
| Ship **Snapshots** | The Ship's disk snapshots (`ship-fleet snapshot`), read from its driver when the tab is opened (one call, nothing polls): name, when taken, note, with **Roll back** (the dialog asks you to type the Ship's name: everything since the snapshot is lost, sign-ins included) and **Delete** (the dialog). **Take a snapshot** below (a name, or `snap-<date>-<time>`, and a note); the action bar's **Snapshot** jumps there. Each runs as a job (`ship-fleet snapshot take|rollback|rm`), shown under the header. A snapshot holds the Ship's sign-ins: treat it like the Ship. On the manual driver the list is not available, Snapshot sits under **More** as "Snapshot (by hand)", roll back and delete take a name, and each job's card shows the steps to do on your hypervisor, each with **Copy**. |
| Ship **Logs** | `ship-ctl logs` for one source. |
| Ship **Network** | Phase 3's visibility for one Ship (`ship-fleet net SHIP`): the collector's state, traffic for 24 hours and 7 days, top destinations for either window with their programs, bytes and scope, destinations first seen in the last day (and whether they are new to the whole fleet), and programs. Shows the last fetched summary; **Fetch the latest summary** asks the Ship. Before any summary is fetched, the Ship's own 24-hour digest from its status is shown; without a collector, "not available". |
| **Network** (`/network`) | Across the fleet, filterable by tag: destinations first seen in the last day, a "who talked to this domain" search (the domain or any subdomain), and each Ship's collector and traffic (`ship-fleet net --new`, `--who`). |
| **Sign-ins** (`/signin`) | The concierge: start a sign-in for one Ship, or for every Ship with a tag; each sign-in's link, code box and countdown; results. |
| **Audit log** (`/audit`) | Who, what and when for every sign-in and sign-out. |

**Needs attention** is `ship-fleet attention`'s list, ranked the same way, plus
two alerts the dashboard adds itself: a disk 80% full (warning) or 90% full
(critical), and a Ship not on the overlay when `ships.toml` has an `[overlay]`.
Its three levels are shown by an icon and a word as well as a colour:
**critical** (high: a Ship presenting the wrong host key or claiming another
identity, unreachable Ships, wrong-account or expired sign-ins, failures, expired
host certificates), **warning** (medium: waiting agents, missing or expiring
sign-ins, pending reboots and herdr restarts, expiring certificates, the overlay)
and **notice** (low: Ships behind on tooling, tools waiting for a sign-in, tags
that differ). Above them come any sign-ins in the browser that are waiting for
you. Each alert carries its fix: a one-click **Sign in**, a link to the log or
setting, or the exact command with a **Copy** button (`ship-fleet reboot ship-01`,
`ship-fleet ca sign ship-01`, `ship-fleet overlay join ship-01`,
`herdr --remote ship-01` for a waiting agent when this computer is trusted, else
`ship-fleet ssh ship-01`). Unknown attention codes from
newer Ships are shown as they are, with a link to the Ship.

### History and charts

While the UI is open, every poll is also sampled into a small history:
`history.sqlite3` in FleetLedger's data folder (`~/.local/share/fleetledger/`,
mode 0600). One sample per Ship at most every 5 minutes, kept for 7 days and
then pruned, so it stays at about 2,000 rows per Ship. A sample holds only
numbers and the Ship's name: whether it answered, CPU (load per core), memory
and disk use, and how many agents were working, waiting and idle. Nothing is
sampled while the UI is closed, so charts show those hours as gaps. Delete the
file to start again.

The Fleet page charts the fleet's CPU, memory and disk (the average and the
busiest or fullest Ship), agents, Ships answering, network traffic (from each
Ship's cached 7-day network summary, refreshed on the Network page) and each
Ship's disk now, fullest first. Charts are SVG drawn on the server (no script,
nothing fetched from elsewhere); hover a chart for the values at that time, and
open **Table** under it for every value.

The status of every Ship is checked over SSH every 30 seconds (`--interval`)
while a page is open, and not at all once nobody has looked for two minutes.
Pages read the last result and never wait on SSH, so a waiting agent appears
within a poll. Before the first check finishes, the last good status (as
`ship-fleet ls --offline` shows it) is shown, and the summary says "last known;
not checked yet" rather than counting Ships as answering.

**Browser notifications:** on the Fleet page, "Notify me when an agent waits"
asks for permission; afterwards a notification appears when an agent starts
waiting while the page is open.

## The concierge in the browser

The same engine and rules as `ship-fleet login`
([runbooks/sign-in.md](runbooks/sign-in.md)):

1. Press **Sign in**. FleetLedger runs the tool's own no-browser sign-in on the
   Ship, as `agent`, in a terminal over SSH.
2. The card shows the sign-in link (and, for GitHub, the device code with a copy
   button) and a countdown.
3. Approve in the browser. For Claude Code and gcloud, paste the code the page
   shows into the card's box and press **Send to the Ship**. The box is a password
   field with autocomplete off.
4. FleetLedger checks the account against the Ship's tags: **Signed in**, or
   **Wrong account** with what the tags expect.

Signing in a whole tag queues one sign-in per Ship; they run one after the other
and each needs its own approval, but you never leave the page. A Ship and
service with a sign-in already open is not queued twice.

If a tool changes its wording and FleetLedger cannot find the link or the code
prompt, the card shows the last few lines of output (redacted) and offers **Take
over the Ship's terminal**: a live terminal in the browser where you finish by
hand. Leave it when done; FleetLedger then checks the account as usual.

## Security

Following Jupyter's model, adapted to "one person, one computer":

- **Local only.** The server binds `127.0.0.1`; there is no option to listen
  anywhere else. Nothing on a Ship talks to it.
- **One-time link.** `ship-fleet ui` makes a random token, keeps only its
  SHA-256, and puts it in the link. The first visit swaps it for a session
  cookie (`HttpOnly`, `SameSite=Strict`, named after the port) and refreshes to
  `/`, dropping the token from the address bar. A token works once and expires
  after 15 minutes. It is printed only to the terminal running `ship-fleet ui`.
  To open the browser, FleetLedger writes a private (0600) local page that
  refreshes to the link, because a browser's command line is visible to every
  process on the computer; the page is deleted on exit.
- **DNS rebinding.** Every request must carry `Host: 127.0.0.1:<port>` or
  `localhost:<port>`; anything else gets 400.
- **CSRF.** Every change (any method but GET and HEAD) needs the session's CSRF
  token (form field or `X-CSRF-Token` header) and an `Origin` that names this
  server exactly. Only small plain form posts are accepted (64 KiB).
- **The live terminal's WebSocket** needs the session cookie and an exact
  `Origin`; its first message must carry the CSRF token.
- **Confirmations.** Anything hard to undo (reboot, update, sign out, trusting a
  certificate, handing keys over, removing an old secret) asks first, in a
  dialog that says what will happen and names the action on its button; for
  destructive ones Cancel has the focus. The server still requires the same
  confirmation field it always did: the dialog ticks the form's hidden confirm
  box only when you confirm. Without script (or a browser without `<dialog>`)
  the tick box shows instead, so nothing ever becomes one click
  (`static/app.js`; tests in `fleetledger/tests/frontend/`). The most destructive
  (a snapshot rollback; destroy) also ask you to type a value, the Ship's name: the
  dialog's button works only once it is typed exactly, and the server checks it
  (`confirm_name`); without script the field shows in the form.
- **Content-Security-Policy** on every response: scripts, styles and connections
  from this server only, no inline code, no framing; `no-store` caching.
  `Referrer-Policy: same-origin` (not `no-referrer`, which makes browsers send
  `Origin: null` on form posts). htmx is vendored (`web/static/`), so no page
  loads anything from the network.
- **Text from Ships is text.** Everything a Ship reports is HTML-escaped. A
  sign-in link is shown as a link only if it is a plain `https://` address.
- **Setup's secrets.** The Proxmox token secret, an OAuth client secret and a
  key's current passphrase are typed into password fields and arrive in a POST
  body only. Each goes straight to its check and then to its store; none is put
  in a URL, echoed back, flashed, logged or audited, and the server keeps none in
  memory after the request.
- **No secrets kept or logged.** Request logging is off (so the token link is
  never logged). The pasted code goes from the form straight to the engine
  through a one-slot queue; it is never stored on the sign-in, echoed to a page,
  logged or put on a command line, and a code that arrives after the sign-in
  window has closed is not typed. Sign-in links and device codes are dropped
  when the sign-in finishes. Live terminal output is held in a bounded memory
  buffer only while the terminal is open. The audit log records time, Ship,
  action, service, outcome and account, as for the CLI.

Limits worth knowing: cookies ignore ports, so another web server you run on
`127.0.0.1` receives the session cookie if your browser visits it (the cookie is
named per port, is `HttpOnly`, and the session ends when `ship-fleet ui` stops).
Anyone who can run commands as you on this computer can already use your fleet
key, so the UI adds no new trust beyond that.

## How it is built

`fleetledger/fleetledger/web/`: Starlette with server-rendered Jinja templates
and htmx for polling; no front-end build.

| File | Purpose |
|---|---|
| `security.py` | the gate: Host, link token, sessions, CSRF, Origin, headers |
| `setup.py` | the setup page: the same steps as `ship-fleet setup --text`, through the same functions (keys.py, proxmox_setup.py, overlay_setup.py) |
| `server.py` | `ship-fleet ui`: binding, the link, uvicorn (access log off) |
| `app.py` | routes and pages |
| `forms.py` | reading posted forms (`form()`, `one()`), shared by `app.py` and `setup.py` |
| `fleet.py` | the background poller; Needs attention from `ops.attention` |
| `flows.py` | the concierge's browser prompter, the live-terminal bridge, batches |
| `actions.py` | settings, tags, logs, update, reboot and joining the overlay through the shared day-2 operations (`fleetledger/ops.py`), sign-out through `ship-fleet logout`; update, reboot and join run in the background |
| `jobs.py` | the background-job runner: one registry for every long action (update, reboot, join, making a Ship), where checking that a Ship is free and starting a job on it is one step; commands run through `cli.main` itself with their output in the job (cleaned of terminal codes), never on the terminal running `ship-fleet ui`; jobs live in memory, and stopping the UI says which were cut short |
| `snapshots.py` | the Snapshots tab: the list from the driver, and take, roll back and delete as jobs running `ship-fleet snapshot` |
| `templates/_outcome.html` | a driver's answer, for any action that calls one: its messages, the steps a by-hand driver leaves to you (each with Copy), or why it cannot (NotSupported) |
| `network.py` | the Network tab and page over `fleetledger/net.py` (shared with `ship-fleet net`), plus the status `net` digest |
| `dashboard.py` | the Fleet page's summary, alerts (each with its fix) and chart data; the history store is `fleetledger/history.py` |
| `charts.py` | SVG chart geometry (lines, stacked columns, bars, sparklines); `templates/_macros.html` draws it |
| `static/app.css` | the theme: timber, brass and parchment, light ("daylight on deck") and dark ("below decks by lamplight") by `prefers-color-scheme`; textures are `static/grain.svg`, `grain-lamp.svg` and `paper.svg`, fonts the system's own |

Tests: `fleetledger/tests/test_web.py` (the gate's every check, pages, actions,
and real sign-in sessions in a terminal driven through the browser routes),
`test_websetup.py` (the setup page, with real OpenSSH keys, a fake keyring, a
fake Proxmox and a fake Tailscale) and `test_dashboard.py` (the history store,
alerts, summary and charts).

To work on the UI or take screenshots without a fleet, run
`.venv/bin/python fleetledger/tests/demo_ui.py`: six made-up Ships, simulated in
the process, in a throwaway FleetLedger home with a week of made-up history. It
never reads your real `~/.config/fleetledger`, `~/.ssh` or keyring. Four of the
Ships run on `DemoDriver`, a hypervisor kept in memory (VMs and snapshots; every
call succeeds at once), standing in for Proxmox; the other two use the manual
driver, so both kinds of answer can be seen.

The Content-Security-Policy allows no inline styles or scripts, so a template
never uses `style=` or `<script>`: colours and sizes are CSS classes, and chart
geometry is SVG attributes.
