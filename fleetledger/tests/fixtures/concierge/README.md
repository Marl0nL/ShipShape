# Concierge fixtures

Sign-in sessions for the pattern tests, played back by `../../fake_cli.py` in a
real pseudo-terminal. Example values only: no real codes, links, accounts or
hosts (`docs/hygiene.md`).

- `claude.txt`, `gcloud.txt`, `github.txt`: the wording recorded from the real
  tools in phase 0 spike S5 (`claude auth login`; `gcloud auth login
  --no-launch-browser --update-adc`; `GH_PROMPT_DISABLED=1 gh auth login --web`),
  with every code, link token and account replaced by example values. Claude's
  prompt does not echo the pasted code; gcloud's does.
- The others are variants of those for the engine's edge cases: escape
  sequences and OSC 8 hyperlinks (`claude-ansi.txt`), a link split across
  reads (`claude-split.txt`, `claude-osc-split.txt`), a link wrapped at the
  terminal's width (`claude-wrapped.txt`), a clean exit without the usual
  success line (`claude-quiet.txt`), a slow verdict (`claude-slow-verdict.txt`), a rejected code (`claude-bad-code.txt`), an
  expired device code (`github-expired.txt`) and changed wording
  (`changed-wording.txt`).
