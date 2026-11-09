# Contracts

Everything the Ship track and the Fleet track agree on lives here. Each track
builds against these files and never reaches into the other track's directory
(see `docs/tracks.md`). If something you need is not written down here, it is
not part of the interface: ask for a contract change instead of guessing.

## Index

| Contract | Machine-readable | Producer | Consumers |
|---|---|---|---|
| Per-Ship settings (`/etc/shipshape/ship.toml`) | `ship.toml.schema.json` | Fleet: seed generator (W3), and Ship: `ship-ctl set` (W5) | Ship: ship-provision (W4), ship-ctl (W5), ship-axi (W7) |
| Inventory (`ships.toml`, local only) | `ships.toml.schema.json` | Fleet (W8, W10) | Fleet (W3, W8, W9, W10) |
| Status document (`ship-ctl status --json`) | `ship-status.schema.json` | Ship: ship-ctl (W5) | Fleet (W8, W10); Ship: ship-axi (W7) |
| ship-ctl command line | `ship-ctl-result.schema.json` | Ship (W5) | Fleet (W8, W9, W10) |
| Seed ISO and tooling bundle | `bundle-manifest.schema.json` | Fleet (W3, `ship-fleet update`) | Ship: seed bootstrap, ship-provision, `ship-ctl converge` (W4, W5) |
| Ship runtime (users, paths, units, sudo) | prose only | Ship (W4) | Fleet (W8, W9) |
| Catalogue recipes and profiles | `tool-recipe.schema.json`, `profile.schema.json` | Ship (W6) | Ship: ship-ctl tools (W5); Fleet: `ship-fleet tools`, `new --profile` (W3, W10) |
| Pinned versions (`/versions.toml`) | `versions.schema.json` | shared | Ship (W4), Fleet (W3, W8) |
| SSH certificates for a Ship (`ship-ctl ssh-trust` stdin) | `ssh-trust.schema.json` | Fleet: the certificate authority (phase 3) | Ship: ship-ctl |
| Network visibility summary (`ship-ctl net --json` data) | `net-summary.schema.json` | Ship: ship-netwatch and ship-ctl (phase 3) | Fleet: Network tab, first-seen destinations, who-talked search |
| Baked image manifest | `image-manifest.schema.json` | Ship: `ship/image/bake` (phase 4) | Fleet: `ship-fleet image` (phase 4) |
| Agent record (`/var/lib/shipshape/agents.json`) | `agent-record.schema.json` | Ship: ship-ctl | Ship: ship-ctl (resume, restart, notices, status); Fleet sees it only through the status document's `agent_record` |

Prose documents:

- `seed.md`: what a seed ISO contains and how first boot hands over to ship-provision; the bundle format.
- `ship-runtime.md`: the users, paths, units and sudo rules every Ship has, and how FleetLedger reaches them.
- `ship-ctl.md`: every ship-ctl command, its output and exit codes, and how status is worked out.
- `settings.md`: what each `ship.toml` field does on the Ship, and how `ships.toml` becomes a Ship's `ship.toml`.
- `tools.md`: how recipes and profiles are run.
- `image.md`: what a baked image holds, how `ship/image/bake` is called, image ids, pinning and rollout (phase 4).

Examples live in `examples/<contract>/`. Every file there must validate, and every
file in `examples/<contract>/invalid/` must fail; `tests/contracts/` checks both.

## Formats

- TOML files are validated as the JSON value they parse to (Python `tomllib`).
- Schemas are JSON Schema draft 2020-12 and self-contained: shared definitions
  (`shipName`, `tag`, `permissionMode`, ...) are copied into each schema, and a test
  keeps the copies identical.
- Timestamps are UTC RFC 3339 with a `Z` suffix.

## Versioning and changes

- Each contract carries a version: `schema = 1` in TOML files, `"schema":
  "shipshape.<name>/1"` in JSON documents.
- Adding an optional field is a minor change: same version, schema updated,
  consumers must ignore fields they do not use.
- Removing or renaming a field, or changing its meaning, is a breaking change:
  bump the version and support both on the consumer side until every Ship is
  updated.
- Change contracts in their own PR, touching only `contracts/` (plus the matching
  examples and tests), so both tracks can review it before building on it.

## Public repo rule

Examples use generic values only (`ship-01`, `192.0.2.x`, `example.com`, tags
`work` and `personal`). Real accounts, hostnames and paths belong in local,
git-ignored config. See `docs/hygiene.md`.
