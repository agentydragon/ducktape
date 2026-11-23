"""Tests for routing card-returned URLs through the configured gateway."""

import pytest

from cpap.card import EZShareClient


@pytest.fixture
def client() -> EZShareClient:
    return EZShareClient("http://test-cpap-gateway.invalid")


def test_card_absolute_url_uses_gateway_host(client: EZShareClient) -> None:
    assert (
        client._proxy_url("http://192.168.4.1/client?command=GETFILELIST&dir=A%3A")
        == "http://test-cpap-gateway.invalid/client?command=GETFILELIST&dir=A%3A"
    )


def test_relative_url_is_resolved_against_gateway(client: EZShareClient) -> None:
    assert client._proxy_url("/download?file=STR.EDF") == ("http://test-cpap-gateway.invalid/download?file=STR.EDF")
