# ship/axi

`ship-axi` (W7): the Ship's own AXI tool, in Node with `axi-sdk-js` (pinned in
`versions.toml` and `package-lock.json`). Not published: it ships in the tooling
bundle, ship-provision installs its dependencies with `npm ci` and links
`/usr/local/bin/ship-axi`, and the `ship-axi` recipe adds the skill and hook.

| Command | What it does |
|---|---|
| `ship-axi` | this Ship: name, tags, your agent and mode, other agents, Remote Control, sign-ins (account names only), tools, attention. Reads `/run/shipshape/status.json` (`SHIPSHAPE_STATUS` overrides). |
| `ship-axi rules` | what agents may and may not change |
| `ship-axi setup hooks` | a Claude Code session-start hook that gives every new session this context |
| `ship-axi request`, `ship-axi message` | reserved; print "not available yet" |
| `ship-axi update` | says it updates with the Ship's tooling (it never installs from npm) |

```sh
npm ci && npm test   # node --test; scripts/check runs the same
```
