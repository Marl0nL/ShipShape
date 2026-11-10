"""Reading posted forms: shared by the pages (app.py), the setup page (setup.py) and
the New Ship form (newship.py)."""

from __future__ import annotations

from urllib.parse import parse_qs

from starlette.requests import Request

from fleetledger.errors import FleetError

# A Ship name or tag (contracts: shipName, tag).
from fleetledger.ops import NAME_RE as NAME_RE


async def form(request: Request) -> dict[str, list[str]]:
    """The posted form (the gate has already capped its size and checked CSRF)."""
    body = await request.body()
    try:
        return parse_qs(body.decode("utf-8"), max_num_fields=100)
    except (UnicodeDecodeError, ValueError):
        return {}


def one(fields: dict[str, list[str]], key: str) -> str:
    """A field's single value, stripped; "" if it is missing or given more than once."""
    values = fields.get(key, [])
    return values[0].strip() if len(values) == 1 else ""


def size_from_form(fields) -> dict[str, int]:
    """cores / memory_mb / disk_gb from a form (memory typed in GB), checked."""
    from fleetledger.lifecycle import check_size

    def number(key: str) -> int | None:
        value = one(fields, key)
        if not value:
            return None
        if not value.isdigit():
            raise FleetError(f"{key.replace('_', ' ')}: a whole number.")
        return int(value)

    memory_gb = number("memory_gb")
    return check_size({"cores": number("cores"), "disk_gb": number("disk_gb"),
                       "memory_mb": memory_gb * 1024 if memory_gb is not None else None})
