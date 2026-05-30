"""Plaid SDK client factory + credentials.

Thin glue over the official `plaid-python` SDK: `plaid_client()` builds a
`plaid_api.PlaidApi` from `PlaidCreds`. The MCP server calls that SDK client directly and
validates the responses into `plaid_utils.models` at the tool boundary; the SDK's
`ApiException` propagates to the FastMCP error boundary. Dev credential loading (sops/env)
lives in `plaid_utils.dev_creds` so the MCP server never bundles that machinery.
"""

from dataclasses import dataclass

import certifi
import plaid
from plaid.api import plaid_api

# Plaid removed the `development` environment in 2024; only sandbox/production remain.
PLAID_HOSTS = {"sandbox": plaid.Environment.Sandbox, "production": plaid.Environment.Production}


@dataclass(frozen=True)
class PlaidCreds:
    client_id: str
    secret: str
    env: str


def plaid_client(creds: PlaidCreds) -> plaid_api.PlaidApi:
    configuration = plaid.Configuration(
        host=PLAID_HOSTS[creds.env], api_key={"clientId": creds.client_id, "secret": creds.secret}
    )
    # plaid-python (urllib3) passes ca_certs=ssl_ca_cert; left unset urllib3 falls back to the
    # system trust store, which the debian_slim runtime image ships empty -> production.plaid.com
    # fails with CERTIFICATE_VERIFY_FAILED. Point it at certifi's bundle (already in the image via
    # fastmcp -> httpx), matching how the other MCP servers get their CA roots.
    configuration.ssl_ca_cert = certifi.where()
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))
