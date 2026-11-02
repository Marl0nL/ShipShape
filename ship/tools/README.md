# ship/tools

The catalogue (W6): one recipe per tool, `<id>.toml`, valid against
`contracts/tool-recipe.schema.json` and run by `ship-ctl tools` as described in
`contracts/tools.md`. npm versions are pinned in `versions.toml` `[npm]`; release
binaries are pinned by version and sha256 in their install scripts.

| Recipe | Default | Kind | Notes |
|---|---|---|---|
| `gh-axi` | yes | npm | setup (`gh-axi setup hooks`) waits for the GitHub sign-in |
| `quota-axi` | yes | npm | reports Claude usage; waits for the Claude sign-in |
| `chrome-devtools-axi` | yes | npm | Google Chrome from Google's apt repo (amd64), pinned chrome-devtools-mcp, idle browsers stop after 30 minutes |
| `ship-axi` | yes | script | the `ship` skill and session-start hook; the tool itself comes with the Ship tooling (`ship/axi`) |
| `lavish-axi` | | npm | firstmate profile |
| `tasks-axi` | | npm | firstmate profile |
| `treehouse` | | script | release binary into `~/.local/bin` (`lib/release-binary.sh`) |
| `no-mistakes` | | script | release binary into `~/.local/bin` |

Ships are amd64 for now: Google publishes Chrome for amd64 only, so on an arm64
Ship `chrome-devtools-axi` fails its apt step (the other recipes are arm64-ready).

Adding a tool: write `<id>.toml`, pin any npm package in `versions.toml`, and take
apt packages only from a `versions.toml` `[apt.*]` repo. `ship/tests/test_ship_catalogue.py`
checks all of this.
