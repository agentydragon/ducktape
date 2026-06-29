"""Shared FastAPI dependencies: typed accessors for the objects `create_app`
stashes on `app.state` (Starlette's untyped `Any` container)."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from haku.console.config import Settings


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


SettingsDep = Annotated[Settings, Depends(_settings)]
