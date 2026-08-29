"""Credential-free in-process MCP tools for owned, principal-scoped temporary grants.

One `grants` server fronts every grant domain (#4918): the shared verb set
(`create_grant`/`list_grants`/`get_grant`/`revoke_grants`) over the #4889
grant envelope, discriminated by a ``domain`` tag (`kubernetes` | `http`) on each per-domain
capability payload. The domains keep their own services, tables, and typed coverage
(`grants.kubernetes` scope/rules; `grants.http` exact origins); this module only routes a
discriminated request to the right one and tags the returned envelope so the Agent can tell
them apart. Kubernetes SAR inspection (`can_i`) is not a grant verb and lives on its own
`kubernetes` server (`haku.console.tools.kubernetes`).
"""

from __future__ import annotations

import datetime
from typing import Annotated, Literal, assert_never
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

import haku.console.grants.http.models as http_models
import haku.console.grants.http.service as http_service
import haku.console.grants.kubernetes.models as kubernetes_models
import haku.console.grants.kubernetes.service as kubernetes_service
import haku.console.tools.kubernetes as kubernetes_tools
from haku.console.grants.envelope import GRANT_SET_LIMIT, GrantNotFoundError
from haku.console.grants.principal import GrantPrincipalKind, grant_principal_for
from haku.console.identity.enrollment import AgentEnrollmentService
from haku.console.mcp.execution import (
    EXECUTION_CONTEXT_DEPENDENCY,
    AgentMcpExecutionCaller,
    McpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
)

GRANTS_SERVER_ID = "grants"

GrantDomain = Literal["kubernetes", "http"]

# The declared read scope for `list_grants`. Only `self` (the caller's own grants) is served today,
# and it is the value the argument-conditional auto-approval policy keys on; a broader nameable
# principal is an Operator-reviewed follow-up. Absent means the (reserved) broader read, which stays
# manual.
GrantReadScope = Literal["self"]


class KubernetesGrantRequest(BaseModel):
    """A Kubernetes grant to create: the ``kubernetes`` domain's scope/rule coverage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: Literal["kubernetes"]
    spec: kubernetes_models.GrantSpec


class HttpGrantRequest(BaseModel):
    """An HTTP egress grant to create: the ``http`` domain's exact-origin coverage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: Literal["http"]
    spec: http_models.GrantSpec


GrantRequest = Annotated[KubernetesGrantRequest | HttpGrantRequest, Field(discriminator="domain")]


class KubernetesGrantView(BaseModel):
    """A durable Kubernetes grant tagged with its domain for the unified server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: Literal["kubernetes"] = "kubernetes"
    grant: kubernetes_models.Grant


class HttpGrantView(BaseModel):
    """A durable HTTP egress grant tagged with its domain for the unified server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: Literal["http"] = "http"
    grant: http_models.Grant


GrantView = Annotated[KubernetesGrantView | HttpGrantView, Field(discriminator="domain")]


class GrantsToolsService:
    """Route the shared grant verbs to the per-domain grant services behind one server."""

    def __init__(
        self,
        *,
        kubernetes: kubernetes_service.GrantService,
        http: http_service.GrantService,
        agents: AgentEnrollmentService,
        can_i: kubernetes_tools.KubernetesToolsService,
    ) -> None:
        self._kubernetes = kubernetes
        self._http = http
        self._agents = agents
        self._can_i = can_i

    async def kubernetes_can_i(
        self, *, context: McpExecutionContext, requests: list[kubernetes_tools.KubernetesAccessCheck]
    ) -> list[kubernetes_tools.CanIResult]:
        return await self._can_i.can_i(context=context, requests=requests)

    async def create_grants(
        self,
        *,
        context: McpExecutionContext,
        requests: list[GrantRequest],
        duration_seconds: int,
        applies_to: GrantPrincipalKind,
    ) -> list[GrantView]:
        if context.tool_call_id is None:
            raise PermissionError("grant creation requires durable tool-call provenance")
        # One source ToolCall creates exactly one immutable grant set in exactly one domain's table,
        # so a call may not straddle domains.
        kubernetes_specs = [request.spec for request in requests if isinstance(request, KubernetesGrantRequest)]
        http_specs = [request.spec for request in requests if isinstance(request, HttpGrantRequest)]
        if kubernetes_specs and http_specs:
            raise ToolError("one create_grant call must create grants in a single domain")

        principal = context.request_principal
        grant_principal = grant_principal_for(principal, applies_to)
        expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=duration_seconds)
        if kubernetes_specs:
            kubernetes_grants = await self._kubernetes.create_grants(
                owner_agent_id=principal.agent_id,
                grant_principal=grant_principal,
                source_tool_call_id=context.tool_call_id,
                grants=kubernetes_specs,
                expires_at=expires_at,
            )
            return [KubernetesGrantView(grant=grant) for grant in kubernetes_grants]
        http_grants = await self._http.create_grants(
            owner_agent_id=principal.agent_id,
            grant_principal=grant_principal,
            source_tool_call_id=context.tool_call_id,
            grants=http_specs,
            expires_at=expires_at,
        )
        return [HttpGrantView(grant=grant) for grant in http_grants]

    async def list_grants(
        self, *, context: McpExecutionContext, principal: GrantReadScope | None = None
    ) -> list[GrantView]:
        # The read is actor-scoped regardless: `list_applicable_grants` filters to the caller's own
        # grants via the trusted request principal. `principal` is the caller's declared scope,
        # carried for argument-conditional auto-approval; only `self` is served today and it equals
        # that own-scoped read.
        del principal
        kubernetes_grants = await self._kubernetes.list_applicable_grants(request_principal=context.request_principal)
        http_grants = await self._http.list_applicable_grants(request_principal=context.request_principal)
        return [
            *(KubernetesGrantView(grant=grant) for grant in kubernetes_grants),
            *(HttpGrantView(grant=grant) for grant in http_grants),
        ]

    async def get_grant(self, *, context: McpExecutionContext, domain: GrantDomain, grant_id: UUID) -> GrantView:
        if domain == "kubernetes":
            return KubernetesGrantView(
                grant=await self._kubernetes.get_applicable_grant(
                    request_principal=context.request_principal, grant_id=grant_id
                )
            )
        return HttpGrantView(
            grant=await self._http.get_applicable_grant(request_principal=context.request_principal, grant_id=grant_id)
        )

    async def revoke_grants(
        self,
        *,
        context: McpExecutionContext,
        domain: GrantDomain,
        grant_ids: list[UUID],
        reason: str,
        owner_agent_id: UUID | None = None,
    ) -> list[GrantView]:
        """End owned grants, dispatching on the trusted caller to the matching end fact.

        An Agent relinquishes only its own grants — ``released_at`` → RELEASED — and may not name
        ``owner_agent_id``. An Operator ends an owned Agent's grants — ``revoked_at`` → REVOKED —
        and must name which owned Agent; a foreign Agent is not found.
        """

        match context.caller:
            case OperatorMcpExecutionCaller(operator_id=operator_id):
                if owner_agent_id is None:
                    raise ToolError("Operator revocation must name the owned Agent (owner_agent_id)")
                owned = await self._agents.list_agents(operator_id=operator_id)
                if owner_agent_id not in {agent.agent_id for agent in owned}:
                    raise GrantNotFoundError(str(owner_agent_id))
                if domain == "kubernetes":
                    revoked = await self._kubernetes.revoke_grants(
                        owner_agent_id=owner_agent_id, grant_ids=grant_ids, reason=reason
                    )
                    return [KubernetesGrantView(grant=grant) for grant in revoked]
                http_revoked = await self._http.revoke_grants(
                    owner_agent_id=owner_agent_id, grant_ids=grant_ids, reason=reason
                )
                return [HttpGrantView(grant=grant) for grant in http_revoked]
            case AgentMcpExecutionCaller(principal=principal):
                if owner_agent_id is not None:
                    raise PermissionError("an Agent relinquishes only its own grants and may not name owner_agent_id")
                if domain == "kubernetes":
                    released = await self._kubernetes.release_applicable_grants(
                        request_principal=principal, grant_ids=grant_ids, reason=reason
                    )
                    return [KubernetesGrantView(grant=grant) for grant in released]
                http_released = await self._http.release_applicable_grants(
                    request_principal=principal, grant_ids=grant_ids, reason=reason
                )
                return [HttpGrantView(grant=grant) for grant in http_released]
        assert_never(context.caller)


def build_mcp(service: GrantsToolsService) -> FastMCP:
    """Build one stable server instance; request identity enters only via hidden dependencies."""

    mcp = FastMCP(
        name=GRANTS_SERVER_ID,
        instructions=(
            "Create/list/get/end explicit Agent- or session-scoped temporary grants across grant "
            "domains. Each create item carries a 'domain' tag: 'kubernetes' for RBAC-like scope/rule coverage, "
            "'http' for one exact canonical public origin narrowed by method set and optional path regex. One "
            "create_grant call creates grants in a single domain, atomically, with one shared expiry. get_grant "
            "and revoke_grants take the 'domain' of the grant IDs (as returned by create/list). One revoke_grants "
            "call ends up to 32 durable grant IDs sequentially, dispatching on the caller: an Agent relinquishes "
            "its own grants (released), an Operator ends an owned Agent's grants (revoked) by naming "
            "owner_agent_id. Agent identity and tool-call provenance are trusted request metadata, never tool "
            "arguments; grant creation is checked before any temporary authority is issued. whoami takes no "
            "arguments and returns the trusted console/MCP identity Console resolved for the caller (durable "
            "Agent id + live session id + access profile, or Operator id) — distinct from the egress-fence "
            "attribution. " + kubernetes_tools.CAN_I_INSTRUCTIONS
        ),
    )

    @mcp.tool
    async def whoami(context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY) -> McpExecutionCaller:
        """Return the trusted identity Console resolved for this caller — its console/MCP principal.

        Takes no arguments and has no side effects. For an Agent it is the durable ``agent_id`` plus
        the live ``session_id`` (present only under a session bearer) and the ``access_profile_id``;
        for a direct Operator it is the ``operator_id``. This is authority Console authenticated the
        caller as; HTTP egress has its own shared-fence ``Authorization`` credential and derives
        Agent/session identity from the live bridge bearer. This tool carries no approval provenance.
        """

        return context.caller

    @mcp.tool
    async def kubernetes_can_i(
        requests: Annotated[
            list[kubernetes_tools.KubernetesAccessCheck],
            Field(
                min_length=1,
                max_length=kubernetes_tools.CAN_I_BATCH_LIMIT,
                description="Kubernetes requests to authorize in one batch.",
            ),
        ],
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[kubernetes_tools.CanIResult]:
        return await service.kubernetes_can_i(context=context, requests=requests)

    @mcp.tool
    async def create_grant(
        grants: Annotated[
            list[GrantRequest],
            Field(
                min_length=1,
                max_length=GRANT_SET_LIMIT,
                description="Exact grants to create atomically with one shared start and expiry, all in one domain.",
            ),
        ],
        duration_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=86_400,
                description="Requested duration in seconds; the deployment may enforce a lower maximum.",
            ),
        ],
        applies_to: Annotated[
            GrantPrincipalKind,
            Field(
                description=(
                    "Principal applicability resolved from trusted source identity. "
                    "'agent' covers every authenticated execution of this Agent; 'session' "
                    "covers only the exact live session that submitted this ToolCall."
                )
            ),
        ] = GrantPrincipalKind.AGENT,
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[GrantView]:
        return await service.create_grants(
            context=context, requests=grants, duration_seconds=duration_seconds, applies_to=applies_to
        )

    @mcp.tool
    async def list_grants(
        principal: Annotated[
            GrantReadScope | None,
            Field(
                description=(
                    "Read scope. 'self' returns only this Agent's own grants (the only scope served "
                    "today) and is the click-free path; omit it for the Operator-reviewed broader read."
                )
            ),
        ] = None,
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[GrantView]:
        return await service.list_grants(context=context, principal=principal)

    @mcp.tool
    async def get_grant(
        domain: Annotated[GrantDomain, Field(description="Domain of the grant ID, as returned by create/list.")],
        grant_id: Annotated[UUID, Field(description="Grant UUID returned by create_grant.")],
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> GrantView:
        return await service.get_grant(context=context, domain=domain, grant_id=grant_id)

    @mcp.tool
    async def revoke_grants(
        domain: Annotated[GrantDomain, Field(description="Domain of the grant IDs, as returned by create/list.")],
        grant_ids: Annotated[
            list[UUID],
            Field(
                min_length=1,
                max_length=GRANT_SET_LIMIT,
                description="Grant UUIDs returned by create_grant; ended sequentially in the supplied order.",
            ),
        ],
        reason: Annotated[str, Field(min_length=1, max_length=500)] = "released",
        owner_agent_id: Annotated[
            UUID | None,
            Field(
                description=(
                    "Operator-only: the acting Operator's owned Agent whose grants to revoke. Omit as an Agent "
                    "caller — you relinquish only your own grants."
                )
            ),
        ] = None,
        context: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> list[GrantView]:
        """End owned grants: an Agent relinquishes its own (released); an Operator revokes an owned Agent's (revoked)."""
        return await service.revoke_grants(
            context=context, domain=domain, grant_ids=grant_ids, reason=reason, owner_agent_id=owner_agent_id
        )

    return mcp
