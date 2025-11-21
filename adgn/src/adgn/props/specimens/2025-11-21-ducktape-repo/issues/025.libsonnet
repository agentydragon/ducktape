local I = import '../../specimens/lib.libsonnet';

// iss-025: ServerStatus and AgentInfoDetailed.status field is misleading for remote agents

I.issueOneOccurrence(
  rationale=|||
    The `ServerStatus` enum and `AgentInfoDetailed.status` field claim to represent "Agent server runtime
    status", but this is misleading. The field represents the status of a *locally running agent*, not a
    server. For *remote agents*, neither "running" nor "stopped" are accurate, making the field semantically
    incorrect.

    **Current code:**

    **ServerStatus enum (lines 197-201):**
    ```python
    class ServerStatus(StrEnum):
        """Agent server runtime status."""

        RUNNING = "running"
        STOPPED = "stopped"
    ```

    **AgentInfoDetailed usage (line 216):**
    ```python
    class AgentInfoDetailed(BaseModel):
        """Basic agent metadata NOT available from other MCP resources.
        For additional data, query the specific MCP resources:
        - Compositor state: resource://agents/{id}/snapshot
        - Policy: resource://approval-policy/policy.py (per-agent server)
        - Approvals: resource://agents/{id}/approvals/pending, resource://agents/{id}/approvals/history
        """

        agent_id: AgentID
        mode: AgentMode
        model: str | None = None  # Model name for local agents
        status: ServerStatus
    ```

    **Why this is problematic:**

    1. **Misleading name**: `ServerStatus` suggests it tracks server status, but it actually tracks
       whether a *local agent* is running or stopped. This is an agent status, not a server status.

    2. **Doesn't apply to remote agents**: For `mode == AgentMode.REMOTE`, the agent runs elsewhere.
       Neither "running" nor "stopped" accurately describes the state from this system's perspective.
       - We don't know if the remote agent is actually running
       - "stopped" would imply we stopped it, but we have no control over remote agents
       - The field is semantically meaningless for remote agents

    3. **Confuses local infrastructure with agent state**: The status tracks local infrastructure
       availability, not the actual agent's operational state. An agent might be "stopped" locally
       but still exist in the database.

    4. **Unclear semantics**: What does "running" mean? Is it:
       - The agent process is running?
       - The agent has active infrastructure?
       - The agent is currently processing a request?
       - The agent is registered and available?

    5. **Comment confirms confusion**: Line 215 says "Model name for local agents" - the model
       recognizes mode-specific fields exist, but `status` doesn't have a similar caveat.

    **Current resolution unclear:**

    The user notes: "unclear currently what exactly is the correct resolution, but either way there's
    no way this field isn't misleading."

    **Possible approaches:**

    **Option 1: Replace with live/available flag**
    ```python
    class AgentInfoDetailed(BaseModel):
        agent_id: AgentID
        mode: AgentMode
        model: str | None = None  # Model name for local agents
        infrastructure_available: bool  # True if local infrastructure is loaded
    ```

    **Option 2: Mode-specific status fields**
    ```python
    class AgentInfoDetailed(BaseModel):
        agent_id: AgentID
        mode: AgentMode
        model: str | None = None
        local_infrastructure_running: bool | None = None  # Only for LOCAL mode
        # No status for REMOTE mode
    ```

    **Option 3: Remove status entirely**
    If the status can be inferred from other fields or queries, remove it:
    ```python
    class AgentInfoDetailed(BaseModel):
        agent_id: AgentID
        mode: AgentMode
        model: str | None = None  # Model name for local agents
        # Status removed - query infrastructure or runs to determine state
    ```

    **Benefits of fixing:**
    - Clearer semantics (no confusion about what "status" means)
    - Correct modeling for remote agents
    - Better separation between local infrastructure and agent state
    - More honest API (doesn't claim to know things we don't know)

    **Note:**
    The exact fix depends on what information is actually needed by consumers. The current field
    is definitely misleading and needs to be redesigned.
  |||,
  properties=['api-design', 'semantic-correctness', 'clarity'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [197, 201],  // ServerStatus enum with misleading name
      [216, 216],  // AgentInfoDetailed.status field
      [198, 198],  // Misleading docstring "Agent server runtime status"
    ],
  },
)
