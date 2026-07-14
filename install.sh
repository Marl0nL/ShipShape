#!/usr/bin/env bash
#
# ShipShape control-plane installer / updater.
# Installs the `shipshape` TUI/CLI via pipx (an isolated venv on your PATH).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$SCRIPT_DIR/control-plane"
APP="shipshape-controlplane"   # pipx app name == pyproject [project].name
BIN="shipshape"

c()   { printf '\033[%sm' "$1"; }
log() { printf '%s==>%s %s\n' "$(c '1;36')" "$(c 0)" "$*"; }
warn(){ printf '%swarn:%s %s\n' "$(c '1;33')" "$(c 0)" "$*" >&2; }
die() { printf '%serror:%s %s\n' "$(c '1;31')" "$(c 0)" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
ShipShape installer — installs or updates the '$BIN' control plane.

  ./install.sh              install, or update to the current repo code (clean reinstall)
  ./install.sh --editable   install live/editable (code edits apply immediately;
                            only re-run if dependencies or entry points change)
  ./install.sh --uninstall  remove '$BIN'
  ./install.sh --help
EOF
}

MODE="install"; EDITABLE=""
for arg in "$@"; do
  case "$arg" in
    -e|--editable) EDITABLE="--editable" ;;
    --uninstall)   MODE="uninstall" ;;
    -h|--help)     usage; exit 0 ;;
    *) die "unknown option: $arg (see --help)" ;;
  esac
done

command -v python3 >/dev/null 2>&1 || die "python3 is required"
[ -f "$PKG_DIR/pyproject.toml" ] || die "control-plane package not found at $PKG_DIR"

if ! command -v pipx >/dev/null 2>&1; then
  log "pipx not found — installing it for your user"
  python3 -m pip install --user --quiet pipx \
    || die "could not install pipx automatically; install it (e.g. 'sudo apt install pipx') and re-run"
fi

CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/shipshape"

if [ "$MODE" = "uninstall" ]; then
  log "removing $BIN"
  pipx uninstall "$APP" 2>/dev/null || warn "$BIN was not installed via pipx"
  rm -f "$CONF_DIR/root"
  exit 0
fi

log "installing/updating '$BIN' from $PKG_DIR${EDITABLE:+ (editable)}"
# --force makes this idempotent: the same command first-installs AND updates,
# rebuilding from the current repo code on every run.
pipx install --force ${EDITABLE:+$EDITABLE} "$PKG_DIR"
pipx ensurepath >/dev/null 2>&1 || true

# Record the repo root so `shipshape` works from ANY directory (find_root() falls
# back to this when you're not inside the repo and SHIPSHAPE_ROOT isn't set).
mkdir -p "$CONF_DIR" && printf '%s\n' "$SCRIPT_DIR" > "$CONF_DIR/root"
log "recorded repo root ($SCRIPT_DIR) — '$BIN' will work from any directory"

# Seed local config from the tracked *.example templates (the live files are
# gitignored, so personal entries never land in the repo). Only creates them if absent.
for pair in \
  "egress/allowed_domains.example.txt:egress/allowed_domains.txt" \
  "shipshape.toml.example:shipshape.toml"; do
  tmpl="$SCRIPT_DIR/${pair%%:*}"; live="$SCRIPT_DIR/${pair##*:}"
  if [ -f "$tmpl" ] && [ ! -f "$live" ]; then
    cp "$tmpl" "$live" && log "seeded $(basename "$live") from its template"
  fi
done

if command -v "$BIN" >/dev/null 2>&1; then
  log "$BIN installed at $(command -v "$BIN")"
else
  warn "$BIN is not on PATH in this shell yet — open a new shell (pipx added ~/.local/bin), then run '$BIN'"
fi

cat <<EOF

$(log "done")
Run it:
  $BIN                 # launch the TUI (Stack tab: boot, quick-start firstmate, shell…)
  $BIN up | status | images | quickstart | list | provision <component>

Note: '$BIN' now works from any directory (the repo root was recorded above).
Precedence: \$SHIPSHAPE_ROOT  >  a repo you're cd'd inside  >  the recorded root.

To update after code changes: re-run ./install.sh  (or use --editable once for live edits).
EOF
