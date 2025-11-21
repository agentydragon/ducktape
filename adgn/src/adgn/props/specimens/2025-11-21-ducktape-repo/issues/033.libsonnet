local I = import '../../specimens/lib.libsonnet';

// iss-033: create_agent has useless comments and empty lines that add no value

I.issueOneOccurrence(
  rationale=|||
    The `create_agent` function body has comments and empty lines that add no value. They should be
    removed to make the code more concise.

    **Current code (lines 785-792):**
    ```python
    # Generate unique agent ID
    agent_id = AgentID(f"agent-{uuid4().hex[:8]}")

    # Create infrastructure for the agent
    await registry.create_agent(agent_id)

    # Return agent brief with the created agent's ID
    return AgentBrief(id=agent_id)
    ```

    **Why these comments are problematic:**

    1. **"Generate unique agent ID"** - The code `agent_id = AgentID(f"agent-{uuid4().hex[:8]}")` is
       self-explanatory. The comment restates exactly what the code does without adding insight.

    2. **"Create infrastructure for the agent"** - The code `await registry.create_agent(agent_id)` is
       obvious from the function name `create_agent`. The comment adds nothing.

    3. **"Return agent brief with the created agent's ID"** - The code `return AgentBrief(id=agent_id)`
       is self-documenting. The comment just repeats what's visually obvious.

    4. **Empty lines between statements** - The empty lines (after line 786 and 789) add vertical
       spacing that makes the function look longer without improving readability. These 3 simple
       statements should be consecutive.

    5. **Code is already documented** - The function has a comprehensive docstring (lines 773-783)
       that explains what it does. The inline comments duplicate this information.

    **Recommended fix:**

    Remove all comments and empty lines:

    ```python
    async def create_agent(preset: str, system_message: str | None = None) -> AgentBrief:
        """Create a new agent with the given preset and optional system message.

        Generates a unique agent ID and initializes infrastructure for a new agent.
        The agent will be ready to accept connections and process requests.

        Args:
            preset: Agent preset name/configuration identifier.
            system_message: Optional system message override for the agent.

        Returns:
            AgentBrief with the newly created agent's ID and initial state.
        """
        agent_id = AgentID(f"agent-{uuid4().hex[:8]}")
        await registry.create_agent(agent_id)
        return AgentBrief(id=agent_id)
    ```

    **Benefits:**
    - More concise function body (3 lines instead of 8)
    - No redundant comments restating obvious code
    - Better readability with consecutive statements
    - Cleaner code that trusts the reader to understand simple operations
    - Follows principle: comments should explain WHY, not WHAT

    **Note:**
    The docstring is sufficient documentation. Inline comments should only be used when the code
    does something non-obvious or counterintuitive. Simple variable assignments and function calls
    don't need commentary.
  |||,
  properties=['code-clarity', 'comment-noise', 'conciseness'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [785, 792],  // Function body with useless comments and empty lines
      [785, 785],  // Comment: "Generate unique agent ID"
      [787, 787],  // Empty line
      [788, 788],  // Comment: "Create infrastructure for the agent"
      [790, 790],  // Empty line
      [791, 791],  // Comment: "Return agent brief with the created agent's ID"
    ],
  },
)
