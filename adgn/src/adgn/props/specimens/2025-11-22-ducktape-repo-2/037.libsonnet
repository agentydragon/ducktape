local I = import '../../specimens/lib.libsonnet';

// iss-037: exception handler swallows critical initialization errors

I.issueOneOccurrence(
  rationale= |||
    The create_global_compositor function catches exceptions when mounting
    agent compositors and continues silently:

    ```python
    try:
        await comp.mount_inproc(f"agent{agent_id}", agent_comp)
        logger.info(f"Mounted agent compositor for agent {agent_id}")
    except Exception as e:
        logger.error(f"Failed to mount compositor for agent {agent_id}: {e}", exc_info=True)
        # Continue mounting other agents
    ```

    This is WRONG. The exception should NOT be caught. It should crash.

    Why this is dangerous:
    1. Silent failure: The server starts successfully but is missing critical infrastructure
    2. Debugging nightmare: Errors are logged but the system appears "healthy"
    3. Inconsistent state: Some agents mounted, others not - partial initialization
    4. No recovery path: The failed agent is simply... missing. Forever.
    5. Violates fail-fast principle: Better to crash loudly than fail silently

    When should you catch exceptions:
    - When you can meaningfully recover
    - When partial success is acceptable
    - When you have a fallback strategy

    When should you let it crash:
    - During initialization/startup (this case!)
    - When partial state is worse than no state
    - When the failure indicates configuration/environment issues

    Mounting compositors is CRITICAL INFRASTRUCTURE. If it fails, the entire
    server is misconfigured and should not start.

    Remove the try/except entirely. Let it crash.

    Before:
    ```python
    try:
        await comp.mount_inproc(f"agent{agent_id}", agent_comp)
        logger.info(f"Mounted agent compositor for agent {agent_id}")
    except Exception as e:
        logger.error(f"Failed to mount compositor for agent {agent_id}: {e}", exc_info=True)
        # Continue mounting other agents
    ```

    After:
    ```python
    await comp.mount_inproc(f"agent{agent_id}", agent_comp)
    logger.info(f"Mounted agent compositor for agent {agent_id}")
    ```

    If mounting fails:
    - Exception propagates
    - Server crashes during startup
    - Operator sees the error immediately
    - System never enters partially-broken state
    - Problem must be fixed before server can start

    This is the correct behavior.

    If you truly need partial mounting (debatable), you need:
    1. Explicit tracking of which agents failed to mount
    2. Health checks that report the failures
    3. API endpoints that return errors for unmounted agents
    4. Recovery/retry logic
    5. Clear documentation of when partial mounting is acceptable

    But most likely: mounting is initialization, initialization failures should crash.
  |||,
  properties=['python/no-swallowing-errors', 'early-bailout'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/compositor_factory.py': [
      [93, 95],
    ],
  },
)
