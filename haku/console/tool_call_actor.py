"""The authentication-context actor — one of five identity roles the tool-call domain keeps apart.

The one boundary these roles inscribe: an actor is a request principal plus the accountability
identities (owning Operator, exact credential binding) that authorization and audit read and must
not confuse with the projected request identity; tool-call principal rows are the durable submitter
provenance revalidated from.

The five roles, at their definitions:

- **Authentication context** — `RuntimeActor` (`OperatorActor | AgentActor`), this module: the actor.
- **Request principal** — `RequestPrincipal` (`mcp/principal.py`): the Agent and access profile
  projected from the authenticated actor, dropping accountability identities.
- **Submitter provenance** — `McpToolCallPrincipal` (`database_schema.py`; wire in
  `haku/shared/haku/console/tool_calls.py`): the durable submitter provenance.
- **Runtime actor / execution** — `McpExecutionCaller` / `McpExecutionContext` (`mcp/execution.py`):
  the trusted in-process execution identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OperatorActor:
    operator_id: UUID


@dataclass(frozen=True, slots=True)
class AgentActor:
    agent_id: UUID
    operator_id: UUID
    binding_id: UUID
    # Persisted config-profile reference. ``None`` is the migration-safe, fail-closed default.
    access_profile_id: str | None = None


type RuntimeActor = OperatorActor | AgentActor
