# Multi-instance ShipShape — plan

Run several independent ShipShape stacks (container + image + proxy + broker +
control-plane) on one host at once — e.g. a **company** instance and a **personal**
instance, each with its own credentials/Claude account, allow-list, image, and
control-plane window.

Status: **planned; implementation in progress.** No agent-side behaviour changes.

## Why it's tractable
- **No host ports are published** — Squid (3128) and the broker (8099) are reachable
  only on each stack's internal network, so there are zero host-port conflicts.
- **The agent reaches the proxy/broker by compose *service* name** (`http://egress-proxy:3128`,
  `http://control-plane:8099`), which resolves inside each compose project's own network
  regardless of the project prefix. So the **agent-side config needs no changes** — this
  is purely a host-side "resolve names/paths per instance" refactor.
- `image:` and `FIRSTMATE_REPO` are already `${VAR}`-parameterised; compose **auto-prefixes
  networks and named volumes with the project name**, so those isolate for free.

## Decisions (confirmed)
1. **One repo, N named instances** under `instances/<name>/` (not multiple clones).
2. **Parameterise `container_name`** per instance via env (`<name>-agent-sandbox`, …),
   defaults preserved for a bare `docker compose up`.
3. **Migrate** an existing single-stack install into `instances/default/`.
4. **Separate image per instance** (`shipshape-agent-<name>:base`), **plus** a
   `fork-image` convenience to copy/fork one instance's image as another's starting point.
5. **Per-instance config, git-ignored, seeded from the git-tracked `*.example` defaults**
   every time a new instance is created.

## Decisions (defaults chosen; flagged for veto)
- **Selection precedence**: `--instance <name>` > `$SHIPSHAPE_INSTANCE` > `default`.
- **Name validation**: `^[a-z0-9][a-z0-9-]{0,30}$` (safe for container/project/image names).
- **Migration**: stop old stack → move data dirs → retag image → back up → idempotent →
  never destructive on failure. `agy-data` volume: best-effort copy, else re-`agy-login`.
- **`fork-image`** retags across instance image namespaces; warns that baked state carries over.
- **adb-relay**: two relays both forward to the host adb server harmlessly (per-project
  network) — left as-is; enable adb on whichever instance needs it.

## Instance layout
```
<repo>/
  docker-compose.yml            # parameterised, ONE file, invoked per instance
  squid.conf.example            # tracked template (bandwidth default)
  egress/allowed_domains.example.txt   # tracked template (defaults)
  shipshape.toml.example               # tracked template (placeholder SA)
  control-plane/                # shared code + broker build context
  instances/                    # gitignored — all per-instance data lives here
    default/
      auth/{gcp-sa.json,gh-token,claude-token}
      state/{pending,commands,creds,otp,settings,active_image}
      spool/{req,resp}
      egress/allowed_domains.txt
      squid.conf
      shipshape.toml
      firstmate-data/
    company/ …
    personal/ …
```
- `instances/` is git-ignored in full. `new-instance <name>` scaffolds a dir from the
  tracked `*.example` + `squid.conf.example` templates.
- Docker `agy-data` volume → auto-prefixed `shipshape-<name>_agy-data` (isolated per instance).

## Naming scheme (per instance `<name>`)
| Thing | Value |
|---|---|
| Compose project | `COMPOSE_PROJECT_NAME=shipshape-<name>` |
| agent container | `<name>-agent-sandbox` |
| proxy container | `<name>-egress-proxy` |
| broker container | `<name>-control-plane` |
| adb container | `<name>-adb-relay` |
| base image | `shipshape-agent-<name>:base` |
| snapshots | `shipshape-agent-<name>:<tag>` |
| networks / volumes | auto-prefixed `shipshape-<name>_*` |

Compose stays a single file; the control plane sets these env vars on every `compose()`
call: `COMPOSE_PROJECT_NAME`, `SS_AGENT_CONTAINER`, `SS_PROXY_CONTAINER`,
`SS_BROKER_CONTAINER`, `SS_ADB_CONTAINER`, `SS_INSTANCE_DIR` (absolute), `SHIPSHAPE_AGENT_IMAGE`,
`FIRSTMATE_REPO`, `HOST_UID`. Mounts become `${SS_INSTANCE_DIR}/auth`, `…/egress`,
`…/squid.conf`, `…/spool`, `…/firstmate-data`. `container_name:` becomes `${SS_*:-<old default>}`.

## Migration (install.sh + a code guard)
Runs once when an old-layout install is detected (root-level `auth/` with real files,
`control-plane/state`, `control-plane/spool`, live `egress/allowed_domains.txt`, live
`shipshape.toml`, `firstmate-data/`) and `instances/default/` does not yet exist:
1. Back up the root data (tar under `instances/.backup-<ts>/`).
2. `docker compose down` the OLD (unprefixed) stack so the old-named containers don't linger.
3. Move the data dirs into `instances/default/`.
4. Retag images: `docker tag shipshape-agent:base shipshape-agent-default:base` (+ snapshots).
5. Best-effort copy the old `agy-data` volume → `shipshape-default_agy-data` (else note re-login).
6. Idempotent: if `instances/default/` exists, do nothing. Never delete source on any failure.

`Paths.ensure()` also seeds a **new/empty** instance dir from the templates (so instances
created outside install.sh still work).

## CLI/TUI surface
- `shipshape --instance <name> <cmd>` (env `$SHIPSHAPE_INSTANCE`; default `default`).
- `shipshape instances` — list instances + running state.
- `shipshape new-instance <name>` — scaffold `instances/<name>/` from templates.
- `shipshape --instance <name> fork-image --from <other> [--tag <src>]` — retag image.
- TUI header shows the active instance; dashboard dots / event feed / pings scope to that
  instance's containers.

## Work breakdown
- **P1 — instance plumbing** (Config/Paths instance-aware; derive the four container names;
  name validation; selection precedence).
- **P2 — docker_ops + compose** (project + env per call; drop the `PROXY` constant; thread
  the instance's proxy/agent names through exec/reconfigure/logs/running/commit/open_shell).
- **P3 — images per instance** (namespace, active-image, build args, `fork-image`).
- **P4 — CLI/TUI** (`--instance`, `instances`, `new-instance`; header + scoping).
- **P5 — scaffolding + templates + docs** (`squid.conf.example`; `.gitignore instances/`;
  seeding; docs).
- **P6 — install.sh migration + testing** (migrate an existing install; boot company +
  personal simultaneously; verify full isolation + single-default back-compat).

## Risks
- **Wrong-instance exec** — centralise name derivation in `Config`; never exec a bare name.
- **Migrating secrets** — back up, verify, never delete on failure.
- **Per-instance `squid.conf`** keeps the single-file-mount inode trap (bandwidth →
  `--force-recreate`).
- **Disk** — each instance builds/tags its own ~1.8 GB image (build layers are shared/cached).

## Unchanged
Agent-side env, spool/broker protocol, egress model, credential mechanisms, and the full
TUI feature set.
