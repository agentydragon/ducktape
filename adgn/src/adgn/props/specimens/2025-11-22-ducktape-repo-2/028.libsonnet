{
  title: 'docker_client None check should be inside self_check, not at call sites',
  severity: 'minor',
  category: 'api-design',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [344, 345],
      context: 'if self.docker_client is not None: self.self_check(content)',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [360, 361],
      context: 'if self.docker_client is not None: self.self_check(got.content)',
    },
  ],
  description: |||
    The pattern `if self.docker_client is not None: self.self_check(...)` appears
    twice in the codebase:

    **create_proposal() - lines 344-345:**
    ```python
    # Self-check proposal program if docker is available
    if self.docker_client is not None:
        self.self_check(content)
    ```

    **approve_proposal() - lines 360-361:**
    ```python
    # Self-check the proposal program before activation
    if self.docker_client is not None:
        self.self_check(got.content)
    ```

    This conditional is repeated at every call site. The check should be
    internal to `self_check()` itself, not the caller's responsibility.

    **Current self_check() implementation (lines 330-335):**
    ```python
    def self_check(self, source: str) -> None:
        run_policy_source(
            docker_client=self.docker_client,  # Passed unconditionally
            source=source,
            input_payload=...,
        )
    ```

    The method assumes `docker_client` is valid, forcing callers to guard it.
  |||,
  recommendation: |||
    Move the None check inside `self_check()`:

    ```python
    def self_check(self, source: str) -> None:
        """Validate policy source by running it in Docker sandbox.

        If docker_client is None, validation is skipped.
        """
        if self.docker_client is None:
            return  # Skip validation if Docker not available

        run_policy_source(
            docker_client=self.docker_client,
            source=source,
            input_payload={"name": build_mcp_function(UI_SERVER_NAME, "send_message"), "arguments": {}},
        )
    ```

    Then simplify call sites:

    **create_proposal():**
    ```python
    # Self-check proposal program (skips if Docker unavailable)
    self.self_check(content)
    ```

    **approve_proposal():**
    ```python
    # Self-check the proposal program before activation
    self.self_check(got.content)
    ```

    **Benefits:**
    - Single responsibility - `self_check()` handles its own preconditions
    - DRY - check not repeated at call sites
    - Cleaner API - callers don't need to know about Docker availability
  |||,
}
