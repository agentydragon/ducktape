{
  title: 'AgentMode should be derived property, not stored field',
  severity: 'minor',
  category: 'data-modeling',
  locations: [
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/server.py',
      lines: [40],
      context: 'RunningAgent dataclass with mode field',
    },
  ],
  description: |||
    The RunningAgent dataclass stores `mode` as a field:

    ```python
    @dataclass
    class RunningAgent:
        """All infrastructure for a running agent (single point of optionality)."""
        running: RunningInfrastructure
        compositor_app: FastAPI
        mode: AgentMode
        local_runtime: LocalAgentRuntime | None  # None for bridge agents
    ```

    Looking at the usage:
    - `mode = AgentMode.BRIDGE` when `local_runtime = None`
    - `mode = AgentMode.LOCAL` when `local_runtime` is not None

    The mode is completely determined by whether local_runtime exists:
    - `local_runtime is None` → `mode = BRIDGE`
    - `local_runtime is not None` → `mode = LOCAL`

    This is redundant - mode should be derived from local_runtime, not stored separately.
  |||,
  recommendation: |||
    Remove the `mode` field and make it a property:

    ```python
    @dataclass
    class RunningAgent:
        """All infrastructure for a running agent (single point of optionality)."""
        running: RunningInfrastructure
        compositor_app: FastAPI
        local_runtime: LocalAgentRuntime | None  # None for bridge agents

        @property
        def mode(self) -> AgentMode:
            """Derive mode from local_runtime presence."""
            return AgentMode.LOCAL if self.local_runtime else AgentMode.BRIDGE
    ```

    Update callsites to only pass running, compositor_app, and local_runtime:

    **Before:**
    ```python
    entry.agent = RunningAgent(
        running=running,
        compositor_app=compositor_app,
        mode=AgentMode.BRIDGE,
        local_runtime=None
    )
    ```

    **After:**
    ```python
    entry.agent = RunningAgent(
        running=running,
        compositor_app=compositor_app,
        local_runtime=None
    )
    ```

    **Benefits:**
    - Single source of truth (local_runtime determines mode)
    - Cannot get out of sync
    - Less data to maintain
    - Clear semantic relationship
  |||,
}
