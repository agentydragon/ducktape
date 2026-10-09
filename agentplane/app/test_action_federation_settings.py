"""Validation and serialization of deployment-authored Action Service URLs."""

import pytest
import pytest_bazel
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from agentplane.app.action_federation_settings import ActionFederationSettings
from agentplane.app.model_catalog import ModelCatalog
from agentplane.app.settings import AppSettingsConfig

_FEDERATION: TypeAdapter[ActionFederationSettings] = TypeAdapter(ActionFederationSettings)


def _settings(service_url: str) -> dict[str, object]:
    return {
        "mode": "direct",
        "service_url": service_url,
        "login_jwks_uri": "https://login.test/jwks",
        "target": {"issuer": "https://login.test/", "audience": "actions", "jwks_uri": "https://login.test/jwks"},
        "scope": "openid",
    }


def test_host_only_service_url_remains_a_url_value_and_serializes_without_slash() -> None:
    federation = _FEDERATION.validate_python(_settings("http://actions.test:8080"))

    assert isinstance(federation.service_url, AnyHttpUrl)
    assert str(federation.service_url) == "http://actions.test:8080"

    config = AppSettingsConfig(
        models=ModelCatalog(models=[], harnesses={}),
        egress_admin_url="http://egress.test:8081",
        action_federation=federation,
    )
    assert (
        config.model_dump(mode="json", exclude_unset=True)["action_federation"]["service_url"]
        == "http://actions.test:8080"
    )


@pytest.mark.parametrize(
    "service_url",
    [
        "ftp://actions.test",
        "http://user:password@actions.test",
        "http://actions.test?private=value",
        "http://actions.test#fragment",
        "http://actions.test?",
        "http://actions.test#",
    ],
)
def test_service_url_rejects_non_http_and_sensitive_components(service_url: str) -> None:
    with pytest.raises(ValidationError):
        _FEDERATION.validate_python(_settings(service_url))


if __name__ == "__main__":
    pytest_bazel.main()
