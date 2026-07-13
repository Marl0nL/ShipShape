# auth/ — rotatable credential drop

This directory is mounted **read-only** into the agent container at `/auth`.
The daily-key feature (host control plane) writes short-lived credentials here
and reloads them without rebuilding or restarting the container.

Expected files:

- `gcp-sa.json` — Google service-account / short-lived credential
  (referenced by `GOOGLE_APPLICATION_CREDENTIALS=/auth/gcp-sa.json`).
- `gh-token`    — GitHub token consumed by `gh` (file-based so it can rotate;
  environment variables cannot be refreshed in a running container).

Place your bootstrap `gcp-sa.json` here to start. Do not commit real
credentials — add this directory to `.gitignore` / `.dockerignore`.
