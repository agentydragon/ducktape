local I = import '../../specimens/lib.libsonnet';

// iss-002: create_global_compositor should use mount_agent_compositor_dynamically

I.issueOneOccurrence(
  rationale=|||
    The `create_global_compositor` function duplicates the logic from
    `mount_agent_compositor_dynamically` when mounting existing agents at startup.
    This violates DRY (Don't Repeat Yourself) and creates maintenance burden.

    **Current code (lines 95-103 in compositor_factory.py):**
    ```python
    # Mount per-agent compositors for existing agents
    for agent_id in registry.known_agents():
        try:
            agent_comp = await create_agent_compositor(agent_id, registry)
            await global_comp.mount_inproc(f"agent{agent_id}", agent_comp)
            logger.info(f"Mounted agent compositor for agent {agent_id}")
        except Exception as e:
            logger.error(f"Failed to mount compositor for agent {agent_id}: {e}", exc_info=True)
            # Continue mounting other agents
    ```

    **But mount_agent_compositor_dynamically exists (lines 113-130):**
    ```python
    async def mount_agent_compositor_dynamically(
        global_compositor: Compositor,
        agent_id: AgentID,
        registry: InfrastructureRegistry
    ) -> None:
        """Dynamically mount a new agent's compositor in the global compositor."""
        agent_comp = await create_agent_compositor(agent_id, registry)
        await global_compositor.mount_inproc(f"agent{agent_id}", agent_comp)
        logger.info(f"Dynamically mounted compositor for new agent {agent_id}")
    ```

    **Problems with duplication:**
    1. **Maintenance burden**: Same logic in two places means changes must be synchronized
    2. **Inconsistency risk**: Logic can drift between initial mount and dynamic mount
    3. **Code smell**: "Dynamically" in the name is a hint it's the canonical implementation
    4. **Different error handling**: Startup suppresses errors, dynamic mount doesn't (see issue 003)
    5. **Different logging**: "Mounted" vs "Dynamically mounted" - inconsistent messages

    **Why it's duplicated:**
    - Historical: mount_agent_compositor_dynamically was likely added after create_global_compositor
    - Separation: Initial mounting seemed different from dynamic mounting
    - Error handling: Startup wanted to continue on failure (see issue 003 for why this is wrong)

    **Correct approach:**
    ```python
    # Mount per-agent compositors for existing agents
    for agent_id in registry.known_agents():
        await mount_agent_compositor_dynamically(
            global_compositor=global_comp,
            agent_id=agent_id,
            registry=registry
        )
    ```

    Remove the try/except entirely (see issue 003).

    **Benefits:**
    - Single source of truth for mounting agent compositors
    - Consistent behavior between startup and runtime
    - Less code to maintain
    - Changes automatically apply to both code paths
    - Uniform error handling (fail-fast)

    **Notes:**
    - This change also requires fixing issue 003 (removing try/except)
    - The function name "dynamically" is fine - it means "at runtime" not "only after startup"
    - Both startup and create_agent are "dynamic" mounting (as opposed to static config)
  |||,
  properties=['code-duplication', 'dry-principle', 'maintainability', 'consistency'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/compositor_factory.py': [
      [95, 103],    // Duplicated mounting logic
      [113, 130],   // Canonical mount_agent_compositor_dynamically function
    ],
  },
)
