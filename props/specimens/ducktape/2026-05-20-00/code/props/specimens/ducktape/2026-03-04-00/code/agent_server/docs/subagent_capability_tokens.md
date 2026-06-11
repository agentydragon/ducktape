# Design: Resource Capability Tokens for Agent Collaboration

> **Status (2025-12):** The `agent_runs` table with `type_config` JSONB is implemented.
> This document describes future capability token extensions for inter-agent access delegation.

## Problem

Agents need to spawn sub-agents and share resources (transcripts, MCP servers) without breaking isolation:

1. **Transcript access**: Agents should only see outputs of agents they spawned (or were delegated access to)
2. **MCP server delegation**: Pass server handles between agents
3. **Hierarchical spawning**: Sub-agents spawning their own helpers
4. **Future extensibility**: K8s scopes, database connections, external service tokens

## Current Infrastructure

**`agent_runs` table** (implemented):

- `agent_run_id`: Primary key (UUID)
- `agent_definition_id`: References agent definition
- `parent_agent_run_id`: Parent for hierarchy tracking
- `type_config`: JSONB with type-specific config (CriticTypeConfig, GraderTypeConfig, etc.)
- `status`: Run status (in_progress, completed, etc.)

**RLS** (implemented):

- `current_agent_run_id()`: Extracts UUID from session username
- `current_agent_type()`: Returns agent type from type_config
- Policies filter by agent_run_id and type

## Proposed Extension: Agent Grants

Add capability delegation via grants table:

```sql
CREATE TABLE agent_grants (
    grantor_agent_id UUID NOT NULL REFERENCES agent_runs(agent_run_id),
    grantee_agent_id UUID NOT NULL REFERENCES agent_runs(agent_run_id),
    target_agent_id UUID NOT NULL REFERENCES agent_runs(agent_run_id),
    capability TEXT NOT NULL,  -- 'read_transcript', 'send_messages', 'administer_grants'
    granted_at TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (grantor_agent_id, grantee_agent_id, target_agent_id, capability)
);
```

**Automatic grants on spawn:**
When agent A spawns agent B:

- `(grantor=A, grantee=A, target=B, capability='read_transcript')`
- `(grantor=A, grantee=A, target=B, capability='send_messages')`
- `(grantor=A, grantee=A, target=B, capability='administer_grants')`

**Delegation example:**

- PO spawns critic (PO gets all capabilities on critic)
- PO spawns grader (PO gets all capabilities on grader)
- PO grants grader read access to critic: `grant_access(target=critic, grantee=grader, capability='read_transcript')`

## Proposed Extension: Typed Messages

Agent-to-agent communication via immutable messages:

```sql
CREATE TABLE messages (
    id UUID PRIMARY KEY,
    schema_type TEXT NOT NULL,  -- 'plaintext', 'structured_critique', 'structured_grade'
    content JSONB NOT NULL,
    created_by_agent_id UUID REFERENCES agent_runs(agent_run_id),
    in_reply_to UUID REFERENCES messages(id),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
```

**Benefits:**

- Zero-copy: Pass message UUIDs instead of copying large JSON
- Type safety: Schema validation at write time
- Immutable: Messages can't be modified after creation
- Async delivery: Fire-and-forget with explicit reply linkage

## MCP Tools (Future)

### Phase 1a: Blocking (MVP)

```python
class RunSubagentInput(BaseModel):
    prompt: str
    model: str = "claude-sonnet-4.5"
    mcp_config: dict = {}
    timeout_secs: int = 300

class RunSubagentOutput(BaseModel):
    status: str  # complete|timeout|error
    final_result: dict | None
    agent_run_id: UUID
    messages: list[dict]
    error: str | None

@mcp.tool(flat=True)
async def run_subagent(input: RunSubagentInput) -> RunSubagentOutput:
    """Spawn subagent, wait for completion, return result."""
    ...
```

### Phase 1b: Async (Token-based)

```python
class SpawnSubagentInput(BaseModel):
    prompt: str
    model: str = "claude-sonnet-4.5"
    mcp_config: dict = {}
    timeout_secs: int = 300

class SpawnSubagentOutput(BaseModel):
    token: str  # Capability token (UUID)
    agent_run_id: UUID
    status: str  # starting|ready

@mcp.tool(flat=True)
async def spawn_subagent(input: SpawnSubagentInput) -> SpawnSubagentOutput:
    """Spawn subagent and return immediately (non-blocking)."""
    ...

@mcp.tool(flat=True)
async def await_completion(input: AwaitCompletionInput) -> AwaitCompletionOutput:
    """Block until subagent completes."""
    ...

@mcp.tool(flat=True)
async def fork_and_continue(input: ForkAndContinueInput) -> ForkAndContinueOutput:
    """Fork parent agent N times with different continuations.

    Each fork inherits parent's transcript + MCP servers.
    70% token savings vs spawning N fresh agents.
    """
    ...
```

## RLS Policy Extensions

**Events table** (extends current policy):

```sql
CREATE POLICY event_access ON events FOR SELECT USING (
    agent_run_id = current_agent_run_id()
    OR agent_run_id IN (
        SELECT target_agent_id FROM agent_grants
        WHERE grantee_agent_id = current_agent_run_id()
          AND capability = 'read_transcript'
    )
);
```

**Agent grants introspection:**

```sql
CREATE POLICY grant_access ON agent_grants FOR SELECT USING (
    grantee_agent_id = current_agent_run_id()
    OR target_agent_id IN (
        SELECT target_agent_id FROM agent_grants
        WHERE grantee_agent_id = current_agent_run_id()
          AND capability = 'administer_grants'
    )
);
```

## Security Properties

- **Tokens are UUIDs**: Unguessable (~122 bits entropy)
- **No ambient authority**: Agents can't enumerate others without tokens
- **Cascading revocation**: Parent disconnect cleans up children
- **No privilege escalation**: Tokens grant fixed capabilities
- **Server-side validation**: All access checks happen server-side

## Implementation Priority

1. **Foundation**: Agent grants table + RLS policies
2. **Blocking MVP**: `run_subagent` tool
3. **Async primitives**: `spawn_subagent` + `await_completion`
4. **Fork optimization**: `fork_and_continue` for token savings
5. **MCP delegation**: Share server handles via tokens

## Current Use Cases

### Prompt Optimizer → Critic → Grader

Currently handled by `CriticAgentEnvironment` and `GraderAgentEnvironment` with:

- Temporary database users (`agent_{run_id}`)
- RLS policies filtering by `current_agent_run_id()`
- HTTP MCP server for submit tools

Subagent tools would simplify this to:

```python
critic_result = await run_subagent(prompt="Review this code...", ...)
grader_result = await run_subagent(prompt=f"Grade critique {critic_result.agent_run_id}...", ...)
```

### Parallel Evaluation

Fork pattern for efficient parallelization:

```python
# Instead of spawning N critics with repeated context
fork_result = await fork_and_continue(
    continuations=["Test specimen A", "Test specimen B", "Test specimen C"]
)
results = await await_many(tokens=fork_result.tokens)
```

Token savings: 50K → 5.5K input tokens (~90% reduction).
