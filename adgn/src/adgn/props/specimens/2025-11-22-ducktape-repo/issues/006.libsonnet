local I = import '../../specimens/lib.libsonnet';

// iss-006: list_agents and get_agent_info duplicate AgentInfo computation logic

I.issueOneOccurrence(
  rationale=|||
    The `list_agents` and `get_agent_info` resource handlers duplicate almost identical
    logic for computing AgentInfo from registry state. This violates DRY and creates
    maintenance burden.

    **Duplicated logic in list_agents (lines 67-100):**
    ```python
    try:
        mode = self._registry.get_agent_mode(agent_id)
    except KeyError:
        continue

    # Get infrastructure if available
    infra = self._registry.get_running_infrastructure(agent_id)
    live = infra is not None

    # Compute status fields
    pending_approvals = 0
    run_phase = RunPhase.IDLE

    if infra:
        # Get pending approvals count
        pending_approvals = len(infra.approval_hub.pending)

        # Derive run phase
        if pending_approvals > 0:
            run_phase = RunPhase.WAITING_APPROVAL
        elif live:
            run_phase = RunPhase.SAMPLING

    # Determine capabilities
    is_local = mode == AgentMode.LOCAL

    agents.append(
        AgentInfo(
            id=agent_id,
            mode=mode,
            live=live,
            run_phase=run_phase,
            pending_approvals=pending_approvals,
            capabilities=AgentCapabilities(chat=is_local, agent_loop=is_local),
        )
    )
    ```

    **Duplicated logic in get_agent_info (lines 108-135):**
    ```python
    try:
        mode = self._registry.get_agent_mode(agent_id)
    except KeyError:
        raise KeyError(f"Agent {agent_id} not found")

    infra = self._registry.get_running_infrastructure(agent_id)
    live = infra is not None

    pending_approvals = 0
    run_phase = RunPhase.IDLE

    if infra:
        pending_approvals = len(infra.approval_hub.pending)
        if pending_approvals > 0:
            run_phase = RunPhase.WAITING_APPROVAL
        elif live:
            run_phase = RunPhase.SAMPLING

    is_local = mode == AgentMode.LOCAL

    return AgentInfo(
        id=agent_id,
        mode=mode,
        live=live,
        run_phase=run_phase,
        pending_approvals=pending_approvals,
        capabilities=AgentCapabilities(chat=is_local, agent_loop=is_local),
    )
    ```

    **What's duplicated:**
    1. Get mode from registry (with KeyError handling)
    2. Get infrastructure from registry
    3. Compute `live` from `infra is not None`
    4. Initialize `pending_approvals` and `run_phase`
    5. If infra exists, get pending approvals count
    6. Derive run_phase from pending_approvals and live status
    7. Compute `is_local` from mode
    8. Construct AgentInfo with identical field mapping

    **Only differences:**
    - Error handling: list_agents continues on KeyError, get_agent_info raises with message
    - Context: list_agents calls in loop and appends, get_agent_info returns directly

    **Problems with duplication:**
    1. **Maintenance burden**: Logic changes must be synchronized across both methods
    2. **Inconsistency risk**: Easy to update one and forget the other
    3. **Code smell**: Identical business logic in multiple places
    4. **Testing overhead**: Same logic needs testing in two contexts
    5. **Readability**: Harder to see the canonical computation

    **Why duplication exists:**
    - list_agents was likely written first
    - get_agent_info added later, logic copy-pasted
    - No refactoring to extract common computation

    **Correct approach - Extract helper method:**

    ```python
    def _compute_agent_info(self, agent_id: AgentID) -> AgentInfo:
        """Compute AgentInfo for a given agent ID.

        Args:
            agent_id: Agent to compute info for

        Returns:
            AgentInfo with current status

        Raises:
            KeyError: If agent not found in registry
        """
        mode = self._registry.get_agent_mode(agent_id)  # May raise KeyError

        # Get infrastructure if available
        infra = self._registry.get_running_infrastructure(agent_id)
        live = infra is not None

        # Compute status fields
        pending_approvals = 0
        run_phase = RunPhase.IDLE

        if infra:
            # Get pending approvals count
            pending_approvals = len(infra.approval_hub.pending)

            # Derive run phase
            if pending_approvals > 0:
                run_phase = RunPhase.WAITING_APPROVAL
            elif live:
                run_phase = RunPhase.SAMPLING

        # Determine capabilities
        is_local = mode == AgentMode.LOCAL

        return AgentInfo(
            id=agent_id,
            mode=mode,
            live=live,
            run_phase=run_phase,
            pending_approvals=pending_approvals,
            capabilities=AgentCapabilities(chat=is_local, agent_loop=is_local),
        )
    ```

    **Then simplify both resource handlers:**

    ```python
    @self.resource("resource://agents/list", name="agents_list", mime_type="application/json")
    async def list_agents() -> AgentsListResponse:
        """List all agents with detailed status."""
        agents = []
        for agent_id in self._registry.known_agents():
            try:
                agent_info = self._compute_agent_info(agent_id)
                agents.append(agent_info)
            except KeyError:
                continue  # Skip agents that disappeared
        return AgentsListResponse(agents=agents)

    @self.resource("resource://agents/{agent_id}/info", name="agent_info", mime_type="application/json")
    async def get_agent_info(agent_id: AgentID) -> AgentInfo:
        """Get detailed information about a specific agent."""
        try:
            return self._compute_agent_info(agent_id)
        except KeyError:
            raise KeyError(f"Agent {agent_id} not found")
    ```

    **Benefits:**
    - Single source of truth for AgentInfo computation
    - Changes automatically apply to both resource handlers
    - Easier to test (test helper method once)
    - Clearer code: resource handlers focus on orchestration
    - DRY principle upheld
    - Helper method is reusable if new endpoints need AgentInfo

    **Alternative considered:**
    Could make list_agents call get_agent_info in a loop, but:
    - Less efficient (would construct KeyError messages unnecessarily)
    - Couples the two resources (list depends on get)
    - Helper method is cleaner separation of concerns

    The helper method approach is the clear winner.
  |||,
  properties=['code-duplication', 'dry-principle', 'maintainability', 'refactoring'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/registry_bridge.py': [
      [67, 100],   // Duplicated logic in list_agents
      [108, 135],  // Duplicated logic in get_agent_info
    ],
  },
)
