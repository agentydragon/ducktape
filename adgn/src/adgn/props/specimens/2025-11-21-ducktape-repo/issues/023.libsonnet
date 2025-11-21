local I = import '../../specimens/lib.libsonnet';

// iss-023: AgentApprovalsHistory count field is redundant and should be removed

I.issueOneOccurrence(
  rationale=|||
    The `count` field in `AgentApprovalsHistory` is redundant - it's a trivial function of the
    `timeline` and `pending` lists already exposed in the response. It should be removed.

    **Current code:**

    **Model definition (lines 162-168):**
    ```python
    class AgentApprovalsHistory(BaseModel):
        """Content for resource://agents/{id}/approvals/history."""

        agent_id: AgentID
        timeline: list[ApprovalHistoryEntry]
        pending: list[PendingApproval]  # Pending approvals not yet decided
        count: int  # Total count (timeline + pending)
    ```

    **Construction (lines 446-450):**
    ```python
    # Count includes both completed and pending
    total_count = len(completed_entries) + len(pending_approvals)

    return AgentApprovalsHistory(
        agent_id=agent_id, timeline=completed_entries, pending=pending_approvals, count=total_count
    )
    ```

    **Why this is problematic:**

    1. **Trivially computable**: `count = len(timeline) + len(pending)` - any client can compute this
       in a single line. There's no value in precomputing it.

    2. **Redundant information**: The response already contains both lists. Sending the count separately
       is redundant and wastes bandwidth.

    3. **Inconsistency risk**: If the code constructing `count` has a bug or if the lists are modified,
       the count could become stale or incorrect. Computable fields should be computed, not stored.

    4. **Violates single source of truth**: The actual data is in the lists; the count is derived.
       Storing both creates two sources of truth for the same information.

    5. **Makes tests more brittle**: Tests must verify count matches the list lengths, adding extra
       assertions for something that should be automatic.

    **Recommended fix:**

    **Step 1**: Remove `count` field from model (line 168):
    ```python
    class AgentApprovalsHistory(BaseModel):
        """Content for resource://agents/{id}/approvals/history."""

        agent_id: AgentID
        timeline: list[ApprovalHistoryEntry]
        pending: list[PendingApproval]  # Pending approvals not yet decided
    ```

    **Step 2**: Remove count computation and parameter (lines 446-450):
    ```python
    # Before:
    total_count = len(completed_entries) + len(pending_approvals)
    return AgentApprovalsHistory(
        agent_id=agent_id, timeline=completed_entries, pending=pending_approvals, count=total_count
    )

    # After:
    return AgentApprovalsHistory(
        agent_id=agent_id, timeline=completed_entries, pending=pending_approvals
    )
    ```

    **Client-side change** (if clients need count):
    ```python
    # Before: use response.count
    # After: compute it
    count = len(response.timeline) + len(response.pending)
    ```

    **Benefits:**
    - Eliminates redundant data from response
    - Smaller payloads
    - No risk of count being out of sync with lists
    - Simpler model and construction code
    - Clients compute what they need (encourages lazy evaluation)
    - One less field to maintain and test

    **Note:**
    If performance is a concern (it shouldn't be for lists of this size), the client can cache the
    computed count rather than having the server send it every time.
  |||,
  properties=['redundancy', 'data-modeling', 'dry-principle'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [168, 168],  // Redundant count field in model
      [446, 450],  // Count computation and construction
      [447, 447],  // Explicit count calculation line
    ],
  },
)
