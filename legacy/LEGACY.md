# Legacy: the container stack

This folder holds the original ShipShape: a Docker sandbox with a default-deny
Squid egress proxy, a request broker and a Textual control plane. It is frozen.

- It is kept only for reference while the rebuild (Ships and FleetLedger) lands.
- It does not run from this location: its paths assume the repo root.
- It is not linted or tested by `scripts/check`, and the generic hygiene rules
  skip it. The private deny-list still applies (see `docs/hygiene.md`).
- It will be deleted once phase 2 of the rebuild is accepted. The last commit
  before the move keeps the full working tree in history.

The original design notes live in `docs/archive/`.
