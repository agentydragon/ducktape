"""Run the owner-authenticated session archive browser."""

from importlib import resources
from pathlib import Path

import uvicorn

from x.ai_session_archive.database import make_engine
from x.ai_session_archive.web import create_app
from x.ai_session_archive.web_settings import WebSettings


def _frontend_dir(settings: WebSettings) -> Path:
    frontend_dir = settings.frontend_dir
    if frontend_dir is None:
        frontend_dir = Path(str(resources.files("x.ai_session_archive") / "frontend" / "dist"))
    if not (frontend_dir / "index.html").is_file():
        raise FileNotFoundError(frontend_dir / "index.html")
    return frontend_dir


def main() -> None:
    settings = WebSettings()
    engine = make_engine(settings.database_url.get_secret_value())
    app = create_app(engine=engine, settings=settings, frontend_dir=_frontend_dir(settings))
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
