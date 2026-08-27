"""Console-side authorization for the Haku Kubernetes API proxy.

The proxy authenticates an Agent with a Haku bearer, but the Kubernetes
authorization decision is made by a Kubernetes SubjectAccessReview (SAR).  A
SAR is always issued for the deploy-configured subject below; request callers
cannot supply a Kubernetes username or groups.

The evaluator itself has no ambient request state. The bearer resolver and optional grant service
are supplied by the Console composition root; both the proxy and in-process MCP path feed the same
trusted Agent evaluation method. Kubernetes authorization is disabled when no config is supplied,
and every client/config failure is surfaced as an unavailable authority so callers fail closed.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, AuthorizationV1Api
from kubernetes_asyncio.config.config_exception import ConfigException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from haku.console.agent_bearer_authority import AgentBearerAuthority
from haku.console.config import KubernetesAuthorizationConfig, KubernetesAuthorizationSubject
from haku.console.grant_principal import RequestPrincipal
from haku.console.kubernetes_grant_models import (
    KubernetesAllNamespacesGrantScope,
    KubernetesClusterGrantScope,
    KubernetesGrantScope,
    KubernetesGrantScopeKind,
    KubernetesNamespacesGrantScope,
    KubernetesNonResourceGrantScope,
    KubernetesRule,
    validate_grant_scope_rules,
)
from haku.console.kubernetes_grant_service import KubernetesGrantService

logger = logging.getLogger(__name__)


class KubernetesAuthorizationUnavailableError(RuntimeError):
    """The Console cannot make an authoritative Kubernetes decision."""


class KubernetesBearerRejectedError(RuntimeError):
    """The presented Haku bearer does not resolve to an active Agent."""


class KubernetesAuthorizationSource(StrEnum):
    """Authority that made the effective Kubernetes decision."""

    SAR = "sar"
    GRANT = "grant"


class RequestAttributes(BaseModel):
    """Kubernetes' canonical interpretation of one HTTP request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_request: bool
    verb: str = Field(min_length=1)
    api_group: str = ""
    api_version: str = ""
    namespace: str = ""
    resource: str = ""
    subresource: str = ""
    name: str = ""
    path: str = ""
    field_selector: str = ""
    label_selector: str = ""

    @model_validator(mode="after")
    def validate_path_for_non_resource(self) -> RequestAttributes:
        if not self.resource_request and not self.path:
            raise ValueError("non-resource requests require a non-empty path")
        return self


class AuthorizationRequest(BaseModel):
    """Proxy-to-Console authorization request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attributes: RequestAttributes
    required_scope: KubernetesGrantScope
    required_rules: list[KubernetesRule] = Field(default_factory=list, min_length=1)

    @model_validator(mode="after")
    def validate_canonical_projection(self) -> AuthorizationRequest:
        validate_grant_scope_rules(self.required_scope, self.required_rules)
        if self.required_rules != [required_rule(self.attributes)]:
            raise ValueError("required_rules must be the canonical minimal rule for attributes")
        attributes = self.attributes
        scope = self.required_scope
        if not attributes.resource_request:
            if scope.kind is not KubernetesGrantScopeKind.NON_RESOURCE:
                raise ValueError("a non-resource request requires non_resource scope")
        elif attributes.namespace:
            expected = KubernetesNamespacesGrantScope(namespaces=(attributes.namespace,))
            if scope != expected:
                raise ValueError("a named-namespace request requires its exact namespace scope")
        elif scope.kind not in {KubernetesGrantScopeKind.ALL_NAMESPACES, KubernetesGrantScopeKind.CLUSTER}:
            raise ValueError("an unnamespaced resource request requires all_namespaces or cluster scope")
        return self


class AuthorizationResponse(BaseModel):
    """The small fail-closed response understood by the proxy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason: str | None = None
    source: KubernetesAuthorizationSource
    decision_id: str = Field(min_length=1)
    valid_until: datetime.datetime | None = None


@dataclass(frozen=True, slots=True)
class SubjectAccessReviewResult:
    allowed: bool
    reason: str | None = None


class SubjectAccessReviewClient(Protocol):
    async def review(
        self, *, subject: KubernetesAuthorizationSubject, attributes: RequestAttributes
    ) -> SubjectAccessReviewResult: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class KubernetesClients:
    api: ApiClient
    authorization: AuthorizationV1Api


class KubernetesSubjectAccessReviewClient:
    """Lazily connect to the in-cluster Kubernetes Authorization API."""

    def __init__(self, clients: KubernetesClients | None = None) -> None:
        self._clients = clients
        self._lock = asyncio.Lock()

    async def _connected(self) -> KubernetesClients:
        async with self._lock:
            if self._clients is None:
                configuration = k8s_client.Configuration()
                try:
                    k8s_config.load_incluster_config(client_configuration=configuration)
                except ConfigException as error:
                    raise KubernetesAuthorizationUnavailableError(
                        "Kubernetes in-cluster configuration is unavailable"
                    ) from error
                api = ApiClient(configuration=configuration)
                self._clients = KubernetesClients(api=api, authorization=AuthorizationV1Api(api))
            return self._clients

    async def review(
        self, *, subject: KubernetesAuthorizationSubject, attributes: RequestAttributes
    ) -> SubjectAccessReviewResult:
        resource_attributes = None
        non_resource_attributes = None
        if attributes.resource_request:
            resource_attributes = k8s_client.V1ResourceAttributes(
                group=attributes.api_group,
                version=attributes.api_version,
                namespace=attributes.namespace,
                resource=attributes.resource,
                subresource=attributes.subresource,
                name=attributes.name,
                verb=attributes.verb,
            )
        else:
            non_resource_attributes = k8s_client.V1NonResourceAttributes(path=attributes.path, verb=attributes.verb)
        request = k8s_client.V1SubjectAccessReview(
            api_version="authorization.k8s.io/v1",
            kind="SubjectAccessReview",
            spec=k8s_client.V1SubjectAccessReviewSpec(
                user=subject.username,
                groups=list(subject.groups),
                resource_attributes=resource_attributes,
                non_resource_attributes=non_resource_attributes,
            ),
        )
        try:
            response = await (await self._connected()).authorization.create_subject_access_review(request)
        except KubernetesAuthorizationUnavailableError:
            raise
        except Exception as error:
            raise KubernetesAuthorizationUnavailableError("Kubernetes authorization API is unavailable") from error
        status = response.status
        if status is None or status.allowed is None:
            raise KubernetesAuthorizationUnavailableError("Kubernetes returned an incomplete authorization response")
        if getattr(status, "evaluation_error", None):
            raise KubernetesAuthorizationUnavailableError("Kubernetes authorization evaluation reported an error")
        if status.allowed and getattr(status, "denied", False):
            raise KubernetesAuthorizationUnavailableError("Kubernetes returned a contradictory authorization response")
        return SubjectAccessReviewResult(allowed=bool(status.allowed), reason=status.reason)

    async def aclose(self) -> None:
        if self._clients is not None:
            await self._clients.api.close()
            self._clients = None


class KubernetesAuthorizationService:
    """Evaluate standing Kubernetes policy, then an Agent-owned temporary grant.

    The bearer-facing proxy route is only an adapter: it resolves the bearer through the canonical
    Agent authority and then calls :meth:`evaluate`. In-process tools carry the same Agent id and
    access profile in trusted execution metadata. SAR is always the first authority. A grant is
    consulted only after a clean SAR denial; SAR failures remain unavailable/fail-closed even when
    a matching grant exists.
    """

    def __init__(
        self,
        *,
        config: KubernetesAuthorizationConfig,
        agent_bearer_authority: AgentBearerAuthority,
        grants: KubernetesGrantService,
        sar_client: SubjectAccessReviewClient,
    ) -> None:
        self._config = config
        self._agent_bearer_authority = agent_bearer_authority
        self._grants = grants
        self._sar_client = sar_client

    async def authorize(self, *, bearer: str, request: AuthorizationRequest) -> AuthorizationResponse:
        token = _bearer_token(bearer)
        if token is None:
            raise KubernetesBearerRejectedError("Bearer authorization is required")
        try:
            actor = await self._agent_bearer_authority.authenticate(token)
        except KubernetesAuthorizationUnavailableError:
            raise
        except Exception as error:
            raise KubernetesAuthorizationUnavailableError("Haku Agent authority is unavailable") from error
        if actor is None:
            raise KubernetesBearerRejectedError("Haku rejected the caller credential")
        return await self.evaluate(request_principal=RequestPrincipal.from_source(actor), request=request)

    async def authorize_agent(
        self, *, request_principal: RequestPrincipal, request: AuthorizationRequest
    ) -> AuthorizationResponse:
        """Evaluate identity already revalidated into trusted in-process execution metadata."""

        return await self.evaluate(request_principal=request_principal, request=request)

    async def evaluate(
        self, *, request_principal: RequestPrincipal, request: AuthorizationRequest
    ) -> AuthorizationResponse:
        """Evaluate one trusted Agent request without mutating grant state."""

        subject = (
            self._config.subjects_by_access_profile.get(request_principal.access_profile_id)
            if request_principal.access_profile_id is not None
            else None
        )
        if subject is None:
            raise KubernetesAuthorizationUnavailableError(
                "Kubernetes authorization is not configured for the Agent access profile"
            )
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                result = await self._sar_client.review(subject=subject, attributes=request.attributes)
        except TimeoutError as error:
            raise KubernetesAuthorizationUnavailableError("Kubernetes authorization timed out") from error
        except KubernetesAuthorizationUnavailableError:
            raise
        except Exception as error:
            raise KubernetesAuthorizationUnavailableError("Kubernetes authorization evaluation failed") from error
        decision_id = f"sar:{uuid4()}"
        if request.attributes.resource_request:
            logger.info(
                "Kubernetes standing-policy decision request_principal=%s subject=%s "
                "decision_id=%s allowed=%s verb=%s namespace=%s resource=%s subresource=%s name=%s",
                request_principal,
                subject.username,
                decision_id,
                result.allowed,
                request.attributes.verb,
                request.attributes.namespace,
                request.attributes.resource,
                request.attributes.subresource,
                request.attributes.name,
            )
        else:
            logger.info(
                "Kubernetes standing-policy decision request_principal=%s subject=%s "
                "decision_id=%s allowed=%s verb=%s path=%s",
                request_principal,
                subject.username,
                decision_id,
                result.allowed,
                request.attributes.verb,
                request.attributes.path,
            )
        if result.allowed:
            return AuthorizationResponse(
                allowed=True, reason=result.reason, source=KubernetesAuthorizationSource.SAR, decision_id=decision_id
            )

        # A normal SAR denial is the only point at which temporary authority may add access.
        # Matching is read-only: the repository query excludes expired rows.
        try:
            grant = await self._grants.match_request(
                request_principal=request_principal,
                required_scope=request.required_scope,
                required_rules=request.required_rules,
            )
        except Exception as error:
            raise KubernetesAuthorizationUnavailableError("Kubernetes grant authority is unavailable") from error
        if grant.allowed and grant.grant_id is not None:
            grant_decision_id = f"grant:{grant.grant_id}"
            logger.info(
                "Kubernetes temporary-grant decision request_principal=%s decision_id=%s allowed=true valid_until=%s",
                request_principal,
                grant_decision_id,
                grant.expires_at,
            )
            return AuthorizationResponse(
                allowed=True,
                reason=grant.reason,
                source=KubernetesAuthorizationSource.GRANT,
                decision_id=grant_decision_id,
                valid_until=grant.expires_at,
            )
        return AuthorizationResponse(
            allowed=False, reason=result.reason, source=KubernetesAuthorizationSource.SAR, decision_id=decision_id
        )

    async def aclose(self) -> None:
        await self._sar_client.aclose()


def _bearer_token(value: str) -> str | None:
    scheme, separator, token = value.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        return None
    return token.strip()


def required_rule(attributes: RequestAttributes) -> KubernetesRule:
    """Build the minimal RBAC rule; Kubernetes spells subresources as ``resource/subresource``."""

    if not attributes.resource_request:
        return KubernetesRule(verbs=(attributes.verb,), non_resource_urls=(attributes.path,))
    resource = attributes.resource
    if attributes.subresource:
        resource = f"{resource}/{attributes.subresource}"
    return KubernetesRule(
        api_groups=(attributes.api_group,),
        resources=(resource,),
        verbs=(attributes.verb,),
        resource_names=(attributes.name,) if attributes.name else (),
    )


def required_scope(
    attributes: RequestAttributes, *, unnamespaced_resource_kind: KubernetesGrantScopeKind | None = None
) -> KubernetesGrantScope:
    """Derive scope when a request either names its namespace or states its unnamespaced kind."""

    if not attributes.resource_request:
        if unnamespaced_resource_kind is not None:
            raise ValueError("non-resource requests cannot declare a resource scope kind")
        return KubernetesNonResourceGrantScope()
    if attributes.namespace:
        if unnamespaced_resource_kind is not None:
            raise ValueError("a named-namespace request cannot declare an unnamespaced resource scope kind")
        return KubernetesNamespacesGrantScope(namespaces=(attributes.namespace,))
    if unnamespaced_resource_kind is KubernetesGrantScopeKind.ALL_NAMESPACES:
        return KubernetesAllNamespacesGrantScope()
    if unnamespaced_resource_kind is KubernetesGrantScopeKind.CLUSTER:
        return KubernetesClusterGrantScope()
    raise ValueError("an unnamespaced resource request must declare all_namespaces or cluster scope")
