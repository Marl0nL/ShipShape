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
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import docker_ops
from .config import Config, Paths

_LOCK = threading.Lock()  # serialise concurrent refreshes (TUI button vs OTP watcher)


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
    with _LOCK:
        paths.auth.mkdir(parents=True, exist_ok=True)
        key_path = paths.auth / "gcp-sa.json"
        sa = cfg.agent_sa
        state = _load_state(paths)
        prev = state.get("key_id")
        pending = [k for k in state.get("pending_delete", []) if k]  # keys still to delete

        # 1. mint to a UNIQUE temp path (create the name, remove the empty file so
        #    gcloud will write it; under _LOCK there is no race on the name).
        fd, tmpname = tempfile.mkstemp(prefix=".gcp-sa.", suffix=".json", dir=str(paths.auth))
        os.close(fd)
        tmp = Path(tmpname)
        _safe_unlink(tmp)
        r = run(["gcloud", "iam", "service-accounts", "keys", "create", str(tmp),
                 "--iam-account", sa, "--quiet"], timeout=90)
        if not r.ok:
            _safe_unlink(tmp)
            return RefreshResult(False, f"key create failed:\n{r.output}")

        # 2. read the new key id
        try:
            new_id = json.loads(tmp.read_text())["private_key_id"]
        except (json.JSONDecodeError, OSError, KeyError) as e:
            _safe_unlink(tmp)
            return RefreshResult(False, f"could not parse minted key: {e}")

        # 3. install atomically (wrap: a failure here must not silently leak the key)
        try:
            os.chmod(tmp, 0o600)
            os.replace(tmp, key_path)
        except OSError as e:
            _safe_unlink(tmp)
            pending.append(new_id)  # minted remotely but not installed -> clean up later
            _save_state(paths, {**state, "pending_delete": pending})
            return RefreshResult(False, f"failed to install key: {e}")

        # 4. record the NEW key and queue the previous one for deletion BEFORE deleting
        #    anything, so a crash can never strand an untracked (undeletable) key.
        if prev and prev != new_id:
            pending.append(prev)
        _save_state(paths, {"key_id": new_id, "created": time.time(), "sa": sa,
                            "pending_delete": pending})

        # 5. re-activate gcloud in-container; if that FAILS, keep the old key (don't
        #    delete) so the CLI still has a working credential.
        if is_running(cfg.agent_container):
            ar = run(["docker", "exec", cfg.agent_container, "gcloud", "auth",
                      "activate-service-account", "--key-file", "/auth/gcp-sa.json"], timeout=60)
            if not ar.ok:
                return RefreshResult(
                    True,
                    f"key installed (new {new_id[:12]}…) but in-container gcloud "
                    f"activation failed — previous key kept:\n{ar.output}",
                    new_id,
                )

        # 6. delete queued old keys (best-effort; keep any failures to retry next time)
        still = []
        for kid in pending:
            if not run(["gcloud", "iam", "service-accounts", "keys", "delete", kid,
                        "--iam-account", sa, "--quiet"], timeout=60).ok:
                still.append(kid)
        latest = _load_state(paths)
        latest["pending_delete"] = still
        _save_state(paths, latest)
        return RefreshResult(
            True,
            f"rotated GCP key for {sa}: new {new_id[:12]}…, deleted {len(pending) - len(still)}"
            + (f", {len(still)} pending retry" if still else ""),
            new_id,
        )


def status(paths: Paths) -> dict:
    return _load_state(paths)
