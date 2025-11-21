local I = import '../../specimens/lib.libsonnet';

// iss-002: Delete broadcast_status no-op method and all call sites

I.issueOneOccurrence(
  rationale=|||
    The `broadcast_status` method is a no-op (lines 223-225) with a comment saying
    "WebSocket status broadcasts removed". It should be deleted along with all its call sites.

    **Current code (lines 223-225):**
    ```python
    async def broadcast_status(self, live: bool, active_run_id) -> None:
        # No-op: WebSocket status broadcasts removed
        pass
    ```

    **Call sites to delete:**
    - Line 140: `await self.broadcast_status(True, active)`
    - Line 162: `await self.broadcast_status(True, active)`
    - Line 395: `await self._manager.broadcast_status(True, run_id)`
    - Line 443: `await self._manager.broadcast_status(True, None)`

    **Why delete:**
    - **Dead code**: The method does nothing (explicit no-op with pass)
    - **Comment confirms obsolescence**: "WebSocket status broadcasts removed" indicates
      this was intentionally disabled, not temporarily stubbed
    - **No effect**: All 4 call sites are awaiting a no-op, wasting cycles
    - **Confusing**: Readers might think it does something, but it doesn't
    - **Maintenance burden**: Keeping dead code around requires mental overhead

    **What to delete:**
    1. Method definition (lines 223-225)
    2. All 4 `await self.broadcast_status(...)` or `await self._manager.broadcast_status(...)`
       call sites (lines 140, 162, 395, 443)

    **Pattern:**
    This is different from stub methods that might be filled in later. The comment explicitly
    says the functionality was "removed", not "TODO" or "not implemented yet".

    **Safety:**
    Since it's a no-op, removing all calls has zero behavioral change. No tests will break
    (unless they explicitly test that this method exists, which would be testing dead code).
  |||,
  properties=['dead-code', 'maintainability', 'cleanup'],
  filesToRanges={
    'adgn/src/adgn/agent/server/runtime.py': [
      [223, 225],  // broadcast_status no-op method definition
      [140, 140],  // Call site 1
      [162, 162],  // Call site 2
      [395, 395],  // Call site 3
      [443, 443],  // Call site 4
    ],
  },
)
