"""The authentication-context actor — one of five identity roles the tool-call domain keeps apart.

The one boundary these roles inscribe: an actor is a request principal plus the accountability
identities (owning Operator, exact credential binding) that authorization and audit read and
applicability must not; grant principals are stored selectors those request principals are tested
against; tool-call principal rows are the durable submitter provenance both are revalidated from.

The five roles, at their definitions:

- **Authentication context** — `RuntimeActor` (`OperatorActor | AgentActor`), this module: the actor.
- **Request principal** — `RequestPrincipal` (`grant_principal.py`): the `agent_id`/`session_id`
  atom the actor projects to, dropping the accountability identities applicability must not read.
- **Grant principal** — `GrantPrincipal` (`grant_principal.py`): the durable stored selector a
  request principal is tested against.
- **Submitter provenance** — `McpToolCallPrincipal` (`database_schema.py`; wire in
  `haku/shared/haku/console/tool_calls.py`): the durable submitter provenance.
- **Runtime actor / execution** — `McpExecutionCaller` / `McpExecutionContext` (`mcp_execution.py`):
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
    # Present for a Console-launched sandbox. The session bearer then scopes reads/withdrawals to
    # this session and leaves an audit link on every tool call; external Agent credentials omit it.
    session_id: UUID | None = None


type RuntimeActor = OperatorActor | AgentActor
