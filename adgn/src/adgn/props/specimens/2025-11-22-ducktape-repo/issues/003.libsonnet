local I = import '../../specimens/lib.libsonnet';

// iss-003: failure to mount agent compositor should crash, not skip-and-continue

I.issueOneOccurrence(
  rationale=|||
    The `create_global_compositor` function catches and suppresses errors when mounting
    agent compositors, continuing to mount other agents. This creates an inconsistent
    state where some agents are accessible and others silently aren't.

    **Current code (lines 96-103 in compositor_factory.py):**
    ```python
    # Mount per-agent compositors for existing agents
    for agent_id in registry.known_agents():
        try:
            agent_comp = await create_agent_compositor(agent_id, registry)
            await global_comp.mount_inproc(f"agent{agent_id}", agent_comp)
            logger.info(f"Mounted agent compositor for agent {agent_id}")
        except Exception as e:
            logger.error(f"Failed to mount compositor for agent {agent_id}: {e}", exc_info=True)
            # Continue mounting other agents  <--- WRONG!
    ```

    **Problems with skip-and-continue:**
    1. **Inconsistent state**: Some agents mounted, others not - user has no way to know which
    2. **Silent failure**: Error logged but system appears healthy to monitoring/users
    3. **Broken invariants**: Registry knows about agent but compositor doesn't expose it
    4. **Debug difficulty**: User tries to access agent, gets 404, no clear indication why
    5. **Cascading issues**: Downstream code assumes "if agent in registry, it's accessible"
    6. **No recovery path**: Once started with partial state, no way to fix without restart

    **Why fail-fast is correct:**
    - **Clear failure**: System startup fails immediately with clear error
    - **Consistent state**: Either all agents mounted or none (no partial state)
    - **Debuggable**: Stack trace points directly to failing agent and cause
    - **Prevents operation**: Won't serve requests in broken state
    - **Forces fix**: Operator must fix underlying issue before system runs
    - **Predictable**: Users know if system started, all agents are accessible

    **What mount failures indicate:**
    - Database corruption (agent exists but infrastructure data invalid)
    - Migration incomplete (old schema agent data)
    - Resource unavailable (Docker, network, filesystem)
    - Code bug (incompatible infrastructure version)

    All of these are serious issues that should prevent startup.

    **Correct approach:**
    ```python
    # Mount per-agent compositors for existing agents
    for agent_id in registry.known_agents():
        # Use mount_agent_compositor_dynamically (see issue 002)
        await mount_agent_compositor_dynamically(
            global_compositor=global_comp,
            agent_id=agent_id,
            registry=registry
        )
        # Let exceptions propagate - fail-fast on mount errors
    ```

    No try/except. If mounting fails, the entire create_global_compositor fails,
    which propagates up and prevents the management UI from starting.

    **Alternative considered:**
    - **Remove failed agent from registry**: Too complex, hides data corruption
    - **Mark agent as "degraded"**: Adds complexity, user still gets confusing errors
    - **Retry logic**: Doesn't help with persistent errors (bad data)
    - **Skip only at startup, crash at runtime**: Inconsistent, confusing behavior

    **Impact:**
    - More aggressive failures at startup
    - Clearer indication of system health
    - Forces operators to fix data/infrastructure issues
    - Simpler code (no error handling)

    **Deployment note:**
    If existing deployments have corrupt agent data, this change will surface it
    immediately rather than hiding it. This is the correct behavior - the corruption
    should be fixed, not masked.
  |||,
  properties=['fail-fast', 'error-handling', 'system-reliability', 'consistency', 'defensive-programming'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/compositor_factory.py': [
      [96, 103],   // try/except that silently suppresses mount failures
      [101, 103],  // Error handling that continues instead of failing
    ],
  },
)
