# Herdr layouts

Layouts and mods for Herdr, the terminal app ShipShape's Ships run their agents in.

**The Desk** (`layouts/desk.json`): Herdr's default layout plus a panel that follows the focused
agent, which agents on any machine, Ships included, fill in themselves with the `desk` CLI. See
[`DESK.md`](DESK.md) for how it works; it installs with `herdr/install-desk.sh`.

## Licences

- `mods/desk-agents/` and `mods/desk-spaces/` are modified copies of Herdr's `agents` and `spaces`
  mods, and `layouts/desk.json` of its `main` layout, from Herdr 0.1.15
  (<https://github.com/herdrdev/herdr-app>, `defaults/`), under the Apache License 2.0
  ([`LICENSE-herdr.txt`](LICENSE-herdr.txt)). The mods' changes are the lines marked `Desk:`;
  the layout's are the Desk's own parts, described in [`DESK.md`](DESK.md).
- `mods/desk/fonts/Urbanist-*.ttf` is Urbanist, under the SIL Open Font License 1.1
  ([`mods/desk/fonts/Urbanist-OFL.txt`](mods/desk/fonts/Urbanist-OFL.txt)).
