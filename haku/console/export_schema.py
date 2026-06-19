"""Print the Haku console's OpenAPI schema to stdout (for frontend type-gen).

Driven by ``//haku/console/frontend:schema`` (``js_openapi_schema``) to generate
``api/schema.d.ts``. Only route/model definitions are needed, so the git settings
are dummies and no clone happens (``app.openapi()`` doesn't run the lifespan).
"""

from __future__ import annotations

import json

from haku.console.app import create_app
from haku.console.config import Settings
from haku.console.git_state import GitState


def main() -> None:
    settings = Settings(git_repo_url="http://localhost:0", git_username="x", git_password="x")
    git_state = GitState(
        repo_url=settings.git_repo_url,
        username=settings.git_username,
        password=settings.git_password.get_secret_value(),
        clone_dir=settings.clone_dir,
        branch=settings.branch,
    )
    app = create_app(settings, git_state=git_state)
    print(json.dumps(app.openapi(), indent=2))


if __name__ == "__main__":
    main()
