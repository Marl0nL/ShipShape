"""ship-ctl: the Ship's control command. Interface: contracts/ship-ctl.md.

Standard library only: this package runs on a fresh Ubuntu cloud image.
"""

# Major versions of the contracts this code implements (`ship-ctl version`).
CONTRACTS = {
    "ship-status": 1,
    "ship-toml": 1,
    "ship-ctl-result": 1,
    "bundle": 1,
    "seed": 1,
    "runtime": 1,
    "ssh-trust": 1,
    "net-summary": 1,
    "inbox": 1,
    "signin": 1,
}

PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
