local I = import '../../specimens/lib.libsonnet';

// iss-021: Delete PendingApproval wrapper and _convert_pending_approvals, return ToolCall list directly

I.issueOneOccurrence(
  rationale=|||
    The `PendingApproval` wrapper class and `_convert_pending_approvals` function add unnecessary
    indirection and a misleading timestamp. The function should be deleted and callers should return
    `list[ToolCall]` directly.

    **Current code:**

    **PendingApproval wrapper (lines 120-124):**
    ```python
    class PendingApproval(BaseModel):
        """A tool call awaiting approval."""

        tool_call: ToolCall
        timestamp: datetime
    ```

    **_convert_pending_approvals function (lines 50-59):**
    ```python
    def _convert_pending_approvals(pending_map: dict[str, ToolCall]) -> list[PendingApproval]:
        result: list[PendingApproval] = []
        for _call_id, tool_call in pending_map.items():
            result.append(
                PendingApproval(
                    tool_call=tool_call,
                    timestamp=datetime.now(),  # TODO: Track creation time in PendingApproval or separately
                )
            )
        return result
    ```

    **Usage sites (3 call sites):**
    - Line 386: `pending = _convert_pending_approvals(infra.approval_hub.pending)`
    - Line 404: `pending_approvals = _convert_pending_approvals(infra.approval_hub.pending)`
    - Line 444: `pending_approvals = _convert_pending_approvals(infra.approval_hub.pending)`

    **Why this is problematic:**

    1. **Misleading timestamp**: Line 56 populates `timestamp=datetime.now()` at query time, not at
       creation time. This timestamp represents "when was this list generated", not "when was the
       approval request created". The TODO comment acknowledges this is wrong.

    2. **Unnecessary wrapper**: `PendingApproval` is just a single-field wrapper around `ToolCall`
       with a misleading timestamp. It adds no value.

    3. **Trivial conversion**: The function just wraps each `ToolCall` in `PendingApproval`. After
       removing the timestamp, it becomes an identity function.

    4. **Inefficient**: Creates unnecessary intermediate objects and loops when callers could use
       the dict values directly.

    5. **Violates YAGNI**: The timestamp isn't used for anything useful and causes confusion.

    **Recommended fix:**

    **Step 1**: Delete `PendingApproval` class (lines 120-124)

    **Step 2**: Delete `_convert_pending_approvals` function (lines 50-59)

    **Step 3**: Update call sites to use `list(pending_map.values())` directly:

    **Line 386:**
    ```python
    # Before:
    pending = _convert_pending_approvals(infra.approval_hub.pending)

    # After:
    pending = list(infra.approval_hub.pending.values())
    ```

    **Line 404:**
    ```python
    # Before:
    pending_approvals = _convert_pending_approvals(infra.approval_hub.pending)

    # After:
    pending_approvals = list(infra.approval_hub.pending.values())
    ```

    **Line 444:**
    ```python
    # Before:
    pending_approvals = _convert_pending_approvals(infra.approval_hub.pending)

    # After:
    pending_approvals = list(infra.approval_hub.pending.values())
    ```

    **Step 4**: Update return types from `list[PendingApproval]` to `list[ToolCall]` at:
    - Any resource/tool handlers that return these lists
    - The `PendingApproval` import should be removed

    **Benefits:**
    - Eliminates misleading timestamp (fixes TODO on line 56)
    - Removes unnecessary wrapper class
    - Removes unnecessary conversion function
    - Simpler, more direct code
    - Callers work directly with `ToolCall` objects
    - No intermediate object allocations

    **Note:**
    If timestamp tracking is truly needed in the future, it should be tracked in the `ApprovalHub`
    at creation time (when the approval request is registered), not at query time. But currently
    the timestamp serves no purpose and should just be removed.
  |||,
  properties=['unnecessary-abstraction', 'misleading-data', 'simplicity'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [120, 124],  // PendingApproval wrapper class
      [50, 59],    // _convert_pending_approvals function
      [56, 56],    // Misleading timestamp=datetime.now() with TODO
      [386, 386],  // Call site 1
      [404, 404],  // Call site 2
      [444, 444],  // Call site 3
    ],
  },
)
