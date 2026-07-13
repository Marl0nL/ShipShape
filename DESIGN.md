# ShipShape — Design & Roadmap

## Context

ShipShape sandboxes autonomous coding agents in a container with **controlled
network egress** and **short-lived, operator-governed credentials**. The goal is
not to hide anything from the agent (it can read everything mounted into it) but
to **bound blast radius** and to give the operator a **low-friction control
surface** — because approving every individual action by hand does not scale.

Everything the operator manages collapses into one abstraction: a
**human-in-the-loop request queue**. Denied domains, agent-proposed commands, and
auth refreshes are all just payloads that surface for **approve / edit / decline**.

## Security posture

- The `agent-sandbox` sits ONLY on an `internal: true` docker network — it has
  **no route to the internet**. Its sole egress is the `egress-proxy` (Squid).
  This is **fail-closed**: a tool that ignores the proxy env vars can't reach the
  network at all (rather than silently bypassing controls).
- Squid runs **default-deny** against a domain allow-list; HTTPS is domain-level
  (CONNECT `host:443`, no TLS interception — auditing is by design domain-granular).
- The control plane runs as the **host user** and shells out to `docker`/`gcloud`/
  `gh`. **No docker socket is mounted anywhere.** The only component on the agent's
  network is a **powerless broker** that can do nothing but drop a request file
  into a spool the operator approves.
- Credentials are **files under `/auth`** (mounted read-only into the agent) so
  they can rotate without a restart. Env-var creds are avoided — they can't be
  refreshed in a running container.

## Topology

```
   HOST (operator terminal; authed to docker / gcloud / gh)
 ┌────────────────────────────────────────────────────────────┐
 │  `shipshape` TUI/CLI  ==  the control plane                  │
 │   • Pending queue: domains │ commands │ auth-refresh         │
 │   • Allow-list: live checkboxes (enable/disable a domain)    │
 │   • Approve / Edit / Decline  → acts with the operator creds  │
 │   privileged actions (host subprocess, NO docker.sock):      │
 │     docker exec egress-proxy squid -k reconfigure            │
 │     docker logs -f egress-proxy   (harvest TCP_DENIED)       │
 │     gcloud (mint token) ; run approved commands              │
 │     atomic file drop → ./auth/{gcp-token,gh-token}           │
 └───────────┬───────────────────────────────┬─────────────────┘
   ./control-plane/spool (bind rw)      ./auth  ./egress (bind ro)
 ┌───────────┴──────────┐         ┌───────────┴──────────────────┐
 │  broker container    │         │  agent-sandbox                │
 │  isolated_agent_net  │◄──HTTP──│  isolated_agent_net (no net)  │
 │  powerless relay:    │         │  skills: request-domain,      │
 │  writes spool/req,   │         │  request-command,             │
 │  reads spool/resp    │         │  refresh-daily-auth           │
 └──────────────────────┘         │  gh wrapper + git helper read │
                                   │  /auth/gh-token live          │
                                   └───────────────────────────────┘
```

Why the broker exists and why IPC is a **file spool** (not a socket): the agent
network has no gateway to the host, so the broker cannot reach the host process
over the network. A bind-mounted spool dir is race-free (write-tmp + atomic
rename), survives restarts of either side, is trivially inspectable, and keeps
the spool **out of the agent container** (only the broker mounts it).

## Decisions (locked)

| Fork | Choice | Consequence |
|---|---|---|
| **GCP creds** | Control-plane-managed **SA key**, **on-demand** rotation | ONE file (`/auth/gcp-sa.json`) serves BOTH gcloud and client libraries. Rotated only on Refresh / OTP (no scheduler). A leaked key is valid for the rotation window and "fails open" if the control plane dies before deleting it — bounded by the dedicated SA's tight scope, which is the real control. |
| **GitHub creds** | **Manual 14-day fine-grained PAT**, out of daily-auth | Operator creates/rotates it in the GitHub UI; control plane just consumes the file. No GitHub App. |
| **Run model** | **Single process** (TUI = daemon) | Simplest. Harvester + refresh + spool-watch run **only while the TUI is open** — keep it in tmux for unattended GCP refresh and remote OTP. No standing minter creds. |
| **Command approval** | **Any command, review + edit only** | Max flexibility, highest risk. The TUI shows the exact command, an edit field, and a "runs on host as YOU" warning; nothing auto-runs; every action is audit-logged. |

### GCP: one flow for both gcloud and client libraries (resolved)
The agent uses GCP via **both** gcloud and client libraries. A raw access token
works for gcloud (`auth/access_token_file`) but is **not** a valid ADC format, so
it can't serve client libraries. A **service-account key** is the one artifact
both consume natively — client libraries via `GOOGLE_APPLICATION_CREDENTIALS`,
gcloud via `activate-service-account` (on start + re-activated after rotation).
WIF would avoid a static key but needs a public JWKS endpoint Google can reach
(impractical for a local host), so it's ruled out. The "short-lived" property
comes from **control-plane rotation**, chosen **on-demand** (Refresh button / OTP),
not from a token TTL.

## Corrections folded into the existing files (Phase 0 — done)

These fixed real defects in the first hardening pass:
1. **`container_name: egress-proxy` pinned** — otherwise the container is
   `shipshape-egress-proxy-1` and `docker exec egress-proxy …` (the reload path)
   fails.
2. **Allow-list is now a directory mount** (`./egress` → `/etc/squid/shipshape`).
   A single-file bind mount pins the host inode, so an atomic replace would leave
   Squid reading the stale file after `squid -k reconfigure`.
3. **`GH_TOKEN` env removed** — it would shadow the rotating `/auth/gh-token`.
   Replaced with a `gh` wrapper + git credential helper (in `Dockerfile.agent`)
   that read the token file on every call.
4. Added `.gitignore` / `.dockerignore` so creds and runtime state never get
   committed or baked into the image.

## Phase roadmap

### Phase 1 — Allow-list management + denied-domain harvesting  ✅ built & tested
`control-plane/` Python package (stdlib core + Textual TUI):
- `allowlist.py` — parse/edit/render `egress/allowed_domains.txt`; disabled
  domains kept in place as `#SS-OFF# <domain>`; atomic save + `.bak` rollback.
- `harvester.py` — extract denied domains from the Squid `agent_audit` log
  (filters out port-policy denials so only real domain requests queue).
- `docker_ops.py` — `container_running`, `reconfigure`, `logs` wrappers.
- `state.py` — de-duplicated pending queue (`control-plane/state/pending.json`).
- `cli.py` — `list / enable / disable / add / approve / dismiss / pending / reload / tui`.
- `app.py` — Textual TUI: Pending tab (approve/wildcard/dismiss) + Allow-list tab
  (checkboxes → Apply → hot-reload). **Needs an interactive smoke test.**
- Every mutation atomically writes the file and `squid -k reconfigure`s, rolling
  back if Squid rejects the rules (egress can never silently break).

### Phase 2 — GCP SA-key refresh + credentials panel  ✅ built (mock-tested; live run needs the SA)
- `creds.py`: `refresh_gcp()` (as the operator) mints a fresh key for the
  dedicated agent SA → atomically installs `./auth/gcp-sa.json` (mode 0600) →
  `docker exec agent-sandbox gcloud auth activate-service-account` → **deletes the
  previous key** (last). Tracks the current key id + timestamp in
  `control-plane/state/creds.json`. ≤2 keys ever live.
- **On-demand only** — no scheduler. Triggered by the TUI "Refresh GCP now (g)"
  button, `shipshape refresh-gcp`, or (Phase 3) the OTP remote-refresh.
- In-container consumption (both from one file): client libraries read
  `GOOGLE_APPLICATION_CREDENTIALS=/auth/gcp-sa.json`; gcloud is activated by the
  image entrypoint on start and re-activated by the control plane after rotation.
- Config: `shipshape.toml` `[gcp].agent_sa` (+ `agent_container`), env-overridable.
- TUI **Credentials** tab: SA, current key id, age, "Refresh GCP now".

**Operator one-time GCP setup** (touches the `market-operations` project — run these yourself):
```bash
# 1. Dedicated, scope-limited agent SA (grant only narrow, resource-level roles)
gcloud iam service-accounts create agent-daily --project market-operations \
  --display-name "ShipShape agent (rotating short-lived creds)"

# 2. Let your user create/delete keys for it (least privilege: only on this one SA)
gcloud iam service-accounts add-iam-policy-binding \
  agent-daily@market-operations.iam.gserviceaccount.com \
  --member "user:marlon.leicester@repositpower.com" \
  --role roles/iam.serviceAccountKeyAdmin

# 3. Grant the agent SA the NARROW, resource-level roles the agent actually needs
#    (e.g. objectViewer on one bucket) — never project-wide editor/owner.

# NOTE: if the org enforces constraints/iam.disableServiceAccountKeyCreation,
# key creation is blocked and this approach can't be used — tell me and we'll
# fall back to gcloud-only short-lived tokens or set up WIF.
```

### Phase 3 — Broker + skills + OTP remote refresh + command approval  ✅ built (stdlib e2e-tested; broker/TUI need a live smoke test)
- **Broker container** (`control-plane/broker/`, `python:3.12-slim`, non-root,
  `cap_drop: ALL`, `read_only`, no creds, no published host port). Service name
  `control-plane`; HTTP on the agent network: `POST /request-domain`,
  `/request-command`, `/refresh-auth`, `GET /status/<id>`, `/health`. Each POST
  writes `spool/req/<id>.json` (atomic rename), briefly polls for a fast
  resolution, else returns 202 `pending`.
- **Spool** (`spool.py`) — atomic file-queue IPC; `unprocessed_requests()` are
  those without a response yet. **Host watcher** (`watcher.py`, a background
  thread in the TUI *and* `shipshape watch`) ingests requests: domain → pending
  queue; command → command queue; refresh → OTP-gated GCP refresh. It never runs
  a command or approves a domain on its own.
- **Skills** (`skills/`, baked into the agent image at `~/.claude/skills/`):
  `request-domain`, `request-command`, `refresh-daily-auth` — thin PATH commands
  (`agent-bin/`) that `curl` `http://control-plane:8099`.
- **OTP remote refresh** (`otp.py`): operator registers a **single-use, short-TTL
  (15m), salted-hash** passphrase (TUI `o` / `shipshape otp`), rate-limited (5
  attempts). Agent (driven from phone via Claude remote control) submits it via
  `refresh-daily-auth`; the host validates + runs the Phase 2 mint+inject. Single
  use is what preserves the TTL bound — a compromised agent can't self-renew — and
  the passphrase authorises a **fixed action** (the agent chooses nothing else).
  Works only while the host watcher/TUI is running (single-process model).
- **Command-approval queue** (`commands.py`, `state.CommandStore`): agent proposes
  a command → `command` queue → operator **accepts / edits / declines** (TUI `y`/`n`
  + `shipshape command-*`). Accepted commands run **on the host as the operator**
  and the exit status + output are returned to the agent via the spool. Chosen
  policy: unconstrained shape; the human review is the only gate.

### Image provisioning philosophy (decided)
The base image is **lean** — framework plumbing only (proxy env, gcloud/gh, git,
python, the auth glue, the control-plane client commands + skills). **Dev stacks
are installed at runtime**, so one container serves a Python day and a Flutter day
without over-provisioning. Flutter is **no longer baked in**; agents run the
`install-flutter` / `install-adb` skills (user-space, no root) on demand. Runtime
installs work because their download domains are on the egress allow-list (and
`request-domain` covers anything missing).

### Phase 4 — Android adb relay  ✅ built (toggle-able; live smoke test + Tier-2 pending)
Host runs the adb *server*; the sandbox reaches it through a **toggle-able `socat`
relay sidecar** (`docker-compose.yml` service `adb-relay`, `profiles: ["adb"]`,
OFF by default). The agent keeps `ADB_SERVER_SOCKET=tcp:adb-relay:5037` set and
fails closed when the relay is down. The relay is the only thing dual-homed to the
host (`adb_host_net` + `host.docker.internal:host-gateway`); a container doesn't
route between its networks, so the agent gains exactly one host port — not LAN or
internet. Toggle: `docker compose --profile adb up -d adb-relay` / `stop`.
- **USB & Wi-Fi both** handled on the host (`adb devices` / `adb connect` / `adb
  pair`); the container just uses the host server. USB passthrough was rejected
  (needs privileges that break the hardening; nothing for Wi-Fi).
- **Tiers:** device mgmt / install / logcat / shell / builds work through the
  remote server. **Hot reload / Dart VM Service** needs extra port bridging (the
  `adb forward` listens on the *host* loopback, Flutter dials the *container*
  loopback) — `--host-vmservice-port` + relay that port + a container loopback
  shim; **needs an empirical smoke test** on the installed adb version.
- **Security:** while the relay is up, any container process can fully drive the
  connected device (`adb shell` = arbitrary commands on the phone). Toggle off when
  not debugging.

### Phase 5 — Init / provision wizard  ✅ built (CLI tested; TUI tab needs a smoke test)
- `components.py`: a **component manifest** (`REGISTRY` + `[components.*]` in
  shipshape.toml) mapping each dev stack to an install command (a PATH command in
  the agent image) + the egress domains it needs. Builtins: `flutter`, `adb`.
- `provision(name)`: enables the component's domains (recording only the ones it
  *newly added*) then runs the installer via `docker exec`. `deprovision(name)`
  disables exactly those added domains — baseline/shared domains are never touched
  — without uninstalling.
- CLI: `shipshape components [--probe]`, `provision <name>`, `deprovision <name>`.
- TUI **Provision** tab: check the stacks you want → Apply provisions the checked
  and deprovisions the unchecked (the init wizard).
- Refactor: the allow-list apply/reload (with rollback) moved to `egress.apply`,
  shared by the CLI, TUI, and provisioner.

### Phase 6 — firstmate-ready image, snapshots, quick-start  ✅ built (agent image needs a live build/smoke-test)
- **Image bake (`Dockerfile.agent`)**: tmux, jq, node, gh, gcloud + the **Claude Code
  CLI** (honors HTTP(S)_PROXY), **herdr** (firstmate's backend), the **Antigravity CLI**
  (`agy`, best-effort), and a clone of the user's firstmate fork
  (`github.com/Marl0nL/firstmate`) at `~/firstmate` (defaulted to `backend=herdr` /
  `crew-harness=claude`), plus firstmate's bootstrap toolchain (treehouse,
  no-mistakes, *-axi). Peripheral installers are tolerant (`|| WARN`) so one flaky
  upstream can't fail the build. Project dev stacks (Flutter/adb) stay runtime-installed.
- **Allow-list** adds `api.anthropic.com` / `claude.ai` / `platform.claude.com` /
  `downloads.claude.ai`, the Antigravity domains, and `registry.npmjs.org`.
- **Snapshot & relaunch (`images.py`)**: `shipshape snapshot <tag>` docker-commits the
  running agent container to `shipshape-agent:<tag>`; `images` lists them; `use <tag>`
  sets the active tag; `up` boots it (compose `image: ${SHIPSHAPE_AGENT_IMAGE:-shipshape-agent:base}`).
  TUI **Images** tab mirrors this. This is how runtime-installed state is persisted.
- **Quick-start firstmate (`firstmate.py`)**: boot the active image → copy the host's
  `~/.claude/.credentials.json` in (chowned) with `CLAUDE_CODE_OAUTH_TOKEN` as fallback
  → open a live `claude` session in `~/firstmate` in a new terminal window. CLI
  `quickstart`; TUI button / `f`.
- **In-container shell**: `shipshape shell [container]` and a TUI **Shell (t)** button
  that opens `docker exec -it` in a new gnome-terminal window (`$SHIPSHAPE_TERMINAL`
  overrides).
- **Auto-refresh**: the TUI reloads queues/creds every 5s (+ a header heartbeat) and on
  every action, so live changes are visible without pressing `r`.
- **firstmate program vs data**: the fork is baked at `~/firstmate`; the persistent mount
  moved to `~/work` so it doesn't shadow the clone. firstmate's own state is ephemeral per
  container — snapshot to persist a configured image.

## Known limitations / deferred (from the code review)

Fixed: reprocess-loop + path-traversal via spool id, cross-thread store
clobbering, command double-run, GCP failed-delete accounting + activation check +
concurrency lock, harvester crash on bad port, sentinel-comment promotion, broker
body cap, command-display sanitization, SSRF `deny to_localhost`, `.env` in
`.dockerignore`, `pip --user`, egress-proxy resource limits, **all images pinned
by digest**, **configurable `HOST_UID` build arg** (key readability), **empty-all
placeholder** (full lock-down without Squid rejecting an empty ACL), **broker
pending-count cap**, **OTP hashed with PBKDF2** (200k iters).

Remaining deferred (documented, low priority):
- **Cross-process lock** — running the TUI and `shipshape watch` at once can race
  the OTP/store read-modify-writes (stores reload-before-mutate, but there's no
  cross-process lock). Run one or the other. `creds` refreshes are process-locked.
- **OTP passphrase in argv** — `refresh-daily-auth <passphrase>` puts it in the
  container's `/proc`; acceptable for a single-tenant sandbox.
- **Healthcheck ordering** — `depends_on` doesn't wait for Squid readiness (needs
  a health probe verified against the pinned image).
- **Broad shared-host allows** (`.googleapis.com`) are an exfil surface inherent
  to domain-granular filtering — operator awareness, not a bug.
- **Agent image is unbuilt here** — `Dockerfile.agent` bakes many external installers
  (claude.ai, herdr.dev, antigravity.google, treehouse/no-mistakes, npm *-axi) that can
  only be validated by a live `docker compose build`. herdr's Linux install and the `agy`
  binary especially need a smoke test; reproducible pinning of those is future work.
- **Antigravity (`agy`) may not work behind Squid** — documented history of ignoring
  HTTP(S)_PROXY on Linux (gRPC dialers bypass it), background self-update, and keyring
  token storage the container lacks. Baked best-effort; verify end-to-end before relying on it.
- **Claude credentials in the sandbox** — quick-start copies the operator's Claude OAuth
  (and passes `CLAUDE_CODE_OAUTH_TOKEN`), so the autonomous agent runs as the operator's
  Claude account. Accepted tradeoff for convenience.
- **Editing `squid.conf`** (not the allow-list) needs `docker compose up -d
  --force-recreate egress-proxy` — the single-file mount pins the inode, so
  `squid -k reconfigure` alone won't pick up a rewrite. The dynamic allow-list
  (`./egress`, dir-mounted) reloads live, so this only affects rare `squid.conf`
  edits. Squid also prints two benign warnings on reconfigure (an ACL
  self-coverage quirk, and `via off`); both are intentional and non-fatal.

## Verification

Stdlib logic is covered by scratch tests (all passing): allow-list
parse/render/toggle/add round-trips + harvester + pending store (24 checks); GCP
key rotation with a mock gcloud (12); Phase 3 spool/OTP/watcher/command execution
end-to-end with stubbed creds (21). `docker compose config` validates the compose
(incl. the `adb` profile gating).

Still needs live passes (not runnable without the running stack / real SA):
- **Build + up:** `docker compose up -d` (lean image now — no Flutter clone).
- **Allow-list:** after an agent hits a blocked domain, `shipshape pending --scan 5000`
  → `shipshape approve <host>` → confirm the agent can reach it.
- **GCP:** create the agent SA (see above), `shipshape refresh-gcp`, confirm the
  container's gcloud + a client-library call both work; rotate again, confirm the
  old key is deleted.
- **Broker/skills:** from the agent, `request-domain …`, `request-command "…"`,
  and `refresh-daily-auth <otp>` (after `shipshape otp`); confirm they surface in
  `shipshape`/`shipshape commands` and resolve via the spool.
- **Install/TUI:** `./install.sh` (pipx; re-run to update, `--editable` for live dev),
  then `shipshape` — smoke-test the Stack / Pending / Allow-list / Commands /
  Credentials / Provision / Images tabs.
- **adb (Phase 4):** `docker compose --profile adb up -d adb-relay`, host
  `adb -a nodaemon server start`, then in-container `install-adb` + `adb devices`.
