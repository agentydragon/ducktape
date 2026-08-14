"""Tests for routing card-returned URLs through the configured gateway."""

import pytest
import pytest_bazel

from cpap.card import EZShareClient


@pytest.fixture
def client() -> EZShareClient:
    return EZShareClient("http://cpap-card.cpap-sync.svc.cluster.local")


def test_card_absolute_url_uses_gateway_host(client: EZShareClient) -> None:
    assert (
        client._proxy_url("http://192.168.4.1/client?command=GETFILELIST&dir=A%3A")
        == "http://cpap-card.cpap-sync.svc.cluster.local/client?command=GETFILELIST&dir=A%3A"
    )


def test_relative_url_is_resolved_against_gateway(client: EZShareClient) -> None:
    assert client._proxy_url("/download?file=STR.EDF") == (
        "http://cpap-card.cpap-sync.svc.cluster.local/download?file=STR.EDF"
    )


if __name__ == "__main__":
    pytest_bazel.main()
