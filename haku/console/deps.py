"""Shared FastAPI dependencies: typed accessors for the objects `create_app`
stashes on `app.state` (Starlette's untyped `Any` container)."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from haku.console.config import Settings
from haku.console.git_state import GitState


def _git_state(request: Request) -> GitState:
    return cast(GitState, request.app.state.git_state)


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


GitStateDep = Annotated[GitState, Depends(_git_state)]
SettingsDep = Annotated[Settings, Depends(_settings)]
