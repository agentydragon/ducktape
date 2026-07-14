"""Tests for Authentik authentication provider composition."""

from collections.abc import Generator
from dataclasses import dataclass
from typing import cast
from unittest.mock import Mock, patch

import httpx
import pytest
import pytest_bazel
from fastmcp.server.auth.auth import TokenVerifier
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration

from mcp_infra.authentik_auth.config import AuthentikAuthConfig, DirectJwtTrust
from mcp_infra.authentik_auth.provider import DEFAULT_VALID_SCOPES, build_authentik_auth, compose_authentik_auth


def _config(
    issuer: str = "https://auth.example.com/application/o/test/", public_base_url: str = "https://mcp.example.com"
) -> AuthentikAuthConfig:
    return AuthentikAuthConfig(
        oidc_issuer=issuer, oidc_client_id="id", oidc_client_secret="secret", public_base_url=public_base_url
    )


def _direct_trust(
    issuer: str = "https://auth.example.com/application/o/machine/",
    audience: str = "machine",
    required_scopes: tuple[str, ...] = ("openid",),
) -> DirectJwtTrust:
    return DirectJwtTrust(issuer=issuer, audiences=(audience,), required_scopes=required_scopes)


@dataclass(frozen=True)
class _AuthBuildHarness:
    http_get: Mock
    downstream_proxy_cls: Mock
    jwt_verifier_cls: Mock
    multi_auth_cls: Mock


@pytest.fixture
def auth_build_harness() -> Generator[_AuthBuildHarness]:
    with (
        patch.object(httpx, "get") as http_get,
        patch("mcp_infra.authentik_auth.provider.DownstreamClientIdentityOIDCProxy") as downstream_proxy_cls,
        patch("mcp_infra.authentik_auth.provider.JWTVerifier") as jwt_verifier_cls,
        patch("mcp_infra.authentik_auth.provider.MultiAuth") as multi_auth_cls,
    ):
        downstream_proxy_cls.return_value.oidc_config = OIDCConfiguration(
            strict=False, jwks_uri="https://auth.example.com/application/o/test/jwks/"
        )
        yield _AuthBuildHarness(
            http_get=http_get,
            downstream_proxy_cls=downstream_proxy_cls,
            jwt_verifier_cls=jwt_verifier_cls,
            multi_auth_cls=multi_auth_cls,
        )


def test_build_authentik_auth_uses_proxy_discovery_jwks_uri(auth_build_harness: _AuthBuildHarness) -> None:
    """Direct JWT verification reuses OIDCProxy's validated discovery result."""
    advertised_jwks = "https://auth.example.com/application/o/test/jwks/"
    auth_build_harness.downstream_proxy_cls.return_value.oidc_config = OIDCConfiguration(
        strict=False, jwks_uri=advertised_jwks
    )
    build_authentik_auth(_config().model_copy(update={"direct_jwt_trusts": (_direct_trust(),)}))

    auth_build_harness.http_get.assert_not_called()
    auth_build_harness.jwt_verifier_cls.assert_called_once()
    kwargs = auth_build_harness.jwt_verifier_cls.call_args.kwargs
    assert kwargs["jwks_uri"] == advertised_jwks


def test_build_authentik_auth_sets_default_scopes_through_proxy_api(auth_build_harness: _AuthBuildHarness) -> None:
    build_authentik_auth(_config())

    auth_build_harness.downstream_proxy_cls.return_value.update_default_scopes.assert_called_once_with(
        DEFAULT_VALID_SCOPES
    )


def test_build_authentik_auth_sets_custom_scopes_through_proxy_api(auth_build_harness: _AuthBuildHarness) -> None:
    custom_scopes = ["openid", "propose", "read"]
    build_authentik_auth(_config(), valid_scopes=custom_scopes)

    auth_build_harness.downstream_proxy_cls.return_value.update_default_scopes.assert_called_once_with(custom_scopes)


def test_build_authentik_auth_requires_authorization_consent(auth_build_harness: _AuthBuildHarness) -> None:
    build_authentik_auth(_config())

    assert auth_build_harness.downstream_proxy_cls.call_args.kwargs["require_authorization_consent"] is True


def test_build_authentik_auth_accepts_issuer_with_trailing_slash(auth_build_harness: _AuthBuildHarness) -> None:
    """JWTVerifier must accept the issuer both with and without a trailing slash.

    Authentik's per-provider tokens carry `iss` WITH a trailing slash, but
    `normalized_issuer()` strips it and JWTVerifier matches `iss` by exact string,
    so the bare form alone rejects every real Authentik token. Regression: this
    silently blocked all direct machine bearer tokens (e.g. haku → grocy MCP).
    """
    build_authentik_auth(
        _config().model_copy(
            update={"direct_jwt_trusts": (_direct_trust(issuer="https://auth.example.com/application/o/machine/"),)}
        )
    )

    issuer = auth_build_harness.jwt_verifier_cls.call_args.kwargs["issuer"]
    assert "https://auth.example.com/application/o/machine/" in issuer
    assert "https://auth.example.com/application/o/machine" in issuer


def test_build_authentik_auth_keeps_direct_jwt_trusts_separate(auth_build_harness: _AuthBuildHarness) -> None:
    cfg = _config().model_copy(
        update={
            "direct_jwt_trusts": (
                _direct_trust(),
                _direct_trust(
                    issuer="https://auth.example.com/application/o/other-machine/",
                    audience="other-machine",
                    required_scopes=("profile",),
                ),
            )
        }
    )
    build_authentik_auth(cfg)

    assert auth_build_harness.jwt_verifier_cls.call_count == 2
    first, second = (call.kwargs for call in auth_build_harness.jwt_verifier_cls.call_args_list)
    assert first == {
        "jwks_uri": "https://auth.example.com/application/o/test/jwks/",
        "issuer": ["https://auth.example.com/application/o/machine", "https://auth.example.com/application/o/machine/"],
        "audience": ["machine"],
        "required_scopes": ["openid"],
    }
    assert second == {
        "jwks_uri": "https://auth.example.com/application/o/test/jwks/",
        "issuer": [
            "https://auth.example.com/application/o/other-machine",
            "https://auth.example.com/application/o/other-machine/",
        ],
        "audience": ["other-machine"],
        "required_scopes": ["profile"],
    }


def test_build_authentik_auth_appends_extra_verifiers(auth_build_harness: _AuthBuildHarness) -> None:
    sentinel = cast("TokenVerifier", object())
    build_authentik_auth(_config(), extra_verifiers=[sentinel])

    auth_build_harness.jwt_verifier_cls.assert_not_called()
    assert auth_build_harness.multi_auth_cls.call_args.kwargs["verifiers"] == [sentinel]


def test_build_authentik_auth_constructs_the_stock_downstream_identity_proxy(
    auth_build_harness: _AuthBuildHarness,
) -> None:
    build_authentik_auth(_config())

    auth_build_harness.downstream_proxy_cls.assert_called_once()


def test_compose_authentik_auth_uses_caller_owned_proxy_without_reconfiguring_it(
    auth_build_harness: _AuthBuildHarness,
) -> None:
    proxy = auth_build_harness.downstream_proxy_cls.return_value
    sentinel = cast("TokenVerifier", object())

    compose_authentik_auth(proxy=proxy, extra_verifiers=[sentinel])

    auth_build_harness.downstream_proxy_cls.assert_not_called()
    proxy.update_default_scopes.assert_not_called()
    assert auth_build_harness.multi_auth_cls.call_args.kwargs == {"server": proxy, "verifiers": [sentinel]}


if __name__ == "__main__":
    pytest_bazel.main()
