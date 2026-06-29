"""Build an authenticated Gmail API service from an OAuth token file."""

from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build

# Read/write scope needed to create/modify labels and change label membership.
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def build_gmail_service(token_file: Path) -> Resource:
    """Build a Gmail v1 service client from an authorized-user OAuth token JSON.

    The token file is the standard google-auth authorized-user format (refresh
    token + client config); google-auth refreshes the access token as needed.
    """
    creds = Credentials.from_authorized_user_file(str(token_file))
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
