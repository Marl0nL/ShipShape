<h1 align="center">ShipShape</h1>

<p align="center"><em>Run autonomous coding agents in a locked-down container — with a one-window control plane.</em></p>

ShipShape boots a coding agent (Claude Code / [firstmate](https://github.com/Marl0nL/firstmate))
inside a Docker sandbox that has **no route to the internet** except through a
default-deny egress proxy you control. A host-side TUI/CLI (`shipshape`) is the
bridge: you approve the domains the agent may reach, hand it short-lived
credentials, review the commands it wants run, and watch everything happen from
one dashboard.

The agent gets a capable workspace; you keep the keys.

---

## How it's isolated

```
                 ┌─────────────────────────────────────────────┐
                 │  host (you)                                  │
                 │                                              │
                 │   shipshape  ──approve/deny──┐               │
                 │   (TUI/CLI, runs as you)     │ file spool    │
                 │        │                     ▼               │
   ┌─────────────┼────────┼──────────────┐  ┌───────────────┐  │
   │ isolated_agent_net (internal: true) │  │ control-plane │  │
   │        │                            │  │ (broker; no   │  │
   │  ┌───────────────┐    ┌──────────┐  │  │ creds/host)   │  │
   │  │ agent-sandbox │───▶│  egress- │──┼──┼──▶ internet    │  │
   │  │ (firstmate,   │    │  proxy   │  │  │  (allow-list)  │  │
   │  │  claude, …)   │    │ (Squid)  │  │  └───────────────┘  │
   │  └───────────────┘    └──────────┘  │                     │
   │      no direct internet             │                     │
   └─────────────────────────────────────┘                     │
                 └─────────────────────────────────────────────┘
```

- **agent-sandbox** lives only on an `internal: true` network — its sole path out
  is the Squid **egress-proxy**, which enforces a default-deny domain allow-list.
  A tool that ignores the proxy env vars simply cannot reach the network
  (fail-closed).
- **control-plane** is a powerless in-container broker: the agent can only drop
  request files into a shared spool that the host `shipshape` process approves.
- Credentials are **file-based and rotatable** (`auth/`, mounted read-only) so they
  refresh without a rebuild. Nothing mounts the Docker socket.

## Requirements

- Docker + Docker Compose, Python 3.11+, `pipx`
- Optional per-feature: `gcloud` (GCP key rotation), `gh` (GitHub), a terminal
  emulator for the pop-out shells (`gnome-terminal`/`konsole`/… or set
  `$SHIPSHAPE_TERMINAL`).

## Quick start

```bash
./install.sh                 # installs the `shipshape` TUI/CLI via pipx
shipshape                    # first launch runs a one-time setup wizard, then the TUI
```

The **first-launch wizard** sets your firstmate source repo, egress bandwidth cap,
and baseline allow-list. After that, from the **Dashboard**:

- **Boot (u)** brings the stack up (first run builds the agent image).
- **Quick-start firstmate (f)** boots the stack, wires Claude auth, and opens the
  firstmate primary inside a [herdr](https://herdr.dev) workspace — already kicked
  off so the agent starts orienting itself immediately.

Then just talk to firstmate. When it asks to reach a new domain or run a host
command, the request surfaces in the Dashboard's **Actionables** for you to
approve, edit, or decline.

## The TUI

- **Dashboard** (default) — service status dots, quick Shell/Shutdown, an
  **Actionables** queue (pending network requests, credential issues, agent
  commands), and a live event feed from the proxy + spool. New requests ping and
  flash the window (mute with `m`).
- **Stack** — boot / shut down / firstmate quick-start / user + root shells.
- **Settings** — firstmate repo, egress bandwidth, and the **Allow-list** and
  **Provision** (dev-stack installer) sub-sections.
- **Images** — snapshot / use / rename / delete / rebuild the agent image.
- **Credentials** — GCP key rotation, remote-refresh passphrase, and the
  persistent Claude token.

Everything is scriptable too — `shipshape <command>` (see `shipshape --help`).

## Credentials

| Consumer | Mechanism |
|---|---|
| **Claude Code** | `auth/claude-token` from `claude setup-token` (long-lived). Injected as `CLAUDE_CODE_OAUTH_TOKEN` and synthesized into a credentials file on login. Set/rotate from the Credentials tab or `shipshape claude-token`. |
| **Google Cloud** | One dedicated, scope-limited SA key minted on demand (`shipshape refresh-gcp`), serving both `gcloud` and client libraries from `/auth/gcp-sa.json`. |
| **GitHub** | A fine-grained PAT in `auth/gh-token`, read live by a `gh` wrapper + git credential helper. |

Real secrets live only under `auth/` (git-ignored) — never in the image build.

## Egress allow-list

One domain per line in `egress/allowed_domains.txt` (a leading dot matches
subdomains). The control plane edits it atomically and hot-reloads Squid
(`squid -k reconfigure`) on every change, rolling back if Squid rejects the rules.
A disabled domain is kept in place as `#SS-OFF# <domain>` rather than deleted.

## Docs

- [`docs/DESIGN.md`](docs/DESIGN.md) — full architecture, the phase-by-phase build
  log, the security model, and known limitations.
- [`control-plane/README.md`](control-plane/README.md) — control-plane specifics.
- [`auth/README.md`](auth/README.md) — the credential drop.
