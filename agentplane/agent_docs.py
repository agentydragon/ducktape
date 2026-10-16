"""Serve the agent documentation packaged with an independent service."""

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from util.bazel.runfiles import get_required_path, own_repo_rlocation


def mount_agent_docs(app: FastAPI, *, package: str) -> None:
    directory = get_required_path(own_repo_rlocation(f"{package}/docs/agents"))

    @app.get("/llms.txt", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(directory / "llms.txt", media_type="text/plain")

    # Bazel runfiles use per-file symlinks into the service's packaged sources.
    app.mount("/docs/agents", StaticFiles(directory=directory, follow_symlink=True), name="agent-docs")
