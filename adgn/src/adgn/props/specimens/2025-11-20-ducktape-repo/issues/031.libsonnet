local I = import '../../specimens/lib.libsonnet';

// iss-031: Inline builder into return statement

I.issueOneOccurrence(
  rationale=|||
    The code creates a builder and immediately uses it in the return statement.
    This intermediate variable should be inlined.

    **Current code (lines 63-67):**
    ```python
    builder = MCPInfrastructure(
        agent_id=agent_id, persistence=persistence, docker_client=docker_client, initial_policy=initial_policy
    )

    return await builder.start(mcp_config)
    ```

    **Should be:**
    ```python
    return await MCPInfrastructure(
        agent_id=agent_id, persistence=persistence, docker_client=docker_client, initial_policy=initial_policy
    ).start(mcp_config)
    ```

    **Why inline:**
    - builder is used exactly once immediately after creation
    - Variable name doesn't add semantic value beyond the class name
    - Standard pattern: create object and call method
    - More concise without sacrificing readability
    - Common Python idiom for builder/factory patterns

    **Readability:**
    The inlined version is clear because:
    - MCPInfrastructure constructor call is already multi-line
    - Method call (.start(mcp_config)) is simple and obvious
    - Chaining constructor → method is a standard pattern
    - Parentheses clearly show the structure

    **Similar patterns in codebase:**
    Check if there are other similar builder patterns that could be inlined.
    Found other instances:
    - runtime/builder.py:74 - same pattern
    - runtime/infrastructure.py:57 - same pattern

    All three locations have the same pattern and should be updated consistently.
  |||,
  properties=['code-style', 'simplicity'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/server.py': [
      [63, 67],  // builder extraction and immediate use
    ],
  },
)
