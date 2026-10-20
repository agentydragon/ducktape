"""Validation and JSON contract for the public KubeVirt guest seed."""

import json

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.sandboxes.guest_config import GuestConfig


def _config(**updates: object) -> GuestConfig:
    values: dict[str, object] = {
        "version": 1,
        "control": {"target": "test-gateway.test:7100", "bearer": "test-control-placeholder"},
        "ingress": {"target": "test-history.test:8081", "bearer": "test-ingress-placeholder"},
        "llm_base_url": "http://llm-ingress.agentplane.svc.cluster.local:8080",
        "proxy_url": "http://10.0.2.2:3128",
        "platform_instructions": "Test platform guidance.",
    }
    return GuestConfig.model_validate(values | updates)


def test_guest_seed_serializes_origin_urls_without_trailing_slashes() -> None:
    config = _config(llm_base_url="HTTPS://LLM.Example:8443/", proxy_url="http://Proxy.Example:3128/")

    serialized = json.loads(config.model_dump_json())

    assert serialized == {
        "version": 1,
        "control": {"target": "test-gateway.test:7100", "bearer": "test-control-placeholder"},
        "ingress": {"target": "test-history.test:8081", "bearer": "test-ingress-placeholder"},
        "llm_base_url": "https://llm.example:8443",
        "proxy_url": "http://proxy.example:3128",
        "platform_instructions": "Test platform guidance.",
        "console_autologin": False,
        "harness_env": None,
    }


def test_harness_env_reaches_the_guest_seed_only_when_set() -> None:
    assert "harness_env" not in json.loads(_config().model_dump_json(exclude_defaults=True))

    seeded = json.loads(_config(harness_env={"EXAMPLE_NAME": "value"}).model_dump_json(exclude_defaults=True))

    assert seeded["harness_env"] == {"EXAMPLE_NAME": "value"}


@pytest.mark.parametrize(
    "updates",
    [
        {"version": 2},
        {"control": {"target": "test-gateway.test:7100"}},
        {"ingress": {"target": "", "bearer": "test-ingress-placeholder"}},
        {"llm_base_url": "ftp://model.test"},
        {"llm_base_url": "model.test"},
        {"proxy_url": "http://proxy.test:65536"},
        {"proxy_url": "http://proxy.test:0"},
        {"llm_base_url": "https://user:password@model.test"},
        {"llm_base_url": "https://model.test/v1"},
        {"proxy_url": "http://proxy.test/path"},
        {"platform_instructions": ""},
        {"unexpected": "value"},
    ],
)
def test_invalid_guest_seed_is_rejected(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _config(**updates)


if __name__ == "__main__":
    pytest_bazel.main()
