# Tools contract: catalogue recipes and profiles

The catalogue is a folder of recipe files, `ship/tools/<id>.toml`, plus profiles,
`ship/profiles/<id>.toml`. The Ship track writes and runs them (W6, W5 `ship-ctl
tools`). The Fleet track only reads them from the repo, to list them
(`ship-fleet tools ls`) and to validate `--tools` and `--profile` (W3, W10).

Schemas: `tool-recipe.schema.json`, `profile.schema.json`. Examples:
`examples/tool-recipe/`, `examples/profile/`.

## Rules for recipe files

- The file name is `<id>.toml` and matches `id`.
- npm versions are pinned. If the package is listed in `versions.toml` `[npm]`,
  the recipe's `version` must equal it (a test checks this once recipes exist).
- ship-provision adds every `versions.toml` `[apt.*]` repository, with its
  pinned signing keys, at provision time. A recipe's `apt` entries, and its
  `package` for kind `apt`, must be Ubuntu packages or appear in some
  `[apt.*].packages` list. Recipes never add repositories.
- Nothing is fetched unpinned at run time (no `npx ...@latest`).
- No real accounts, hostnames or URLs that point at a person: values that differ
  per person come from profile options or local config.

## How ship-ctl runs a recipe

For each id in `ship.toml` `[tools].install`, plus the tools of each listed profile:

1. Install `apt` packages as root.
2. Install by kind, as `run_as` (default `agent`):
   - `npm`: `npm install -g <package>@<version>` and every `also_npm` entry, with
     the agent's npm prefix in `~/.local`.
   - `apt`: `apt-get install <package>`.
   - `script`: run `ship/tools/<script>` with bash.
   - `skill-dir`: copy `ship/<skill_dir>` to `~/.claude/skills/<id>/`.
   - `claude-plugin`: `claude plugin install` (from `marketplace` if given).
   - `mcp`: `claude mcp add --scope user <name> -- <command> <args...>`.
3. Write `env` to the agent's environment file.
4. If every service in `needs` shows `ok` in the status document, run `after`;
   otherwise mark the tool `waiting` with `waiting_for` set, and retry on the next
   `tools run`. The Ship does this by itself within a minute of the sign-in
   (see "Reacting to sign-ins" in `ship-ctl.md`).
5. Run `check`. Exit 0 marks the tool `ok`, and the first line of output is its
   version. Anything else marks it `failed`.

Recipes must be safe to run again: running them twice changes nothing the second
time.

## Profiles

A profile is a list of recipe ids plus an optional setup script:

- `options` are values the profile needs from your local config (for example a
  repository URL). FleetLedger copies them from `ships.toml` `[profile_options]`
  into the Ship's `ship.toml`. The repo never holds real values.
- `setup` runs as `agent` after the profile's tools, with each option available
  as an environment variable `SHIPSHAPE_OPT_<NAME>` (upper-case).
