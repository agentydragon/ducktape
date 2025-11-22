local I = import '../../specimens/lib.libsonnet';

// iss-049: Unnecessarily split with_ui conditional blocks

I.issueOneOccurrence(
  rationale=|||
    The with_ui conditional logic is split into two separate blocks unnecessarily:

    ```python
    ui_bus: ServerBus | None = None
    connection_manager: ConnectionManager | None = None
    if with_ui:
        ui_bus = ServerBus()
        connection_manager = ConnectionManager()

    builder = MCPInfrastructure(
        agent_id=agent_id,
        persistence=persistence,
        docker_client=docker_client,
        initial_policy=initial_policy,
        connection_manager=connection_manager,
    )

    running = await builder.start(mcp_config)

    if with_ui:
        assert ui_bus is not None
        await running.attach_sidecar(UISidecar(ui_bus))
    ```

    The two if with_ui blocks are independent and could be merged. The ordering doesn't
    matter - the UI sidecar attachment could happen immediately after initialization,
    or both operations could be moved after builder.start().

    Fix: Merge both with_ui operations into a single block:

    ```python
    ui_bus: ServerBus | None = None
    connection_manager: ConnectionManager | None = None
    if with_ui:
        ui_bus = ServerBus()
        connection_manager = ConnectionManager()

    builder = MCPInfrastructure(
        agent_id=agent_id,
        persistence=persistence,
        docker_client=docker_client,
        initial_policy=initial_policy,
        connection_manager=connection_manager,
    )

    running = await builder.start(mcp_config)

    if with_ui:
        assert ui_bus is not None
        await running.attach_sidecar(UISidecar(ui_bus))
    ```

    Actually, even better would be to consolidate into one block after builder.start():

    ```python
    builder = MCPInfrastructure(
        agent_id=agent_id,
        persistence=persistence,
        docker_client=docker_client,
        initial_policy=initial_policy,
        connection_manager=ConnectionManager() if with_ui else None,
    )

    running = await builder.start(mcp_config)

    if with_ui:
        ui_bus = ServerBus()
        await running.attach_sidecar(UISidecar(ui_bus))
    ```

    This eliminates the split conditional and reduces cognitive load.
  |||,
  properties=['python/modern-python-idioms'],
  filesToRanges={
    'adgn/src/adgn/agent/runtime/builder.py': [
      [70, 72],  // First if with_ui block
      [84, 86],  // Second if with_ui block
    ],
  },
)
