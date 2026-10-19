"""Build an authenticated Gmail API service from an OAuth token."""

from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

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
