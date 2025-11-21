local I = import '../../specimens/lib.libsonnet';

// iss-008: Replace parallel dicts in ApprovalHub with single dict to dataclass

I.issueOneOccurrence(
  rationale=|||
    ApprovalHub maintains two parallel dicts keyed by call_id (lines 75-78), which should
    be combined into one dict pointing to a dataclass.

    **Current code (lines 75-78):**
    ```python
    self._futures: dict[
        str, asyncio.Future[ContinueDecision | DenyContinueDecision | AbortTurnDecision]
    ] = {}
    self._requests: dict[str, ApprovalRequest] = {}
    ```

    **Why parallel dicts are problematic:**
    - **Synchronization burden**: Must keep both dicts in sync manually
    - **Error-prone**: Easy to update one dict and forget the other
    - **Unclear lifecycle**: Not obvious that entries in both dicts come and go together
    - **No type safety**: Can't enforce that both dicts have the same keys

    **Evidence they're managed in parallel:**

    Line 94: `self._requests[call_id] = request`
    Line 98: `self._futures[call_id] = fut`

    Lines 104-105 (both popped together):
    ```python
    fut = self._futures.pop(call_id, None)
    self._requests.pop(call_id, None)
    ```

    Line 95-96 (checking futures but not requests):
    ```python
    fut = self._futures.get(call_id)
    if fut is None:
    ```

    This asymmetry suggests potential bugs - what if a request exists without a future?

    **Correct approach:**

    Create a dataclass to hold both:
    ```python
    @dataclass
    class PendingApproval:
        request: ApprovalRequest
        future: asyncio.Future[ContinueDecision | DenyContinueDecision | AbortTurnDecision]
    ```

    Then use a single dict:
    ```python
    def __init__(self, notifier: Callable[[], None] | None = None) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()
        self._notifier = notifier
    ```

    **Updated methods:**

    ```python
    async def await_decision(
        self, call_id: str, request: ApprovalRequest
    ) -> ContinueDecision | DenyContinueDecision | AbortTurnDecision:
        async with self._lock:
            pending = self._pending.get(call_id)
            if pending is None:
                fut = asyncio.get_running_loop().create_future()
                self._pending[call_id] = PendingApproval(request=request, future=fut)
            else:
                fut = pending.future
        if self._notifier:
            self._notifier()
        return await fut

    def resolve(self, call_id: str, decision: ...) -> None:
        pending = self._pending.pop(call_id, None)
        if pending is not None and not pending.future.done():
            pending.future.set_result(decision)
        if self._notifier:
            self._notifier()

    @property
    def pending(self) -> dict[str, ApprovalRequest]:
        """Public view of pending approval requests."""
        return {call_id: p.request for call_id, p in self._pending.items()}
    ```

    **Benefits:**
    - Single source of truth
    - Impossible to have mismatched state (future without request or vice versa)
    - Type-safe: dataclass ensures both fields exist
    - Clearer lifecycle: one dict entry = one pending approval
    - Easier to add more fields later (timestamps, metadata, etc.)
    - Simpler code: one lookup instead of two

    **Note on property:**
    The `pending` property (lines 111-114) currently returns `self._requests` directly.
    After refactoring, it needs to extract requests from the dataclass (as shown above).
  |||,
  properties=['data-structure', 'type-safety', 'maintainability', 'parallel-dicts'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [75, 78],   // Two parallel dicts definition
      [94, 94],   // _requests assignment
      [98, 98],   // _futures assignment
      [104, 105], // Both dicts popped together
      [111, 114], // pending property returning _requests
    ],
  },
)
