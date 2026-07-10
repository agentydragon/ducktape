"""Tests for credentials_from_token_dir's refresh handling."""

import datetime as dt
from pathlib import Path

import pytest_bazel

from gmail_api.service import credentials_from_token_dir


def test_refresh_calls_handler_the_way_google_auth_does(tmp_path: Path) -> None:
    """google-auth's Credentials.refresh() calls refresh_handler(request, scopes=scopes) —
    a keyword argument named `scopes`. A prior refactor renamed the parameter to
    `requested_scopes` and broke this at runtime (TypeError), undetected because no test
    exercised the actual google-auth call convention.
    """
    expires_at = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(hours=1)
    (tmp_path / "access_token").write_text("the-token")
    (tmp_path / "expires_at").write_text(expires_at.isoformat())

    creds = credentials_from_token_dir(tmp_path, ["https://www.googleapis.com/auth/gmail.modify"])
    creds.refresh(None)

    assert creds.token == "the-token"
    assert creds.expiry == expires_at


if __name__ == "__main__":
    pytest_bazel.main()
