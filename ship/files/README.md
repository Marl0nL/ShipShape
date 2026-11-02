# ship/files

Files ship-provision installs (paths on the Ship: `contracts/ship-runtime.md`).

| File | Installed as |
|---|---|
| `shim/claude.py` | `/opt/shipshape/shim/claude`: resume shim, first on herdr's PATH |
| `hooks/mode_ledger.py` | `/opt/shipshape/hooks/mode-ledger`: Claude hook recording each session's mode |
| `libexec/ship_resume.py` | run from the release by `ship-resume.service` |
| `libexec/ship_netwatch.py` | network visibility collector, run from the release by `ship-netwatch.service` (`docs/network.md`) |
| `systemd/system/ship-netwatch.service` | installed to `/etc/systemd/system/`; root with four capabilities (eBPF, `ss -p`), `ProtectHome=yes`, observes only |
| `systemd/user/*.service` | `/etc/systemd/user/`: herdr and ship-resume, for the agent user only |
| `systemd/system/ship-status.*` | `/etc/systemd/system/`: the 30-second status refresh |
| `ssh/10-shipshape.conf` | `/etc/ssh/sshd_config.d/` |
| `logind/50-shipshape.conf`, `journald/50-shipshape.conf` | `/etc/systemd/*.conf.d/` |
| `profile.d/shipshape.sh` | `/etc/profile.d/`: the agent's PATH, npm prefix and tool environment |

The avahi announcement is not here: it carries the Ship's name and id, so
`ship-ctl apply` writes it from `ship.toml`.
