from __future__ import annotations

import copy
import logging
from functools import lru_cache
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict
from uvicorn.config import LOGGING_CONFIG

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


def _log_config() -> dict[str, Any]:
    config: dict[str, Any] = copy.deepcopy(LOGGING_CONFIG)

    default_fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    access_fmt = '%(asctime)s %(levelname)s [%(name)s] %(client_addr)s - "%(request_line)s" %(status_code)s'

    config["formatters"]["default"]["fmt"] = default_fmt
    config["formatters"]["access"]["fmt"] = access_fmt
    config.setdefault("loggers", {})
    config["loggers"][""] = {"handlers": ["default"], "level": "INFO", "propagate": True}
    config["loggers"]["uvicorn.error"] = {"level": "INFO"}
    config["loggers"]["uvicorn.access"] = {"handlers": ["access"], "level": "INFO", "propagate": False}
    config["loggers"]["uvicorn"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    config["loggers"]["ember"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    config["loggers"]["ember.matrix_client"] = {"handlers": ["default"], "level": "DEBUG", "propagate": False}
    return config


def main() -> None:
    uvicorn.run(
        "ember.app:create_app", factory=True, host="0.0.0.0", port=8000, log_level="info", log_config=_log_config()
    )


if __name__ == "__main__":
    main()
