#!/bin/sh
# /usr/local/bin/ship-fleet on FleetLedger's own VM (docs/runbooks/fleetledger-vm.md).
#
# pipx installs ship-fleet in ~/.local/bin, which only a login shell puts on PATH, so a
# plain `ssh fleetledger@<vm> ship-fleet ...` would not find it. This root-owned file is
# on the PATH ssh gives a command, and runs the fleetledger user's own install. Anyone
# else is refused: vmadmin, who has sudo, never runs code the fleetledger user can change.
if [ "$(/usr/bin/id -un)" != fleetledger ]; then
  echo "ship-fleet: on this VM, FleetLedger is the fleetledger user's: ssh fleetledger@<this VM> ship-fleet ..." >&2
  exit 126
fi
exec /home/fleetledger/.local/bin/ship-fleet "$@"
