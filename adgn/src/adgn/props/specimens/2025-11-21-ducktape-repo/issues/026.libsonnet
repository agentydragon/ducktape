local I = import '../../specimens/lib.libsonnet';

// iss-026: list_agents constructs dict objects instead of using Pydantic models

I.issueOneOccurrence(
  rationale=|||
    The `list_agents` function manually constructs dict objects for the agent list response instead
    of using Pydantic models. This loses type safety, validation, and documentation benefits.

    **Current code (lines 270-312):**
    ```python
    async def list_agents() -> str:
        """Global agent list with detailed status for each agent.

        Returns JSON with agents array containing status information including:
        - id, mode, live status
        - active_run_id, run_phase
        - pending_approvals count
        - capabilities (chat, agent_loop)
        """
        agents = []
        for agent_id in registry.known_agents():
            # ... compute fields ...

            agents.append(
                {
                    "id": agent_id,
                    "mode": mode,
                    "live": live,
                    "active_run_id": str(active_run_id) if active_run_id else None,
                    "run_phase": run_phase,
                    "pending_approvals": pending_approvals,
                    "capabilities": capabilities,
                }
            )

        return json.dumps({"agents": agents})
    ```

    **Why this is problematic:**

    1. **No type safety**: Dict construction is untyped. Typos in field names won't be caught:
       ```python
       {"id": x, "mdoe": y}  # Typo in "mode" - no error!
       ```

    2. **No validation**: Pydantic would validate field types and required fields. With dicts,
       you can accidentally set wrong types or omit fields:
       ```python
       {"id": 123}  # id should be str, no validation error
       ```

    3. **No documentation**: Pydantic models serve as documentation for the response schema.
       Dicts require looking at the code to understand the structure.

    4. **No IDE support**: IDEs can't autocomplete field names or check types with dict construction.

    5. **Inconsistent with codebase**: The rest of the codebase uses Pydantic models for structured
       responses (e.g., `AgentInfo`, `AgentList`, `AgentApprovalsHistory`, etc.). This function
       is an outlier.

    6. **Manual JSON serialization**: Returns `str` with `json.dumps()` instead of returning a
       Pydantic model and letting the framework handle serialization.

    7. **Hard to evolve**: Adding/removing fields requires manual updates without compile-time checks.

    **Recommended fix:**

    **Step 1**: Define a Pydantic model for the agent list item:
    ```python
    class AgentListItem(BaseModel):
        """Single agent in the agents list."""
        id: AgentID
        mode: AgentMode
        live: bool
        active_run_id: str | None
        run_phase: str
        pending_approvals: int
        capabilities: dict[str, bool]


    class AgentsList(BaseModel):
        """Response for list_agents resource."""
        agents: list[AgentListItem]
    ```

    **Step 2**: Update the function to use the model:
    ```python
    async def list_agents() -> AgentsList:
        """Global agent list with detailed status for each agent."""
        agent_items = []
        for agent_id in registry.known_agents():
            try:
                mode = registry.get_agent_mode(agent_id)
            except KeyError:
                continue

            infra = registry.get_running_infrastructure(agent_id)
            live = infra is not None

            # Compute status fields
            active_run_id = None
            pending_approvals = 0
            run_phase = "idle"

            if infra:
                pending_approvals = len(infra.approval_hub.pending)
                if pending_approvals > 0:
                    run_phase = "waiting_approval"
                elif live:
                    run_phase = "sampling"

            is_local = mode == AgentMode.LOCAL
            capabilities = {"chat": is_local, "agent_loop": is_local}

            agent_items.append(
                AgentListItem(
                    id=agent_id,
                    mode=mode,
                    live=live,
                    active_run_id=str(active_run_id) if active_run_id else None,
                    run_phase=run_phase,
                    pending_approvals=pending_approvals,
                    capabilities=capabilities,
                )
            )

        return AgentsList(agents=agent_items)
    ```

    **Benefits:**
    - Type-safe field construction (typos caught at development time)
    - Automatic validation of field types and required fields
    - IDE autocomplete and type checking
    - Self-documenting schema (Pydantic models describe the structure)
    - Consistent with rest of codebase
    - Easier to evolve (add/remove fields with compiler checks)
    - Framework can handle JSON serialization automatically
    - Can generate OpenAPI/JSON schema from Pydantic models

    **Note:**
    The return type should change from `-> str` to `-> AgentsList`, and the framework should handle
    the JSON serialization (removing the manual `json.dumps()` call).
  |||,
  properties=['type-safety', 'maintainability', 'code-consistency'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [261, 312],  // list_agents function with manual dict construction
      [300, 310],  // Dict literal construction
      [312, 312],  // Manual json.dumps() call
    ],
  },
)
