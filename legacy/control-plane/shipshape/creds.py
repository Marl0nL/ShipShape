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


# --- Claude Code OAuth token (persistent; from `claude setup-token` on the host) ---
#
# Unlike the GCP key, this isn't minted by the control plane — the operator runs
# `claude setup-token` on their authenticated host and drops the result here. It's
# file-based (auth/claude-token) so it rotates without a rebuild: the stack injects
# it as CLAUDE_CODE_OAUTH_TOKEN on boot, and a baked /etc/profile.d snippet re-reads
# the live mount in every login shell (so a rotation reaches new sessions at once).

CLAUDE_TOKEN_FILE = "claude-token"


def _claude_token_path(paths: Paths) -> Path:
    return paths.auth / CLAUDE_TOKEN_FILE


def claude_token(paths: Paths) -> str:
    """The persistent Claude OAuth token from auth/claude-token, or '' if absent."""
    try:
        return _claude_token_path(paths).read_text().strip()
    except OSError:
        return ""


def push_token_to_container(paths: Paths, container: str | None = None) -> bool:
    """Best-effort: overwrite the RUNNING container's ~/.claude/.credentials.json from the
    current /auth/claude-token, so a new/rotated token (e.g. a different account) takes
    effect immediately — even on snapshot images whose baked login script won't replace an
    existing creds file (the cause of a stale-token 401). Returns True if it ran."""
    if not claude_token(paths):
        return False
    if container is None:
        from . import config

        try:
            container = config.active_config().agent_container
        except Exception:
            return False
    if not docker_ops.container_running(container):
        return False
    exp = int((time.time() + 31536000) * 1000)  # ~1y out, in ms
    script = (
        "mkdir -p ~/.claude && "
        "printf '{\"claudeAiOauth\":{\"accessToken\":\"%s\",\"expiresAt\":%s,"
        "\"scopes\":[\"user:inference\",\"user:profile\"]}}' "
        f'"$(cat /auth/claude-token)" {exp} > ~/.claude/.credentials.json && '
        "chmod 600 ~/.claude/.credentials.json"
    )
    return docker_ops.run(["docker", "exec", container, "bash", "-lc", script], timeout=15).ok


def set_claude_token(paths: Paths, token: str) -> RefreshResult:
    """Write/rotate the Claude OAuth token to auth/claude-token (0600, atomic), and push it
    into the running container so it takes effect live (no recreate needed)."""
    token = (token or "").strip()
    if not token:
        return RefreshResult(
            False, "empty token — run `claude setup-token` on your authed host and paste the output"
        )
    paths.auth.mkdir(parents=True, exist_ok=True)
    f = _claude_token_path(paths)
    tmp = f.with_name(f.name + ".tmp")
    try:
        tmp.write_text(token + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, f)
    except OSError as e:
        _safe_unlink(tmp)
        return RefreshResult(False, f"could not write {f}: {e}")
    pushed = push_token_to_container(paths)
    live = " and pushed into the running container" if pushed else ""
    return RefreshResult(
        True,
        f"Claude token saved ({len(token)} chars){live}. New firstmate sessions use it "
        "immediately.",
    )


def claude_token_status(paths: Paths) -> dict:
    """Presence/metadata for the Credentials screen — never returns the token itself."""
    f = _claude_token_path(paths)
    if not f.is_file():
        return {}
    try:
        tok = f.read_text().strip()
        mtime = f.stat().st_mtime
    except OSError:
        return {}
    return {"present": bool(tok), "chars": len(tok), "mtime": mtime}
