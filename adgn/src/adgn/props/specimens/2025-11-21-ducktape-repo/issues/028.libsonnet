local I = import '../../specimens/lib.libsonnet';

// iss-028: Redundant local_runtime None checks after verifying mode is LOCAL

I.issueOneOccurrence(
  rationale=|||
    Multiple functions check both `get_agent_mode(agent_id) != AgentMode.LOCAL` and then
    `get_local_runtime(agent_id) is None`. The second check is redundant because of the invariant:
    **mode == LOCAL ⟺ local_runtime is not None**.

    **Evidence of invariant:**

    1. `RunningAgent` class (server.py:43):
       ```python
       local_runtime: LocalAgentRuntime | None  # None for bridge agents
       ```

    2. `get_local_runtime` docstring (server.py:159):
       ```python
       """Returns None if agent is not local. Raises KeyError if agent not in registry."""
       ```

    3. `register_local_agent` implementation (server.py:171-173):
       ```python
       self._agents[agent_id].agent = RunningAgent(
           running=running, compositor_app=compositor_app, mode=AgentMode.LOCAL, local_runtime=local_runtime
       )
       ```
       Always sets mode=LOCAL with a local_runtime value.

    **Five redundant check patterns:**

    **1. agent_state (lines 322-326):**
    ```python
    if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
        raise ValueError(f"Agent {agent_id} is not a local agent")

    if (local_runtime := registry.get_local_runtime(agent_id)) is None:
        raise ValueError(f"Agent {agent_id} has no local runtime")
    ```
    **Fully redundant** - if mode is LOCAL, local_runtime will never be None.

    **2. agent_snapshot (lines 345-349):**
    ```python
    if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
        raise ValueError(f"Agent {agent_id} is not a local agent")

    if (local_runtime := registry.get_local_runtime(agent_id)) is None:
        raise ValueError(f"Agent {agent_id} has no local runtime")
    ```
    **Fully redundant** - same pattern.

    **3. agent_mcp_state (lines 367-371):**
    ```python
    if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
        raise ValueError(f"Agent {agent_id} is not a local agent")

    if (local_runtime := registry.get_local_runtime(agent_id)) is None:
        raise ValueError(f"Agent {agent_id} has no local runtime")
    ```
    **Fully redundant** - same pattern.

    **4. session_state (lines 561-566):**
    ```python
    if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
        raise ValueError(f"Agent {agent_id} is not a local agent")

    local_runtime = registry.get_local_runtime(agent_id)
    if local_runtime is None or local_runtime.session is None:
        raise ValueError(f"Agent {agent_id} has no session")
    ```
    **Partially redundant** - the `local_runtime is None` part is redundant, but
    `local_runtime.session is None` is a valid additional check.

    **5. abort_agent (lines 644-649):**
    ```python
    if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
        raise ValueError(f"Agent {agent_id} is not a local agent (cannot abort)")

    local_runtime = registry.get_local_runtime(agent_id)
    if local_runtime is None or local_runtime.agent is None:
        raise ValueError(f"Agent {agent_id} has no agent loop")
    ```
    **Partially redundant** - the `local_runtime is None` part is redundant, but
    `local_runtime.agent is None` is a valid additional check.

    **Why this is problematic:**

    1. **Violates DRY**: Same condition checked twice (mode == LOCAL implies local_runtime is not None)
    2. **Misleading error messages**: Suggests local_runtime could be None for local agents (it can't)
    3. **Unnecessary code**: Extra lines that provide no value
    4. **Confuses readers**: Makes them wonder if there's a case where mode is LOCAL but local_runtime is None

    **Recommended fix:**

    **For cases 1-3 (fully redundant):**
    Remove the second check entirely and use assertion if needed:
    ```python
    if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
        raise ValueError(f"Agent {agent_id} is not a local agent")

    local_runtime = registry.get_local_runtime(agent_id)
    assert local_runtime is not None  # Guaranteed by LOCAL mode
    ```

    Or even simpler (just remove the None check):
    ```python
    if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
        raise ValueError(f"Agent {agent_id} is not a local agent")

    local_runtime = registry.get_local_runtime(agent_id)
    # Continue using local_runtime - guaranteed not None for LOCAL agents
    ```

    **For cases 4-5 (partially redundant):**
    Remove the `local_runtime is None` part, keep the specific field check:
    ```python
    # Case 4 (session_state):
    if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
        raise ValueError(f"Agent {agent_id} is not a local agent")

    local_runtime = registry.get_local_runtime(agent_id)
    if local_runtime.session is None:  # local_runtime is None check removed
        raise ValueError(f"Agent {agent_id} has no session")

    # Case 5 (abort_agent):
    if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
        raise ValueError(f"Agent {agent_id} is not a local agent (cannot abort)")

    local_runtime = registry.get_local_runtime(agent_id)
    if local_runtime.agent is None:  # local_runtime is None check removed
        raise ValueError(f"Agent {agent_id} has no agent loop")
    ```

    **Benefits:**
    - Eliminates redundant checks
    - Clearer code (no confusion about when local_runtime could be None)
    - Trusts the documented invariant
    - Simpler error handling
    - Fewer lines of code

    **Note:**
    If the invariant ever needs to change (e.g., LOCAL agents without local_runtime), that would
    require refactoring the entire agent infrastructure, not just adding None checks.
  |||,
  properties=['redundancy', 'code-clarity', 'dry-principle'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [322, 326],  // agent_state - fully redundant
      [345, 349],  // agent_snapshot - fully redundant
      [367, 371],  // agent_mcp_state - fully redundant
      [561, 566],  // session_state - partially redundant (local_runtime is None part)
      [644, 649],  // abort_agent - partially redundant (local_runtime is None part)
    ],
  },
)
