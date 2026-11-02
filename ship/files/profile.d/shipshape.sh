# ShipShape: login shells for the agent user get ~/.local/bin first on PATH, the
# npm prefix in their home, and the catalogue tools' environment.
# shellcheck shell=sh
if [ "$(id -un 2>/dev/null)" = agent ]; then
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) PATH="$HOME/.local/bin:$PATH" ;;
  esac
  npm_config_prefix="$HOME/.local"
  export PATH npm_config_prefix
  if [ -r "$HOME/.config/shipshape/env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$HOME/.config/shipshape/env"
    set +a
  fi
fi
