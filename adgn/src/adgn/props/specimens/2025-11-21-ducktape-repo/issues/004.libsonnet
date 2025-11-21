local I = import '../../specimens/lib.libsonnet';

// iss-004: Remove ApprovalKind type alias, use UserApprovalDecision directly

I.issueOneOccurrence(
  rationale=|||
    The `ApprovalKind` type alias (line 41) is unnecessary and should be removed.
    All usages should be replaced with `UserApprovalDecision` directly.

    **Current code (line 41):**
    ```python
    ApprovalKind = UserApprovalDecision
    ```

    **Usages to replace:**
    - state.py:73 - `decision: ApprovalKind | None`
    - state.py:130 - `decision: ApprovalKind | None`
    - servers/agents.py:30 - `from adgn.agent.server.state import ApprovalKind`
    - servers/agents.py:609 - `decision: ApprovalKind`

    **Why remove the alias:**
    - **No semantic value**: `ApprovalKind` doesn't convey anything different from
      `UserApprovalDecision`
    - **Indirection**: Adds an extra layer readers must look up
    - **Inconsistent naming**: The actual type is `UserApprovalDecision`, using a different
      name is confusing
    - **Not a true abstraction**: Just a 1:1 alias with no additional behavior
    - **Import clutter**: Need to import both or remember which to use where

    **Good type aliases vs bad type aliases:**

    **Good alias** (adds value):
    ```python
    AgentID = NewType("AgentID", str)  # Adds type safety, semantic meaning
    ```

    **Bad alias** (no value):
    ```python
    ApprovalKind = UserApprovalDecision  # Just another name for the same thing
    ```

    **After replacement:**
    - state.py:41 - Delete the alias line entirely
    - state.py:73 - `decision: UserApprovalDecision | None`
    - state.py:130 - `decision: UserApprovalDecision | None`
    - servers/agents.py:30 - `from adgn.agent.server.state import UserApprovalDecision`
    - servers/agents.py:609 - `decision: UserApprovalDecision`

    **Note on imports:**
    May need to add `UserApprovalDecision` to imports in files that currently only import
    `ApprovalKind`. But this is better than having two names for the same thing.

    **Benefits:**
    - One canonical name for the type
    - Clearer code - you see the actual type name
    - Less cognitive overhead - no "what's the difference?" questions
    - Easier to search and refactor
  |||,
  properties=['type-safety', 'clarity', 'simplicity', 'indirection'],
  filesToRanges={
    'adgn/src/adgn/agent/server/state.py': [
      [41, 41],   // ApprovalKind type alias definition - delete
      [73, 73],   // ToolItem.decision field - replace with UserApprovalDecision
      [130, 130], // update_tool_decision parameter - replace with UserApprovalDecision
    ],
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [30, 30],   // Import statement - replace ApprovalKind with UserApprovalDecision
      [609, 609], // Parameter type - replace ApprovalKind with UserApprovalDecision
    ],
  },
)
