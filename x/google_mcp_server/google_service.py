"""Shared per-call builder for haku-console's in-process Google MCP servers.

Both the `gmail` and `google_calendar` servers build a fresh google-api-python-client
service for each call from the acting Operator's access token; this is the one place that
construction lives.
"""

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


def build_google_api_service(api: str, version: str, access_token: str | None):
    """Build a bearer-only google-api-python-client service (Resource) for one call.

    Bearer-only credentials: the provider-connection store keeps the token fresh, so no
    refresh handler is needed. ``static_discovery=True`` keeps ``build()`` offline (no I/O
    per call). ``access_token`` is None only when building for tool-schema reflection (no
    API call runs).
    """
    creds = Credentials(token=access_token)
    return build(api, version, credentials=creds, cache_discovery=False, static_discovery=True)
