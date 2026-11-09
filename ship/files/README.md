# ship/files

Files ship-provision installs (paths on the Ship: `contracts/ship-runtime.md`).

| File | Installed as |
|---|---|
| `hooks/mode_ledger.py` | `/opt/shipshape/hooks/mode-ledger`: Claude hook recording each session's mode, and how it ended |
| `libexec/ship_netwatch.py` | network visibility collector, run from the release by `ship-netwatch.service` (`docs/network.md`) |
| `systemd/system/ship-netwatch.service` | installed to `/etc/systemd/system/`; root with four capabilities (eBPF, `ss -p`), `ProtectHome=yes`, observes only |
| `systemd/user/herdr.service` | `/etc/systemd/user/`: the herdr server, for the agent user only |
| `systemd/system/ship-status.*` | `/etc/systemd/system/`: the 30-second status refresh |
| `systemd/system/ship-lan-guard-early.service` | `/etc/systemd/system/`: loads the stored LAN guard rules before the network comes up |
| `systemd/system/ship-lan-guard.service` | `/etc/systemd/system/`: refreshes the LAN guard's gateway and DNS once the network is online; `ship-ctl apply` enables both units only while `network.lan_guard` is on |
| `systemd/system/ship-wake.*` | `/etc/systemd/system/`: wakes the primary agent once, some minutes after each boot (`ship-ctl wake`); `ship-ctl apply` enables the timer only while `wake.after_boot` is on, and writes its delay to `ship-wake.timer.d/` |
| `ssh/10-shipshape.conf` | `/etc/ssh/sshd_config.d/` |
| `logind/50-shipshape.conf`, `journald/50-shipshape.conf` | `/etc/systemd/*.conf.d/` |
| `profile.d/shipshape.sh` | `/etc/profile.d/`: the agent's PATH, npm prefix and tool environment |

The avahi announcement is not here: it carries the Ship's name and id, so
`ship-ctl apply` writes it from `ship.toml`.
