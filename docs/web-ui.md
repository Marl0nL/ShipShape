# The web UI (`ship-fleet ui`)

FleetLedger's dashboard and login concierge, in your browser, on your own
computer only. The CLI stays complete: everything the UI does, a `ship-fleet`
command does too, and the UI runs the same code.

```sh
ship-fleet ui                 # opens your browser; Ctrl-C to stop
ship-fleet ui --no-browser    # prints the link instead
ship-fleet ui --port 8765     # a fixed port (default: any free one)
```

The link works once, for 15 minutes. Press Enter in the terminal running
`ship-fleet ui` for a new one (for a second browser, or after a restart of the
browser). Stopping the UI ends every session and cancels any sign-in still open.

## What it shows

| Page | What is on it |
|---|---|
| **Fleet** (`/`) | Needs attention, then one row per Ship: state, agents, each sign-in, CPU · memory · disk, notes. Filter by tag. Batch actions for every Ship (or every Ship with the selected tag): sign in to a service; update and reboot once `ship-fleet update` and `reboot` exist. |
| **Ship** (`/ships/<name>`) | Status, Needs attention, sign-ins with the account each tag expects (sign in, re-sign, sign out), agents grouped by herdr workspace with their state and mode, herdr's last resume, Claude Code, host and tooling. |
| Ship **Settings** | Remote Control, default mode, bypass, update channel, agent sudo, LAN guard (`ship-ctl set`, like `ship-fleet set`), and tags. |
| Ship **Logs** | `ship-ctl logs` for one source. |
| Ship **Network** | Phase 3's visibility summary (traffic totals and first-seen destinations) from the status document's `net` field. Until a Ship's collector reports, it says "not available". |
| **Sign-ins** (`/signin`) | The concierge: start a sign-in for one Ship, or for every Ship with a tag; each sign-in's link, code box and countdown; results. |
| **Audit log** (`/audit`) | Who, what and when for every sign-in and sign-out. |

**Needs attention** lists, most urgent first: a Ship presenting the wrong host
key or claiming another identity; wrong-account sign-ins; waiting agents;
missing or expired sign-ins; failed provisioning, units or tools; unreachable
Ships; expiring sign-ins; tag mismatches; pending reboots; Ships behind on
tooling; tools waiting for a sign-in; and any sign-in in the browser that is
waiting for you. Sign-in items have a one-click **Sign in** button. Unknown
attention codes from newer Ships are shown as they are.

The status of every Ship is checked over SSH every 30 seconds (`--interval`)
while a page is open, and not at all once nobody has looked for two minutes.
Pages read the last result and never wait on SSH, so a waiting agent appears
within a poll. Before the first check finishes, the last good status (as
`ship-fleet ls --offline` shows it) is shown.

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
  after 15 minutes. To open the browser, FleetLedger writes a private (0600)
  local page that refreshes to the link, because a browser's command line is
  visible to every process on the computer; the page is deleted on exit.
- **DNS rebinding.** Every request must carry `Host: 127.0.0.1:<port>` or
  `localhost:<port>`; anything else gets 400.
- **CSRF.** Every change (any method but GET and HEAD) needs the session's CSRF
  token (form field or `X-CSRF-Token` header) and an `Origin` that names this
  server exactly. Only small plain form posts are accepted (64 KiB).
- **The live terminal's WebSocket** needs the session cookie and an exact
  `Origin`; its first message must carry the CSRF token.
- **Content-Security-Policy** on every response: scripts, styles and connections
  from this server only, no inline code, no framing; `no-store` caching.
  `Referrer-Policy: same-origin` (not `no-referrer`, which makes browsers send
  `Origin: null` on form posts). htmx is vendored (`web/static/`), so no page
  loads anything from the network.
- **Text from Ships is text.** Everything a Ship reports is HTML-escaped. A
  sign-in link is shown as a link only if it is a plain `https://` address.
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
| `server.py` | `ship-fleet ui`: binding, the link, uvicorn (access log off) |
| `app.py` | routes and pages |
| `fleet.py` | the background poller and the Needs attention ranking |
| `flows.py` | the concierge's browser prompter, the live-terminal bridge, batches |
| `actions.py` | settings, tags, sign-out, logs, update, reboot: through the shared operations module when installed, else the `ship-fleet` command functions |
| `network.py` | the Network tab's reading of the status `net` field |

Tests: `fleetledger/tests/test_web.py` (the gate's every check, pages, actions,
and real sign-in sessions in a terminal driven through the browser routes).
