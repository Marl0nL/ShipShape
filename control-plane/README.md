# ShipShape control plane

Host-side management for the ShipShape agent sandbox. Runs as **you** on the
host (it shells out to `docker`, `gcloud`, `gh` — no docker socket is mounted
anywhere). See `../DESIGN.md` for the full architecture and phase roadmap.

## Install

```bash
pipx install ./control-plane      # pulls Textual for the TUI
# or, for development:
pip install -e ./control-plane
```

## Use

```bash
shipshape                 # launch the TUI (Pending + Allow-list tabs)
shipshape list            # show the egress allow-list
shipshape add pub.dev     # add + hot-reload Squid
shipshape disable .gcr.io # turn a domain off for now (kept, not deleted)
shipshape enable .gcr.io  # turn it back on
shipshape pending --scan 5000   # harvest recent denials, show the queue
shipshape approve api.foo.com   # allow a denied domain + drop it from pending
```

Every change edits `../egress/allowed_domains.txt` atomically and runs
`docker exec egress-proxy squid -k reconfigure`, rolling back if Squid rejects
the new rules. The file stays hand-editable; a disabled domain is kept in place
as `#SS-OFF# <domain>`.

The control plane finds the repo via `$SHIPSHAPE_ROOT`, else by walking up from
the current directory to the folder containing `docker-compose.yml` + `egress/`.

## Status

- **Phase 1 (this):** allow-list management + live denied-domain harvesting.
- **Phase 2:** GCP short-lived token refresh + credentials panel.
- **Phase 3:** in-container broker + `refresh-daily-auth` skill + one-time-passphrase
  remote refresh + the agent command-approval queue.
