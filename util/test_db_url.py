from __future__ import annotations

import pytest_bazel
from sqlalchemy.engine import URL, make_url

from util.db_url import render_postgresql_url


def test_render_preserves_escaped_credentials_and_duplicate_query_values() -> None:
    url = (
        "postgresql://user%40ops:p%2F%3F%23%3A%25@db.example:5432/app"
        "?options=-c%20search_path%3Dpublic&options=-c%20application_name%3Dworker"
    )

    rendered = render_postgresql_url(url, "postgresql+asyncpg")
    parsed = make_url(rendered)

    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.username == "user@ops"
    assert parsed.password == "p/?#:%"
    assert parsed.query["options"] == ("-c search_path=public", "-c application_name=worker")
    assert rendered.count("options=") == 2
    assert "user%40ops:p%2F%3F%23%3A%25@" in rendered


def test_render_accepts_sqlalchemy_url_and_keeps_target_driver() -> None:
    url = URL.create(
        "postgresql+asyncpg",
        username="user@ops",
        password="p/?#:%",
        host="db.example",
        database="app",
        query={"options": ["-c search_path=public", "-c application_name=worker"]},
    )

    rendered = render_postgresql_url(url, "postgresql+asyncpg")

    assert rendered == url.render_as_string(hide_password=False)
    assert make_url(rendered).query["options"] == url.query["options"]


if __name__ == "__main__":
    pytest_bazel.main()
