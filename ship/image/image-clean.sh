#!/usr/bin/env bash
# image-clean.sh: the last step of a bake, inside the image (contracts/image.md).
# Removes everything a first boot must create fresh, and anything that would make
# two Ships alike: cloud-init state, machine id, SSH host keys, logs, caches.
set -euo pipefail
rm -rf /var/lib/shipshape/provision.json /var/log/shipshape
rm -f /etc/ssh/ssh_host_*
apt-get clean
rm -rf /var/lib/apt/lists/*
# cloud-init must run again on the first real boot, with the Ship's own seed.
if command -v cloud-init >/dev/null 2>&1; then cloud-init clean --logs || true; fi
rm -rf /var/lib/cloud
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
rm -f /root/.bash_history
find /var/log -type f \( -name '*.log' -o -name '*.gz' -o -name '*.[0-9]' \) -delete
journal=/var/log/journal
[ ! -d "$journal" ] || find "$journal" -type f -delete
fstrim -av >/dev/null 2>&1 || true
