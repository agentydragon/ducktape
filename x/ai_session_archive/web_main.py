"""Run the owner-authenticated session archive browser."""

import os
from pathlib import Path

import uvicorn

from devinfra.bazel.runfiles import get_required_path
from x.ai_session_archive.database import make_engine
from x.ai_session_archive.web import create_app
from x.ai_session_archive.web_settings import WebSettings

_FRONTEND_RLOCATION_ENV = "AI_SESSION_ARCHIVE_FRONTEND_RLOCATION"


def _frontend_dir(settings: WebSettings) -> Path:
    if settings.frontend_dir is not None:
        frontend_dir = settings.frontend_dir
    else:
        rlocation = os.environ.get(_FRONTEND_RLOCATION_ENV)
        if not rlocation:
            raise RuntimeError(f"{_FRONTEND_RLOCATION_ENV} must be set by the web binary's Bazel target")
        frontend_dir = get_required_path(rlocation)
    if not (frontend_dir / "index.html").is_file():
        raise RuntimeError(f"session-archive frontend at {frontend_dir} has no index.html")
    return frontend_dir


def main() -> None:
    settings = WebSettings()
    engine = make_engine(settings.database_url.get_secret_value())
    app = create_app(engine=engine, settings=settings, frontend_dir=_frontend_dir(settings))
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
