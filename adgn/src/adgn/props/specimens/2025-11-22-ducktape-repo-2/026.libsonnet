{
  title: 'Decision objects should be inlined in approve/reject tool handlers',
  severity: 'minor',
  category: 'code-style',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [215, 216],
      context: 'decision = ContinueDecision(...); self.resolve(call_id, decision)',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [227, 228],
      context: 'decision = DenyContinueDecision(...); self.resolve(call_id, decision)',
    },
  ],
  description: |||
    In both the `approve()` and `reject()` tool handlers, decision objects
    are created, assigned to a variable, and immediately passed to resolve():

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

    Both `decision` variables are used exactly once - they should be inlined.
  |||,
  recommendation: |||
    Inline the decision construction directly in the resolve() call:

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

    This removes unnecessary intermediate variables and makes the code more concise.
  |||,
}
