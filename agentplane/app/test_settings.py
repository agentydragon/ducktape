"""App caller TLS settings preserve the service and operator authority boundaries."""

from pathlib import Path

import pytest
from pydantic import AnyHttpUrl, ValidationError

from agentplane.app.action_federation_settings import DirectFederationSettings
from agentplane.app.settings import Settings
from agentplane.identity.operator.settings import OperatorOidcSettings
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    APP_URI,
    EGRESS_PROXY_URI,
    HISTORY_SERVICE_URI,
    MCP_EDGE_URI,
    NOTIFICATIONS_URI,
    POLICY_CONTROLLER_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
    ClientTLS,
)
from agentplane.sandboxes.runtimeconfig.model_catalog import ModelCatalog


def _settings_fields() -> dict[str, object]:
    return {
        "models": ModelCatalog(models=[], harnesses={}),
        "egress_admin_url": "http://egress.test:8080",
        "connection_service_url": "http://mcp-edge.test:8080",
        "action_federation": DirectFederationSettings(
            service_url=AnyHttpUrl("https://actions.test"),
            login_jwks_uri="https://login.test/jwks",
            target=OperatorOidcSettings(
                issuer="https://actions.test", audience="agentplane", jwks_uri="https://actions.test/jwks"
            ),
            scope="actions:operator",
        ),
        "sandbox_namespace": "sandboxes",
        "runner_gateway_target": "runner-gateway.test:8080",
        "session_dispatcher_target": "session-dispatcher.test:8080",
        "history_service_target": "history.test:8080",
        "policy_service_target": "policy-controller.test:8083",
        "policy_service_tls": ClientTLS(
            ca_file=Path("/var/run/agentplane/trust/ca.pem"), server_uri=POLICY_CONTROLLER_URI
        ),
        "notifications_url": "http://notifications.test:8080",
    }


def _settings(**updates: object) -> Settings:
    fields = _settings_fields()
    fields.update(updates)
    return Settings.model_validate(fields)


def _svid(server_uri: str) -> ClientTLS:
    return ClientTLS(
        ca_file=Path("/var/run/agentplane/trust/ca.pem"),
        server_uri=server_uri,
        cert_file=Path("/var/run/agentplane/app/tls.crt"),
        key_file=Path("/var/run/agentplane/app/tls.key"),
        client_uri=APP_URI,
    )


def test_policy_service_requires_tls_and_an_explicit_target() -> None:
    fields = _settings_fields()
    del fields["policy_service_tls"]
    with pytest.raises(ValidationError, match="policy_service_tls"):
        Settings.model_validate(fields)

    fields = _settings_fields()
    del fields["policy_service_target"]
    with pytest.raises(ValidationError, match="policy_service_target"):
        Settings.model_validate(fields)


def test_service_svids_clear_only_omitted_legacy_token_defaults() -> None:
    legacy = _settings()
    assert legacy.session_dispatcher_token_file == Path("/var/run/secrets/agentplane-session-dispatcher/token")

    migrated = _settings(
        session_dispatcher_tls=_svid(SESSION_DISPATCHER_URI),
        runner_gateway_tls=_svid(SESSION_DISPATCHER_URI),
        history_service_tls=_svid(HISTORY_SERVICE_URI),
        policy_service_tls=_svid(POLICY_CONTROLLER_URI),
        egress_admin_tls=_svid(EGRESS_PROXY_URI),
        notifications_tls=_svid(NOTIFICATIONS_URI),
    )
    assert migrated.session_dispatcher_token_file is None
    assert migrated.runner_gateway_token_file is None
    assert migrated.history_service_token_file is None
    assert migrated.policy_service_token_file is None
    assert migrated.egress_admin_token_file is None
    assert migrated.notifications_token_file is None


@pytest.mark.parametrize("server_uri", [SESSION_DISPATCHER_URI, SANDBOX_CONTROLLER_URI])
def test_runtime_proxy_peer_accepts_each_cutover_receiver(server_uri: str) -> None:
    configured = _settings(runner_gateway_tls=_svid(server_uri))
    assert configured.runner_gateway_token_file is None
    assert configured.runner_gateway_tls is not None
    assert configured.runner_gateway_tls.server_uri == server_uri


def test_environment_service_peer_is_optional_and_uses_its_own_controller_svid() -> None:
    legacy = _settings()
    assert legacy.environment_service is None

    configured = _settings(
        environment_service={
            "target": "sandbox-controller.agentplane.svc:8082",
            "request_timeout_s": 12,
            "tls": _svid(SANDBOX_CONTROLLER_URI),
        }
    )
    assert configured.environment_service is not None
    assert configured.environment_service.target == "sandbox-controller.agentplane.svc:8082"
    assert configured.environment_service.tls.server_uri == SANDBOX_CONTROLLER_URI
    assert configured.session_dispatcher_target == "session-dispatcher.test:8080"
    assert configured.environment_service.model_dump(mode="json")["request_timeout_s"] == 12


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {
                "session_dispatcher_tls": _svid(SESSION_DISPATCHER_URI),
                "session_dispatcher_token_file": Path("/var/run/secrets/agentplane-session-dispatcher/token"),
            },
            "service SVID and bearer token authentication cannot be combined",
        ),
        (
            {
                "session_dispatcher_tls": _svid(SESSION_DISPATCHER_URI).model_copy(
                    update={"client_uri": NOTIFICATIONS_URI}
                ),
                "session_dispatcher_token_file": None,
            },
            "client_uri must be",
        ),
        ({"session_dispatcher_tls": None, "session_dispatcher_token_file": None}, "a token file is required"),
        ({"runner_gateway_tls": _svid(HISTORY_SERVICE_URI)}, "runner_gateway_tls.server_uri must identify"),
        ({"session_dispatcher_tls": _svid(SANDBOX_CONTROLLER_URI)}, "session_dispatcher_tls.server_uri must identify"),
        ({"connection_service_tls": _svid(SESSION_DISPATCHER_URI)}, "connection_service_tls.server_uri must identify"),
        ({"environment_service": {"target": "controller:8082", "request_timeout_s": 10}}, "Field required"),
        (
            {
                "environment_service": {
                    "target": "controller:8082",
                    "request_timeout_s": 10,
                    "tls": _svid(SESSION_DISPATCHER_URI),
                }
            },
            "environment_service.tls.server_uri must identify",
        ),
        (
            {
                "environment_service": {
                    "target": "controller:8082",
                    "request_timeout_s": 10,
                    "tls": _svid(SANDBOX_CONTROLLER_URI).model_copy(update={"client_uri": NOTIFICATIONS_URI}),
                }
            },
            "client_uri must be",
        ),
        (
            {
                "environment_service": {
                    "target": "controller:8082",
                    "request_timeout_s": 10,
                    "tls": _svid(SANDBOX_CONTROLLER_URI).model_copy(update={"key_file": None}),
                }
            },
            "service SVID requires",
        ),
    ],
)
def test_invalid_app_caller_tls_settings_are_rejected(updates: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _settings(**updates)


def test_operator_facades_can_add_app_svid_without_replacing_operator_oauth() -> None:
    settings = _settings(actions_tls=_svid(ACTIONS_URI), connection_service_tls=_svid(MCP_EDGE_URI))
    assert settings.actions_tls is not None
    assert settings.actions_tls.uses_svid
    assert settings.connection_service_tls is not None
    assert settings.connection_service_tls.uses_svid
