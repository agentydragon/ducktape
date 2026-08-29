"""Bearer-facing adapter over the catalog's Kubernetes authorization check."""

from __future__ import annotations

from haku.console.grants.catalog import GrantCatalog
from haku.console.grants.kubernetes.authorization import AuthorizationRequest, KubernetesBearerRejectedError
from haku.console.grants.principal import RequestPrincipal
from haku.console.identity.agent_bearer_authority import AgentBearerAuthority
from haku.grants.authorization import AuthorizationDecision, AuthorizationUnavailableError


class KubernetesAuthorizationService:
    """Authenticate an Agent bearer, then delegate to the unified grant catalog."""

    def __init__(self, *, agent_bearer_authority: AgentBearerAuthority, catalog: GrantCatalog) -> None:
        self._agent_bearer_authority = agent_bearer_authority
        self._catalog = catalog

    async def authorize(self, *, bearer: str, request: AuthorizationRequest) -> AuthorizationDecision:
        token = _bearer_token(bearer)
        if token is None:
            raise KubernetesBearerRejectedError("Bearer authorization is required")
        try:
            actor = await self._agent_bearer_authority.authenticate(token)
        except Exception as error:
            raise AuthorizationUnavailableError("Haku Agent authority is unavailable") from error
        if actor is None:
            raise KubernetesBearerRejectedError("Haku rejected the caller credential")
        return await self.evaluate(request_principal=RequestPrincipal.from_source(actor), request=request)

    async def evaluate(
        self, *, request_principal: RequestPrincipal, request: AuthorizationRequest
    ) -> AuthorizationDecision:
        return await self._catalog.authorize_kubernetes(request_principal=request_principal, request=request)

    async def aclose(self) -> None:
        await self._catalog.aclose()


def _bearer_token(value: str) -> str | None:
    scheme, separator, token = value.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        return None
    return token.strip()
