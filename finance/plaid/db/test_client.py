"""Tests for the Plaid SDK client factory."""

from pathlib import Path
from typing import cast

import certifi
import pytest_bazel
from plaid import ApiClient

from finance.plaid.db.client import PlaidClient, PlaidCreds


def test_plaid_client_pins_certifi_ca_bundle() -> None:
    # The debian_slim runtime image ships no system CA bundle, so the client must point
    # urllib3 at certifi's, or production.plaid.com fails with CERTIFICATE_VERIFY_FAILED.
    with PlaidClient(PlaidCreds(client_id="cid", secret="secret", env="production")) as client:
        ca_cert = cast(ApiClient, client.api_client).configuration.ssl_ca_cert
    assert ca_cert == certifi.where()
    assert Path(ca_cert).exists()


if __name__ == "__main__":
    pytest_bazel.main()
