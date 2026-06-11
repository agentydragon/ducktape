# Design: Resource Capability Tokens for Agent Collaboration

> **OUTDATED (2025-12):** This document predates the unified `agent_runs` table.
> The separate `critic_runs`, `grader_runs`, and `prompt_optimization_runs` tables
> described here have been replaced by a single `agent_runs` table with `type_config` JSONB.
> Some concepts (capability tokens, RLS patterns) are still relevant but the schema details are outdated.
> See `docs/design/agent-definitions.md` for the current architecture.

## Summary

Enable agents to spawn sub-agents with proper isolation via:

- **Hub table pattern**: `agents` table for metadata/hierarchy + type-specific tables for execution details
- **Messages as first-class entities**: Typed, immutable, addressable by UUID (no JSON copying)
- **Capability tokens** (UUIDs) for access delegation
- **Postgres RLS** with per-agent roles for DB security
- **In-memory status**: No status column in DB, derive from runtime registry
- **Continuing conversations**: Agents can be resumed anytime by sending messages

## Problem

Critics need to spawn sub-agents and share resources without breaking isolation:

1. **Transcript access**: Agents see only their own outputs + spawned children (or delegated)
2. **Hierarchical spawning**: Sub-critics spawning helpers
3. **Direct DB access**: Agents query Postgres with full SQL (no MCP wrappers)
4. **Continuing conversations**: Resume agents hours/days later

## Solution: Hub Table + Capability Tokens

### Agents Hub Table

```sql
CREATE TABLE agents (
    id UUID PRIMARY KEY,
    name TEXT,  -- Friendly name (e.g., "dead-code-critic-v3")
    type TEXT NOT NULL,  -- 'critic', 'grader', 'optimizer', 'generic'
    parent_id UUID REFERENCES agents(id),
    model TEXT,  -- 'claude-sonnet-4.5', etc.
    input_schema JSONB,   -- Expected inputs (for validation/docs)
    output_schema JSONB,  -- Expected outputs (what caller expects)
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMP  -- Updated when execution starts/stops
);

CREATE INDEX idx_agents_parent ON agents(parent_id);
```

**No status column!** Agent status (running vs. idle) is purely in-memory runtime state.

### Type-Specific Tables (1:1 with agents)

```sql
-- Rename transcript_id → agent_id, add FK to agents table
ALTER TABLE critic_runs RENAME COLUMN transcript_id TO agent_id;
ALTER TABLE critic_runs ADD CONSTRAINT fk_critic_runs_agent FOREIGN KEY (agent_id) REFERENCES agents(id);
ALTER TABLE critic_runs ADD CONSTRAINT uq_critic_runs_agent UNIQUE (agent_id);

-- Move model column to agents table
ALTER TABLE critic_runs DROP COLUMN model;

-- Similar for grader_runs, prompt_optimization_runs, events
```

### Agent Grants (Access Delegation)

Agent grants specify **specific capabilities** one agent has on another:

1. **`read_transcript`** - Read the agent's events (transcript)
2. **`send_messages`** - Send messages to the agent and receive responses
3. **`administer_grants`** - Grant others access to this agent (delegation)

```sql
CREATE TABLE agent_grants (
    grantor_agent_id UUID NOT NULL REFERENCES agents(id),  -- Agent doing the granting
    grantee_agent_id UUID NOT NULL REFERENCES agents(id),  -- Agent receiving the capability
    target_agent_id UUID NOT NULL REFERENCES agents(id),   -- Agent the capability applies TO
    capability TEXT NOT NULL,                              -- 'read_transcript', 'send_messages', 'administer_grants'
    granted_at TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (grantor_agent_id, grantee_agent_id, target_agent_id, capability),
    CONSTRAINT valid_capability CHECK (capability IN ('read_transcript', 'send_messages', 'administer_grants'))
);
CREATE INDEX idx_agent_grants_grantee_target_cap ON agent_grants(grantee_agent_id, target_agent_id, capability);
```

**Grant examples:**

- `(grantor=PO, grantee=grader, target=critic, capability='read_transcript')` → grader can read critic's transcript
- `(grantor=PO, grantee=PO, target=critic, capability='send_messages')` → PO can send messages to critic
- `(grantor=PO, grantee=PO, target=critic, capability='administer_grants')` → PO can grant others access to critic

**Automatic grants on spawn:**
When agent A spawns agent B, create:

- `(grantor=A, grantee=A, target=B, capability='read_transcript')`
- `(grantor=A, grantee=A, target=B, capability='send_messages')`
- `(grantor=A, grantee=A, target=B, capability='administer_grants')`

**Example delegation:**

- PO spawns critic (PO gets all capabilities on critic)
- PO spawns grader (PO gets all capabilities on grader)
- PO wants grader to read critic's output:
  - `grant_access(target=critic, grantee=grader, capability='read_transcript')`
- PO wants grader to send messages to critic:
  - `grant_access(target=critic, grantee=grader, capability='send_messages')`

**Grant management tool:**

```python
from enum import StrEnum

class GrantCapability(StrEnum):
    READ_TRANSCRIPT = "read_transcript"
    SEND_MESSAGES = "send_messages"
    ADMINISTER_GRANTS = "administer_grants"

class GrantAccessInput(BaseModel):
    target_agent_id: UUID        # Agent to grant capability on
    grantee_agent_id: UUID       # Agent receiving the capability
    capability: GrantCapability  # Which capability to grant

class GrantAccessOutput(BaseModel):
    success: bool

@mcp.tool(flat=True)
async def grant_access(input: GrantAccessInput) -> GrantAccessOutput:
    """Delegate a capability on an agent to another agent.

    Requires: caller must have 'administer_grants' capability on target_agent_id.
    """
    grantor = get_current_agent_id()

    # Verify caller has administer_grants capability on target
    can_administer = await db.fetch_one("""
        SELECT 1 FROM agent_grants
        WHERE grantee_agent_id = :grantor
          AND target_agent_id = :target
          AND capability = 'administer_grants'
    """, {"grantor": grantor, "target": input.target_agent_id})

    if not can_administer:
        raise ToolError(f"No permission to administer grants for agent {input.target_agent_id}")

    # Create grant (idempotent)
    await db.execute("""
        INSERT INTO agent_grants (grantor_agent_id, grantee_agent_id, target_agent_id, capability)
        VALUES (:grantor, :grantee, :target, :capability)
        ON CONFLICT DO NOTHING
    """, {
        "grantor": grantor,
        "grantee": input.grantee_agent_id,
        "target": input.target_agent_id,
        "capability": input.capability
    })

    return GrantAccessOutput(success=True)

# TODO: If we add RLS policies on INSERT/DELETE for agent_grants, we could skip this tool entirely.
# Agents would just run:
#   INSERT INTO agent_grants (grantor_agent_id, grantee_agent_id, target_agent_id, capability)
#   VALUES (my_id, 'grantee', 'target', 'capability')
# RLS would enforce: can only insert if you have 'administer_grants' on target.
# Similarly for DELETE/revocation. Would require:
#   - INSERT policy: WHERE grantor = current_user AND has administer_grants on target
#   - DELETE policy: WHERE grantor = current_user AND has administer_grants on target
# More flexible, no tool layer needed.
```

### Messages (Typed, Immutable Data Blobs)

Agents communicate via **typed messages** stored in DB. Messages are immutable, addressable by UUID, enabling zero-copy data passing between agents.

```sql
CREATE TABLE messages (
    id UUID PRIMARY KEY,
    schema_type TEXT NOT NULL,  -- 'plaintext', 'structured_critique', 'structured_grade'
    content JSONB NOT NULL,  -- Validated against schema
    created_by_agent_id UUID REFERENCES agents(id),
    in_reply_to UUID REFERENCES messages(id),  -- For async reply tracking
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT valid_schema_type CHECK (schema_type IN ('plaintext', 'structured_critique', 'structured_grade'))
);
CREATE INDEX idx_messages_schema_type ON messages(schema_type);
CREATE INDEX idx_messages_created_by ON messages(created_by_agent_id);
CREATE INDEX idx_messages_in_reply_to ON messages(in_reply_to);
```

**Schema Registry** (Python):

```python
# props/core/messages/schemas.py
from enum import StrEnum
from pydantic import BaseModel

class MessageSchema(StrEnum):
    PLAINTEXT = "plaintext"
    STRUCTURED_CRITIQUE = "structured_critique"
    STRUCTURED_GRADE = "structured_grade"

class PlaintextMessage(BaseModel):
    text: str

class StructuredCritique(BaseModel):
    issues: list[dict]
    notes_md: str

class StructuredGrade(BaseModel):
    recall: float
    precision: float
    # ... other fields

SCHEMA_MODELS = {
    MessageSchema.PLAINTEXT: PlaintextMessage,
    MessageSchema.STRUCTURED_CRITIQUE: StructuredCritique,
    MessageSchema.STRUCTURED_GRADE: StructuredGrade,
}
```

**MCP Tools** (Only upsert, agents read via SQL):

```python
from pydantic import BaseModel
from uuid import UUID

class UpsertMessageInput(BaseModel):
    schema_type: MessageSchema
    content: dict[str, Any]  # Will be validated against schema_type
    in_reply_to: UUID | None = None

class UpsertMessageOutput(BaseModel):
    message_id: UUID

@mcp.tool(flat=True)
async def upsert_message(input: UpsertMessageInput) -> UpsertMessageOutput:
    """Store typed message. Validates schema and returns immutable ID."""

    # Validate against schema
    schema_model = SCHEMA_MODELS[input.schema_type]
    validated_content = schema_model.model_validate(input.content)

    # Store (immutable)
    message_id = uuid4()
    await db.execute("""
        INSERT INTO messages (id, schema_type, content, created_by_agent_id, in_reply_to)
        VALUES (:id, :schema_type, :content, :agent_id, :in_reply_to)
    """, {
        "id": message_id,
        "schema_type": input.schema_type,
        "content": validated_content.model_dump(),
        "agent_id": get_current_agent_id(),
        "in_reply_to": input.in_reply_to
    })

    return UpsertMessageOutput(message_id=message_id)
```

**Agents read via SQL** (no read_message tool):

```sql
-- Agent queries directly
SELECT content FROM messages WHERE id = 'some-uuid';
-- RLS enforces: can only see own messages + children's messages
```

**RLS Policy**:

```sql
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY message_access ON messages FOR SELECT USING (
    -- My own messages
    created_by_agent_id::text = replace(current_user, 'agent_', '')
    OR
    -- Messages from agents I have read_transcript capability on
    created_by_agent_id IN (
        SELECT target_agent_id FROM agent_grants
        WHERE grantee_agent_id::text = replace(current_user, 'agent_', '')
          AND capability = 'read_transcript'
    )
);
```

**Schema Registry as MCP Resources**:

```python
# Agents can read available schemas
@mcp.resource("resource://messages/schemas")
async def list_schemas() -> str:
    return json.dumps({
        schema.value: {
            "description": model.__doc__,
            "json_schema": model.model_json_schema()
        }
        for schema, model in SCHEMA_MODELS.items()
    })
```

**Benefits**:

- **Zero-copy**: Pass message UUIDs instead of copying large JSON payloads
- **Type safety**: Schema validation at write time
- **Immutable**: Messages can't be modified after creation
- **Versioning ready**: Can add `schema_version` field for evolution
- **Audit trail**: `created_by_agent_id` tracks provenance
- **Async delivery**: Fire-and-forget with explicit reply linkage

### Message Delivery Protocol (Async)

Messages are delivered **asynchronously** via formatted user messages in the agent transcript (events table).

**Delivery (fire-and-forget):**

```python
class SendMessageInput(BaseModel):
    agent_id: UUID
    message_id: UUID

class SendMessageOutput(BaseModel):
    status: Literal["delivered"]

async def send_message(input: SendMessageInput) -> SendMessageOutput:
    """Send a message to another agent. Requires 'send_messages' capability."""

    sender = get_current_agent_id()

    # Verify sender has send_messages capability on target
    can_send = await db.fetch_one("""
        SELECT 1 FROM agent_grants
        WHERE grantee_agent_id = :sender
          AND target_agent_id = :target
          AND capability = 'send_messages'
    """, {"sender": sender, "target": input.agent_id})

    if not can_send:
        raise ToolError(f"No permission to send messages to agent {input.agent_id}")

    # Insert user_message event with formatted notification
    notification = f"""📬 Message received

ID: {input.message_id}
From: {sender}

Read: SELECT * FROM messages WHERE id='{input.message_id}'
Reply: upsert_message(in_reply_to='{input.message_id}', ...)
"""

    await append_event(input.agent_id, user_message_event(notification))

    # Start agent loop if idle
    if input.agent_id not in running_agents:
        start_agent_loop(input.agent_id)  # Non-blocking

    return SendMessageOutput(status="delivered")

# TODO: Consider convenience option to embed message body in send_message
# Instead of: msg = upsert_message(...); send_message(agent_id, msg.message_id)
# Could allow: send_message(agent_id, message_body={schema_type, content})
# Would upsert internally and deliver. Reduces two-step to one-step for common case.
```

**Polling for responses (agent-side, no tool needed):**

Agents poll using SQL directly. Example code will be provided in agent prompts:

```python
# TODO: Add this example to agent system prompts
# Wait for reply to a message you sent

import time

message_id = "abc-123"  # Message you sent
timeout = 300.0
start = time.time()

while time.time() - start < timeout:
    result = execute_sql(f"""
        SELECT id FROM messages
        WHERE in_reply_to = '{message_id}'
    """)

    if result:
        reply_id = result[0]['id']
        # Process reply...
        break

    time.sleep(1.0)
```

No `poll_messages` tool needed - agents have SQL access and can implement polling logic as needed.

**Response delivery (when reply is sent):**

```python
# In upsert_message tool, after inserting reply:
if input.in_reply_to:
    # Find who sent the original message
    original = await db.fetch_one(
        "SELECT created_by_agent_id FROM messages WHERE id = :id",
        {"id": input.in_reply_to}
    )

    # Notify original sender (if idle - running agents will poll)
    if original["created_by_agent_id"] not in running_agents:
        notification = f"""📬 Reply received

Your message: {input.in_reply_to}
Reply ID: {message_id}

Read: SELECT * FROM messages WHERE id='{message_id}'
"""
        await append_event(original["created_by_agent_id"], user_message_event(notification))
```

**Key design decisions:**

- **Formatted user messages**: Notifications are text-based `user_message` events (no coupling to agent.py)
- **Lazy content delivery**: Only message ID in notification, agent reads content via SQL
- **Explicit reply linkage**: Agents call `upsert_message(in_reply_to=...)` to respond
- **No ordering enforcement**: Agents can process messages in any order (batch processing allowed)
- **Agent-side polling**: No `poll_messages` tool - agents poll via SQL (example code in prompts)
- **Response notifications**: Replies trigger formatted user messages to original sender

**Agent loop:**

```python
async def agent_loop(agent_id: UUID):
    """Agent processes messages until no unreplied messages remain."""
    while True:
        # Check for unreplied messages
        unreplied = await db.fetch_all("""
            SELECT m.id FROM messages m
            WHERE m.created_by_agent_id != :agent_id
              AND m.id IN (
                  -- Messages delivered to this agent (via events)
                  SELECT payload->>'message_id' FROM events
                  WHERE agent_id = :agent_id AND event_type = 'user_message'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM messages replies
                  WHERE replies.in_reply_to = m.id
                    AND replies.created_by_agent_id = :agent_id
              )
        """, {"agent_id": agent_id})

        if not unreplied:
            # All messages handled - go idle
            del running_agents[agent_id]
            break

        # Sample one turn (agent can call tools, upsert_message, etc.)
        await sample_turn(agent_id)
```

**Example flow:**

1. PO spawns critic: `critic_id = spawn_agent(type='critic', ...)`
2. PO creates input message: `input_msg = upsert_message(schema_type='plaintext', content={'text': 'analyze this code'})`
3. PO sends to critic: `send_message(agent_id=critic_id, message_id=input_msg.message_id)` → returns immediately
4. PO polls via SQL: `SELECT id FROM messages WHERE in_reply_to = :input_msg_id` (loop until found, or timeout)
5. Critic wakes up, sees "📬 Message received" notification in transcript
6. Critic reads: `SELECT content FROM messages WHERE id = :input_msg_id`
7. Critic analyzes, calls: `upsert_message(schema_type='structured_critique', content={...}, in_reply_to=input_msg_id)`
8. Critic reply triggers response notification to PO (if PO idle)
9. PO's poll query returns row: `critique_msg_id`
10. PO sees "📬 Reply received" notification in transcript (if was idle)
11. PO reads critique: `SELECT content FROM messages WHERE id = :critique_msg_id`

### Runtime State (In-Memory Only)

```python
# In subagents MCP server
running_agents: dict[UUID, AgentLoop] = {}

@dataclass
class AgentLoop:
    agent_id: UUID
    task: asyncio.Task
    compositor: Compositor
    agent_runtime: Agent

async def start_agent_loop(agent_id: UUID):
    """Start agent execution loop (non-blocking)."""
    loop = AgentLoop(
        agent_id=agent_id,
        task=asyncio.create_task(agent_loop(agent_id)),
        compositor=await build_compositor(agent_id),
        agent_runtime=await build_minicodex(agent_id)
    )
    running_agents[agent_id] = loop
```

**Agent lifecycle**:

- **Idle**: Not in `running_agents`, can receive messages anytime
- **Running**: In `running_agents`, actively processing messages
- **Message delivery**: Fire-and-forget, multiple messages can be delivered while agent is running
- **Auto-idle**: Agent removes itself from `running_agents` when no unreplied messages remain
- Agents never "complete" - return to idle after all messages handled
- Server restart → all agents idle (`running_agents = {}`)

### SQLAlchemy Models (DRY via Mixin)

```python
# props/db/models.py

class Agent(Base):
    """Hub table for all agents."""
    __tablename__ = "agents"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True, index=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    last_active_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)

    # Relationships
    parent: Mapped["Agent | None"] = relationship("Agent", remote_side=[id], back_populates="children")
    children: Mapped[list["Agent"]] = relationship("Agent", back_populates="parent")

class Message(Base):
    """Typed, immutable messages for agent communication."""
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    schema_type: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by_agent_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    in_reply_to: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("messages.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    # Relationships
    created_by: Mapped[Agent] = relationship("Agent")
    reply_to: Mapped["Message | None"] = relationship("Message", remote_side=[id])

class CriticRun(Base):
    __tablename__ = "critic_runs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("agents.id"), unique=True, nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String(64), ForeignKey("prompts.prompt_sha256"), nullable=False)
    specimen_slug: Mapped[str] = mapped_column("specimen", String, ForeignKey("specimens.specimen"), nullable=False)
    # model removed (now in agents table)
    critique_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("critiques.id"), nullable=True)
    files: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now())

    # Relationships
    agent: Mapped[Agent] = relationship("Agent")
    prompt_obj: Mapped[Prompt] = relationship(back_populates="critic_runs")
    specimen_obj: Mapped[Specimen] = relationship(back_populates="critic_runs")
    critique_obj: Mapped[Critique | None] = relationship(back_populates="critic_run", foreign_keys=[critique_id], post_update=True)

class AgentGrants(Base):
    __tablename__ = "agent_grants"

    grantor_agent_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("agents.id"), primary_key=True)
    grantee_agent_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("agents.id"), primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
```

### Postgres RLS (Per-Agent Roles)

```sql
-- Enable RLS on agents table
ALTER TABLE agents ENABLE ROW LEVEL SECURITY;

CREATE POLICY agent_isolation ON agents FOR SELECT USING (
    -- My own agent
    id::text = replace(current_user, 'agent_', '')
    OR
    -- Agents I have read_transcript capability on
    id IN (
        SELECT target_agent_id
        FROM agent_grants
        WHERE grantee_agent_id::text = replace(current_user, 'agent_', '')
          AND capability = 'read_transcript'
    )
);

-- Type-specific tables inherit via FK (no separate policies needed)
-- Agent queries: SELECT * FROM critic_runs cr JOIN agents a ON cr.agent_id = a.id

-- Events table (transcript access)
ALTER TABLE events ENABLE ROW LEVEL SECURITY;

CREATE POLICY event_access ON events FOR SELECT USING (
    -- My own events
    transcript_id::text = replace(current_user, 'agent_', '')
    OR
    -- Events of agents I have read_transcript capability on
    transcript_id IN (
        SELECT target_agent_id
        FROM agent_grants
        WHERE grantee_agent_id::text = replace(current_user, 'agent_', '')
          AND capability = 'read_transcript'
    )
);

-- Agent grants table (introspection + management)
ALTER TABLE agent_grants ENABLE ROW LEVEL SECURITY;

CREATE POLICY grant_access ON agent_grants FOR SELECT USING (
    -- Grants I am the grantee of (my capabilities)
    grantee_agent_id::text = replace(current_user, 'agent_', '')
    OR
    -- Grants for agents I can administer
    target_agent_id IN (
        SELECT target_agent_id
        FROM agent_grants
        WHERE grantee_agent_id::text = replace(current_user, 'agent_', '')
          AND capability = 'administer_grants'
    )
);
```

**Agent spawning creates Postgres role**:

```python
async def create_agent_role(agent_id: UUID) -> tuple[str, str]:
    username = f"agent_{agent_id.hex}"
    password = secrets.token_urlsafe(32)

    await db.execute(f"""
        CREATE ROLE {username} LOGIN PASSWORD '{password}';
        GRANT SELECT, INSERT, UPDATE ON
            agents, critic_runs, grader_runs, critiques, specimens, prompts
        TO {username};
    """)

    return username, password
```

**Cleanup only on explicit termination** (not automatic):

```python
async def drop_agent_role(agent_id: UUID):
    username = f"agent_{agent_id.hex}"
    await db.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{username}'")
    await db.execute(f"DROP ROLE IF EXISTS {username}")
```

## Implementation Order (11 Milestones, ~8-9 days)

### Milestone 1: Messages Foundation (Day 1 AM, ~2h)

- **Schema changes**:

  ```sql
  CREATE TABLE messages (
      id UUID PRIMARY KEY,
      schema_type TEXT NOT NULL,
      content JSONB NOT NULL,
      created_by_agent_id UUID REFERENCES agents(id),
      in_reply_to UUID REFERENCES messages(id),
      created_at TIMESTAMP NOT NULL DEFAULT NOW()
  );
  CREATE INDEX idx_messages_schema_type ON messages(schema_type);
  CREATE INDEX idx_messages_created_by ON messages(created_by_agent_id);
  CREATE INDEX idx_messages_in_reply_to ON messages(in_reply_to);
  ```

- Create `props/core/messages/schemas.py`:
  - `MessageSchema` StrEnum
  - Pydantic models (`PlaintextMessage`, `StructuredCritique`, `StructuredGrade`)
  - `SCHEMA_MODELS` registry
- Add SQLAlchemy `Message` model with `in_reply_to` relationship
- RLS policy on messages table

### Milestone 2: Messages MCP Server (Day 1 AM, ~2h)

- Create `mcp_infra/messages/server.py`
- Implement `upsert_message` tool:
  - Pydantic I/O: `UpsertMessageInput` (schema_type, content, in_reply_to) → `UpsertMessageOutput` (message_id)
  - Schema validation via `SCHEMA_MODELS` registry
  - Response notifications: when `in_reply_to` is set, notify original sender (formatted user message)
- Implement `send_message` tool:
  - Pydantic I/O: `SendMessageInput` (agent_id, message_id) → `SendMessageOutput` (status)
  - Fire-and-forget delivery via formatted user messages
  - Validates: caller has `send_messages` capability on target agent
- Implement `grant_access` tool:
  - Pydantic I/O: `GrantAccessInput` (target_agent_id, grantee_agent_id, capability) → `GrantAccessOutput` (success)
  - Creates: `(grantor=caller, grantee=grantee_agent_id, target=target_agent_id, capability=...)`
  - Validates: caller has `administer_grants` capability on `target_agent_id`
- Add schema resources (`resource://messages/schemas`)
- Mount on all agents
- **No read_message or poll_messages tools** (agents use SQL directly)
- **TODO**: Add SQL polling example code to agent system prompts

### Milestone 3: Agents Hub Table (Day 1 PM, ~2h)

- **Schema changes (ALTER TABLE directly in prod)**:

  ```sql
  -- Create agents table
  CREATE TABLE agents (...);

  -- Rename columns
  ALTER TABLE critic_runs RENAME COLUMN transcript_id TO agent_id;
  ALTER TABLE grader_runs RENAME COLUMN transcript_id TO agent_id;

  -- Add FKs
  ALTER TABLE critic_runs ADD CONSTRAINT fk_critic_runs_agent FOREIGN KEY (agent_id) REFERENCES agents(id);

  -- Move model to agents table (backfill, then drop)
  INSERT INTO agents (id, type, model, ...) SELECT agent_id, 'critic', model, ... FROM critic_runs;
  ALTER TABLE critic_runs DROP COLUMN model;

  -- Create agent_grants
  CREATE TABLE agent_grants (...);
  ```

- Update SQLAlchemy models (`Agent`, `CriticRun`, `AgentGrants`)

### Milestone 4: Postgres RLS Setup (Day 1 PM, ~2h)

- RLS policies on agents table
- `create_agent_role()`, `drop_agent_role()` helpers
- Grant agents SELECT/INSERT/UPDATE on `messages` table

### Milestone 5: Subagents Server Scaffolding (Day 2 AM, ~2h)

- Capability registry (in-memory)
- `mcp_infra/subagents/server.py`, `models.py`
- `running_agents` registry

### Milestone 6: `run_subagent` MVP (Day 2, ~6h)

- Agent spawning with DB credentials baked in
- `create_agent_role` → pass via container env vars
- Rebuild agent from events table (for continuing conversations)
- Grant creation on spawn:
  - `(grantor=parent, grantee=parent, target=child, capability='read_transcript')`
  - `(grantor=parent, grantee=parent, target=child, capability='send_messages')`
  - `(grantor=parent, grantee=parent, target=child, capability='administer_grants')`
- Initial message ID → message UUID conversion

### Milestone 7: Simplify Critic (Day 3, ~4h)

- Update critic prompt to use `upsert_message`
- Remove old critic_submit server (funky multi-tool pattern)
- Test: critic produces valid `structured_critique` message
- Link critic_runs to messages table (optional `message_id` FK)

### Milestone 8: Grader Integration (Day 3, ~2h)

- Update grader prompt: read via SQL, write via `upsert_message`
- Test: grader produces valid `structured_grade` message

### Milestone 9: PO Message Piping (Day 4, ~4h)

- Update PO to pipe message IDs between agents
- Test end-to-end: PO → critic → grader via message UUIDs
- **🚩 SHIP THIS** - MVP complete with messages

### Milestone 5-9: Async, Fork, MCP Delegation, Volumes

(Days 3-8, parallelism + optimization)

## Benefits

1. **No throwaway MCP tools**: DB access baked in from Day 1
2. **Continuing conversations**: Resume agents anytime
3. **Clean hierarchy**: `agents.parent_id` → simple recursive queries
4. **Type safety**: Specific tables keep schemas, no JSONB polymorphism
5. **Generic agents**: Can spawn agents with `type='generic'`, no critic_run needed
6. **Extensible**: Add `name`, `input_schema`, `output_schema` once, all types get it
7. **Simple status**: In-memory only, no DB sync issues

## Trade-offs

- **Hub table overhead**: Extra join (agents + critic_runs), but cleaner hierarchy
- **No automatic cleanup**: Postgres roles persist until explicit termination (agents are cheap)
- **No status in DB**: Can't query "all running agents" from DB (only from server state)
