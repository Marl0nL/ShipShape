# Sourced by every login shell (the firstmate quick-start runs `bash -lc`). Makes
# Claude Code launch non-interactively from the rotatable, mounted token:
#   1. export CLAUDE_CODE_OAUTH_TOKEN from the live /auth/claude-token, and
#   2. synthesize ~/.claude/.credentials.json from it — the CLI honors the token most
#      reliably when a structured credentials file is present.
# The file is (re)written whenever it doesn't already contain the CURRENT token, so
# SWITCHING ACCOUNTS or rotating the token actually takes effect — otherwise a stale
# token (from a snapshot image or an earlier session) keeps being used and gets a 401.
if [ -s /auth/claude-token ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(cat /auth/claude-token)"
  export CLAUDE_CODE_OAUTH_TOKEN
  _cc="${HOME:-/home/agentdev}/.claude"
  mkdir -p "$_cc"
  if ! grep -qF "$CLAUDE_CODE_OAUTH_TOKEN" "$_cc/.credentials.json" 2>/dev/null; then
    _exp=$(( ($(date +%s) + 31536000) * 1000 ))   # ~1y out, in ms
    printf '{"claudeAiOauth":{"accessToken":"%s","expiresAt":%s,"scopes":["user:inference","user:profile"]}}' \
      "$CLAUDE_CODE_OAUTH_TOKEN" "$_exp" > "$_cc/.credentials.json"
    chmod 600 "$_cc/.credentials.json"
    unset _exp
  fi
  unset _cc
fi
