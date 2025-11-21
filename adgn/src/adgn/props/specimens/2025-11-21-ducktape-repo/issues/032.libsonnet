local I = import '../../specimens/lib.libsonnet';

// iss-032: Delete abort_run alias - unnecessary wrapper with verbose docstring

I.issueOneOccurrence(
  rationale=|||
    The `abort_run` function is a trivial alias for `abort_agent` that adds no value. It should
    be deleted. The verbose docstring makes the function appear more substantial than it is.

    **Current code (lines 672-689):**
    ```python
    @server.tool()
    async def abort_run(agent_id: AgentID) -> SimpleOk:
        """Abort a running agent (alias for abort_agent).

        Requests immediate termination of the agent's active loop.
        This is a semantic alias for abort_agent that returns SimpleOk for consistency.

        Args:
            agent_id: The target agent ID.

        Returns:
            SimpleOk indicating successful abort request.

        Raises:
            ValueError: If agent is not local or has no agent loop.
        """
        await abort_agent(agent_id)
        return SimpleOk(ok=True)
    ```

    **What abort_agent looks like (lines 642-651):**
    ```python
    @server.tool()
    async def abort_agent(agent_id: AgentID) -> None:
        """Raises ValueError if agent is not local or has no agent loop."""
        if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
            raise ValueError(f"Agent {agent_id} is not a local agent (cannot abort)")

        local_runtime = registry.get_local_runtime(agent_id)
        if local_runtime is None or local_runtime.agent is None:
            raise ValueError(f"Agent {agent_id} has no agent loop")

        await local_runtime.agent.abort()
    ```

    **Why this is problematic:**

    1. **Unnecessary alias**: The function just calls `abort_agent` and wraps the result in `SimpleOk`.
       This provides no value - callers should just call `abort_agent` directly.

    2. **Confusing API**: Having two functions that do the same thing creates confusion about which
       one to use. Is there a semantic difference? (No, the docstring admits it's just an alias.)

    3. **Return type inconsistency**: `abort_agent` returns `None`, `abort_run` returns `SimpleOk`.
       This is described as "for consistency" but it's actually inconsistent - now there are two
       different return types for the same operation.

    4. **Verbose docstring for trivial code**: The 15-line docstring describes a 2-line function.
       The docstring has:
       - Summary line
       - Detailed explanation
       - Args section
       - Returns section
       - Raises section

       For a function that literally just calls another function and wraps the result.

    5. **Misleading docstring**: Says "This is a semantic alias for abort_agent that returns SimpleOk
       for consistency." What consistency? If it's just an alias, why does it exist?

    6. **YAGNI violation**: There's no evidence that this alias is needed. If some callers prefer
       `SimpleOk` return type, they can wrap it themselves.

    **Recommended fix:**

    Delete the entire `abort_run` function:

    ```python
    # DELETE lines 672-689
    ```

    Callers should use `abort_agent` directly:

    ```python
    # Before:
    result = await abort_run(agent_id)

    # After:
    await abort_agent(agent_id)
    # Or if SimpleOk is needed:
    await abort_agent(agent_id)
    return SimpleOk(ok=True)
    ```

    **Benefits:**
    - Simpler API with one clear way to abort
    - No confusion about which function to use
    - Less code to maintain
    - No verbose docstring for trivial wrapper
    - Consistent return types (abort operations return None)

    **Note:**
    If this alias exists because some callers specifically need `SimpleOk`, that's a code smell.
    The caller should handle the wrapping, not create a duplicate function in the API.
  |||,
  properties=['unnecessary-abstraction', 'code-bloat', 'api-design'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [672, 689],  // abort_run alias with verbose docstring
      [642, 651],  // abort_agent original function (for comparison)
    ],
  },
)
