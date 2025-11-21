local I = import '../../specimens/lib.libsonnet';

// iss-029: approvals_pending_global hand-constructs JSON instead of using Pydantic

I.issueOneOccurrence(
  rationale=|||
    The `approvals_pending_global` function manually constructs JSON dictionaries and uses
    `json.dumps()` instead of using Pydantic models. This loses type safety and validation.

    **Current code (lines 395-424):**
    ```python
    async def approvals_pending_global():
        """Each approval is a separate MCP TextResourceContents block.

        Returns a list of text resource contents where each pending approval
        gets its own block with URI and content.
        """
        result: list[dict[str, Any]] = []
        for agent_id in registry.known_agents():
            infra = registry.get_running_infrastructure(agent_id)
            if infra is None:
                continue

            pending = infra.approval_hub.pending
            if not pending:
                continue

            # Each agent's pending approvals as a single JSON block
            pending_list = [
                {
                    "call_id": call_id,
                    "tool_call": {
                        "name": tc.name,
                        "call_id": tc.call_id,
                        "args_json": tc.args_json,
                    },
                }
                for call_id, tc in pending.items()
            ]

            result.append(
                {
                    "uri": f"resource://agents/{agent_id}/approvals/pending",
                    "mimeType": "application/json",
                    "text": json.dumps({"agent_id": agent_id, "pending": pending_list}),
                }
            )
        return result
    ```

    **Why this is problematic:**

    1. **No type safety**: Manual dict construction with string keys - typos won't be caught:
       ```python
       {"call_idd": x}  # Typo - no error!
       ```

    2. **No validation**: Pydantic would validate field types. With dicts, wrong types slip through:
       ```python
       {"call_id": 123}  # Should be str, no validation
       ```

    3. **Manual JSON serialization**: Uses `json.dumps()` instead of letting Pydantic/framework handle it.

    4. **Hard to evolve**: Adding/removing fields requires manual updates without type checking.

    5. **Inconsistent with codebase**: Other functions use Pydantic models (e.g., `AgentApprovalsPending`,
       `PendingApproval`, etc.). This is an outlier.

    6. **Nested dict construction**: The tool_call dict is manually constructed when `ToolCall` model
       already exists and should be used.

    7. **No IDE support**: Can't autocomplete fields or check types with dict literals.

    **Recommended fix:**

    **Step 1**: Define Pydantic models for the response:
    ```python
    class PendingApprovalItem(BaseModel):
        """Single pending approval in global list."""
        call_id: str
        tool_call: ToolCall


    class AgentPendingApprovalsBlock(BaseModel):
        """Pending approvals for one agent in global list."""
        agent_id: AgentID
        pending: list[PendingApprovalItem]


    class ResourceBlock(BaseModel):
        """Single resource block with URI and content."""
        uri: str
        mimeType: str
        text: str  # JSON-serialized content
    ```

    **Step 2**: Update the function to use Pydantic:
    ```python
    async def approvals_pending_global() -> list[ResourceBlock]:
        """Each approval is a separate MCP TextResourceContents block.

        Returns a list of text resource contents where each pending approval
        gets its own block with URI and content.
        """
        result: list[ResourceBlock] = []
        for agent_id in registry.known_agents():
            infra = registry.get_running_infrastructure(agent_id)
            if infra is None:
                continue

            pending = infra.approval_hub.pending
            if not pending:
                continue

            # Build pending list using Pydantic models
            pending_items = [
                PendingApprovalItem(call_id=call_id, tool_call=tc)
                for call_id, tc in pending.items()
            ]

            # Build agent block
            agent_block = AgentPendingApprovalsBlock(
                agent_id=agent_id,
                pending=pending_items
            )

            # Create resource block
            result.append(
                ResourceBlock(
                    uri=f"resource://agents/{agent_id}/approvals/pending",
                    mimeType="application/json",
                    text=agent_block.model_dump_json(),  # Pydantic serialization
                )
            )
        return result
    ```

    **Alternative (simpler):**
    If the response format allows returning structured data directly:
    ```python
    class GlobalPendingApprovals(BaseModel):
        """All pending approvals across agents."""
        agents: list[AgentPendingApprovalsBlock]


    async def approvals_pending_global() -> GlobalPendingApprovals:
        agents_blocks = []
        for agent_id in registry.known_agents():
            # ... same logic ...
            pending_items = [
                PendingApprovalItem(call_id=call_id, tool_call=tc)
                for call_id, tc in pending.items()
            ]
            agents_blocks.append(
                AgentPendingApprovalsBlock(agent_id=agent_id, pending=pending_items)
            )
        return GlobalPendingApprovals(agents=agents_blocks)
    ```

    **Benefits:**
    - Type-safe field construction
    - Automatic validation
    - IDE autocomplete and type checking
    - Self-documenting schema
    - Consistent with rest of codebase
    - Easier to maintain and evolve
    - Proper use of existing `ToolCall` model
    - Framework handles JSON serialization

    **Note:**
    This issue is related to issue 026 (list_agents should use Pydantic). Both functions manually
    construct JSON and should be refactored together for consistency.
  |||,
  properties=['type-safety', 'maintainability', 'code-consistency'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [395, 424],  // approvals_pending_global with manual dict construction
      [411, 419],  // Manual pending_list dict construction
      [421, 424],  // Manual result dict construction with json.dumps
    ],
  },
)
