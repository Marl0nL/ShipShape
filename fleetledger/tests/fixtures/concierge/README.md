# Concierge fixtures

Synthetic sign-in sessions for the pattern tests, played back by
`../../fake_cli.py` in a real pseudo-terminal. Each mirrors the shape of the
real CLI's no-browser sign-in (link, prompt, success line), with example values
only: no real codes, links, accounts or hosts (`docs/hygiene.md`).

They will be replaced by the sessions recorded in phase 0 spike S5 (with every
code, URL token and account swapped for example values) once those land.
