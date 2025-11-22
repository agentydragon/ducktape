local I = import '../../specimens/lib.libsonnet';

// iss-041: redundant stored field that should be derived

I.issueOneOccurrence(
  rationale= |||
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

    Before:
    ```python
    entry.agent = RunningAgent(
        running=running,
        compositor_app=compositor_app,
        mode=AgentMode.BRIDGE,
        local_runtime=None
    )
    ```

    After:
    ```python
    entry.agent = RunningAgent(
        running=running,
        compositor_app=compositor_app,
        local_runtime=None
    )
    ```

    Benefits:
    - Single source of truth (local_runtime determines mode)
    - Cannot get out of sync
    - Less data to maintain
    - Clear semantic relationship
  |||,
  properties=['structured-data-over-untyped-mappings'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/server.py': [
      40,
    ],
  },
  gap_note= |||
    This finding represents a generalizable principle: "no-redundant-stored-fields" or
    "prefer-derived-properties". When a field's value can be deterministically computed
    from other fields in the same object, it should be a computed property rather than
    stored data.

    The existing "structured-data-over-untyped-mappings" property covers using proper
    types, but doesn't specifically address the redundancy aspect - storing duplicate
    information that can be derived.

    A dedicated property would cover:
    - Fields that are pure functions of other fields should be @property methods
    - Prevents data inconsistency (stored value differs from derived value)
    - Reduces maintenance burden (fewer fields to track)
    - Makes the semantic dependency explicit

    Similar to database normalization principles applied to in-memory data structures.
  |||,
)
