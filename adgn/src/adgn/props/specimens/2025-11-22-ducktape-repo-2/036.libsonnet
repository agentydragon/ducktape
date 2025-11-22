local I = import '../../specimens/lib.libsonnet';

// iss-036: ternary oneliner for simple conditional assignment

I.issueOneOccurrence(
  rationale= |||
    The policy_source initialization uses two lines when it could be a single ternary expression:

    ```python
    policy_source = None
    if initial_policy:
        policy_source = initial_policy.read_text()
    ```

    This is a simple conditional assignment - perfect for a ternary operator.

    Replace with ternary oneliner:

    ```python
    policy_source = initial_policy.read_text() if initial_policy else None
    ```

    Benefits:
    - More concise (one line vs three)
    - Standard Python idiom for conditional assignment
    - Clearer intent (assigning based on condition)
    - Variable is const-assigned (not mutated)
  |||,
  properties=['python/modern-python-idioms'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/cli.py': [
      [88, 90],
    ],
  },
)
