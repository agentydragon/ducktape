"""Runner Gateway rejects ambiguous control security in its authored deployment settings."""

from typing import cast

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.runner_gateway.settings import Settings


def _settings(runner_security: object) -> Settings:
    return cast(
        Settings,
        Settings.model_validate(
            {
                "sandbox_namespace": "test-sandboxes",
                "dispatcher_database_url": "postgresql://test:test@db.invalid/dispatcher",
                "pod_ip": "192.0.2.10",
                "runner_token_file": "/run/test/token",
                "dispatcher_audience": "test-dispatcher",
                "gateway_audience": "test-gateway",
                "dispatcher_callers": {"test-app/test-dispatcher"},
                "gateway_callers": {"test-app/test-gateway"},
                "runner_security": runner_security,
            }
        ),
    )


@pytest.mark.parametrize(
    "security",
    [
        {"mode": "mtls", "tls": {"cert_file": "/test/cert", "key_file": "/test/key"}},
        {"mode": "insecure", "tls": {"cert_file": "/test/cert", "key_file": "/test/key", "ca_file": "/test/ca"}},
        {"mode": "unknown"},
        {"tls": {"cert_file": "/test/cert", "key_file": "/test/key", "ca_file": "/test/ca"}},
    ],
    ids=["incomplete-mtls", "insecure-with-tls", "unknown-mode", "missing-mode"],
)
def test_gateway_rejects_ambiguous_runner_security(security: object) -> None:
    with pytest.raises(ValidationError):
        _settings(security)


def test_gateway_requires_explicit_runner_security_mode() -> None:
    with pytest.raises(ValidationError):
        _settings(None)


if __name__ == "__main__":
    pytest_bazel.main()
