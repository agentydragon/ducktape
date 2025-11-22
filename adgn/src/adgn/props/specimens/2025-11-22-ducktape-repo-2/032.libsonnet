{
  title: 'ApprovalHub and ApprovalPolicyEngine methods should be inlined into MCP tools/resources',
  severity: 'minor',
  category: 'code-organization',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [142, 148],
      context: 'ApprovalHub.resolve() only called by approve/reject tools',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [316, 322],
      context: 'ApprovalPolicyEngine.set_policy() only called by set_policy tool',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [337, 349],
      context: 'ApprovalPolicyEngine.create_proposal() only called by create_proposal tool',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [351, 366],
      context: 'ApprovalPolicyEngine.approve_proposal() only called by approve_proposal tool',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [367, 370],
      context: 'ApprovalPolicyEngine.reject_proposal() only called by reject_proposal tool',
    },
  ],
  description: |||
    Several methods in ApprovalHub and ApprovalPolicyEngine are called ONLY by their
    corresponding MCP tools/resources, and nowhere else in production code.

    **ApprovalHub.resolve() - only called by approve/reject tools:**
    ```python
    def resolve(self, call_id: str, decision: ...) -> None:
        # ... logic
    ```
    Called at:
    - Line 216: approve tool calls `self.resolve(call_id, decision)`
    - Line 228: reject tool calls `self.resolve(call_id, decision)`
    Not called anywhere else in production code.

    **ApprovalPolicyEngine.set_policy() - only called by set_policy tool:**
    ```python
    async def set_policy(self, source: str) -> int:
        # ... logic
    ```
    Called at:
    - Line 419: set_policy tool calls `await self.set_policy(source)`
    - Line 363: approve_proposal calls it (but approve_proposal is also only called by its tool)

    **ApprovalPolicyEngine.create_proposal() - only called by create_proposal tool:**
    ```python
    async def create_proposal(self, content: str) -> int:
        # ... logic
    ```
    Called at:
    - Line 430: create_proposal tool calls `await self.create_proposal(content)`
    Not called anywhere else in production code.

    **ApprovalPolicyEngine.approve_proposal() - only called by approve_proposal tool:**
    ```python
    async def approve_proposal(self, proposal_id: int) -> None:
        # ... logic
    ```
    Called at:
    - Line 441: approve_proposal tool calls `await self.approve_proposal(int(proposal_id))`
    Not called anywhere else in production code.

    **ApprovalPolicyEngine.reject_proposal() - only called by reject_proposal tool:**
    ```python
    async def reject_proposal(self, proposal_id: int) -> None:
        # ... logic
    ```
    Called at:
    - Line 453: reject_proposal tool calls `await self.reject_proposal(int(proposal_id))`
    Not called anywhere else in production code.

    These are unnecessary abstractions - the methods exist solely to be called by
    their corresponding MCP tool, with no other callers.
  |||,
  recommendation: |||
    Inline these methods directly into their MCP tool/resource implementations:

    **Example for ApprovalHub.resolve():**

    Before:
    ```python
    def resolve(self, call_id: str, decision: ...) -> None:
        pending = self._pending.pop(call_id, None)
        if pending is not None and not pending.future.done():
            pending.future.set_result(decision)
        if self._has_mcp:
            asyncio.create_task(self.notify_approvals_changed())

    @self.tool()
    async def approve(call_id: str, reasoning: str | None = None) -> dict:
        decision = ContinueDecision(reasoning=reasoning)
        self.resolve(call_id, decision)
        # ...
    ```

    After:
    ```python
    @self.tool()
    async def approve(call_id: str, reasoning: str | None = None) -> dict:
        # Inline resolve logic
        pending = self._pending.pop(call_id, None)
        if pending is not None and not pending.future.done():
            pending.future.set_result(ContinueDecision(reasoning=reasoning))
        await self.notify_approvals_changed()
        return {"status": "approved", "call_id": call_id, "agent_id": self._agent_id}
    ```

    **Benefits:**
    - Removes unnecessary indirection
    - Makes the tool implementation self-contained and easier to understand
    - Reduces method count in the class
    - Clearer that this is the ONLY place this logic is used

    **Note:** Methods like `await_decision()`, `get_policy()`, `load_policy()`, and
    `self_check()` should NOT be inlined - they're called externally by production code
    (policy gateway, startup initialization, etc.).
  |||,
}
