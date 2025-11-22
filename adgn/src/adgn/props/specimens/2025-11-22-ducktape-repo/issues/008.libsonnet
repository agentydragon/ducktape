local I = import '../../specimens/lib.libsonnet';

// iss-008: await_decision should use walrus operator for pending lookup

I.issueOneOccurrence(
  rationale=|||
    The `await_decision` method uses a two-line pattern to get a value and check if
    it's None, when the walrus operator provides a cleaner one-line solution.

    **Current code (lines 95-96):**
    ```python
    pending = self._pending.get(call_id)
    if pending is None:
    ```

    **Problems:**
    1. **Verbose**: Two lines for what should be one conditional
    2. **Scope pollution**: `pending` variable exists outside the if/else blocks
    3. **Less Pythonic**: Walrus operator was designed for exactly this pattern
    4. **Inconsistent**: Other code may use walrus, creating style inconsistency

    **Correct approach with walrus operator:**
    ```python
    if (pending := self._pending.get(call_id)) is None:
    ```

    **Full context (lines 91-103):**
    ```python
    async def await_decision(
        self, call_id: str, tool_call: ToolCall
    ) -> ContinueDecision | DenyContinueDecision | AbortTurnDecision:
        async with self._lock:
            if (pending := self._pending.get(call_id)) is None:
                fut = asyncio.get_running_loop().create_future()
                self._pending[call_id] = PendingApproval(tool_call=tool_call, future=fut)
            else:
                fut = pending.future
        if self._notifier:
            self._notifier()
        return await fut
    ```

    **Benefits:**
    1. **More concise**: One line instead of two
    2. **Clearer scope**: `pending` only exists in the else branch where it's used
    3. **Pythonic**: Idiomatic use of walrus operator (PEP 572)
    4. **Consistent**: Matches modern Python style guidelines
    5. **Safer**: Can't accidentally use stale `pending` value later

    **When NOT to use walrus:**
    - Value needed in multiple branches (not the case here - only used in else)
    - Complex expressions that hurt readability (not the case - simple .get())
    - Nested walruses creating confusion (not the case - single usage)

    This is a textbook case for the walrus operator: get a value, immediately check
    if it's None, and use it in one branch if not None.

    **Note on similar pattern:**
    Line 106 has a similar pattern but is correct:
    ```python
    pending = self._pending.pop(call_id, None)
    if pending is not None and not pending.future.done():
    ```
    This uses `pending` in the condition itself (not just checking None), so the
    two-line form is appropriate. The walrus would be:
    ```python
    if (pending := self._pending.pop(call_id, None)) is not None and not pending.future.done():
    ```
    Which is fine but the current form is also acceptable since `pending` appears
    in the condition. Line 95-96 is different because `pending` is NOT used in the
    condition, only in the else branch.
  |||,
  properties=['code-style', 'python-idioms', 'conciseness', 'pep572'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [95, 96],   // pending = get(); if None pattern that should use walrus
    ],
  },
)
