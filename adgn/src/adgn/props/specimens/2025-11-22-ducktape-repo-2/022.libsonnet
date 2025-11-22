{
  title: 'ApprovalHub.resolve and await_decision silently swallow missing call_id errors',
  severity: 'medium',
  category: 'error-handling',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [142, 148],
      context: 'resolve() uses pop() which returns None for missing keys',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [131, 137],
      context: 'await_decision() uses get() which returns None for missing keys',
    },
  ],
  description: |||
    Both `resolve()` and `await_decision()` silently handle missing call_ids incorrectly:

    **1. resolve() swallows missing call_id (lines 142-148):**
    ```python
    def resolve(self, call_id: str, decision: ...) -> None:
        pending = self._pending.pop(call_id, None)  # Returns None if missing
        if pending is not None and not pending.future.done():
            pending.future.set_result(decision)
        # Schedule notification asynchronously if MCP is enabled
        if self._has_mcp:
            asyncio.create_task(self.notify_approvals_changed())  # WRONG!
    ```

    **Problem:** If `call_id` doesn't exist, `pending` is None, but the code:
    1. Does nothing (silently fails)
    2. **Still sends a notification** that approvals changed (even though nothing changed!)

    This is doubly wrong - it swallows the error AND incorrectly notifies.

    **2. await_decision() has similar issue (lines 131-137):**
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

    While this creates a new entry if missing (which might be correct), it's
    unclear if this is the intended behavior or should raise an error.
  |||,
  recommendation: |||
    **For resolve() - be explicit about errors:**

    ```python
    def resolve(self, call_id: str, decision: ...) -> None:
        # Option 1: Raise KeyError if call_id doesn't exist
        pending = self._pending[call_id]  # Raises KeyError if missing
        if not pending.future.done():
            pending.future.set_result(decision)
        del self._pending[call_id]

        # Schedule notification only after successful resolution
        if self._has_mcp:
            asyncio.create_task(self.notify_approvals_changed())

    # Option 2: Friendly error message
    def resolve(self, call_id: str, decision: ...) -> None:
        if call_id not in self._pending:
            raise KeyError(f"No pending approval found for call_id={call_id}")
        pending = self._pending.pop(call_id)
        if not pending.future.done():
            pending.future.set_result(decision)
        if self._has_mcp:
            asyncio.create_task(self.notify_approvals_changed())
    ```

    **Key fixes:**
    1. Don't use `.pop(call_id, None)` - use direct dict access to raise KeyError
    2. Only notify if something actually changed
    3. Optionally provide a helpful error message

    For await_decision(), clarify whether creating a new pending approval
    is correct behavior or should also raise an error.
  |||,
}
