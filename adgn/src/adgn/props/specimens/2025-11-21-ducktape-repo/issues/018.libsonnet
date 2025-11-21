local I = import '../../specimens/lib.libsonnet';

// iss-018: Remove misleading comments about REQUIRED fields in persist/__init__.py

I.issueOneOccurrence(
  rationale=|||
    The comments claiming "All fields are REQUIRED" in Decision and ToolCallExecution classes
    are misleading and should be deleted. The code itself makes field requirements obvious.

    **Current code with misleading comments:**

    **Lines 90-98 (Decision class):**
    ```python
    class Decision(BaseModel):
        """Decision made about a tool call.

        All fields are REQUIRED. The entire Decision object is optional on ToolCallRecord.
        """

        outcome: ApprovalOutcome
        decided_at: datetime
        reason: str | None = None
    ```

    **Lines 101-109 (ToolCallExecution class):**
    ```python
    class ToolCallExecution(BaseModel):
        """Tool execution result.

        All fields are REQUIRED. The entire ToolCallExecution object is optional on ToolCallRecord.
        """

        completed_at: datetime
        output: mcp_types.CallToolResult
        model_config = ConfigDict(arbitrary_types_allowed=True)
    ```

    **Line 123 (ToolCallRecord.agent_id):**
    ```python
    agent_id: AgentID  # REQUIRED - every tool call must be associated with an agent
    ```

    **Why these comments are problematic:**

    1. **Decision comment is factually incorrect**: Claims "All fields are REQUIRED" but `reason: str | None = None`
       has a default value, making it optional. The comment contradicts the code.

    2. **Comments state the obvious**: In Pydantic, fields WITHOUT defaults are required. The type annotations
       already make this clear:
       - `outcome: ApprovalOutcome` - no default → required
       - `decided_at: datetime` - no default → required
       - `reason: str | None = None` - has default → optional
       - `completed_at: datetime` - no default → required
       - `output: mcp_types.CallToolResult` - no default → required

    3. **Redundant with code**: The field definitions themselves are the source of truth. Comments duplicating
       this information add noise without value.

    4. **agent_id comment is redundant**: The field has no default, so it's obviously required. The comment
       "REQUIRED - every tool call must be associated with an agent" just restates what the type annotation
       already says.

    **Recommended fix:**

    Delete all three misleading/redundant comments:

    ```python
    class Decision(BaseModel):
        """Decision made about a tool call.

        The entire Decision object is optional on ToolCallRecord.
        """

        outcome: ApprovalOutcome
        decided_at: datetime
        reason: str | None = None


    class ToolCallExecution(BaseModel):
        """Tool execution result.

        The entire ToolCallExecution object is optional on ToolCallRecord.
        """

        completed_at: datetime
        output: mcp_types.CallToolResult
        model_config = ConfigDict(arbitrary_types_allowed=True)


    class ToolCallRecord(BaseModel):
        """Complete tool call record from policy gate (tracks ALL calls through gate).

        States:
        - PENDING: decision=None, execution=None
        - EXECUTING: decision!=None, execution=None
        - COMPLETED: decision!=None, execution!=None
        """

        call_id: str
        run_id: str | None
        agent_id: AgentID
        tool_call: ToolCall
        decision: Decision | None = None
        execution: ToolCallExecution | None = None
        model_config = ConfigDict(arbitrary_types_allowed=True)
    ```

    **Benefits:**
    - Removes incorrect comment (Decision.reason IS optional)
    - Eliminates redundant noise (field definitions speak for themselves)
    - Cleaner, more maintainable code
    - Follows principle: comments should explain WHY, not WHAT
    - Reduces risk of comments getting out of sync with code

    **Note:**
    The useful information ("The entire Decision/ToolCallExecution object is optional on ToolCallRecord")
    is preserved. Only the redundant "All fields are REQUIRED" and inline "# REQUIRED" comments are removed.
  |||,
  properties=['documentation', 'code-clarity', 'redundancy'],
  filesToRanges={
    'adgn/src/adgn/agent/persist/__init__.py': [
      [90, 98],    // Decision class with misleading "All fields are REQUIRED" comment
      [93, 93],    // Line with incorrect comment
      [101, 109],  // ToolCallExecution class with misleading "All fields are REQUIRED" comment
      [104, 104],  // Line with redundant comment
      [123, 123],  // agent_id with redundant "# REQUIRED" inline comment
    ],
  },
)
