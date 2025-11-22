local I = import '../../specimens/lib.libsonnet';

// iss-043: duplicated agent lookup logic

I.issueOneOccurrence(
  rationale= |||
    Multiple methods duplicate the same "get agent or raise KeyError" logic:

    get_agent_mode (lines 168-177):
    ```python
    def get_agent_mode(self, agent_id: AgentID) -> AgentMode:
        """Raises KeyError if agent not in registry or not yet initialized."""
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        agent = self._agents[agent_id].agent
        if agent is None:
            raise KeyError(f"Agent {agent_id} mode not yet initialized")
        return agent.mode
    ```

    get_infrastructure (lines 158-165):
    ```python
    async def get_infrastructure(self, agent_id: AgentID) -> RunningInfrastructure:
        """Raises KeyError if agent not in registry or not yet initialized."""
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        agent = self._agents[agent_id].agent
        if agent is None:
            raise KeyError(f"Agent {agent_id} infrastructure not yet initialized")
        return agent.running
    ```

    Both methods:
    1. Check if agent_id in self._agents
    2. Get self._agents[agent_id].agent
    3. Check if agent is None
    4. Raise KeyError with similar messages

    This is classic code duplication - the only difference is what field they return
    (agent.mode vs agent.running).

    Extract a common helper method:

    ```python
    def _get_agent_or_raise(self, agent_id: AgentID) -> RunningAgent:
        """Get RunningAgent or raise KeyError if not found/initialized."""
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        if (agent := self._agents[agent_id].agent) is None:
            raise KeyError(f"Agent {agent_id} not yet initialized")
        return agent
    ```

    Then simplify all callers:

    ```python
    def get_agent_mode(self, agent_id: AgentID) -> AgentMode:
        """Get agent mode. Raises KeyError if not found."""
        return self._get_agent_or_raise(agent_id).mode

    async def get_infrastructure(self, agent_id: AgentID) -> RunningInfrastructure:
        """Get infrastructure. Raises KeyError if not found."""
        return self._get_agent_or_raise(agent_id).running

    def get_local_runtime(self, agent_id: AgentID) -> LocalAgentRuntime | None:
        """Get local runtime or None if bridge agent. Raises KeyError if not found."""
        return self._get_agent_or_raise(agent_id).local_runtime

    async def remove_agent(self, agent_id: AgentID) -> None:
        """Remove and clean up agent infrastructure."""
        agent = self._get_agent_or_raise(agent_id)
        await agent.running.close()
        del self._agents[agent_id]
        await self.notify_agents_list_changed()
    ```

    Benefits:
    - DRY - single implementation of lookup logic
    - Consistent error messages
    - Easier to maintain
    - Could even inline some of these one-liners if they're called in few places
  |||,
  properties=[],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/server.py': [
      [158, 165],
      [168, 177],
      [224, 237],
    ],
  },
  gap_note= |||
    This finding represents a generalizable DRY principle specific to accessor patterns:
    "extract-common-lookup-validation" or "consolidate-getter-logic".

    When multiple getters share identical validation/lookup steps and only differ in
    which field they return, extract the common validation into a shared helper.

    Pattern:
    - Multiple methods with same guard checks (exists? initialized? valid?)
    - Only difference is the final return (different field/transformation)
    - Extract validation to `_get_X_or_raise()`, then accessors become one-liners

    This is more specific than general "no code duplication" - it's about the
    accessor/getter pattern specifically.
  |||,
)
