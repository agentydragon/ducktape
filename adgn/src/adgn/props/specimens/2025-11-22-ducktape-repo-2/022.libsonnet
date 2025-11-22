local I = import '../../specimens/lib.libsonnet';

I.issueOneOccurrence(
  rationale= |||
    Both `resolve()` and `await_decision()` silently handle missing call_ids instead of failing fast.

    **Problem 1: resolve() swallows missing call_id (lines 142-148)**
    ```python
    def resolve(self, call_id: str, decision: ...) -> None:
        pending = self._pending.pop(call_id, None)  # Returns None if missing
        if pending is not None and not pending.future.done():
            pending.future.set_result(decision)
        # Schedule notification asynchronously if MCP is enabled
        if self._has_mcp:
            asyncio.create_task(self.notify_approvals_changed())  # WRONG!
    ```

    When `call_id` doesn't exist in `_pending`:
    1. The method silently does nothing (swallows the error)
    2. **Still sends a notification** that approvals changed, even though nothing actually changed

    This is doubly wrong - it both hides the error AND incorrectly notifies listeners of a non-existent change.

    **Problem 2: await_decision() creates entry for missing call_id (lines 131-137)**
    ```python
    async def await_decision(self, call_id: str, tool_call: ToolCall) -> ...:
        async with self._lock:
            pending = self._pending.get(call_id)  # Returns None if missing
            if pending is None:
                fut = asyncio.get_running_loop().create_future()
                self._pending[call_id] = PendingApproval(...)
            else:
                fut = pending.future
    ```

    This automatically creates a new pending approval if one doesn't exist. While this might be correct for first-time calls, it's unclear if this is intentional or if it should raise an error for truly missing entries.

    **Fix:**
    For `resolve()`, use direct dict access to raise KeyError on missing call_id:
    ```python
    def resolve(self, call_id: str, decision: ...) -> None:
        pending = self._pending[call_id]  # Raises KeyError if missing
        if not pending.future.done():
            pending.future.set_result(decision)
        del self._pending[call_id]
        # Only notify after successful resolution
        if self._has_mcp:
            asyncio.create_task(self.notify_approvals_changed())
    ```

    This ensures errors are surfaced immediately rather than silently swallowed, and notifications only occur when state actually changes.
  |||,
  properties=['python/no-swallowing-errors'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [142, 148],  // resolve() with pop(call_id, None)
      [131, 137],  // await_decision() with get() fallback
    ],
  },
)
