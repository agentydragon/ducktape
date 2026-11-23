"""Props dashboard routes used by the backend and frontend schema exporter."""

from fastapi import FastAPI, Response
from starlette.types import Lifespan

from props.backend.routes import agent_definitions, auth_routes, ground_truth, model_metadata, runs, stats


def build_app(*, lifespan: Lifespan[FastAPI] | None, sso_enabled: bool) -> FastAPI:
    app = FastAPI(
        title="Props Backend",
        description="Props backend: dashboard and eval APIs",
        version="0.1.0",
        lifespan=lifespan,
        debug=True,
    )
    if sso_enabled:
        app.include_router(auth_routes.router)
    app.include_router(stats.router, prefix="/api/stats", tags=["stats"])
    app.include_router(runs.router, prefix="/api/runs", tags=["runs"])
    app.include_router(ground_truth.router, prefix="/api/gt", tags=["ground_truth"])
    app.include_router(agent_definitions.router, prefix="/api/definitions", tags=["definitions"])
    app.include_router(model_metadata.router, prefix="/api/model_metadata", tags=["model_metadata"])

    @app.get("/health")
    def health() -> dict[str, str]:
        # Liveness begins only after the startup probe succeeds.
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(response: Response) -> dict[str, str]:
        if app.state.startup_complete:
            return {"status": "ready"}
        response.status_code = 503
        return {"status": "starting"}

    return app
