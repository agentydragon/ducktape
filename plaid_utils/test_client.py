"""Tests for the Plaid SDK client factory."""

from pathlib import Path

import certifi
import pytest_bazel

from plaid_utils.client import PlaidCreds, plaid_client


def test_plaid_client_pins_certifi_ca_bundle() -> None:
    # The debian_slim runtime image ships no system CA bundle, so the client must point
    # urllib3 at certifi's, or production.plaid.com fails with CERTIFICATE_VERIFY_FAILED.
    api = plaid_client(PlaidCreds(client_id="cid", secret="secret", env="production"))
    ca_cert = api.api_client.configuration.ssl_ca_cert
    assert ca_cert == certifi.where()
    assert Path(ca_cert).exists()


if __name__ == "__main__":
    pytest_bazel.main()
