"""One authority-query surface over configuration and database grants.

Callers ask this catalog what a principal may do; they never select the backing
store.  The mutable Postgres grant services remain private implementation details
of the database source.  Kubernetes configuration deliberately remains a
SubjectAccessReview rather than a synthetic RBAC grant.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Sequence
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from haku.console.config import KubernetesAuthorizationConfig, KubernetesAuthorizationSubject
from haku.console.grants.envelope import GrantNotFoundError, GrantStatus, validated_end_batch
from haku.console.grants.http.decide_config import EgressStandingPolicyEntry
from haku.console.grants.http.models import (
    Grant as HttpGrant,
    HttpMethod,
    HttpOrigin,
    HttpRequestAllowed,
    HttpRequestCoverage,
    HttpRequestDenied,
)
from haku.console.grants.http.service import GrantService as HttpGrantService
from haku.console.grants.kubernetes.authorization import (
    AuthorizationRequest,
    KubernetesAuthorizationUnavailableError,
    SubjectAccessReviewClient,
)
from haku.console.grants.kubernetes.models import Grant as KubernetesGrant, GrantScope, Rule
from haku.console.grants.kubernetes.service import GrantService as KubernetesGrantService
from haku.console.grants.principal import (
    AccessProfileGrantPrincipal,
    AgentGrantPrincipal,
    GrantPrincipal,
    RequestPrincipal,
    SessionGrantPrincipal,
)
from haku.grants.authorization import AuthorizationAllowed, AuthorizationDecision, AuthorizationDenied, GrantSourceKind


class DatabaseGrantSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[GrantSourceKind.DATABASE] = GrantSourceKind.DATABASE
    id: UUID
    tool_call_id: str
    created_at: datetime.datetime


class ConfigFileGrantSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[GrantSourceKind.CONFIG_FILE] = GrantSourceKind.CONFIG_FILE
    entry_id: str


type GrantSource = Annotated[DatabaseGrantSource | ConfigFileGrantSource, Field(discriminator="kind")]


class KubernetesRulesCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["kubernetes_rules"] = "kubernetes_rules"
    scope: GrantScope
    rules: tuple[Rule, ...]


class KubernetesSarCoverage(BaseModel):
    """A configured SAR identity, not a claim to enumerate its Kubernetes RBAC."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["kubernetes_sar"] = "kubernetes_sar"
    subject: KubernetesAuthorizationSubject


class HttpCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["http"] = "http"
    origins: frozenset[HttpOrigin]
    coverage: HttpRequestCoverage
    credential_handles: frozenset[str]
    allow_prohibited_address: bool


type GrantCoverage = Annotated[
    KubernetesRulesCoverage | KubernetesSarCoverage | HttpCoverage, Field(discriminator="kind")
]


class GrantValidity(BaseModel):
    """Lifecycle facts for a grant; an absent deadline means it does not expire."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ends_at: datetime.datetime | None
    status: GrantStatus
    ended_at: datetime.datetime | None = None
    end_reason: str | None = None

    @model_validator(mode="after")
    def _terminal_facts_match_status(self) -> GrantValidity:
        terminal = self.status is GrantStatus.ENDED
        if terminal != (self.ended_at is not None):
            raise ValueError("an ended grant carries exactly one end timestamp")
        if self.ended_at is None and self.end_reason is not None:
            raise ValueError("a grant end reason requires an end timestamp")
        if self.ends_at is None and self.status is not GrantStatus.ACTIVE:
            raise ValueError("a grant without a deadline is active")
        return self


class Grant(BaseModel):
    """One effective authority, irrespective of its declaration location or validity period."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: GrantSource
    subject: GrantPrincipal
    coverage: GrantCoverage
    validity: GrantValidity


class HttpAccessAllowed(AuthorizationAllowed):
    """An allowed HTTP decision plus the handles eligible for substitution."""

    credential_handles: frozenset[str]


type HttpAccessDecision = HttpAccessAllowed | AuthorizationDenied


class GrantCatalog:
    """Compose configuration and database authority for every read/check path."""

    def __init__(
        self,
        *,
        kubernetes_grants: KubernetesGrantService,
        http_grants: HttpGrantService,
        kubernetes_config: KubernetesAuthorizationConfig | None = None,
        sar_client: SubjectAccessReviewClient | None = None,
        http_config_policies: tuple[EgressStandingPolicyEntry, ...] = (),
    ) -> None:
        if (kubernetes_config is None) != (sar_client is None):
            raise ValueError("Kubernetes authorization config and SAR client must be configured together")
        self._kubernetes_grants = kubernetes_grants
        self._http_grants = http_grants
        self._kubernetes_config = kubernetes_config
        self._sar_client = sar_client
        self._http_config_policies = http_config_policies

    async def list_applicable(
        self, *, request_principal: RequestPrincipal, include_inactive: bool = False
    ) -> tuple[Grant, ...]:
        """List current authority the authenticated principal may exercise, optionally including database history."""

        kubernetes, http = await asyncio.gather(
            self._kubernetes_grants.list_applicable_grants(
                request_principal=request_principal, include_inactive=include_inactive
            ),
            self._http_grants.list_applicable_grants(
                request_principal=request_principal, include_inactive=include_inactive
            ),
        )
        entries: list[Grant] = [
            *(self._database_kubernetes_grant(grant) for grant in kubernetes),
            *(self._database_http_grant(grant) for grant in http),
        ]
        entries.extend(self._config_grants(request_principal=request_principal))
        return tuple(entries)

    async def list(
        self, *, principal: GrantPrincipal | None = None, include_inactive: bool = False
    ) -> tuple[Grant, ...]:
        """List declared authority, optionally for one subject and with database history."""

        kubernetes, http = await asyncio.gather(
            self._kubernetes_grants.list(principal=principal, include_inactive=include_inactive),
            self._http_grants.list(principal=principal, include_inactive=include_inactive),
        )
        return (
            *(
                self._all_config_grants()
                if principal is None
                else self._config_grants_for_principal(principal=principal)
            ),
            *(self._database_kubernetes_grant(grant) for grant in kubernetes),
            *(self._database_http_grant(grant) for grant in http),
        )

    async def get_kubernetes_grant(self, *, request_principal: RequestPrincipal, grant_id: UUID) -> Grant:
        """Read one applicable database Kubernetes grant through the unified surface."""

        return self._database_kubernetes_grant(
            await self._kubernetes_grants.get_applicable_grant(request_principal=request_principal, grant_id=grant_id)
        )

    async def get_http_grant(self, *, request_principal: RequestPrincipal, grant_id: UUID) -> Grant:
        """Read one applicable database HTTP grant through the unified surface."""

        return self._database_http_grant(
            await self._http_grants.get_applicable_grant(request_principal=request_principal, grant_id=grant_id)
        )

    async def end_database_grant(
        self, *, owner_agent_ids: frozenset[UUID], grant_id: UUID, reason: str | None
    ) -> Grant:
        """End one database grant without exposing which domain stores it."""

        try:
            return self._database_kubernetes_grant(
                await self._kubernetes_grants.end_grant(
                    owner_agent_ids=owner_agent_ids, grant_id=grant_id, reason=reason
                )
            )
        except GrantNotFoundError:
            return self._database_http_grant(
                await self._http_grants.end_grant(owner_agent_ids=owner_agent_ids, grant_id=grant_id, reason=reason)
            )

    async def end_database_grants(
        self, *, owner_agent_ids: frozenset[UUID], grant_ids: tuple[UUID, ...], reason: str | None
    ) -> tuple[Grant, ...]:
        """End database grants by durable ID without exposing their storage domains."""

        grant_ids, reason = validated_end_batch(grant_ids, reason)
        return tuple(
            [
                await self.end_database_grant(owner_agent_ids=owner_agent_ids, grant_id=grant_id, reason=reason)
                for grant_id in grant_ids
            ]
        )

    def _config_grants(self, *, request_principal: RequestPrincipal) -> tuple[Grant, ...]:
        return (
            *self._config_kubernetes_grants(request_principal=request_principal),
            *self._config_http_grants(request_principal=request_principal),
        )

    def _all_config_grants(self) -> tuple[Grant, ...]:
        kubernetes = (
            tuple(
                grant
                for access_profile_id in self._kubernetes_config.subjects_by_access_profile
                for grant in self._config_kubernetes_grants_for_access_profile(access_profile_id=access_profile_id)
            )
            if self._kubernetes_config is not None
            else ()
        )
        return (
            *kubernetes,
            *(
                self._config_http_grant(policy=policy, agent_id=agent_id)
                for policy in self._http_config_policies
                for agent_id in policy.agent_ids
            ),
        )

    def _config_grants_for_principal(self, *, principal: GrantPrincipal) -> tuple[Grant, ...]:
        match principal:
            case AgentGrantPrincipal(agent_id=agent_id):
                return self._config_http_grants_for_agent(agent_id=agent_id)
            case AccessProfileGrantPrincipal(access_profile_id=access_profile_id):
                return self._config_kubernetes_grants_for_access_profile(access_profile_id=access_profile_id)
            case SessionGrantPrincipal():
                return ()

    def _config_kubernetes_grants(self, *, request_principal: RequestPrincipal) -> tuple[Grant, ...]:
        if request_principal.access_profile_id is not None:
            return self._config_kubernetes_grants_for_access_profile(
                access_profile_id=request_principal.access_profile_id
            )
        return ()

    def _config_kubernetes_grants_for_access_profile(self, *, access_profile_id: str) -> tuple[Grant, ...]:
        if self._kubernetes_config is not None and (
            subject := self._kubernetes_config.subjects_by_access_profile.get(access_profile_id)
        ):
            return (
                Grant(
                    subject=AccessProfileGrantPrincipal(access_profile_id=access_profile_id),
                    coverage=KubernetesSarCoverage(subject=subject),
                    source=ConfigFileGrantSource(entry_id=f"kubernetes-profile:{access_profile_id}"),
                    validity=GrantValidity(ends_at=None, status=GrantStatus.ACTIVE),
                ),
            )
        return ()

    def _config_http_grants(self, *, request_principal: RequestPrincipal) -> tuple[Grant, ...]:
        return self._config_http_grants_for_agent(agent_id=request_principal.agent_id)

    def _config_http_grants_for_agent(self, *, agent_id: UUID) -> tuple[Grant, ...]:
        return tuple(
            self._config_http_grant(policy=policy, agent_id=agent_id)
            for policy in self._http_config_policies
            if agent_id in policy.agent_ids
        )

    @staticmethod
    def _config_http_grant(*, policy: EgressStandingPolicyEntry, agent_id: UUID) -> Grant:
        return Grant(
            subject=AgentGrantPrincipal(agent_id=agent_id),
            coverage=HttpCoverage(
                origins=policy.origins,
                coverage=policy.coverage,
                credential_handles=frozenset({policy.credential_handle}) if policy.credential_handle else frozenset(),
                allow_prohibited_address=policy.allow_prohibited_address,
            ),
            source=ConfigFileGrantSource(entry_id=policy.id),
            validity=GrantValidity(ends_at=None, status=GrantStatus.ACTIVE),
        )

    @staticmethod
    def _database_kubernetes_grant(grant: KubernetesGrant) -> Grant:
        return Grant(
            subject=grant.principal,
            coverage=KubernetesRulesCoverage(scope=grant.scope, rules=grant.rules),
            source=DatabaseGrantSource(
                id=grant.grant_id, tool_call_id=grant.source_tool_call_id, created_at=grant.created_at
            ),
            validity=GrantValidity(
                ends_at=grant.expires_at, status=grant.status, ended_at=grant.ended_at, end_reason=grant.end_reason
            ),
        )

    @staticmethod
    def _database_http_grant(grant: HttpGrant) -> Grant:
        return Grant(
            subject=grant.principal,
            coverage=HttpCoverage(
                origins=frozenset({grant.spec.origin}),
                coverage=grant.spec.coverage,
                credential_handles=(
                    frozenset({grant.spec.credential_handle}) if grant.spec.credential_handle else frozenset()
                ),
                allow_prohibited_address=grant.spec.allow_prohibited_address,
            ),
            source=DatabaseGrantSource(
                id=grant.grant_id, tool_call_id=grant.source_tool_call_id, created_at=grant.created_at
            ),
            validity=GrantValidity(
                ends_at=grant.expires_at, status=grant.status, ended_at=grant.ended_at, end_reason=grant.end_reason
            ),
        )

    async def authorize_kubernetes(
        self, *, request_principal: RequestPrincipal, request: AuthorizationRequest
    ) -> AuthorizationDecision:
        """Run the configured SAR, then consult database grants on a clean denial only."""

        if self._kubernetes_config is None or self._sar_client is None:
            raise KubernetesAuthorizationUnavailableError("Kubernetes authorization is not configured")
        subject = (
            self._kubernetes_config.subjects_by_access_profile.get(request_principal.access_profile_id)
            if request_principal.access_profile_id is not None
            else None
        )
        if subject is None:
            raise KubernetesAuthorizationUnavailableError(
                "Kubernetes authorization is not configured for the Agent access profile"
            )
        try:
            async with asyncio.timeout(self._kubernetes_config.timeout_seconds):
                configuration_decision = await self._sar_client.review(subject=subject, attributes=request.attributes)
        except TimeoutError as error:
            raise KubernetesAuthorizationUnavailableError("Kubernetes authorization timed out") from error
        except KubernetesAuthorizationUnavailableError:
            raise
        except Exception as error:
            raise KubernetesAuthorizationUnavailableError("Kubernetes authorization evaluation failed") from error
        decision_id = f"config_file:kubernetes-profile:{request_principal.access_profile_id}"
        if configuration_decision.allowed:
            return AuthorizationAllowed(
                reason=configuration_decision.reason, source=GrantSourceKind.CONFIG_FILE, decision_id=decision_id
            )
        try:
            grant = await self._kubernetes_grants.match_request(
                request_principal=request_principal,
                required_scope=request.required_scope,
                required_rules=request.required_rules,
            )
        except Exception as error:
            raise KubernetesAuthorizationUnavailableError("Kubernetes grant authority is unavailable") from error
        if grant.allowed and grant.grant_id is not None:
            return AuthorizationAllowed(
                reason=grant.reason,
                source=GrantSourceKind.DATABASE,
                decision_id=f"database:{grant.grant_id}",
                valid_until=grant.expires_at,
            )
        return AuthorizationDenied(reason=configuration_decision.reason or "Kubernetes denied the request")

    async def match_http_request(
        self,
        *,
        request_principal: RequestPrincipal,
        method: HttpMethod,
        origin: HttpOrigin,
        path: str,
        require_prohibited_address_allowance: bool,
    ) -> HttpAccessDecision:
        matching = self._matching_http_config_policies(
            request_principal=request_principal,
            origin=origin,
            method=method,
            path=path,
            require_prohibited_address_allowance=require_prohibited_address_allowance,
        )
        if matching:
            return HttpAccessAllowed(
                source=GrantSourceKind.CONFIG_FILE,
                decision_id=f"config_file:{matching[0].id}",
                credential_handles=frozenset(
                    policy.credential_handle for policy in matching if policy.credential_handle
                ),
            )
        grant = await self._http_grants.match_request(
            request_principal=request_principal,
            method=method,
            origin=origin,
            path=path,
            require_prohibited_address_allowance=require_prohibited_address_allowance,
        )
        return self._http_decision(grant)

    async def match_http_tunnel(
        self, *, request_principal: RequestPrincipal, origin: HttpOrigin, require_prohibited_address_allowance: bool
    ) -> HttpAccessDecision:
        matching = self._matching_http_config_policies(
            request_principal=request_principal,
            origin=origin,
            method=None,
            path=None,
            require_prohibited_address_allowance=require_prohibited_address_allowance,
        )
        if matching:
            return HttpAccessAllowed(
                source=GrantSourceKind.CONFIG_FILE,
                decision_id=f"config_file:{matching[0].id}",
                credential_handles=frozenset(
                    policy.credential_handle for policy in matching if policy.credential_handle
                ),
            )
        return self._http_decision(
            await self._http_grants.match_tunnel(
                request_principal=request_principal,
                origin=origin,
                require_prohibited_address_allowance=require_prohibited_address_allowance,
            )
        )

    def _matching_http_config_policies(
        self,
        *,
        request_principal: RequestPrincipal,
        origin: HttpOrigin,
        method: HttpMethod | None,
        path: str | None,
        require_prohibited_address_allowance: bool,
    ) -> Sequence[EgressStandingPolicyEntry]:
        return [
            policy
            for policy in self._http_config_policies
            if request_principal.agent_id in policy.agent_ids
            and origin in policy.origins
            and (method is None or policy.coverage.covers(method=method, path=path or ""))
            and (not require_prohibited_address_allowance or policy.allow_prohibited_address)
        ]

    @staticmethod
    def _http_decision(decision: HttpRequestAllowed | HttpRequestDenied) -> HttpAccessDecision:
        if isinstance(decision, HttpRequestAllowed):
            return HttpAccessAllowed(
                source=GrantSourceKind.DATABASE,
                decision_id=f"database:{decision.grant_id}",
                valid_until=decision.expires_at,
                credential_handles=decision.credential_handles,
            )
        return AuthorizationDenied(reason=decision.reason)

    async def aclose(self) -> None:
        if self._sar_client is not None:
            await self._sar_client.aclose()
