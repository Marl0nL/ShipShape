"""The refresher a trusted machine runs to keep itself current (refresh.sh, beside this file),
and the one line `ship-fleet trust` prints to install it there.

How the script reaches the machine: `ship-fleet machine-bundle NAME` always carries it,
base64, as `refresher` (contracts/machine-bundle.schema.json). Only the first-install line
below decodes it, once, when you paste that line on the machine: the same act of trust as
running a bundle's install.sh by hand. The refresher itself never reads that field and never
replaces itself, so a later refresh applies data only, and a FleetLedger that was taken over
cannot run code on trusted machines through it. The line fetches with the machine's own key
through the same forced command as every refresh (a key that may run only
`ship-fleet machine-bundle NAME` gets nothing more for it), with FleetLedger's host key
pinned from this computer's own record.
"""

from __future__ import annotations

import base64
import re
from importlib import resources

SCRIPT = "refresh.sh"
# What the line puts into a shell: each value is checked against these first, so none can
# hold a quote, a space or anything else a shell or ssh would read as more than a word.
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_ED25519 = re.compile(r"AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{1,100}={0,2}")


def script() -> bytes:
    return resources.files(__package__).joinpath(SCRIPT).read_bytes()


def script_b64() -> str:
    return base64.b64encode(script()).decode("ascii")


def first_install_line(name: str, key_blob: str, user: str, hosts: list[str],
                       host_key: str) -> str | None:
    """The line to paste on machine `name`, or None when a value is not one it can carry
    (then trust says to install the bundle by hand, as before).

    key_blob: the base64 of the machine's trusted public key, to find its private half under
    ~/.ssh by the .pub file beside it (so nobody types its path). user and hosts: how to reach
    FleetLedger's own VM, tried in turn (its LAN name, then others such as its overlay name).
    host_key: the VM's host key, "ssh-ed25519 AAAA...", pinned for the fetch. Anything after
    the line (--no-timer, --no-hc, --lan) goes to the refresher's install."""
    hosts = list(dict.fromkeys(hosts))
    kind, _, hk = host_key.partition(" ")
    if not (_NAME.fullmatch(name) and _USER.fullmatch(user) and _ED25519.fullmatch(key_blob)
            and kind == "ssh-ed25519" and _ED25519.fullmatch(hk) and 1 <= len(hosts) <= 8
            and all(_HOST.fullmatch(h) for h in hosts)):
        return None
    words = " ".join(hosts)
    pin = f"{','.join(hosts)} ssh-ed25519 {hk}"
    inner = (
        'umask 077 && d="$HOME/.ssh/shipshape" && mkdir -p "$d" && '
        f'k=$(grep -lF {key_blob} "$HOME"/.ssh/*.pub "$HOME"/.ssh/*/*.pub 2>/dev/null '
        '| grep -v -e -cert.pub | head -n 1) && '
        f'{{ [ -n "$k" ] || {{ echo "No .pub file under ~/.ssh holds the key trusted as '
        f'{name}." >&2; exit 1; }}; }} && k=${{k%.pub}} && '
        f'echo "{pin}" >"$d/.first-pin" && '
        f'for h in {words}; do ssh -F none -o BatchMode=yes -o IdentitiesOnly=yes '
        '-o StrictHostKeyChecking=yes -o UserKnownHostsFile="$d/.first-pin" '
        '-o GlobalKnownHostsFile=/dev/null -o ConnectTimeout=15 '
        f'-i "$k" -l {user} "$h" ship-fleet machine-bundle {name} </dev/null >"$d/.first-bundle" '
        '&& break; done && '
        'grep "^  .refresher.: " "$d/.first-bundle" | cut -d: -f2 | tr -dc A-Za-z0-9+/= '
        '| base64 --decode >"$d/.first-refresh" && '
        '{ [ -s "$d/.first-refresh" ] || { echo "FleetLedger sent no refresher: update it '
        '(ship-fleet self-update) and paste the line again." >&2; exit 1; }; } && '
        f'sh "$d/.first-refresh" install {name} "$k" {user} "{words}" "ssh-ed25519 {hk}" "$@"'
    )
    assert "'" not in inner
    return f"sh -c '{inner}' sh"
