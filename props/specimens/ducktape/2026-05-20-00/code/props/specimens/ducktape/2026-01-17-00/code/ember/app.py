from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict

from ember.config import EmberSettings, load_settings
from ember.runtime import EmberRuntime

logger = logging.getLogger(__name__)


class RestartRequest(BaseModel):
    reason: str | None = None
    model_config = ConfigDict(extra="forbid")


class HealthResponse(BaseModel):
    status: Literal["ok"]
    model_config = ConfigDict(extra="forbid")


class RestartResponse(BaseModel):
    status: Literal["restarted"]
    reason: str
    model_config = ConfigDict(extra="forbid")


class ShutdownResponse(BaseModel):
    status: Literal["shutting_down"]
    model_config = ConfigDict(extra="forbid")


@lru_cache(maxsize=1)
def _settings() -> EmberSettings:
    settings = load_settings()
    if not settings.matrix.configured:
        raise RuntimeError(
            "Matrix settings incomplete; set MATRIX_BASE_URL and provide a Matrix access token "
            "(env MATRIX_ACCESS_TOKEN or /var/run/ember/secrets/matrix_access_token)"
        )
    return settings


def create_app(settings: EmberSettings | None = None) -> FastAPI:
    settings = settings or _settings()

    # Runtime holder - created during startup
    runtime_holder: dict[str, EmberRuntime] = {}

    app = FastAPI(title="Ember", version="0.0.1")

    @app.on_event("startup")
    async def _startup() -> None:
        runtime = await EmberRuntime.create(settings)
        runtime_holder["runtime"] = runtime
        await runtime.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if "runtime" in runtime_holder:
            await runtime_holder["runtime"].stop()

    @app.get("/healthz")
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    async def _get_runtime() -> EmberRuntime:
        return runtime_holder["runtime"]

    runtime_dep_annotation = Annotated[EmberRuntime, Depends(_get_runtime)]

    @app.post("/control/restart")
    async def control_restart(request: RestartRequest, runtime_dep: runtime_dep_annotation) -> RestartResponse:
        await runtime_dep.restart()
        return RestartResponse(status="restarted", reason=request.reason or "")

    @app.post("/control/shutdown")
    async def control_shutdown(runtime_dep: runtime_dep_annotation) -> ShutdownResponse:
        await runtime_dep.stop()
        return ShutdownResponse(status="shutting_down")

    return app
