local I = import '../../specimens/lib.libsonnet';

// iss-030: Inline mcp_config_obj into uvicorn.Server call

I.issueOneOccurrence(
  rationale=|||
    The code extracts mcp_config_obj and immediately uses it once to create uvicorn.Server.
    This intermediate variable should be inlined.

    **Current pattern (appears in 2 locations):**

    Location 1 (lines 139-140):
    ```python
    mcp_config_obj = uvicorn.Config(app=mcp_app, host=host, port=mcp_port, log_level="info")
    mcp_server = uvicorn.Server(mcp_config_obj)
    await mcp_server.serve()
    ```

    Location 2 (lines 155-156):
    ```python
    mcp_config_obj = uvicorn.Config(app=mcp_app, host=host, port=mcp_port, log_level="info")
    mcp_server = uvicorn.Server(mcp_config_obj)
    await mcp_server.serve()
    ```

    **Should be:**
    ```python
    mcp_server = uvicorn.Server(uvicorn.Config(app=mcp_app, host=host, port=mcp_port, log_level="info"))
    await mcp_server.serve()
    ```

    **Why inline:**
    - mcp_config_obj is used exactly once immediately after creation
    - Variable name doesn't add semantic clarity beyond the constructor call
    - Two lines instead of three
    - Standard pattern: Config is just a parameter to Server
    - No need to give Config instance a name if it's not reused

    **Readability consideration:**
    The inlined version is still readable because:
    - Config parameters are self-documenting (app=, host=, port=, log_level=)
    - Line is not excessively long (~100 chars with typical values)
    - Clear nesting: Server(Config(...))

    **Alternative (if line too long):**
    Could use parentheses for multi-line formatting:
    ```python
    mcp_server = uvicorn.Server(
        uvicorn.Config(app=mcp_app, host=host, port=mcp_port, log_level="info")
    )
    ```

    **Locations:**
    - cli.py:139-140 (single-agent mode)
    - cli.py:155-156 (multi-agent mode)
  |||,
  properties=['code-style', 'simplicity'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/cli.py': [
      [139, 141],  // mcp_config_obj in single-agent mode
      [155, 158],  // mcp_config_obj in multi-agent mode
    ],
  },
)
