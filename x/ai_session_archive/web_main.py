"""Run the owner-authenticated session archive browser."""

import uvicorn

from x.ai_session_archive.database import make_engine
from x.ai_session_archive.web import create_app
from x.ai_session_archive.web_settings import WebSettings


def main() -> None:
    settings = WebSettings()
    engine = make_engine(settings.database_url.get_secret_value())
    app = create_app(engine=engine, settings=settings, frontend_dir=settings.frontend_dir)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
