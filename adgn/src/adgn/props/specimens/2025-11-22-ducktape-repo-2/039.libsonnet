local I = import '../../specimens/lib.libsonnet';

// iss-039: imports inside functions instead of at module top

I.issueOneOccurrence(
  rationale= |||
    Multiple files have imports inside functions instead of at the top of the file.
    This violates PEP 8 style guidelines.

    Examples:

    auth.py line 180:
    ```python
    def _create_error_response(self, status_code: int, detail: str) -> dict:
        """Create error response dict."""
        import json  # <-- Should be at top
        body = json.dumps({"detail": detail}).encode()
    ```

    server.py lines 438-439:
    ```python
    async def create_management_ui_app(...):
        from adgn.agent.mcp_bridge.auth import UITokenAuthMiddleware, generate_ui_token
        from adgn.agent.mcp_bridge.compositor_factory import create_global_compositor
        # ... rest of function
    ```

    server.py lines 475-476:
    ```python
    if static_files_dir and static_files_dir.exists():
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import FileResponse
    ```

    Note: Some TYPE_CHECKING imports are at the top (correctly guarded), but these
    are runtime imports that should also be at the top.

    Common reasons for in-function imports (and why they're usually wrong):
    1. "Avoid circular imports" - Usually indicates bad module organization
    2. "Lazy loading" - Premature optimization, import time is negligible
    3. "Optional dependencies" - Should use try/except at module level
    4. "Conditional imports" - Rare legitimate use case (e.g., platform-specific)

    Move all imports to the top of the file.

    For auth.py:
    ```python
    # At top of file
    import json
    import logging
    import os
    # ... other imports

    # Remove from function
    def _create_error_response(self, status_code: int, detail: str) -> dict:
        """Create error response dict."""
        body = json.dumps({"detail": detail}).encode()
        # ... rest
    ```

    For server.py:
    ```python
    # At top of file
    from adgn.agent.mcp_bridge.auth import UITokenAuthMiddleware, generate_ui_token
    from adgn.agent.mcp_bridge.compositor_factory import create_global_compositor
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    ```

    If avoiding circular imports:
    - Restructure modules to eliminate the cycle
    - Or use TYPE_CHECKING guard for type annotations
    - Only resort to in-function imports as absolute last resort

    Benefits of top-level imports:
    - PEP 8 compliance
    - Easier to see all dependencies at a glance
    - Import errors caught at module load time, not runtime
    - Better for static analysis tools (linters, type checkers)
  |||,
  properties=['python/imports-top'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/auth.py': [
      180,
    ],
    'adgn/src/adgn/agent/mcp_bridge/server.py': [
      [438, 439],
      [475, 476],
    ],
    'adgn/src/adgn/agent/runtime/builder.py': [
      46,
    ],
    'adgn/src/adgn/agent/runtime/infrastructure.py': [
      67,
    ],
    'adgn/src/adgn/agent/runtime/local_runtime.py': [
      40,
    ],
    'adgn/src/adgn/agent/server/mcp_routing.py': [
      [23, 24],
    ],
  },
)
