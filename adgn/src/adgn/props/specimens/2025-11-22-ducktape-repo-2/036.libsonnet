{
  title: 'policy_source initialization should be ternary oneliner',
  severity: 'minor',
  category: 'code-style',
  locations: [
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/cli.py',
      lines: [88, 90],
      context: 'policy_source = None; if initial_policy: policy_source = ...',
    },
  ],
  description: |||
    The policy_source initialization uses two lines when it could be a single ternary expression:

    ```python
    policy_source = None
    if initial_policy:
        policy_source = initial_policy.read_text()
    ```

    This is a simple conditional assignment - perfect for a ternary operator.
  |||,
  recommendation: |||
    Replace with ternary oneliner:

    ```python
    policy_source = initial_policy.read_text() if initial_policy else None
    ```

    **Benefits:**
    - More concise (one line vs three)
    - Standard Python idiom for conditional assignment
    - Clearer intent (assigning based on condition)
    - Variable is const-assigned (not mutated)
  |||,
}
