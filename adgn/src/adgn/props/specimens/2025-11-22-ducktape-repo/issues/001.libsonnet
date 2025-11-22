local I = import '../../specimens/lib.libsonnet';

// iss-001: _global_compositor should not be optional in AgentRegistryBridgeServer

I.issueOneOccurrence(
  rationale=|||
    The `_global_compositor` parameter in `AgentRegistryBridgeServer.__init__` is typed
    as `Compositor | None`, making it optional. However, the server's core functionality
    (creating and deleting agents) depends on having a global compositor to mount/unmount
    agent compositors dynamically.

    **Current code (line 53):**
    ```python
    def __init__(self, registry: InfrastructureRegistry, global_compositor: Compositor | None = None):
        super().__init__(name="registry")
        self._registry = registry
        self._global_compositor = global_compositor  # Optional!
    ```

    **Usage in create_agent tool (lines 152-159):**
    ```python
    # Dynamically mount the new agent's compositor if we have a global compositor
    if self._global_compositor is not None:
        from adgn.agent.mcp_bridge.compositor_factory import mount_agent_compositor_dynamically

        await mount_agent_compositor_dynamically(
            global_compositor=self._global_compositor,
            agent_id=agent_id,
            registry=self._registry
        )
    ```

    **Problems with optional global_compositor:**
    1. **Silent degradation**: If None, create_agent silently skips mounting the agent compositor
    2. **Broken invariant**: The registry server exists to manage agents in the global compositor
    3. **Inconsistent state**: Agent exists in registry but not mounted in compositor
    4. **No error feedback**: User calls create_agent, gets success, but agent isn't actually accessible
    5. **Dead code path**: The None case should never happen in practice, so it's untested code

    **Why it should be required:**
    - AgentRegistryBridgeServer's purpose is to manage agents in the global compositor
    - Without global_compositor, create_agent and delete_agent tools are broken
    - The server is only instantiated in create_global_compositor (line 91) which always has a compositor
    - There's no legitimate use case for AgentRegistryBridgeServer without a global compositor

    **Correct approach:**
    ```python
    def __init__(self, registry: InfrastructureRegistry, global_compositor: Compositor):
        super().__init__(name="registry")
        self._registry = registry
        self._global_compositor = global_compositor  # Required!
    ```

    Then remove the `if self._global_compositor is not None:` guards in create_agent and delete_agent.

    **Benefits:**
    - Fail-fast: Constructor requires compositor or raises at initialization
    - Clear contract: Server requires compositor to function
    - Simpler code: No defensive None checks
    - Type safety: No Optional propagation through the codebase
    - Correct errors: Misconfiguration caught immediately, not silently

    **Impact:**
    - No existing code broken: create_global_compositor always passes a compositor
    - Prevents future bugs: Can't accidentally instantiate with None
    - Clearer API: Required parameters document requirements
  |||,
  properties=['api-design', 'type-safety', 'fail-fast', 'defensive-programming'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/registry_bridge.py': [
      [53, 53],    // Optional global_compositor parameter
      [56, 56],    // Assignment of optional value
      [152, 159],  // Defensive None check in create_agent
      [177, 183],  // Defensive None check in delete_agent
    ],
  },
)
