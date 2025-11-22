local I = import '../../specimens/lib.libsonnet';

// iss-004: create_agent_compositor should inline policy_server and approvals_server

I.issueOneOccurrence(
  rationale=|||
    The `create_agent_compositor` function creates intermediate variables
    `policy_server` and `approvals_server` that are used exactly once.
    These unnecessary variables add no clarity and should be inlined.

    **Current code (lines 47-59 in compositor_factory.py):**
    ```python
    # Mount approval policy server
    policy_server = ApprovalPolicyBridgeServer(infra.approval_engine, agent_id)
    await comp.mount_inproc("policy", policy_server)
    logger.info(f"Mounted approval policy server for agent {agent_id}")

    # Mount approvals server
    approvals_server = ApprovalsBridgeServer(
        infra.approval_hub,
        registry.persistence,
        agent_id
    )
    await comp.mount_inproc("approvals", approvals_server)
    logger.info(f"Mounted approvals server for agent {agent_id}")
    ```

    **Problems with intermediate variables:**
    1. **Unnecessary binding**: Variables used exactly once, never referenced again
    2. **Name pollution**: `policy_server` and `approvals_server` add no semantic value
    3. **Cognitive load**: Reader must track "what is this variable for?"
    4. **No reuse**: Not used in logging or error handling
    5. **Inconsistency**: Some code inlines, some doesn't
    6. **False complexity**: Suggests the variable might be used later (but isn't)

    **Why they exist:**
    - Likely habit from longer function bodies where variables were reused
    - Possibly for debugging (but not actually used in logging)
    - May have been copy-pasted from code where the variable was needed

    **Correct approach:**
    ```python
    # Mount approval policy server
    await comp.mount_inproc(
        "policy",
        ApprovalPolicyBridgeServer(infra.approval_engine, agent_id)
    )
    logger.info(f"Mounted approval policy server for agent {agent_id}")

    # Mount approvals server
    await comp.mount_inproc(
        "approvals",
        ApprovalsBridgeServer(
            infra.approval_hub,
            registry.persistence,
            agent_id
        )
    )
    logger.info(f"Mounted approvals server for agent {agent_id}")
    ```

    **Benefits:**
    - **Clearer**: Directly shows what's being mounted
    - **Less code**: Fewer lines, fewer names to track
    - **Consistent**: Matches Python idiom for single-use values
    - **Focused**: Reader sees "mount this server" not "create variable, then mount"
    - **Better formatting**: Multi-line constructor args read naturally when inlined

    **When NOT to inline:**
    - Value used multiple times
    - Complex expression that needs explaining
    - Type inference needed for clarity
    - Debugging aids (but use logger, not variables)

    None of these apply here. The server types are clear from the class names,
    construction is straightforward, and values are used exactly once.

    **Python idiom:**
    Python encourages inline construction for single-use values:
    ```python
    # Good
    await comp.mount_inproc("foo", FooServer(arg1, arg2))

    # Unnecessarily verbose
    server = FooServer(arg1, arg2)
    await comp.mount_inproc("foo", server)
    ```

    This is standard practice in idiomatic Python code.
  |||,
  properties=['code-style', 'simplicity', 'readability', 'python-idioms'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/compositor_factory.py': [
      [47, 50],   // Unnecessary policy_server variable
      [52, 59],   // Unnecessary approvals_server variable
    ],
  },
)
