# auth/ — rotatable credential drop

This directory is mounted **read-only** into the agent container at `/auth`.
The daily-key feature (host control plane) writes short-lived credentials here
and reloads them without rebuilding or restarting the container.

Expected files:

- `gcp-sa.json` — Google service-account / short-lived credential
  (referenced by `GOOGLE_APPLICATION_CREDENTIALS=/auth/gcp-sa.json`).
- `gh-token`    — GitHub token consumed by `gh` (file-based so it can rotate;
  environment variables cannot be refreshed in a running container).
- `claude-token` — persistent Claude Code OAuth token from `claude setup-token`
  (run it on your authenticated host machine). On `up`, the control plane injects
  it as `CLAUDE_CODE_OAUTH_TOKEN`, and a baked `/etc/profile.d` snippet re-reads
  this live mount in every login shell so `claude` auto-authenticates. Rotate it
  from the TUI **Credentials** tab or `shipshape claude-token` — no rebuild needed.

Place your bootstrap `gcp-sa.json` here to start. Do not commit real
credentials — add this directory to `.gitignore` / `.dockerignore`.
