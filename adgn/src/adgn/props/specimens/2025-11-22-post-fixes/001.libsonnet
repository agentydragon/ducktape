local I = import '../../specimens/lib.libsonnet';

// iss-001: approve/reject tools silently ignore already-resolved futures

I.issueOneOccurrence(
  rationale=|||
    The approve() and reject() tools in ApprovalHub check if a future is already done,
    but silently ignore this case instead of raising an error:

    ```python
    @self.tool()
    async def approve(call_id: str, reasoning: str | None = None) -> dict:
        """Approve a pending tool call."""
        # Inline resolve logic
        pending = self._pending.pop(call_id, None)
        if pending is not None and not pending.future.done():
            pending.future.set_result(ContinueDecision(reasoning=reasoning))
        await self.notify_approvals_changed()
        return {"status": "approved", "call_id": call_id, "agent_id": self._agent_id}
    ```

    **Problem:**

    If the future is already done (`.done()` returns True), the code silently:
    - Doesn't set the result
    - Doesn't notify about the problem
    - Returns success status {"status": "approved"}
    - Could mask race conditions or double-approval bugs

    This can happen if:
    1. User clicks "Approve" twice in quick succession
    2. Two UI clients try to approve the same call_id
    3. Policy auto-approved while user was clicking
    4. Bug in the approval flow logic

    **Why this is bad:**

    1. **Hides bugs**: Race conditions and double-processing bugs are silently ignored
    2. **Misleading response**: Returns success when nothing actually happened
    3. **No visibility**: No log, no error, no way to detect the problem occurred
    4. **Data integrity**: The fact that the future was already resolved might indicate
       a serious bug that should be investigated, not hidden

    **Fix:**

    Raise an error if the future is already done:

    ```python
    @self.tool()
    async def approve(call_id: str, reasoning: str | None = None) -> dict:
        """Approve a pending tool call."""
        pending = self._pending.pop(call_id, None)
        if pending is None:
            raise ValueError(f"Approval {call_id} not found in pending approvals")

        if pending.future.done():
            raise ValueError(
                f"Approval {call_id} already resolved - "
                f"possible race condition or double-approval"
            )

        pending.future.set_result(ContinueDecision(reasoning=reasoning))
        await self.notify_approvals_changed()
        return {"status": "approved", "call_id": call_id, "agent_id": self._agent_id}
    ```

    Same fix needed for reject() tool.

    **Alternative (softer):**

    If legitimate retries are expected, at minimum log a warning:

    ```python
    if pending.future.done():
        logger.warning(
            f"Approval {call_id} already resolved, ignoring duplicate approval attempt",
            extra={"call_id": call_id, "agent_id": self._agent_id}
        )
        return {
            "status": "already_resolved",
            "call_id": call_id,
            "agent_id": self._agent_id
        }
    ```

    Benefits of raising:
    - Fail-fast behavior catches bugs early
    - Clear signal that something unexpected happened
    - Prevents silent corruption of approval state
    - Forces callers to handle the race condition properly
  |||,
  properties=['python/no-swallowing-errors', 'early-bailout'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [185, 189],  // approve() tool - silent ignore if future.done()
      [199, 203],  // reject() tool - silent ignore if future.done()
    ],
  },
)
