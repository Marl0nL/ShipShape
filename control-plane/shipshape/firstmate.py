"""Quick-start firstmate: boot the (active-image) stack, inject the operator's
Claude credentials into the sandbox, and open a live `claude` session in the
baked ~/firstmate directory in a new terminal window.

Claude auth (per the chosen policy) is BOTH: copy the host's
~/.claude/.credentials.json in, and rely on CLAUDE_CODE_OAUTH_TOKEN (passed via
compose) as the fallback if the copied token doesn't authenticate.
"""

from __future__ import annotations

from pathlib import Path

from . import docker_ops, images
from .config import Config, Paths

HOST_CREDS = Path.home() / ".claude" / ".credentials.json"
IN_CONTAINER_CREDS = "/home/agentdev/.claude/.credentials.json"


def quick_start(paths: Paths, cfg: Config, run=docker_ops.run) -> tuple[bool, str]:
    # 1. boot the active image
    up = docker_ops.compose(paths.root, ["up", "-d"], image=images.active(paths))
    if not up.ok:
        return False, f"stack up failed:\n{up.output}"

    # 2. copy the host's Claude credentials in (best-effort; token env is the fallback)
    note = ""
    if HOST_CREDS.is_file():
        cp = run(["docker", "cp", str(HOST_CREDS), f"{cfg.agent_container}:{IN_CONTAINER_CREDS}"])
        if cp.ok:
            run(["docker", "exec", "-u", "0", cfg.agent_container,
                 "chown", "agentdev:agentdev", IN_CONTAINER_CREDS])
            note = "copied host Claude creds; "
        else:
            note = "creds copy failed (falling back to CLAUDE_CODE_OAUTH_TOKEN); "
    else:
        note = "no host ~/.claude/.credentials.json (using CLAUDE_CODE_OAUTH_TOKEN if set); "

    # 3. open a live claude session in the baked firstmate dir, in a new window,
    #    kicked off so the agent starts orienting itself immediately.
    inner = (
        "cd ~/firstmate && exec claude --permission-mode auto "
        '"Ahoy firstmate, get yourself oriented and ready to work."'
    )
    term = docker_ops.open_terminal(cfg.agent_container, inner)
    return term.ok, note + term.output
