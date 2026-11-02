# ship/profiles

Profiles, one `<id>.toml` per profile (W6), valid against
`contracts/profile.schema.json`, plus their setup scripts. Values that differ per
person (repo URLs, accounts) are profile options set in local config, never here.

## firstmate

Tools: `treehouse`, `no-mistakes`, `lavish-axi`, `tasks-axi`. Setup clones the
repo from the `repo_url` option into `~/work/firstmate` once (an existing checkout
is left alone). Set the URL in your local `ships.toml`:

```toml
[profile_options.firstmate]
repo_url = "https://github.com/example/firstmate.git"
```

A private repo needs the GitHub sign-in: until then the setup fails, and the Ship
retries it by itself after the next sign-in.
