"""Build Google API service resources for the Gmail and Calendar MCP servers.

The standalone server supplies its Airlock-minted access token. Schema reflection supplies no
token and does not make API calls.
"""

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


def build_google_api_service(api: str, version: str, access_token: str | None):
    """Build a bearer-only google-api-python-client service (Resource).

    ``static_discovery=True`` keeps service construction offline. ``access_token`` is None only
    when building for tool-schema reflection, where no API call runs.
    """
    creds = Credentials(token=access_token)
    return build(api, version, credentials=creds, cache_discovery=False, static_discovery=True)
