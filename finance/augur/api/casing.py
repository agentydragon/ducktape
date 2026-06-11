from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel


def _plain(value: Any, *, exclude_none: bool) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=exclude_none)
    if isinstance(value, Mapping):
        return {str(key): _plain(item, exclude_none=exclude_none) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item, exclude_none=exclude_none) for item in value]
    return value


def plain_json(value: Any, *, exclude_none: bool = True) -> Any:
    return _plain(value, exclude_none=exclude_none)
