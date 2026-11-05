"""Reading posted forms: shared by the pages (app.py) and the setup page (setup.py)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs

from starlette.requests import Request

# A Ship name or tag (contracts: shipName, tag).
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


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
