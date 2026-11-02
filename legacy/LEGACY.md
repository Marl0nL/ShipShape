# Legacy: the container stack

This folder holds the original ShipShape: a Docker sandbox with a default-deny
Squid egress proxy, a request broker and a Textual control plane. It is frozen.

- It is kept only for reference while the rebuild (Ships and FleetLedger) lands.
- It does not run from this location: its paths assume the repo root.
- It is not linted or tested by `scripts/check`. The home-path and email
  hygiene rules skip it; private IPs and the private deny-list are still
  checked (see `docs/hygiene.md`).
- **Still running the container stack?** Stay on the `container-stack-final` tag
  (`git checkout container-stack-final`) until Ships replace it: the stack does
  not run from this layout.
- It will be deleted once phase 2 of the rebuild is accepted. The last commit
  before the move keeps the full working tree in history.

The original design notes live in `docs/archive/`.
