# tests/e2e

End-to-end tests (W11) on a real Proxmox pool, using this checkout's `ship/` and
`fleetledger/` together. They need hypervisor access, so they are skipped unless
`SHIPSHAPE_E2E` names a config with `[drivers.proxmox]`; CI never sets it. Set it
only for the command that runs them (as below): with it exported, `scripts/check`
would run them too.

You need a `ships.toml`-format file with the `[drivers.proxmox]` section from
`docs/runbooks/proxmox-setup.md`; its `vmid_range` limits which VMIDs the tests
may use. Nothing else is read from it: each run makes throwaway keys and a
throwaway FleetLedger home, and destroys every VM it made.

## Unattended: `test_e2e.py`

```sh
SHIPSHAPE_E2E=~/.config/fleetledger/e2e.toml python -m pytest tests/e2e -v -s
```

One Ship, through the `ship-fleet` command:

1. `ship-fleet new` with an auto and a bypass autostart agent: seed, upload,
   clone, boot, provision, enrol.
2. The seed is gone: ejected, deleted from storage, no local copy.
3. `ship-fleet status --json`: `needs-sign-in`, valid against
   `contracts/ship-status.schema.json`, provisioning ok, herdr running at the
   pinned version, Claude Code installed, the settings from the seed applied, no
   failed units.
4. The runtime contract (`contracts/ship-runtime.md`): only the pinned ed25519
   host key, sshd settings, lingering, no sudo for agent, file modes, managed
   and user Claude settings.
5. A reboot (`systemctl reboot`, as FleetLedger does): herdr and the resume
   service come back, still `needs-sign-in`.
6. Destroy: the VM leaves the pool.

Phase 1's bar is two passes in a row, each from scratch.

## With a person: `acceptance.py`

The checks that need sign-ins and a look at the Claude app. Run it yourself, in
your own terminal: the sign-in links and codes go only between you, your browser
and the Ship.

```sh
python tests/e2e/acceptance.py --config ~/.config/fleetledger/e2e.toml
```

The config also needs a `[tag.e2e]` section with the accounts to sign in as. It
creates a Ship, signs it in, and checks:

- the accounts match the tag, and a mismatch is flagged as a wrong account;
- the autostart agents start in their modes and show in the Claude app;
- a bypass agent whose autostart entry has no prompt is still recorded in its
  mode (ship-ctl sends a default first prompt);
- all of them come back after a reboot with nobody attached, each in its mode;
- with Remote Control off, they drop out of the app after a reboot (the app can
  take a few minutes);
- a bypass-default Ship starts new agents in bypass without a prompt;
- no token (nor a Google sign-in code; the other codes are too plain to search
  for) is in any log or shell history, on the Ship or locally.

It then signs the Ship out and destroys it (unless `--keep`). It takes about 30
to 45 minutes; you are needed for the sign-ins and two looks at the app.
