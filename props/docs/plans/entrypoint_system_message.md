# Replace /init with ENTRYPOINT for System Message Generation

## Current Mechanism

Agent containers have an `/init` script that produces the system message:

1. Container starts with `CMD = ["sleep", "infinity"]`
2. Container stays alive
3. We use MCP `exec` tool to run `/init` script
4. Capture stdout as system message
5. Proceed with agent loop

Files involved:

- `agent_pkg/host/init_runner.py` - `run_init_script()` function
- `props/core/agent_handle.py` - calls `run_init_script()`
- Agent images have `/init` script (executable)

## Proposed Mechanism

Use Docker ENTRYPOINT to produce system message on container startup:

1. Container starts with:
   - `ENTRYPOINT = ["/entrypoint.sh"]`
   - `CMD = ["sleep", "infinity"]`
2. Entrypoint prints system message to stdout, then `exec "$@"`
3. Container transitions to running `sleep infinity` (stays alive)
4. We capture entrypoint output via Docker logs API
5. Use that as system message
6. Proceed with agent loop

## Benefits

1. **Simpler**: No MCP tool call needed just to get system message
2. **Standard Docker Pattern**: ENTRYPOINT is designed for initialization
3. **Earlier Validation**: Container fails to start if system message generation fails
4. **Cleaner Separation**: System message is pure image concern, not runtime behavior
5. **No Circular Dependency**: Don't need MCP server to be fully initialized just to run /init

## Implementation Plan

### Phase 1: Agent Image Changes

**For each agent in `props/core/agent_defs/*/`:**

1. Create `/entrypoint.sh` script:

   ```bash
   #!/bin/sh
   set -e

   # Generate and print system message
   cat /system_message.txt  # or whatever generates it

   # Transfer control to CMD arguments (sleep infinity)
   exec "$@"
   ```

2. Update `BUILD.bazel` OCI image targets:

   ```python
   oci_image(
       name = "image",
       base = "@python_slim_linux_amd64",
       entrypoint = ["/entrypoint.sh"],
       # CMD is set by container launcher (sleep infinity)
       ...
   )
   ```

3. Remove `/init` scripts (or keep for backward compatibility period)

### Phase 2: Container Startup Changes

**File**: `mcp_infra/container_session.py`

Add function to capture entrypoint output:

```python
async def get_entrypoint_output(
    client: aiodocker.Docker,
    container_id: str,
    timeout_seconds: float = 10.0
) -> str:
    """Capture output from container's ENTRYPOINT.

    The entrypoint should print system message to stdout,
    then exec into its CMD arguments (sleep infinity).

    This function waits for the container to be in 'running' state
    (indicating entrypoint completed and exec'd into sleep),
    then retrieves stdout logs from the entrypoint execution.

    Returns:
        System message as string (stdout from entrypoint)

    Raises:
        TimeoutError: If container doesn't reach running state in time
        RuntimeError: If container exits instead of staying running
    """
    container = client.containers.container(container_id)

    # Wait for container to reach 'running' state
    # (means entrypoint finished and exec'd into sleep)
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while True:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("Container did not reach running state")

        info = await container.show()
        status = info["State"]["Status"]

        if status == "running":
            break
        elif status in ("exited", "dead"):
            # Entrypoint failed - get logs for debugging
            logs = await container.log(stdout=True, stderr=True)
            log_text = b''.join(logs).decode('utf-8', errors='replace')
            raise RuntimeError(
                f"Container entrypoint failed (status={status}). Logs:\n{log_text}"
            )

        await asyncio.sleep(0.05)

    # Container is running - get stdout logs (entrypoint output)
    logs = await container.log(stdout=True, stderr=False)
    system_message = b''.join(logs).decode('utf-8')

    return system_message
```

Modify `_create_and_start_container()` or add new variant:

```python
async def _create_and_start_container(
    client: aiodocker.Docker,
    opts: ContainerOptions
) -> tuple[str, str]:
    """Create and start container, returning (container_id, entrypoint_output).

    Returns:
        Tuple of (container_id, system_message_from_entrypoint)
    """
    container_config = opts.to_container_config(cmd=SLEEP_FOREVER_CMD, auto_remove=False)
    container = await client.containers.create(container_config, name=opts.name)
    container_id = container.id

    try:
        await container.start()
        entrypoint_output = await get_entrypoint_output(client, container_id)
        return container_id, entrypoint_output
    except Exception:
        # Cleanup on failure
        try:
            await container.delete(force=True)
        except Exception as cleanup_error:
            logger.error(f"Failed to cleanup container {container_id}: {cleanup_error}")
        raise
```

Update `ContainerSessionState` to include system message:

```python
@dataclass
class ContainerSessionState:
    docker_client: aiodocker.Docker
    container_id: str | None
    image: str
    binds: list[BindMount] | None
    working_dir: Path
    network_mode: str
    environment: dict[str, str] | None
    system_message: str  # NEW: captured from entrypoint
```

Update `make_container_lifespan()`:

```python
@asynccontextmanager
async def lifespan(server: FastMCP):
    container_id, system_message = await _create_and_start_container(docker_client, opts)
    try:
        yield ContainerSessionState(
            docker_client=docker_client,
            container_id=container_id,
            image=opts.image,
            binds=opts.binds,
            working_dir=opts.working_dir,
            network_mode=opts.network_mode,
            environment=opts.environment,
            system_message=system_message,
        )
    finally:
        # cleanup as before
        ...
```

### Phase 3: AgentHandle Changes

**File**: `props/core/agent_handle.py`

Remove call to `run_init_script()`:

```python
@classmethod
async def create(
    cls,
    *,
    agent_run_id: UUID,
    definition_id: DefinitionId,
    model_client: OpenAIModelProto,
    mcp_client: Client,
    compositor: PropertiesDockerCompositor,
    handlers: list[BaseHandler],
    dynamic_instructions: Callable[[], Awaitable[str]] | None = None,
    parallel_tool_calls: bool = False,
    reasoning_summary: ReasoningSummary | None = None,
) -> AgentHandle:
    agent = await Agent.create(
        mcp_client=mcp_client,
        client=model_client,
        handlers=[DatabaseEventHandler(agent_run_id=agent_run_id), *handlers],
        tool_policy=AllowAnyToolOrTextMessage(),
        dynamic_instructions=dynamic_instructions,
        parallel_tool_calls=parallel_tool_calls,
        reasoning_summary=reasoning_summary,
    )

    # OLD: system_prompt = await run_init_script(mcp_client, compositor.runtime)
    # NEW: Get system message from compositor's session state
    system_prompt = await compositor.get_system_message()

    logger.debug(f"Entrypoint returned {len(system_prompt)} bytes")
    agent.process_message(SystemMessage.text(system_prompt))

    return cls(
        agent_run_id=agent_run_id,
        definition_id=definition_id,
        agent=agent,
        compositor=compositor
    )
```

**File**: `props/core/docker_env.py` (or wherever `PropertiesDockerCompositor` is)

Add method to access system message:

```python
class PropertiesDockerCompositor(Compositor):
    # ... existing code ...

    async def get_system_message(self) -> str:
        """Retrieve system message captured from container entrypoint."""
        # Access the lifespan context's session state
        # This requires exposing it through the compositor somehow
        # Option 1: Store in compositor during __aenter__
        # Option 2: Query the MCP server's lifespan context
        # Option 3: Store system_message as instance variable
        return self._system_message
```

### Phase 4: Cleanup

**Delete**:

- `agent_pkg/host/init_runner.py`
- Tests that specifically test `/init` behavior
- Remove `/init` scripts from agent image definitions

**Update**:

- Documentation mentioning `/init`
- Any references to `run_init_script()`

## Migration Strategy

### Option A: Clean Break

1. Update all agent images in one PR
2. Update container startup + AgentHandle in same PR
3. Remove old `/init` infrastructure
4. All-or-nothing change

### Option B: Gradual Migration

1. Phase 1: Support both mechanisms
   - Try entrypoint first
   - Fall back to `/init` if no system message in logs
2. Phase 2: Migrate images one by one
3. Phase 3: Remove `/init` support once all migrated

**Recommendation**: Option A (clean break) - simpler, clearer, fewer edge cases

## Key Challenge: Container Lifecycle

**Problem**: Docker containers must have a running PID 1 process to stay alive for `docker exec` calls. This creates a timing challenge:

1. **Container must stay alive**: Entrypoint must `exec` into `sleep infinity` to keep container running for MCP tool calls
2. **Need to capture output**: We need to know when entrypoint has finished printing system message before it execs into sleep
3. **Race condition**: If we check logs too early, might get partial system message; if we wait too long, we waste time

**Current approach avoids this**: Container starts with `sleep infinity` immediately, then we explicitly call `/init` via MCP exec and capture its output synchronously.

**Proposed approach requires signaling**: Entrypoint prints system message, then execs into sleep. We need a mechanism to know when it's done printing:

- **Option 1**: Poll container status until "running" + wait for logs to stabilize (check if log size unchanged for N ms)
- **Option 2**: Entrypoint prints marker after system message: `\n---END-SYSTEM-MESSAGE---\n`, we wait for marker
- **Option 3**: Use health check or custom signaling mechanism (file creation, HTTP endpoint)
- **Option 4**: Fixed delay after container reaches "running" state (crude but simple)

**Trade-off**: Added complexity vs. benefits (remove init_runner.py, no circular dependency for MCP server initialization). Current approach works and is already implemented.

**Status**: This plan is **exploratory**. The synchronization challenge may not justify the refactor.

## Edge Cases to Handle

1. **Entrypoint Failure**: Container exits instead of staying running
   - Detect via container status check
   - Include logs in error message

2. **Empty System Message**: Entrypoint produces no output
   - Validate non-empty before proceeding
   - Fail fast with clear error

3. **Stderr Output**: Entrypoint writes errors to stderr
   - Log stderr separately for debugging
   - Only use stdout as system message

4. **Slow Entrypoint**: Takes >10s to generate system message
   - Make timeout configurable
   - Consider if some agents need longer generation time

5. **Multi-line Output with Special Characters**: System message contains Unicode, long text
   - Ensure proper UTF-8 decoding
   - No length limits (Docker logs API handles this)

## Testing Plan

1. **Unit Tests**: Mock container startup, verify log capture
2. **Integration Tests**: Real container with test entrypoint
3. **E2E Tests**: Full agent flow with new mechanism
4. **Compatibility**: Ensure existing agent tests pass with new images

## Rollout Plan

1. **Implement infrastructure** (Phase 2-3)
2. **Convert one agent** (e.g., critic) as proof of concept
3. **Test thoroughly** with existing test suite
4. **Convert remaining agents** in batch
5. **Remove old code** (Phase 4)

## Design Decisions Made

1. **No validation of system message format** - Keep it simple, no format restrictions
2. **Env vars passed on container creation** - Already supported via `ContainerOptions.environment`
3. **Stderr output is not an error** - Only non-zero exit code indicates failure, stderr can contain debug logs
4. **10 second timeout** - More than enough for system message generation
5. **No shell script wrapper needed** - Can set entrypoint directly in `oci_image()` to Python script/CLI

## Implementation Complexity

- **Agent images**: Low (simple shell script change)
- **Container startup**: Medium (new log capture logic, error handling)
- **AgentHandle**: Low (remove old code, call new method)
- **Testing**: Medium (need to verify all agents work)

**Total Estimate**: Medium complexity, high value (significant simplification)
