local I = import '../../specimens/lib.libsonnet';

// iss-034: delete_agent docstring is overly verbose and states the obvious

I.issueOneOccurrence(
  rationale=|||
    The `delete_agent` docstring is unnecessarily verbose, with most of it restating obvious
    information that doesn't add value. It should be pruned to just the essential information.

    **Current docstring (lines 796-810):**
    ```python
    async def delete_agent(agent_id: AgentID) -> SimpleOk:
        """Delete an agent and clean up its infrastructure.

        Removes the agent from the registry, closes all running infrastructure,
        and releases associated resources. The agent can no longer be accessed
        after deletion.

        Args:
            agent_id: ID of the agent to delete.

        Returns:
            SimpleOk confirming successful deletion.

        Raises:
            KeyError: If the agent is not found in the registry.
        """
        await registry.remove_agent(agent_id)
        return SimpleOk(ok=True)
    ```

    **Why this docstring is problematic:**

    1. **Summary is sufficient** - "Delete an agent and clean up its infrastructure" is clear.

    2. **Redundant elaboration** - "Removes the agent from the registry, closes all running infrastructure,
       and releases associated resources" just expands on "clean up its infrastructure". This is implied
       by deletion.

    3. **Obvious statement** - "The agent can no longer be accessed after deletion" is what deletion means.
       This is like saying "After you delete a file, the file is deleted."

    4. **Obvious Args** - "agent_id: ID of the agent to delete" just restates the parameter name and
       function name. Provides zero new information.

    5. **Obvious Returns** - "SimpleOk confirming successful deletion" is obvious from the return type
       and function name. All tool functions return status indicators.

    6. **Raises is valuable** - The Raises section is the only part that adds information (KeyError
       when agent not found). This should be kept.

    **Recommended fix:**

    Keep only the summary line and Raises section:

    ```python
    async def delete_agent(agent_id: AgentID) -> SimpleOk:
        """Delete an agent and clean up its infrastructure.

        Raises:
            KeyError: If the agent is not found in the registry.
        """
        await registry.remove_agent(agent_id)
        return SimpleOk(ok=True)
    ```

    Or even simpler (if the KeyError is obvious from the function):

    ```python
    async def delete_agent(agent_id: AgentID) -> SimpleOk:
        """Delete an agent and clean up its infrastructure."""
        await registry.remove_agent(agent_id)
        return SimpleOk(ok=True)
    ```

    **Benefits:**
    - Concise documentation (4 lines instead of 15)
    - No redundant information
    - Focuses on essential information only
    - Easier to read and maintain
    - Follows principle: don't state the obvious

    **Note:**
    Good docstrings answer "what does this do that isn't obvious from the signature?" For a function
    named `delete_agent`, the summary "Delete an agent" is sufficient unless there are non-obvious
    behaviors or exceptions to document.
  |||,
  properties=['documentation', 'verbosity', 'obvious-comments'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [796, 810],  // Overly verbose docstring
      [798, 800],  // Redundant elaboration paragraph
      [802, 806],  // Obvious Args and Returns sections
    ],
  },
)
