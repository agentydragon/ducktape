"""Pinned FastMCP OAuth protocol around Agentplane's durable consent and grant authority."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, SupportsFloat, cast
from uuid import UUID

import fastmcp
import jwt
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.wrappers.base import BaseWrapper
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.errors import stringify_pydantic_error
from mcp.server.auth.handlers.revoke import RevocationErrorResponse, RevocationRequest
from mcp.server.auth.json_response import PydanticJSONResponse
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken as McpAccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
)
from mcp.server.auth.routes import cors_middleware
from mcp.server.auth.settings import RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from mcp_infra.authentik_auth.fastmcp_proxy import DownstreamClientIdentityOIDCProxy, RetryableJWTVerifier
from mcp_infra.authentik_auth.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    InvalidOidcPrincipalError,
    OidcPrincipalVerificationUnavailableError,
)
from mcp_infra.persistence import PostgresPersistence, build_shared_client_storage
from x.agentplane.action_service.connections import (
    ConnectionAuthority,
    ConnectionConflictError,
    ConnectionNotFoundError,
    Grant,
    GrantRejectedError,
)
from x.agentplane.action_service.enrollments import EnrollmentAuthority, EnrollmentInput, EnrollmentRejectedError
from x.agentplane.action_service.models import Principal, PrincipalRole

_ISSUING: ContextVar[UUID | None] = ContextVar("agentplane_oauth_issuing", default=None)
_VERIFY_FAILURES: ContextVar[list[Exception] | None] = ContextVar("agentplane_oauth_verify_failures", default=None)
_INVALID_GRANT = "The Connection authorization grant is invalid; authorize a new connection."


def _observe_failure(error: Exception) -> None:
    failures = _VERIFY_FAILURES.get()
    if failures is not None:
        failures.append(error)


class _ObservedStorage(BaseWrapper):
    """Preserve storage outages before the pinned provider converts them to a false 401."""

    def __init__(self, storage: AsyncKeyValue) -> None:
        self.key_value = storage

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        try:
            return cast(dict[str, Any] | None, await super().get(key, collection=collection))
        except Exception as error:
            _observe_failure(error)
            raise

    async def put(
        self, key: str, value: Mapping[str, Any], *, collection: str | None = None, ttl: SupportsFloat | None = None
    ) -> None:
        try:
            await super().put(key, value, collection=collection, ttl=ttl)
        except Exception as error:
            _observe_failure(error)
            raise

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        try:
            return cast(bool, await super().delete(key, collection=collection))
        except Exception as error:
            _observe_failure(error)
            raise


class _ObservedVerifier(RetryableJWTVerifier):
    async def _fetch_jwks(self) -> dict[str, Any]:
        try:
            return await super()._fetch_jwks()
        except Exception as error:
            _observe_failure(error)
            raise


class OAuthSettings(BaseModel):
    """Explicit deployment pins; no default issuer, operator mapping, or ephemeral keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    config_url: str
    upstream_client_id: str = Field(min_length=1)
    upstream_client_secret_file: Path
    base_url: str
    integration_app_url: str
    jwt_signing_key_file: Path
    encryption_key_file: Path
    upstream_issuer: str
    upstream_subject: str = Field(min_length=1)
    approving_operator: Principal

    @model_validator(mode="after")
    def operator_mapping(self) -> OAuthSettings:
        if self.approving_operator.role is not PrincipalRole.OPERATOR:
            raise ValueError("approving_operator must be the configured operator principal")
        return self


class ActionsOAuthProxy(DownstreamClientIdentityOIDCProxy):
    """Keep DCR/PKCE/token persistence in FastMCP; resolve every grant in canonical storage.

    The one private read, _code_store, is required before FastMCP consumes the code.
    The protected claim hook runs too late for retryable principal/authority checks.
    A durable one-shot enrollment claim precedes consumption; ambiguous issuance after
    that point requires fresh OAuth, never a blind second token family.
    """

    def __init__(
        self,
        settings: OAuthSettings,
        *,
        client_storage: AsyncKeyValue,
        enrollments: EnrollmentAuthority,
        connections: ConnectionAuthority,
    ) -> None:
        if fastmcp.__version__ != "3.4.4":
            raise RuntimeError("ActionsOAuthProxy requires compatibility verification against FastMCP 3.4.4")
        signing_key = settings.jwt_signing_key_file.read_text().strip()
        client_secret = settings.upstream_client_secret_file.read_text().strip()
        if len(signing_key) < 32 or not client_secret:
            raise ValueError("OAuth requires a nonempty client secret and a signing key of at least 32 characters")
        super().__init__(
            config_url=settings.config_url,
            client_id=settings.upstream_client_id,
            client_secret=client_secret,
            base_url=settings.base_url,
            client_storage=_ObservedStorage(client_storage),
            jwt_signing_key=signing_key,
            require_authorization_consent="external",
            enable_cimd=True,
        )
        self._settings = settings
        # Local grants are revocable even if the authentication IdP has no revoke endpoint.
        self.revocation_options = RevocationOptions(enabled=True)
        self._enrollments = enrollments
        self._connections = connections
        self._principal_resolver = AuthentikOidcPrincipalResolver(
            expected_issuer=settings.upstream_issuer,
            discovered_issuer=str(self.oidc_config.issuer),
            jwks_uri=str(self.oidc_config.jwks_uri),
            signing_algorithms=self.oidc_config.id_token_signing_alg_values_supported,
            client_id=settings.upstream_client_id,
        )
        self.update_default_scopes(["openid", "email", "profile", "offline_access"])

    def get_token_verifier(
        self,
        *,
        algorithm: str | None = None,
        audience: str | None = None,
        required_scopes: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> TokenVerifier:
        del timeout_seconds
        return _ObservedVerifier(
            jwks_uri=str(self.oidc_config.jwks_uri),
            issuer=str(self.oidc_config.issuer),
            algorithm=algorithm,
            audience=audience,
            required_scopes=required_scopes,
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        upstream_url = await super().authorize(client, params)
        if client.client_id is None or not params.code_challenge:
            raise AuthorizeError("invalid_request", "A registered client and S256 PKCE challenge are required")
        try:
            created = await self._enrollments.create(
                EnrollmentInput(
                    issuer=str(self.base_url),
                    client_id=client.client_id,
                    client_name=client.client_name,
                    redirect_uri=str(params.redirect_uri),
                    code_challenge=params.code_challenge,
                    upstream_url=upstream_url,
                    expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
            )
        except EnrollmentRejectedError:
            raise AuthorizeError("invalid_request", "This authorization interaction cannot be reused") from None
        except SQLAlchemyError:
            raise AuthorizeError("temporarily_unavailable", "Connection consent is temporarily unavailable") from None
        return f"{self._settings.integration_app_url.rstrip('/')}/#/connection-enrollments/{created.handle}"

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        try:
            code = await self._code_store.get(key=authorization_code.code)
            if code is None or code.client_id != client.client_id or authorization_code.client_id != client.client_id:
                raise TokenError("invalid_grant", _INVALID_GRANT)
            principal = await self._principal_resolver.resolve(code.idp_tokens)
            if (principal.issuer, principal.subject) != (
                self._settings.upstream_issuer,
                self._settings.upstream_subject,
            ):
                raise InvalidOidcPrincipalError
            binding = await self._enrollments.approved(
                client_id=code.client_id,
                redirect_uri=code.redirect_uri,
                code_challenge=code.code_challenge or "",
                operator=self._settings.approving_operator,
            )
            grant = await self._connections.bind(binding)
            await self._connections.validate_pending(grant.id, issuer=binding.issuer, client_id=code.client_id)
            await self._enrollments.claim_exchange(grant.id)
        except (
            InvalidOidcPrincipalError,
            EnrollmentRejectedError,
            GrantRejectedError,
            ConnectionConflictError,
            ConnectionNotFoundError,
        ):
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        except (OidcPrincipalVerificationUnavailableError, SQLAlchemyError):
            raise _unavailable() from None

        context = _ISSUING.set(grant.id)
        try:
            token = await super().exchange_authorization_code(client, authorization_code)
        finally:
            _ISSUING.reset(context)
        self._validate_family(token, grant)
        try:
            await self._connections.activate(grant.id)
        except GrantRejectedError:
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        except SQLAlchemyError:
            raise _unavailable() from None
        return token

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any]:
        grant_id = _ISSUING.get()
        if grant_id is None:
            raise TokenError("invalid_grant", _INVALID_GRANT)
        # Refresh must not attach the old grant to a different upstream login.
        try:
            principal = await self._principal_resolver.resolve(idp_tokens)
            if (principal.issuer, principal.subject) != (
                self._settings.upstream_issuer,
                self._settings.upstream_subject,
            ):
                raise InvalidOidcPrincipalError
        except InvalidOidcPrincipalError:
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        except OidcPrincipalVerificationUnavailableError:
            raise _unavailable() from None
        return {"grant_id": str(grant_id)}

    def _reference(self, token: str, *, token_use: str) -> tuple[UUID, str, str]:
        claims = self.jwt_issuer.verify_token(token, expected_token_use=token_use)
        grant_claims = claims["upstream_claims"]
        if not isinstance(grant_claims, dict) or set(grant_claims) != {"grant_id"}:
            raise ValueError("invalid grant reference")
        grant_id = UUID(grant_claims["grant_id"])
        issuer, client_id = claims["iss"], claims["client_id"]
        if not isinstance(issuer, str) or not isinstance(client_id, str) or not client_id:
            raise ValueError("invalid token identity")
        return grant_id, issuer, client_id

    async def _resolve(self, reference: tuple[UUID, str, str]) -> Grant:
        grant_id, issuer, client_id = reference
        return await self._connections.resolve(grant_id, issuer=issuer, client_id=client_id)

    def _validate_family(self, token: OAuthToken, grant: Grant) -> None:
        expected = (grant.id, grant.issuer, grant.client_id)
        if self._reference(token.access_token, token_use="access") != expected:
            raise TokenError("invalid_grant", _INVALID_GRANT)
        if token.refresh_token is not None and self._reference(token.refresh_token, token_use="refresh") != expected:
            raise TokenError("invalid_grant", _INVALID_GRANT)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        try:
            reference = self._reference(refresh_token.token, token_use="refresh")
        except Exception:
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        if reference[2] != client.client_id or refresh_token.client_id != client.client_id:
            raise TokenError("invalid_grant", _INVALID_GRANT)
        try:
            grant = await self._resolve(reference)
            context = _ISSUING.set(grant.id)
            try:
                token = await super().exchange_refresh_token(client, refresh_token, scopes)
            finally:
                _ISSUING.reset(context)
            self._validate_family(token, grant)
            await self._resolve(reference)
            return token
        except GrantRejectedError:
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        except SQLAlchemyError:
            raise _unavailable() from None

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            reference = self._reference(token, token_use="access")
        except Exception:
            return None
        try:
            await self._resolve(reference)
            failures: list[Exception] = []
            observation = _VERIFY_FAILURES.set(failures)
            try:
                validated = await super().load_access_token(token)
            finally:
                _VERIFY_FAILURES.reset(observation)
            if validated is None:
                if failures:
                    raise _unavailable() from failures[0]
                return None
            if (validated.claims.get("iss"), validated.claims.get("sub")) != (
                self._settings.upstream_issuer,
                self._settings.upstream_subject,
            ):
                return None
            await self._resolve(reference)
            # The SDK revocation handler passes this object back to revoke_token. Keep
            # its credential local: this service does not expose upstream account tokens.
            return validated.model_copy(update={"token": token})
        except GrantRejectedError:
            return None
        except SQLAlchemyError:
            raise _unavailable() from None

    async def authenticate(self, token: str) -> Grant | None:
        if await self.load_access_token(token) is None:
            return None
        try:
            return await self._resolve(self._reference(token, token_use="access"))
        except GrantRejectedError:
            return None
        except SQLAlchemyError:
            raise _unavailable() from None

    def targets_issuer(self, token: str) -> bool:
        """Untrusted routing hint only: never send our failed/refresh bearer to TokenReview.

        This can only refuse a fallback. Authentication and grant provenance always come
        from full signature/audience/token-use validation and canonical grant resolution.
        """
        try:
            return jwt.decode(token, options={"verify_signature": False}).get("iss") == str(self.base_url)
        except (jwt.InvalidTokenError, ValueError):
            return False

    async def revoke_token(self, token: McpAccessToken | RefreshToken) -> None:
        try:
            reference = self._reference(
                token.token, token_use="refresh" if isinstance(token, RefreshToken) else "access"
            )
        except Exception:
            return
        try:
            await self._connections.revoke(reference[0])
        except SQLAlchemyError:
            raise _unavailable() from None
        # Do not forward local reference credentials to the upstream IdP. Canonical
        # revocation gates every access/refresh; encrypted SDK metadata expires by TTL.

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        return [
            Route(
                "/revoke",
                endpoint=cors_middleware(self._revoke_request, ["POST", "OPTIONS"]),
                methods=["POST", "OPTIONS"],
            )
            if route.path == "/revoke"
            else route
            for route in super().get_routes(mcp_path)
        ]

    async def _revoke_request(self, request: Request) -> Response:
        """Pinned SDK shim: optional client_secret must actually be optional for public clients.

        Authentication, parsing/error models, token loading and RFC 7009 responses stay
        equivalent to the SDK handler. Only absent client fields are filled from the
        authenticated client before RevocationRequest parsing, including Basic-auth clients.
        """
        try:
            client = await ClientAuthenticator(self).authenticate_request(request)
        except AuthenticationError:
            return PydanticJSONResponse(
                RevocationErrorResponse(error="unauthorized_client", error_description="Client authentication failed"),
                status_code=401,
            )
        try:
            values = dict(await request.form())
            values.setdefault("client_id", client.client_id or "")
            parsed = RevocationRequest.model_validate({"client_secret": None, **values})
        except ValidationError as error:
            return PydanticJSONResponse(
                RevocationErrorResponse(error="invalid_request", error_description=stringify_pydantic_error(error)),
                status_code=400,
            )
        loaded: McpAccessToken | RefreshToken | None
        if parsed.token_type_hint == "refresh_token":
            loaded = await self.load_refresh_token(client, parsed.token)
            if loaded is None:
                loaded = await self.load_access_token(parsed.token)
        else:
            loaded = await self.load_access_token(parsed.token)
            if loaded is None:
                loaded = await self.load_refresh_token(client, parsed.token)
        if loaded is not None and loaded.client_id == client.client_id:
            await self.revoke_token(loaded)
        return Response(status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _unavailable() -> HTTPException:
    return HTTPException(503, "Connection authorization is temporarily unavailable.", headers={"Retry-After": "60"})


@asynccontextmanager
async def running_oauth(
    settings: OAuthSettings, database_url: str, enrollments: EnrollmentAuthority, connections: ConnectionAuthority
) -> AsyncIterator[ActionsOAuthProxy]:
    """Shared credential-bearing KV is encrypted; lifecycle stays in the Action Service."""
    persistence = PostgresPersistence(
        kind="postgres",
        url=make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False),
        table_name="agentplane_oauth_kv",
    )
    async with build_shared_client_storage(persistence) as storage:
        encrypted = FernetEncryptionWrapper(
            key_value=storage,
            fernet=Fernet(settings.encryption_key_file.read_bytes().strip()),
            raise_on_decryption_error=True,
        )
        yield ActionsOAuthProxy(settings, client_storage=encrypted, enrollments=enrollments, connections=connections)
