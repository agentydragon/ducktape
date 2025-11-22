from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import uuid
from uuid import UUID

from fastmcp.mcp_config import MCPConfig
from pydantic import JsonValue
from sqlalchemy import event, select, text, update, delete
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine, async_sessionmaker

from adgn.agent.models.proposal_status import ProposalStatus
from adgn.agent.persist import PolicyProposal
from adgn.agent.runtime.auto_attach import filter_persistable_servers
from adgn.agent.types import AgentID

from . import (
    AgentRow,
    Decision,
    EventType,
    Persistence,
    PersistenceRunStatus,
    PolicyStatus,
    RunRow,
    ToolCall,
    ToolCallExecution,
    TypedPayload,
    ToolCallRecord,
)
from .events import EventRecord, parse_event
from .models import Agent, Run, Event, ToolCall as ToolCallModel, Policy, Base

MAX_EVENT_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB hard limit per event payload


def _now() -> datetime:
    return datetime.now(UTC)


class SQLitePersistence(Persistence):
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{self.db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
        self.async_session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    @asynccontextmanager
    async def _session(self):
        """Get an async session."""
        async with self.async_session_maker() as session:
            yield session

    async def ensure_schema(self) -> None:
        """Create all tables using SQLAlchemy ORM models."""
        async with self.engine.begin() as conn:
            # Drop all tables for clean slate (no backward compatibility)
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    # Agents -----------------------------------------------------------------
    async def create_agent(self, *, mcp_config: MCPConfig, preset: str) -> AgentID:
        agent_id = AgentID(uuid.uuid4().hex)
        async with self._session() as session:
            # Persist only user-configured servers (exclude default auto-attached)
            spec_json = filter_persistable_servers(mcp_config).model_dump(mode="json")
            agent = Agent(
                id=agent_id,
                created_at=_now(),
                mcp_config=spec_json,
                preset=preset,
            )
            session.add(agent)
            await session.commit()
        return agent_id

    async def update_agent_specs(self, agent_id: AgentID, *, mcp_config: MCPConfig) -> None:
        async with self._session() as session:
            spec_json = filter_persistable_servers(mcp_config).model_dump(mode="json")
            await session.execute(
                update(Agent).where(Agent.id == agent_id).values(mcp_config=spec_json)
            )
            await session.commit()

    async def patch_agent_specs(
        self, agent_id: AgentID, *, attach: dict[str, MCPConfig] = {}, detach: list[str] = []
    ) -> MCPConfig:
        async with self._session() as session:
            result = await session.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                raise KeyError(f"agent not found: {agent_id}")
            cfg = MCPConfig.model_validate(agent.mcp_config) if agent.mcp_config else MCPConfig()
            # Apply detach
            for name in detach:
                cfg.mcpServers.pop(name, None)
            # Apply attach: when given a whole config per name, merge servers
            for _name, subcfg in attach.items():
                # Runtime assertion: values must be MCPConfig per typed API
                assert isinstance(subcfg, MCPConfig), "attach values must be MCPConfig instances"
                # Merge all entries from the provided config
                for k, v in subcfg.mcpServers.items():
                    cfg.mcpServers[k] = v
            await self.update_agent_specs(agent_id, mcp_config=cfg)
        return cfg

    async def list_agents(self) -> list[AgentRow]:
        async with self._session() as session:
            result = await session.execute(select(Agent).order_by(Agent.created_at.desc()))
            agents = result.scalars().all()
            return [
                AgentRow(
                    id=agent.id,
                    created_at=agent.created_at,
                    mcp_config=MCPConfig.model_validate(agent.mcp_config) if agent.mcp_config else MCPConfig(),
                    preset=agent.preset,
                )
                for agent in agents
            ]

    async def get_agent(self, agent_id: AgentID) -> AgentRow | None:
        async with self._session() as session:
            result = await session.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                return None
            return AgentRow(
                id=agent.id,
                created_at=agent.created_at,
                mcp_config=MCPConfig.model_validate(agent.mcp_config) if agent.mcp_config else MCPConfig(),
                preset=agent.preset,
            )

    async def list_agents_last_activity(self) -> dict[AgentID, datetime | None]:
        """Return a mapping of agent_id -> last activity timestamp (UTC) or None.

        Activity considers any of: event event_at, run finished_at, run started_at, or
        agent created_at as a fallback, taking the maximum.
        """
        async with self._session() as session:
            # This is complex to do purely in ORM, so we'll use raw SQL
            result = await session.execute(
                text("""
SELECT a.id as agent_id,
       MAX(
         COALESCE(e.event_at, r.finished_at, r.started_at, a.created_at)
       ) as last_ts
FROM agents a
LEFT JOIN runs r ON r.agent_id = a.id
LEFT JOIN events e ON e.run_id = r.id
GROUP BY a.id
                    """)
            )
            return {AgentID(row.agent_id): row.last_ts for row in result}

    async def delete_agent(self, agent_id: AgentID) -> None:
        """Delete an agent and all associated records (cascaded by ORM)."""
        async with self._session() as session:
            await session.execute(delete(Agent).where(Agent.id == agent_id))
            await session.commit()

    # ---- Approval policy (per-agent) ---------------------------------------
    async def get_latest_policy(self, agent_id: AgentID) -> tuple[str, int] | None:
        """Return (content, id) of the latest ACTIVE policy for the agent, or None."""
        async with self._session() as session:
            result = await session.execute(
                select(Policy)
                .where(Policy.agent_id == agent_id, Policy.status == PolicyStatus.ACTIVE)
                .order_by(Policy.id.desc())
                .limit(1)
            )
            policy = result.scalar_one_or_none()
            if not policy:
                return None
            return (policy.content, policy.id)

    async def set_policy(self, agent_id: AgentID, *, content: str) -> int:
        """Persist a new ACTIVE policy for agent; supersedes any existing ACTIVE policy; returns assigned id."""
        async with self._session() as session:
            # Mark existing ACTIVE policy as SUPERSEDED
            await session.execute(
                update(Policy)
                .where(Policy.agent_id == agent_id, Policy.status == PolicyStatus.ACTIVE)
                .values(status=PolicyStatus.SUPERSEDED)
            )
            policy = Policy(
                agent_id=agent_id,
                content=content,
                status=PolicyStatus.ACTIVE,
                created_at=_now(),
                decided_at=_now(),
            )
            session.add(policy)
            await session.commit()
            await session.refresh(policy)
            return policy.id

    # ---- Policy proposals (single-store: SQLite) ----------------------------
    async def create_policy_proposal(self, agent_id: AgentID, *, proposal_id: int, content: str) -> int:
        async with self._session() as session:
            # proposal_id is provided but new schema uses autoincrement
            # Store the proposal with PROPOSED status
            policy = Policy(
                agent_id=agent_id,
                content=content,
                status=ProposalStatus.PENDING,
                created_at=_now(),
                decided_at=None,
            )
            session.add(policy)
            await session.commit()
            # Return the actual database-assigned ID
            await session.refresh(policy)
            return policy.id

    async def list_policy_proposals(self, agent_id: AgentID) -> list[PolicyProposal]:
        async with self._session() as session:
            result = await session.execute(
                select(Policy)
                .where(Policy.agent_id == agent_id, Policy.status.in_([ProposalStatus.PENDING, ProposalStatus.APPROVED, ProposalStatus.REJECTED]))
                .order_by(Policy.created_at.desc())
            )
            policies = result.scalars().all()
            return [
                PolicyProposal(
                    id=str(policy.id),  # Convert int id to string for API compatibility
                    status=policy.status,
                    created_at=policy.created_at,
                    decided_at=policy.decided_at,
                    content="",  # content not selected in list; leave empty
                )
                for policy in policies
            ]

    async def get_policy_proposal(self, agent_id: AgentID, proposal_id: int) -> PolicyProposal | None:
        async with self._session() as session:
            result = await session.execute(
                select(Policy).where(Policy.id == proposal_id, Policy.agent_id == agent_id)
            )
            policy = result.scalar_one_or_none()
            if not policy:
                return None
            return PolicyProposal(
                id=str(policy.id),
                status=policy.status,
                created_at=policy.created_at,
                decided_at=policy.decided_at,
                content=policy.content,
            )

    async def approve_policy_proposal(self, agent_id: AgentID, proposal_id: int) -> int:
        """Mark proposal approved and make it the active policy.

        Returns the new active policy id.
        """
        async with self._session() as session:
            result = await session.execute(
                select(Policy).where(Policy.id == proposal_id, Policy.agent_id == agent_id)
            )
            policy = result.scalar_one_or_none()
            if not policy:
                raise KeyError("proposal_not_found")

            # Mark existing ACTIVE policies as SUPERSEDED
            await session.execute(
                update(Policy)
                .where(Policy.agent_id == agent_id, Policy.status == PolicyStatus.ACTIVE)
                .values(status=PolicyStatus.SUPERSEDED)
            )

            # Mark proposal as ACTIVE
            policy.status = PolicyStatus.ACTIVE
            policy.decided_at = _now()
            await session.commit()
            return policy.id

    async def reject_policy_proposal(self, agent_id: AgentID, proposal_id: int) -> None:
        async with self._session() as session:
            await session.execute(
                update(Policy)
                .where(Policy.id == proposal_id, Policy.agent_id == agent_id)
                .values(status=ProposalStatus.REJECTED, decided_at=_now())
            )
            await session.commit()

    # Runs --------------------------------------------------------------------
    async def start_run(
        self,
        *,
        run_id: UUID,
        agent_id: AgentID,
        system_message: str | None,
        model: str | None,
        model_params: dict[str, JsonValue] | None,
        started_at: datetime,
    ) -> None:
        async with self._session() as session:
            run = Run(
                id=run_id,
                agent_id=agent_id,
                started_at=started_at,
                finished_at=None,
                status=PersistenceRunStatus.RUNNING,
                system_message=system_message,
                model=model,
                model_params=model_params,
            )
            session.add(run)
            await session.commit()

    async def finish_run(self, run_id: UUID, *, status: PersistenceRunStatus, finished_at: datetime) -> None:
        async with self._session() as session:
            await session.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(status=status, finished_at=finished_at)
            )
            await session.commit()

    async def append_event(
        self,
        *,
        run_id: UUID,
        seq: int,
        ts: datetime,
        type: EventType,
        payload: TypedPayload,
        call_id: str | None = None,
        tool_key: str | None = None,
    ) -> None:
        # Serialize TypedPayload to dict for persistence
        payload_dict = payload.model_dump(mode="json", exclude_none=True)
        # Apply hard limit per event payload (serialized JSON)
        s = json.dumps(payload_dict, ensure_ascii=False)
        if len(s.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError(f"event payload exceeds {MAX_EVENT_PAYLOAD_BYTES} bytes")

        async with self._session() as session:
            event = Event(
                run_id=run_id,
                seq=seq,
                event_at=ts,
                type=type,
                payload=payload_dict,
                call_id=call_id,
                tool_key=tool_key,
            )
            session.add(event)
            await session.commit()

    async def list_runs(self, *, agent_id: AgentID | None = None, limit: int = 50) -> list[RunRow]:
        async with self._session() as session:
            query = select(Run)
            if agent_id:
                query = query.where(Run.agent_id == agent_id)
            query = query.order_by(Run.started_at.desc()).limit(limit)

            result = await session.execute(query)
            runs = result.scalars().all()

            return [
                RunRow(
                    id=run.id,
                    agent_id=run.agent_id,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    status=run.status,
                    system_message=run.system_message,
                    model=run.model,
                    model_params=run.model_params,
                )
                for run in runs
            ]

    async def get_run(self, run_id: UUID) -> RunRow | None:
        async with self._session() as session:
            result = await session.execute(select(Run).where(Run.id == run_id))
            run = result.scalar_one_or_none()
            if not run:
                return None
            return RunRow(
                id=run.id,
                agent_id=run.agent_id,
                started_at=run.started_at,
                finished_at=run.finished_at,
                status=run.status,
                system_message=run.system_message,
                model=run.model,
                model_params=run.model_params,
            )

    async def load_events(self, run_id: UUID) -> list[EventRecord]:
        async with self._session() as session:
            result = await session.execute(
                select(Event).where(Event.run_id == run_id).order_by(Event.seq.asc())
            )
            events = result.scalars().all()
            return [
                parse_event(
                    {
                        "seq": event.seq,
                        "ts": event.event_at,
                        "type": event.type,
                        "payload": event.payload,
                        "call_id": event.call_id,
                        "tool_key": event.tool_key,
                    }
                )
                for event in events
            ]

    # Tool Calls (new ToolCallRecord persistence) --------------------------------
    async def save_tool_call(self, record: ToolCallRecord) -> None:
        """Save or update a tool call record."""
        async with self._session() as session:
            result = await session.execute(
                select(ToolCallModel).where(ToolCallModel.call_id == record.call_id)
            )
            existing = result.scalar_one_or_none()

            tool_call_json = json.loads(record.tool_call.model_dump_json())
            decision_json = json.loads(record.decision.model_dump_json()) if record.decision else None
            execution_json = json.loads(record.execution.model_dump_json()) if record.execution else None

            # Extract timestamps
            created_at = _now()
            decided_at = record.decision.decided_at if record.decision else None
            completed_at = record.execution.completed_at if record.execution else None

            if existing:
                # Update
                existing.run_id = record.run_id
                existing.agent_id = record.agent_id
                existing.tool_call_json = tool_call_json
                existing.decision_json = decision_json
                existing.execution_json = execution_json
                existing.decided_at = decided_at
                existing.completed_at = completed_at
            else:
                # Insert
                tool_call = ToolCallModel(
                    call_id=record.call_id,
                    run_id=record.run_id,
                    agent_id=record.agent_id,
                    tool_call_json=tool_call_json,
                    decision_json=decision_json,
                    execution_json=execution_json,
                    created_at=created_at,
                    decided_at=decided_at,
                    completed_at=completed_at,
                )
                session.add(tool_call)
            await session.commit()

    async def get_tool_call(self, call_id: str) -> ToolCallRecord | None:
        """Get a tool call record by call_id."""
        async with self._session() as session:
            result = await session.execute(
                select(ToolCallModel).where(ToolCallModel.call_id == call_id)
            )
            tool_call = result.scalar_one_or_none()
            if not tool_call:
                return None

            # Deserialize JSON to Pydantic models
            tc = ToolCall.model_validate(tool_call.tool_call_json)
            decision = Decision.model_validate(tool_call.decision_json) if tool_call.decision_json else None
            execution = (
                ToolCallExecution.model_validate(tool_call.execution_json) if tool_call.execution_json else None
            )

            return ToolCallRecord(
                call_id=tool_call.call_id,
                run_id=tool_call.run_id,
                agent_id=AgentID(tool_call.agent_id),
                tool_call=tc,
                decision=decision,
                execution=execution,
            )

    async def list_tool_calls(self, run_id: str | None = None) -> list[ToolCallRecord]:
        """List tool call records, optionally filtered by run_id."""
        async with self._session() as session:
            query = select(ToolCallModel)
            if run_id:
                query = query.where(ToolCallModel.run_id == run_id)
            query = query.order_by(ToolCallModel.created_at.asc())

            result = await session.execute(query)
            tool_calls = result.scalars().all()

            return [
                ToolCallRecord(
                    call_id=tool_call.call_id,
                    run_id=tool_call.run_id,
                    agent_id=AgentID(tool_call.agent_id),
                    tool_call=ToolCall.model_validate(tool_call.tool_call_json),
                    decision=Decision.model_validate(tool_call.decision_json) if tool_call.decision_json else None,
                    execution=(
                        ToolCallExecution.model_validate(tool_call.execution_json)
                        if tool_call.execution_json
                        else None
                    ),
                )
                for tool_call in tool_calls
            ]

    # Policy state management (removed - now handled by unified Policy table above)
    # The old create_policy, get_policy, update_policy, list_policies, delete_policy
    # methods for named/reusable policies have been removed since policies are now
    # per-agent only.
