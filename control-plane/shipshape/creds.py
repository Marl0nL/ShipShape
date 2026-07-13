"""GCP credential refresh (Phase 2).

One flow that serves BOTH consumers from a single file:
  * client libraries  -> GOOGLE_APPLICATION_CREDENTIALS=/auth/gcp-sa.json (ADC)
  * gcloud CLI        -> `gcloud auth activate-service-account --key-file=...`
                         (run on container start + re-run after each rotation)

Rotation is ON-DEMAND: the TUI "Refresh" button, the `refresh-gcp` CLI command,
or (Phase 3) the OTP remote-refresh. No background scheduler.

Each refresh: mint a fresh key for the dedicated agent SA -> inject atomically
-> re-activate gcloud in-container -> delete the PREVIOUS key (last, so nothing
breaks mid-rotation). At most ~2 keys are ever live (10-key/SA cap is a non-issue).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import docker_ops
from .config import Config, Paths


@dataclass
class RefreshResult:
    ok: bool
    message: str
    key_id: str | None = None


def _state_file(paths: Paths) -> Path:
    return paths.state / "creds.json"


def _load_state(paths: Paths) -> dict:
    f = _state_file(paths)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(paths: Paths, data: dict) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    f = _state_file(paths)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(f)


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def refresh_gcp(
    paths: Paths,
    cfg: Config,
    run=docker_ops.run,
    is_running=docker_ops.container_running,
) -> RefreshResult:
    if not cfg.agent_sa:
        return RefreshResult(
            False,
            "no agent SA configured — set [gcp].agent_sa in shipshape.toml "
            "or $SHIPSHAPE_AGENT_SA",
        )
    paths.auth.mkdir(parents=True, exist_ok=True)
    key_path = paths.auth / "gcp-sa.json"
    prev = _load_state(paths).get("key_id")

    # 1. mint a fresh key to a transient file in the auth dir (same fs -> atomic move)
    tmp = paths.auth / ".gcp-sa.new.json"
    _safe_unlink(tmp)
    r = run(
        ["gcloud", "iam", "service-accounts", "keys", "create", str(tmp),
         "--iam-account", cfg.agent_sa, "--quiet"],
        timeout=90,
    )
    if not r.ok:
        _safe_unlink(tmp)
        return RefreshResult(False, f"key create failed:\n{r.output}")

    # 2. read the new key id
    try:
        new_id = json.loads(tmp.read_text())["private_key_id"]
    except (json.JSONDecodeError, OSError, KeyError) as e:
        _safe_unlink(tmp)
        return RefreshResult(False, f"could not parse minted key: {e}")

    # 3. install atomically, locked down
    os.chmod(tmp, 0o600)
    os.replace(tmp, key_path)

    # 4. re-activate gcloud inside the container so the CLI uses the new key too
    if is_running(cfg.agent_container):
        run(["docker", "exec", cfg.agent_container, "gcloud", "auth",
             "activate-service-account", "--key-file", "/auth/gcp-sa.json"], timeout=60)

    # 5. delete the previous key LAST
    deleted = "none"
    if prev and prev != new_id:
        dr = run(["gcloud", "iam", "service-accounts", "keys", "delete", prev,
                  "--iam-account", cfg.agent_sa, "--quiet"], timeout=60)
        deleted = prev[:12] + ("…" if dr.ok else " (delete FAILED)")

    _save_state(paths, {"key_id": new_id, "created": time.time(), "sa": cfg.agent_sa})
    return RefreshResult(
        True,
        f"rotated GCP key for {cfg.agent_sa}: new {new_id[:12]}…, deleted {deleted}",
        new_id,
    )


def status(paths: Paths) -> dict:
    return _load_state(paths)
