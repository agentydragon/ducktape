from __future__ import annotations

import typing
from typing import TYPE_CHECKING

# Both forms should be allowed - they're legitimate for avoiding circular imports

if TYPE_CHECKING:
    import json

if typing.TYPE_CHECKING:
    from pathlib import Path


def use_types(data: str) -> json.JSONDecoder:
    # Type hints reference the TYPE_CHECKING imports
    return json.JSONDecoder()


def use_path(p: Path) -> str:
    return str(p)
