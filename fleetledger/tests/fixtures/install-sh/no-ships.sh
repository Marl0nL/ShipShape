#!/bin/sh
# Install this ShipShape trust bundle for the current user (run it from the
# bundle's folder, on the machine it was made for). Idempotent.
#
#   sh install.sh            # SSH config, certificate, CA line, herdr saved machines
#   sh install.sh --no-herdr # everything except herdr
set -eu
here=$(cd "$(dirname "$0")" && pwd)
dest="$HOME/.ssh/shipshape"
herdr=1
[ "${1:-}" = "--no-herdr" ] && herdr=0

umask 077
mkdir -p "$HOME/.ssh" "$dest"
chmod 700 "$HOME/.ssh" "$dest"
for f in config known_hosts laptop-cert.pub; do
  cp "$here/$f" "$dest/$f.tmp" && chmod 600 "$dest/$f.tmp" && mv "$dest/$f.tmp" "$dest/$f"
done

key="$HOME/.ssh/shipshape/id_ed25519"
if [ ! -f "$key" ]; then
  echo "Missing $key: this bundle is for the key whose public half was trusted." >&2
  echo "Copy that private key there, or re-run ship-fleet trust with --identity." >&2
  exit 1
fi

include="Include $dest/config"
# A ~/.ssh/config that is a link (to a dotfiles folder, say) is written through, so
# the link and the file it points at stay one.
cfg="$HOME/.ssh/config"
if [ -L "$cfg" ]; then
  cfg=$(readlink -f "$cfg") || { echo "Cannot follow the link ~/.ssh/config." >&2; exit 1; }
fi
if [ ! -f "$cfg" ] || ! grep -qxF "$include" "$cfg"; then
  # Include must come before any Host block to apply everywhere: put it first.
  tmp="$cfg.shipshape-tmp"
  { echo "$include"; if [ -f "$cfg" ]; then cat "$cfg"; fi; } > "$tmp"
  chmod 600 "$tmp" && mv "$tmp" "$cfg"
  echo "Added '$include' to the top of ~/.ssh/config."
fi

if [ "$herdr" = 1 ]; then
  if command -v herdr >/dev/null 2>&1; then
    known=$(herdr machine list --json 2>/dev/null || true)
    for ship in ; do
      if printf '%s' "$known" | grep -q "\"$ship\""; then
        echo "herdr already has $ship."
      else
        herdr machine add "$ship" --label "$ship" --remote-session default </dev/null \
          || echo "herdr could not add $ship; check \`ssh $ship true\` first." >&2
      fi
    done
  else
    echo "herdr is not installed here; skipping saved machines."
  fi
fi
echo "Done. Try: ssh <ship> true   (then herdr --remote <ship>)"
