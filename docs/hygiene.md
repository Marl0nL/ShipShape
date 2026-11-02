# Public repo hygiene

ShipShape is a public repository. Nothing committed to it, and nothing in its pull
requests, issues or commit messages, may point at a specific person, their work,
their machines or their network. That means no:

- employer or company names, internal tools or services;
- hostnames, IP addresses, usernames or email addresses;
- paths under a real home directory;
- project, ticket or cloud project ids;
- details of anyone's machines, hypervisor or network.

Use generic values in examples, docs and tests:

| Kind | Use |
|---|---|
| Ship names | `ship-01`, `ship-02` |
| Tags | `work`, `personal` |
| IP addresses | `192.0.2.x`, `198.51.100.x`, `203.0.113.x` (RFC 5737) |
| Domains and emails | `example.com`, `example.org`, `example.net`, `*.example`, `*.test` |
| Paths | `~/...`, or the Ship's own users: `/home/agent`, `/home/fleet` |
| Accounts | `you@example.com`, `example-user` |

Anything personal (accounts, repository URLs, Proxmox node, storage and bridge
names, the token file) lives only in local, git-ignored config such as
`~/.config/fleetledger/ships.toml`.

## The check

`scripts/check` runs `scripts/hygiene.py`, which has two layers:

1. **Generic rules** in the repo: private IPv4 addresses (`10/8`, `172.16/12`,
   `192.168/16`, `100.64/10`), home-directory paths that name a user, and email
   addresses outside the reserved example domains. They skip `legacy/` and
   `docs/archive/`, which predate the rule.
2. **Your private deny-list**, kept outside the repo, of exact terms that must
   never appear: names, employer, hostnames, project ids and so on. A match is
   reported by entry number, never by the term itself, so CI logs and terminal
   output never repeat it. The deny-list applies everywhere, including `legacy/`
   and `docs/archive/`.

The deny-list is read from `$SHIPSHAPE_HYGIENE_DENYLIST`, or else
`$XDG_CONFIG_HOME/shipshape/hygiene-denylist`, or else
`~/.config/shipshape/hygiene-denylist`. One entry per line; `#` starts a comment;
`re:<regex>` is a case-insensitive regular expression; anything else is a
case-insensitive literal. The script refuses a deny-list stored inside the repo.

```sh
scripts/hygiene.py --tree                      # files
scripts/hygiene.py --commits origin/main..HEAD # commit messages on your branch
scripts/hygiene.py pr-body.md                  # a PR or issue text before posting it
```

CI has no deny-list, so it runs the generic rules on files and on the PR's commit
messages. Run the full check locally before pushing.

A line containing `hygiene: allow` is skipped by the generic rules (not by the
deny-list). Use it only for lines that must contain such a pattern, such as the
rule definitions themselves.
