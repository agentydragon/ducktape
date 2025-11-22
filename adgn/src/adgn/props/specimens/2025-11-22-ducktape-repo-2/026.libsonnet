local I = import '../../specimens/lib.libsonnet';

I.issueOneOccurrence(
  rationale= |||
    In both the `approve()` and `reject()` tool handlers, decision objects are created, assigned to a variable, and immediately passed to `resolve()`:

    **approve() handler (lines 215-216):**
    ```python
    decision = ContinueDecision(reasoning=reasoning)
    self.resolve(call_id, decision)
    ```

    **reject() handler (lines 227-228):**
    ```python
    decision = DenyContinueDecision(reason=reasoning or "Rejected by user")
    self.resolve(call_id, decision)
    ```

    Both `decision` variables are textbook one-off variables - they're assigned once and used exactly once in the very next line. These intermediate variables add no value:
    - They're not reused
    - They don't improve clarity (the decision constructor is already descriptive)
    - They don't capture complex logic (just a simple constructor call)
    - They add names that readers must track unnecessarily

    **Fix:**
    Inline the decision construction directly into the `resolve()` call:

    **approve() handler:**
    ```python
    self.resolve(call_id, ContinueDecision(reasoning=reasoning))
    await self.notify_approvals_changed()
    return {"status": "approved", "call_id": call_id, "agent_id": self._agent_id}
    ```

    **reject() handler:**
    ```python
    self.resolve(call_id, DenyContinueDecision(reason=reasoning or "Rejected by user"))
    await self.notify_approvals_changed()
    return {"status": "rejected", "call_id": call_id, "agent_id": self._agent_id}
    ```

    This removes unnecessary intermediate variables and makes the code more concise while maintaining complete clarity about what's happening.
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [215, 216],  // approve: decision = ContinueDecision(...); self.resolve(...)
      [227, 228],  // reject: decision = DenyContinueDecision(...); self.resolve(...)
    ],
  },
)
