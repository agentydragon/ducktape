local I = import '../../specimens/lib.libsonnet';

// iss-035: boot_agent Args and Returns sections are useless and should be deleted

I.issueOneOccurrence(
  rationale=|||
    The `boot_agent` docstring has Args and Returns sections that provide no useful information
    beyond what's obvious from the function signature. They should be deleted.

    **Current docstring (lines 815-829):**
    ```python
    async def boot_agent(agent_id: AgentID) -> SimpleOk:
        """Ensure an agent is booted and its infrastructure is running.

        Creates or resumes the agent's infrastructure to ensure it's ready
        for operation. If the agent is already running, this is a no-op.

        Args:
            agent_id: ID of the agent to boot.

        Returns:
            SimpleOk confirming the agent is ready.

        Raises:
            KeyError: If the agent is not found in the registry.
        """
        await registry.ensure_live(agent_id)
        return SimpleOk(ok=True)
    ```

    **Why Args and Returns are useless:**

    1. **Args section**: "agent_id: ID of the agent to boot" just restates the parameter name and
       function name. The type annotation `agent_id: AgentID` already tells you it's an agent ID.
       The function name `boot_agent` tells you it boots an agent. This adds zero information.

    2. **Returns section**: "SimpleOk confirming the agent is ready" is obvious from:
       - Return type annotation `-> SimpleOk`
       - Function name suggests it's a command/action that succeeds or fails
       - All tool functions follow this pattern

       The Returns section just restates what the type annotation already says.

    3. **Summary and behavior note are valuable**: The first two paragraphs explain:
       - What the function does (ensure booted)
       - Non-obvious behavior (no-op if already running)

       These are worth keeping.

    4. **Raises section is valuable**: Documents that KeyError is raised if agent not found.
       This is useful information.

    **Recommended fix:**

    Delete Args and Returns sections, keep summary and Raises:

    ```python
    async def boot_agent(agent_id: AgentID) -> SimpleOk:
        """Ensure an agent is booted and its infrastructure is running.

        Creates or resumes the agent's infrastructure to ensure it's ready
        for operation. If the agent is already running, this is a no-op.

        Raises:
            KeyError: If the agent is not found in the registry.
        """
        await registry.ensure_live(agent_id)
        return SimpleOk(ok=True)
    ```

    **Benefits:**
    - More concise docstring (9 lines instead of 15)
    - No redundant sections restating the signature
    - Keeps only valuable information (behavior note and exception)
    - Easier to read and maintain
    - Consistent with good documentation practices

    **Note:**
    Args and Returns sections should only be included when they provide information beyond what's
    in the type signature. For simple functions with obvious parameters and return types, omit
    these sections.
  |||,
  properties=['documentation', 'verbosity', 'obvious-comments'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [815, 829],  // Full docstring
      [821, 825],  // Useless Args and Returns sections
    ],
  },
)
