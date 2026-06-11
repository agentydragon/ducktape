from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import aiosqlite
import pydantic_core
from fastmcp.mcp_config import MCPConfig
from pydantic import JsonValue

from agent_core.events import EventType as Event, ToolCall, ToolCallOutput
from agent_server.agent_types import AgentID
from agent_server.models.proposal_status import ProposalStatus
from agent_server.persist.types import AgentMetadata, AgentRow, ApprovalOutcome, Persistence, PolicyProposal
from agent_server.runtime.auto_attach import filter_persistable_servers

MAX_EVENT_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB hard limit per event payload


def _now() -> datetime:
    return datetime.now(UTC)


class SQLitePersistence(Persistence):
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    # Centralized connection helpers to keep row_factory consistent
    @asynccontextmanager
    async def _open(self):
        async with aiosqlite.connect(self.db_path) as db:
            # Enforce FK cascades on every connection
            await db.execute("PRAGMA foreign_keys = ON;")
            yield db

    @asynccontextmanager
    async def _open_row(self):
        async with aiosqlite.connect(self.db_path) as db:
            # Enforce FK cascades on every connection
            await db.execute("PRAGMA foreign_keys = ON;")
            db.row_factory = aiosqlite.Row
            yield db

    async def ensure_schema(self) -> None:
        """Create base tables if missing using the current schema.

        Note: This function does not implement versioned migrations. To apply
        schema changes, recreate the database or manage data migration outside
        this helper.
        """
        async with self._open() as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            # executescript allows multiple statements in one call
            await db.executescript(
                """
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  specs TEXT NOT NULL,
  metadata TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  call_id TEXT NULL,
  tool_key TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_agent_seq ON events(agent_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_call ON events(call_id);
CREATE TABLE IF NOT EXISTS approvals (
  call_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  tool_key TEXT NOT NULL,
  outcome TEXT NOT NULL,
  decided_at TEXT NOT NULL,
  details TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_agent_decided ON approvals(agent_id, decided_at);
CREATE TABLE IF NOT EXISTS approval_policies (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_proposals (
  id TEXT NOT NULL,
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT NULL,
  PRIMARY KEY (agent_id, id)
);
-- Chat: messages and per-server last-read (HWM)
CREATE TABLE IF NOT EXISTS chat_messages (
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  author TEXT NOT NULL,
  mime TEXT NOT NULL,
  content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_agent ON chat_messages(agent_id);
CREATE TABLE IF NOT EXISTS chat_last_read (
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  server_name TEXT NOT NULL,
  last_id INTEGER NULL,
  PRIMARY KEY (agent_id, server_name)
);
                    """
            )
            await db.commit()

    # Agents -----------------------------------------------------------------
    async def create_agent(self, *, mcp_config: MCPConfig | None = None, metadata: AgentMetadata) -> AgentID:
        if mcp_config is None:
            mcp_config = MCPConfig()
        # Generate agent_id compatible with MCPMountPrefix pattern: ^[a-z][a-z0-9_]*$
        # Prefix with 'a' to ensure it starts with a letter; UUID hex is [0-9a-f] which fits the pattern
        agent_id = f"a{uuid.uuid4().hex}"
        async with self._open() as db:
            # Persist only user-configured servers (exclude default auto-attached)
            spec_json = filter_persistable_servers(mcp_config).model_dump(mode="json")
            await db.execute(
                "INSERT INTO agents (id, created_at, specs, metadata) VALUES (?, ?, ?, ?)",
                (
                    agent_id,
                    _now().isoformat(),
                    pydantic_core.to_json(spec_json, fallback=str).decode("utf-8"),
                    pydantic_core.to_json(metadata.model_dump(), fallback=str).decode("utf-8"),
                ),
            )
            await db.commit()
        return agent_id

    async def update_agent_specs(self, agent_id: AgentID, *, mcp_config: MCPConfig) -> None:
        async with self._open() as db:
            spec_json = filter_persistable_servers(mcp_config).model_dump(mode="json")
            await db.execute(
                "UPDATE agents SET specs = ? WHERE id = ?",
                (pydantic_core.to_json(spec_json, fallback=str).decode("utf-8"), agent_id),
            )
            await db.commit()

    async def list_agents(self) -> list[AgentRow]:
        out: list[AgentRow] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT id, created_at, specs, metadata FROM agents ORDER BY created_at DESC") as cur:
                async for r in cur:
                    meta_val = AgentMetadata.model_validate_json(cast(str, r["metadata"]))
                    out.append(
                        AgentRow(
                            id=r["id"],
                            created_at=datetime.fromisoformat(r["created_at"]),
                            mcp_config=MCPConfig.model_validate(json.loads(r["specs"])) if r["specs"] else MCPConfig(),
                            metadata=meta_val,
                        )
                    )
        return out

    async def get_agent(self, agent_id: AgentID) -> AgentRow | None:
        async with self._open_row() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT id, created_at, specs, metadata FROM agents WHERE id = ?", (agent_id,))
            r = await cur.fetchone()
            if not r:
                return None
            meta_val = AgentMetadata.model_validate_json(cast(str, r["metadata"]))
            return AgentRow(
                id=r["id"],
                created_at=datetime.fromisoformat(r["created_at"]),
                mcp_config=MCPConfig.model_validate(json.loads(r["specs"])) if r["specs"] else MCPConfig(),
                metadata=meta_val,
            )

    async def list_agents_last_activity(self) -> dict[AgentID, datetime | None]:
        """Return a mapping of agent_id -> last activity timestamp (UTC) or None.

        Activity considers any of: event ts or agent created_at as a fallback,
        taking the maximum.
        """
        out: dict[str, datetime | None] = {}
        async with (
            self._open_row() as db,
            db.execute(
                """
SELECT a.id as agent_id,
       MAX(COALESCE(e.ts, a.created_at)) as last_ts
FROM agents a
LEFT JOIN events e ON e.agent_id = a.id
GROUP BY a.id
                    """
            ) as cur,
        ):
            async for r in cur:
                ts = r["last_ts"]
                out[r["agent_id"]] = datetime.fromisoformat(ts) if ts is not None else None
        return out

    async def delete_agent(self, agent_id: AgentID) -> None:
        """Delete an agent and all associated records.

        Cascades to events, approvals, policies, and proposals.
        """
        async with self._open() as db:
            await db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            await db.commit()

    # ---- Approval policy (per-agent) ---------------------------------------
    async def get_latest_policy(self, agent_id: AgentID) -> tuple[str, int] | None:
        """Return (content, version) of the latest approval policy for the agent, or None."""
        async with (
            self._open_row() as db,
            db.execute(
                """
SELECT content, version
FROM approval_policies
WHERE agent_id = ?
ORDER BY version DESC
LIMIT 1
                """,
                (agent_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
            if not row:
                return None
            return (cast(str, row["content"]), int(row["version"]))

    async def set_policy(self, agent_id: AgentID, *, content: str) -> int:
        """Persist a new policy version for agent; returns assigned version."""
        async with self._open() as db:
            await db.execute(
                "INSERT INTO approval_policies (agent_id, content, created_at) VALUES (?, ?, ?)",
                (agent_id, content, _now().isoformat()),
            )
            # In SQLite, last_insert_rowid() returns the INTEGER PRIMARY KEY value for this connection
            cur = await db.execute("SELECT last_insert_rowid();")
            row = await cur.fetchone()
            await db.commit()
            return int(row[0]) if row and row[0] is not None else 0

    # ---- Policy proposals (single-store: SQLite) ----------------------------
    async def create_policy_proposal(self, agent_id: AgentID, *, proposal_id: str, content: str) -> None:
        async with self._open() as db:
            await db.execute(
                """
INSERT INTO policy_proposals (id, agent_id, content, status, created_at, decided_at)
VALUES (?, ?, ?, 'pending', ?, NULL)
                """,
                (proposal_id, agent_id, content, _now().isoformat()),
            )
            await db.commit()

    async def list_policy_proposals(self, agent_id: AgentID) -> list[PolicyProposal]:
        async with self._open_row() as db:
            out: list[PolicyProposal] = []
            async with db.execute(
                """
SELECT id, status, created_at, decided_at
FROM policy_proposals
WHERE agent_id = ?
ORDER BY created_at DESC
                """,
                (agent_id,),
            ) as cur:
                async for row in cur:
                    out.append(
                        PolicyProposal(
                            id=str(row["id"]),
                            status=ProposalStatus(str(row["status"])),
                            created_at=datetime.fromisoformat(cast(str, row["created_at"])),
                            decided_at=(
                                datetime.fromisoformat(cast(str, row["decided_at"])) if row["decided_at"] else None
                            ),
                            content="",  # content not selected in list; leave empty
                        )
                    )
        return out

    async def get_policy_proposal(self, agent_id: AgentID, proposal_id: str) -> PolicyProposal | None:
        async with (
            self._open_row() as db,
            db.execute(
                """
SELECT id, status, created_at, decided_at, content
FROM policy_proposals
WHERE agent_id = ? AND id = ?
                """,
                (agent_id, proposal_id),
            ) as cur,
        ):
            row = await cur.fetchone()
            if not row:
                return None
            return PolicyProposal(
                id=str(row["id"]),
                status=ProposalStatus(str(row["status"])),
                created_at=datetime.fromisoformat(cast(str, row["created_at"])),
                decided_at=(datetime.fromisoformat(cast(str, row["decided_at"])) if row["decided_at"] else None),
                content=cast(str, row["content"]),
            )

    async def approve_policy_proposal(self, agent_id: AgentID, proposal_id: str) -> int:
        """Mark proposal approved and persist content as new active policy version.

        Returns the new active policy version.
        """
        # Read proposal content
        async with (
            self._open_row() as db,
            db.execute(
                "SELECT content FROM policy_proposals WHERE agent_id = ? AND id = ?", (agent_id, proposal_id)
            ) as cur,
        ):
            row = await cur.fetchone()
            if not row:
                raise KeyError("proposal_not_found")
            content = cast(str, row["content"])
        # Persist as active policy and mark proposal approved in one transaction
        async with self._open() as db:
            await db.execute(
                "INSERT INTO approval_policies (agent_id, content, created_at) VALUES (?, ?, ?)",
                (agent_id, content, _now().isoformat()),
            )
            await db.execute(
                "UPDATE policy_proposals SET status = 'approved', decided_at = ? WHERE agent_id = ? AND id = ?",
                (_now().isoformat(), agent_id, proposal_id),
            )
            cur = await db.execute("SELECT last_insert_rowid();")
            row = await cur.fetchone()
            await db.commit()
            return int(row[0]) if row and row[0] is not None else 0

    async def reject_policy_proposal(self, agent_id: AgentID, proposal_id: str) -> None:
        async with self._open() as db:
            await db.execute(
                "UPDATE policy_proposals SET status = 'rejected', decided_at = ? WHERE agent_id = ? AND id = ?",
                (_now().isoformat(), agent_id, proposal_id),
            )
            await db.commit()

    async def delete_policy_proposal(self, agent_id: AgentID, proposal_id: str) -> None:
        async with self._open() as db:
            await db.execute("DELETE FROM policy_proposals WHERE agent_id = ? AND id = ?", (agent_id, proposal_id))
            await db.commit()

    # Seatbelt templates are volume-backed via Docker; no DB APIs in final shape

    # Events and approvals ----------------------------------------------------
    async def append_event(self, *, agent_id: AgentID, seq: int, ts: datetime, event: Event) -> None:
        # Serialize Pydantic event to dict with aliases
        payload = event.model_dump(mode="json", by_alias=True, exclude_none=True)
        event_type = payload["type"]

        # Derive indexable fields from event type
        call_id = event.call_id if isinstance(event, ToolCall | ToolCallOutput) else None
        tool_key = event.name if isinstance(event, ToolCall) else None

        # Apply hard limit per event payload (serialized JSON)
        # Use pydantic_core.to_json for proper serialization of nested Pydantic models
        s = pydantic_core.to_json(payload, fallback=str).decode("utf-8")
        if len(s.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError(f"event payload exceeds {MAX_EVENT_PAYLOAD_BYTES} bytes")
        async with self._open() as db:
            await db.execute(
                "INSERT INTO events (agent_id, seq, ts, type, payload, call_id, tool_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (agent_id, seq, ts.isoformat(), str(event_type), s, call_id, tool_key),
            )
            await db.commit()

    async def record_approval(
        self,
        *,
        agent_id: AgentID,
        call_id: str,
        tool_key: str,
        outcome: ApprovalOutcome,
        decided_at: datetime,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        async with self._open() as db:
            await db.execute(
                """
INSERT OR REPLACE INTO approvals (call_id, agent_id, tool_key, outcome, decided_at, details)
VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    agent_id,
                    tool_key,
                    outcome.value,
                    decided_at.isoformat(),
                    pydantic_core.to_json(details, fallback=str).decode("utf-8") if details else None,
                ),
            )
            await db.commit()
