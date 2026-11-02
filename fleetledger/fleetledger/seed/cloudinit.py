"""Render a Ship's cloud-init NoCloud files (contracts/seed.md).

user-data has exactly the top-level keys the contract lists, so the Ship track
can rely on nothing else happening at first boot. YAML is written with PyYAML,
the same library cloud-init reads it with.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from fleetledger.errors import ConfigError
from fleetledger.seed.bundle import Bundle
from fleetledger.seed.hostkey import HostKey

# The seed runs exactly this script; everything else belongs to ship-provision.
# Kept identical to the block in contracts/seed.md by a test.
BOOTSTRAP = """\
set -euo pipefail
install -d -m 0750 /var/log/shipshape
exec >>/var/log/shipshape/provision.log 2>&1
cd /var/lib/shipshape
sha256sum -c bundle.sha256
tmp="$(mktemp -d)"
tar -xzf bundle.tar.gz -C "$tmp"
release="$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m["tooling_version"] + "-" + m["commit"][:12])' "$tmp/bundle.json")"
install -d -m 0755 /opt/shipshape/releases
rm -rf "/opt/shipshape/releases/$release"
mv "$tmp" "/opt/shipshape/releases/$release"
chmod 0755 "/opt/shipshape/releases/$release"
ln -sfn "/opt/shipshape/releases/$release" /opt/shipshape/current
exec /opt/shipshape/current/provision/ship-provision --first-boot
"""  # noqa: E501

USER_DATA_KEYS = (
    "hostname",
    "preserve_hostname",
    "ssh_pwauth",
    "disable_root",
    "ssh_deletekeys",
    "ssh_keys",
    "users",
    "write_files",
    "runcmd",
)

PUBLIC_KEY_PREFIXES = ("ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-")


class _Dumper(yaml.SafeDumper):
    """Multi-line strings as literal blocks; never wrap long lines."""


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def dump_yaml(data: Any) -> str:
    return yaml.dump(
        data, Dumper=_Dumper, sort_keys=False, default_flow_style=False, width=1 << 30
    )


def read_public_keys(path: Path, what: str) -> list[str]:
    """Public key lines from a .pub or authorized_keys-style file.

    Refuses anything that looks like a private key: the seed may hold no secret
    except the Ship's own host key.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read {what} ({path}).") from exc
    if "PRIVATE KEY" in text:
        raise ConfigError(f"{what} ({path}) is a private key; give the .pub file instead.")
    keys = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    bad = [k for k in keys if not k.startswith(PUBLIC_KEY_PREFIXES)]
    if bad or not keys:
        raise ConfigError(f"{what} ({path}) does not hold OpenSSH public keys.")
    return keys


def new_instance_id(ship_name: str) -> str:
    """<ship name>-<8 random hex>: new for every seed, so cloud-init runs again."""
    return f"{ship_name}-{secrets.token_hex(4)}"


def user_data(
    ship_name: str,
    host_key: HostKey,
    admin_keys: Sequence[str],
    agent_keys: Sequence[str],
    ship_toml: str,
    bundle: Bundle,
) -> str:
    doc: dict[str, Any] = {
        "hostname": ship_name,
        "preserve_hostname": False,
        "ssh_pwauth": False,
        "disable_root": True,
        "ssh_deletekeys": True,
        "ssh_keys": {
            "ed25519_private": host_key.private,
            "ed25519_public": host_key.public,
        },
        "users": [
            {
                "name": "fleet",
                "gecos": "ShipShape admin",
                "shell": "/bin/bash",
                "lock_passwd": True,
                "sudo": "ALL=(ALL:ALL) NOPASSWD: ALL",
                "ssh_authorized_keys": list(admin_keys),
            },
            {
                "name": "agent",
                "gecos": "ShipShape agent",
                "shell": "/bin/bash",
                "lock_passwd": True,
                "ssh_authorized_keys": list(agent_keys),
            },
        ],
        "write_files": [
            {
                "path": "/etc/shipshape/ship.toml",
                "owner": "root:root",
                "permissions": "0644",
                "content": ship_toml,
            },
            {
                "path": "/var/lib/shipshape/bundle.tar.gz",
                "owner": "root:root",
                "permissions": "0600",
                "encoding": "b64",
                "content": base64.b64encode(bundle.data).decode("ascii"),
            },
            {
                "path": "/var/lib/shipshape/bundle.sha256",
                "owner": "root:root",
                "permissions": "0644",
                "content": bundle.checksum_line,
            },
        ],
        "runcmd": [["bash", "-c", BOOTSTRAP]],
    }
    assert tuple(doc) == USER_DATA_KEYS
    return "#cloud-config\n" + dump_yaml(doc)


def meta_data(ship_name: str, instance_id: str) -> str:
    return dump_yaml({"instance-id": instance_id, "local-hostname": ship_name})


def network_config() -> str:
    """DHCP on the first Ethernet interface (network config version 2)."""
    return dump_yaml(
        {
            "version": 2,
            "ethernets": {"primary": {"match": {"name": "e*"}, "dhcp4": True}},
        }
    )
