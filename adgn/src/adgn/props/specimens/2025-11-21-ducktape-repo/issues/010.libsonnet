local I = import '../../specimens/lib.libsonnet';

// iss-010: Delete ApprovalRequest wrapper, use ToolCall directly

I.issueOneOccurrence(
  rationale=|||
    The `ApprovalRequest` class (approvals.py:63-64) is a pointless single-field Pydantic wrapper
    around `ToolCall`. It serves no purpose and should be deleted, with all uses replaced by
    `ToolCall` directly.

    **Current code (lines 63-64):**
    ```python
    class ApprovalRequest(BaseModel):
        tool_call: ToolCall
    ```

    **Why it's pointless:**
    - Single field that just wraps another type
    - No additional validation, methods, or behavior
    - All usage sites immediately unwrap it to access `.tool_call`
    - Creates unnecessary indirection and cognitive overhead

    **Usage patterns showing pointlessness:**

    Line 291-297 (middleware.py): Wraps ToolCall, then immediately unwraps it
    ```python
    req = ApprovalRequest(
        tool_call=ToolCall(name=name, call_id=call_id, args_json=...)
    )
    wait_coro = self._hub.await_decision(call_id, req)
    if self._notify is not None:
        await self._notify(req.tool_call)  # Unwraps to access .tool_call
    ```

    Lines 54-56 (servers/agents.py): Extracts .tool_call from ApprovalRequest
    ```python
    result.append(
        PendingApproval(
            tool_call=request.tool_call,  # Unwraps to access .tool_call
            timestamp=datetime.now(),
        )
    )
    ```

    **Correct approach - delete ApprovalRequest and use ToolCall directly:**

    1. Change PendingApproval dataclass (line 71):
    ```python
    @dataclass
    class PendingApproval:
        """Pending approval with tool call and future."""
        tool_call: ToolCall
        future: asyncio.Future[ContinueDecision | DenyContinueDecision | AbortTurnDecision]
    ```

    2. Change await_decision signature (line 96):
    ```python
    async def await_decision(
        self, call_id: str, tool_call: ToolCall
    ) -> ContinueDecision | DenyContinueDecision | AbortTurnDecision:
        async with self._lock:
            pending = self._pending.get(call_id)
            if pending is None:
                fut = asyncio.get_running_loop().create_future()
                self._pending[call_id] = PendingApproval(tool_call=tool_call, future=fut)
            else:
                fut = pending.future
        ...
    ```

    3. Change pending property return type (line 117):
    ```python
    @property
    def pending(self) -> dict[str, ToolCall]:
        """Public view of pending approval tool calls."""
        return {call_id: p.tool_call for call_id, p in self._pending.items()}
    ```

    4. Update middleware.py caller (lines 291-297):
    ```python
    tool_call = ToolCall(name=name, call_id=call_id, args_json=(json.dumps(arguments) if arguments else None))
    wait_coro = self._hub.await_decision(call_id, tool_call)
    if self._notify is not None:
        await self._notify(tool_call)  # No unwrapping needed
    ```

    5. Update servers/agents.py (lines 51, 56):
    ```python
    def _convert_pending_approvals(pending_map: dict[str, ToolCall]) -> list[PendingApproval]:
        result: list[PendingApproval] = []
        for _call_id, tool_call in pending_map.items():
            result.append(
                PendingApproval(
                    tool_call=tool_call,  # Direct use, no unwrapping
                    timestamp=datetime.now(),
                )
            )
        return result
    ```

    6. Delete ApprovalRequest class (lines 63-64)
    7. Remove imports of ApprovalRequest from middleware.py and servers/agents.py

    **Benefits:**
    - Less code to maintain
    - One less layer of indirection
    - Clearer what data is being passed around
    - No pointless wrapping/unwrapping
    - Easier to understand the code flow

    **Note:**
    The TODO comment at servers/agents.py:57 mentions "Track creation time in ApprovalRequest".
    This should be moved to PendingApproval or tracked separately, not used as justification
    to keep the wrapper class.
  |||,
  properties=['unnecessary-abstraction', 'simplicity', 'indirection'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [63, 64],   // ApprovalRequest class definition
      [71, 71],   // PendingApproval.request field
      [96, 96],   // await_decision parameter
      [102, 102], // PendingApproval construction with request
      [117, 119], // pending property return type and dict comprehension
    ],
    'adgn/src/adgn/mcp/policy_gateway/middleware.py': [
      [16, 16],   // Import ApprovalRequest
      [291, 293], // Wrapping ToolCall in ApprovalRequest
      [297, 297], // Unwrapping req.tool_call
    ],
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [21, 21],   // Import ApprovalRequest
      [51, 51],   // Function signature with dict[str, ApprovalRequest]
      [56, 56],   // Extracting request.tool_call
      [57, 57],   // TODO comment about ApprovalRequest
    ],
  },
)
