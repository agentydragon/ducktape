"""Plaid SDK client factory + credentials.

Thin glue over the official `plaid-python` SDK: `plaid_client()` builds a
`plaid_api.PlaidApi` from `PlaidCreds`. The MCP server calls that SDK client directly and
validates the responses into `plaid_utils.models` at the tool boundary; the SDK's
`ApiException` propagates to the FastMCP error boundary. Dev credential loading (sops/env)
lives in `plaid_utils.dev_creds` so the MCP server never bundles that machinery.
"""

from dataclasses import dataclass

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
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))
