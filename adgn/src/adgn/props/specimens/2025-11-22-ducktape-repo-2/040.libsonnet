local I = import '../../specimens/lib.libsonnet';

// iss-040: unnecessary and outdated comments

I.issueOneOccurrence(
  rationale= |||
    Multiple unnecessary comments that either state the obvious or reference
    outdated implementation details.

    1. "now an MCP server" comments (compositor_factory.py lines 40, 45):
    ```python
    # Mount approval policy engine (now an MCP server)
    await comp.mount_inproc("policy", infra.approval_engine)
    logger.info(f"Mounted approval policy engine for agent {agent_id}")

    # Mount approvals hub (now an MCP server)
    await comp.mount_inproc("approvals", infra.approval_hub)
    logger.info(f"Mounted approvals hub for agent {agent_id}")
    ```
    Problems:
    - "now an MCP server" is historical commentary about past code state
    - Irrelevant now that refactoring is complete
    - Comment duplicates what the next line's log message says
    - Log message makes the mount operation obvious

    2. "Notify that..." comments (server.py):
    ```python
    # Notify that agent list changed
    await self.notify_agents_list_changed()
    ```
    The method name `notify_agents_list_changed()` already says this.

    3. Confusing auth comment (server.py line 416):
    ```python
    # Add UI token authentication (applies to all routes except /mcp which has its own auth)
    # Actually, we want auth on /mcp too, so add middleware
    app.add_middleware(UITokenAuthMiddleware, expected_token=ui_token)
    ```
    Language is confused - "except... Actually, we want..." - Just say what it does.

    4. Obvious comments:
    ```python
    # Generate or use provided UI token
    if ui_token is None:
        ui_token = generate_ui_token()
    ```
    The code is self-explanatory.

    5. Comment should be removed (server.py line 431):
    ```python
    # The compositor (FastMCP) is itself an ASGI app
    app.mount("/mcp", global_compositor)
    ```
    This is implementation detail that doesn't help understanding.

    6. Comment should be docstring (server.py line 459):
    ```python
    # Health check endpoint
    @app.get("/health")
    async def health():
        return {"status": "ok"}
    ```
    If needed, should be a docstring on the function.

    Remove all these comments:

    compositor_factory.py:
    ```python
    await comp.mount_inproc("policy", infra.approval_engine)
    logger.info(f"Mounted approval policy engine for agent {agent_id}")

    await comp.mount_inproc("approvals", infra.approval_hub)
    logger.info(f"Mounted approvals hub for agent {agent_id}")
    ```

    server.py - remove "Notify that..." comments:
    ```python
    await self.notify_agents_list_changed()
    # ... everywhere
    ```

    server.py - simplify auth comment or remove:
    ```python
    # Add UI token authentication to all routes
    app.add_middleware(UITokenAuthMiddleware, expected_token=ui_token)
    ```
    Or just remove the comment entirely - the code is clear.

    server.py - remove obvious comments:
    ```python
    if ui_token is None:
        ui_token = generate_ui_token()

    app.mount("/mcp", global_compositor)
    ```

    server.py - health endpoint:
    ```python
    @app.get("/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok"}
    ```
    Or remove comment entirely if the endpoint name is clear enough.

    General principle: Comments should explain *why*, not *what*. If the code
    is self-explanatory, remove the comment.
  |||,
  properties=['no-useless-docs'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/compositor_factory.py': [
      40,
      45,
    ],
    'adgn/src/adgn/agent/mcp_bridge/server.py': [
      209,
      237,
      361,
      363,
      416,
      421,
      429,
      431,
      441,
      443,
      459,
    ],
  },
)
