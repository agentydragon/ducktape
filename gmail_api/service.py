"""Build an authenticated Gmail API service from an OAuth token."""

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Read/write scope needed to create/modify labels and change label membership.
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"

# google-api-python-client's `Resource` is dynamically generated and untyped, so the
# service is typed `Any` (matching gmail_archiver's GmailClient) — there is no usable
# static type for `.users().labels()...`.


def build_gmail_service(token_file: Path) -> Any:
    """Build a Gmail v1 service from an authorized-user OAuth token JSON.

    The token file is the standard google-auth authorized-user format (refresh
    token + client config); google-auth refreshes the access token as needed.
    """
    creds = Credentials.from_authorized_user_file(str(token_file))
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def credentials_from_token_dir(token_dir: Path, scopes: Sequence[str]) -> Credentials:
    """Google credentials backed by an externally-rotated access token.

    `token_dir` is a mounted secret holding an `access_token` (and `expires_at`)
    file, as written by Airlock and synced into the namespace by ESO. This
    process holds no refresh_token; google-auth's `refresh_handler` re-reads the
    directory when the access token expires, so the token Airlock rotates is
    picked up automatically. Generic over `scopes` and the target API — callers
    build whichever `googleapiclient.discovery.build(...)` service(s) the token's
    scopes cover from the one `Credentials` object.
    """

    def refresh_handler(request: object, scopes: Sequence[str] | None) -> tuple[str, dt.datetime]:
        # google-auth's Credentials.refresh() calls this with `scopes=` as a keyword
        # argument (see google.oauth2.credentials); the parameter name must match exactly.
        token = (token_dir / "access_token").read_text().strip()
        return token, _read_expiry(token_dir / "expires_at")

    return Credentials(token=None, scopes=list(scopes), refresh_handler=refresh_handler)


def build_gmail_service_from_token_dir(token_dir: Path) -> Any:
    """Build a Gmail v1 service backed by an externally-rotated access token.

    See `credentials_from_token_dir` for the token-directory contract.
    `static_discovery=True` keeps `build()` offline.
    """
    creds = credentials_from_token_dir(token_dir, [GMAIL_MODIFY_SCOPE])
    return build("gmail", "v1", credentials=creds, cache_discovery=False, static_discovery=True)


def _read_expiry(path: Path) -> dt.datetime:
    """Token expiry as a naive UTC datetime (google-auth's convention).

    Airlock writes `expires_at` as an ISO-8601 timestamp. Falls back to a
    near-future expiry so the handler re-reads soon if it is missing/unparseable.
    """
    fallback = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(minutes=5)
    if not path.exists():
        return fallback
    try:
        parsed = dt.datetime.fromisoformat(path.read_text().strip())
    except ValueError:
        return fallback
    return parsed.astimezone(dt.UTC).replace(tzinfo=None) if parsed.tzinfo is not None else parsed
