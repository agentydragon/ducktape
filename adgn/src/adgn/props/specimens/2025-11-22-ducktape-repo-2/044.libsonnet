local I = import '../../specimens/lib.libsonnet';

// iss-044: poorly designed iteration and duplicated logic

I.issueOneOccurrence(
  rationale= |||
    1. Iteration catches KeyError - poorly designed (lines 245-249):
    ```python
    for agent_id in self.known_agents():
        try:
            mode = self.get_agent_mode(agent_id)
        except KeyError:
            continue
    ```

    This is a code smell. We're iterating over `known_agents()`, then calling
    `get_agent_mode()` which can raise KeyError for agents that aren't initialized.
    If KeyError can happen during iteration, the iteration should be structured to
    avoid it, not catch and suppress it.

    The issue: `known_agents()` returns ALL agent IDs (initialized or not), but
    `get_agent_mode()` requires the agent to be initialized. This mismatch forces
    the try/except.

    Should iterate over a structure where agent mode is guaranteed to exist.

    2. Confusing "elif live:" inside "if infra:" (lines 264-267):
    ```python
    if infra:
        # Get pending approvals count
        pending_approvals = len(infra.approval_hub.pending)

        # Derive run phase
        if pending_approvals > 0:
            run_phase = RunPhase.WAITING_APPROVAL
        elif live:  # <-- CONFUSING!
            run_phase = RunPhase.SAMPLING
    ```

    The `elif live:` is inside `if infra:`, but `live = infra is not None`.
    So if we're inside `if infra:`, then `live` is always True!

    This is confusing and suggests the logic should be flattened:
    ```python
    if not infra:
        run_phase = RunPhase.IDLE
    elif pending_approvals > 0:
        run_phase = RunPhase.WAITING_APPROVAL
    else:
        run_phase = RunPhase.SAMPLING
    ```

    3. Duplicated run_phase logic (lines 255-267 and 296-304):
    The exact same run_phase determination logic appears in both:
    - `list_agents()` resource
    - `get_agent_info()` resource

    This is code duplication that should be extracted.

    Fixes:

    1. Iterate over structure with guaranteed agent data:
    ```python
    for agent_id, entry in self._agents.items():
        if entry.agent is None:
            continue  # Skip uninitialized agents
        agent = entry.agent

        infra = agent.running
        # ... rest of logic with guaranteed agent data
    ```

    Or if we want to include uninitialized agents with different status, make that explicit.

    2. Flatten run_phase logic:
    ```python
    if not infra:
        run_phase = RunPhase.IDLE
        pending_approvals = 0
    elif (pending_approvals := len(infra.approval_hub.pending)) > 0:
        run_phase = RunPhase.WAITING_APPROVAL
    else:
        run_phase = RunPhase.SAMPLING
    ```

    3. Extract run_phase determination:
    ```python
    def _determine_run_phase(
        self, infra: RunningInfrastructure | None
    ) -> tuple[RunPhase, int]:
        """Determine run phase and pending approvals count."""
        if not infra:
            return RunPhase.IDLE, 0

        pending_approvals = len(infra.approval_hub.pending)
        if pending_approvals > 0:
            return RunPhase.WAITING_APPROVAL, pending_approvals
        else:
            return RunPhase.SAMPLING, pending_approvals
    ```

    Then use it:
    ```python
    run_phase, pending_approvals = self._determine_run_phase(infra)
    ```

    Benefits:
    - Clearer iteration (no catching KeyErrors)
    - Simpler logic (no confusing elif)
    - DRY (single run_phase implementation)
  |||,
  properties=['python/no-swallowing-errors'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/server.py': [
      [245, 249],
      [264, 267],
      [255, 315],
    ],
  },
  gap_note= |||
    This finding touches on multiple principles:

    1. "iterate-over-valid-structure" - When iterating, the collection should contain
    only items in a valid state for the iteration body. Don't iterate over a superset
    then filter/skip via exception handling.

    2. "extract-duplicated-business-logic" - Complex business logic (like determining
    run phase) that appears in multiple places should be extracted to a helper method,
    even if it's only a few lines.

    The existing "no-swallowing-errors" property covers not catching exceptions, but
    doesn't specifically address the iteration pattern issue (iterating over the wrong
    collection and catching errors vs. iterating over the right collection).

    A dedicated property for iteration patterns would help catch cases where iteration
    structure doesn't match iteration logic.
  |||,
)
