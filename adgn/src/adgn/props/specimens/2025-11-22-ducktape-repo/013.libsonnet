local I = import '../../specimens/lib.libsonnet';

// iss-013: Remove docstring Args sections that restate obvious type information

I.issueOneOccurrence(
  rationale=|||
    Many functions have Args sections in docstrings that simply restate information
    already obvious from type annotations:

    ```python
    async def create_agent(agent_id: AgentID) -> dict:
        """Create a new agent and mount its compositor.

        Args:
            agent_id: Unique identifier for the new agent  # OBVIOUS from AgentID type

        Returns:
            Dictionary with agent_id and status
        """
    ```

    Another example:
    ```python
    async def approve(call_id: str, reasoning: str | None = None) -> dict:
        """Approve a pending tool call.

        Args:
            call_id: ID of the tool call to approve  # OBVIOUS: str parameter named call_id
            reasoning: Optional reasoning for the approval  # OBVIOUS from str | None type

        Returns:
            Dictionary confirming the approval
        """
    ```

    These Args sections provide no additional value beyond the type annotations and
    parameter names. Good documentation should explain WHY or HOW, not WHAT (which
    is already clear from the signature).

    Fix - remove Args sections that simply restate type information. Keep the one-line
    summary and Returns section:

    ```python
    async def create_agent(agent_id: AgentID) -> dict:
        """Create a new agent and mount its compositor.

        Returns:
            Dictionary with agent_id and status
        """
    ```

    Only document parameters when there's non-obvious semantic information:
    ```python
    async def process_batch(items: list[str], max_retries: int = 3) -> dict:
        """Process a batch of items with automatic retry.

        Args:
            max_retries: Number of retry attempts before failing (default: 3).
                         Exponential backoff is applied between retries.

        Returns:
            Dictionary with success count and failed items
        """
    ```

    In this case, `max_retries` is documented because it explains the retry behavior,
    not just the type.
  |||,
  properties=['no-useless-docs'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/registry_bridge.py': [
      [142, 143], // create_agent Args
      [170, 171], // delete_agent Args
    ],
    'adgn/src/adgn/agent/mcp_bridge/servers/approvals_bridge.py': [
      [125, 127], // approve Args
      [141, 143], // reject Args
    ],
  },
)
