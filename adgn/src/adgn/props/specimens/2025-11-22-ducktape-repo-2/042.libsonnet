{
  title: 'Inline single-use builder variable and use walrus operators',
  severity: 'minor',
  category: 'code-style',
  locations: [
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/server.py',
      lines: [85, 89],
      context: 'builder variable assigned and immediately used',
    },
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/server.py',
      lines: [172, 173],
      context: 'agent = ... if agent is None: raise - should use walrus',
    },
  ],
  description: |||
    **1. create_bridge_infrastructure has unnecessary builder variable:**
    ```python
    async def create_bridge_infrastructure(...):
        """Create RunningInfrastructure for external agent HTTP bridge."""
        builder = MCPInfrastructure(
            agent_id=agent_id, persistence=persistence, docker_client=docker_client, initial_policy=initial_policy
        )

        return await builder.start(mcp_config)
    ```

    The `builder` variable is assigned and immediately used once - should be inlined.

    **2. Multiple methods should use walrus operator:**
    ```python
    agent = self._agents[agent_id].agent
    if agent is None:
        raise KeyError(...)
    ```

    This pattern appears multiple times and should use walrus in the conditional.
  |||,
  recommendation: |||
    **1. Inline builder:**
    ```python
    async def create_bridge_infrastructure(...):
        """Create RunningInfrastructure for external agent HTTP bridge."""
        return await MCPInfrastructure(
            agent_id=agent_id,
            persistence=persistence,
            docker_client=docker_client,
            initial_policy=initial_policy
        ).start(mcp_config)
    ```

    **2. Use walrus operator:**
    ```python
    if (agent := self._agents[agent_id].agent) is None:
        raise KeyError(f"Agent {agent_id} infrastructure not yet initialized")
    return agent.running  # or whatever we need from agent
    ```

    These changes make the code more concise and idiomatic.
  |||,
}
