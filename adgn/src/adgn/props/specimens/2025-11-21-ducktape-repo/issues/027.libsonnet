local I = import '../../specimens/lib.libsonnet';

// iss-027: run_phase should be an enum instead of string literals

I.issueOneOccurrence(
  rationale=|||
    The `run_phase` field in `list_agents` uses string literals ("idle", "waiting_approval", "sampling")
    instead of a proper enum. This loses type safety and makes it easy to introduce typos or inconsistencies.

    **Current code (lines 284-294):**
    ```python
    run_phase = "idle"

    if infra:
        # Get pending approvals count
        pending_approvals = len(infra.approval_hub.pending)

        # Derive run phase based on active state
        if pending_approvals > 0:
            run_phase = "waiting_approval"
        elif live:
            run_phase = "sampling"
    ```

    **Why this is problematic:**

    1. **No type safety**: String literals can have typos that won't be caught:
       ```python
       run_phase = "wating_approval"  # Typo in "waiting" - no error!
       ```

    2. **No exhaustiveness checking**: Can't verify all possible phases are handled.

    3. **No IDE support**: IDEs can't autocomplete valid phase values.

    4. **Magic strings**: The valid values are scattered throughout code rather than defined in one place.

    5. **Hard to discover**: Must search codebase to find all possible phase values.

    6. **Inconsistent with codebase**: Other status fields use enums (e.g., `AgentMode`, `ServerStatus`,
       `ProposalStatus`, `ApprovalOutcome`, `EventType`, `PolicyStatus`).

    **Recommended fix:**

    **Step 1**: Define an enum for run phase:
    ```python
    class RunPhase(StrEnum):
        """Agent run phase status."""
        IDLE = "idle"
        WAITING_APPROVAL = "waiting_approval"
        SAMPLING = "sampling"
    ```

    **Step 2**: Use the enum in the code:
    ```python
    run_phase = RunPhase.IDLE

    if infra:
        # Get pending approvals count
        pending_approvals = len(infra.approval_hub.pending)

        # Derive run phase based on active state
        if pending_approvals > 0:
            run_phase = RunPhase.WAITING_APPROVAL
        elif live:
            run_phase = RunPhase.SAMPLING
    ```

    **Step 3**: Update the Pydantic model (from issue 026):
    ```python
    class AgentListItem(BaseModel):
        """Single agent in the agents list."""
        id: AgentID
        mode: AgentMode
        live: bool
        active_run_id: str | None
        run_phase: RunPhase  # Typed as enum, not str
        pending_approvals: int
        capabilities: dict[str, bool]
    ```

    **Benefits:**
    - Type-safe phase values (typos caught at development time)
    - IDE autocomplete for valid values
    - Single source of truth for valid phases
    - Self-documenting (enum shows all possible values)
    - Consistent with other status enums in codebase
    - Easier to refactor (can find all uses of a phase)
    - Can validate at Pydantic model boundary

    **Note:**
    This issue should be fixed together with issue 026 (converting list_agents to use Pydantic models).
  |||,
  properties=['type-safety', 'code-consistency', 'enums'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [284, 294],  // run_phase assignment with string literals
      [284, 284],  // run_phase = "idle"
      [292, 292],  // run_phase = "waiting_approval"
      [294, 294],  // run_phase = "sampling"
    ],
  },
)
