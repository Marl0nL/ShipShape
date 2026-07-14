# Sourced by every login shell (the firstmate quick-start runs `bash -lc`). Makes
# Claude Code launch non-interactively from the rotatable, mounted token:
#   1. export CLAUDE_CODE_OAUTH_TOKEN from the live /auth/claude-token, and
#   2. synthesize ~/.claude/.credentials.json from it when none exists — the CLI
#      honors the token most reliably when a structured credentials file is present.
# A rotated token therefore reaches new sessions with no container recreate.
if [ -s /auth/claude-token ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(cat /auth/claude-token)"
  export CLAUDE_CODE_OAUTH_TOKEN
  _cc="${HOME:-/home/agentdev}/.claude"
  mkdir -p "$_cc"
  if [ ! -s "$_cc/.credentials.json" ]; then
    _exp=$(( ($(date +%s) + 31536000) * 1000 ))   # ~1y out, in ms
    printf '{"claudeAiOauth":{"accessToken":"%s","expiresAt":%s,"scopes":["user:inference","user:profile"]}}' \
      "$CLAUDE_CODE_OAUTH_TOKEN" "$_exp" > "$_cc/.credentials.json"
    chmod 600 "$_cc/.credentials.json"
    unset _exp
  fi
  unset _cc
fi
