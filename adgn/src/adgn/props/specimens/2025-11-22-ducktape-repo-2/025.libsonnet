local I = import '../../specimens/lib.libsonnet';

I.issueOneOccurrence(
  rationale= |||
    The `approvals_list` variable is assigned the result of `sorted()` and then immediately used as the only argument to the return statement:

    ```python
    # Combine and sort by timestamp (most recent first)
    approvals_list = sorted(
        pending_approvals + decided_approvals,
        key=lambda x: x.timestamp,
        reverse=True,
    )

    return ApprovalsResponse(
        agent_id=self._agent_id,
        approvals=approvals_list,  # Used once
    )
    ```

    This is a classic one-off variable - it's created and used exactly once in the very next line. The variable serves no purpose:
    - It's not reused elsewhere
    - It doesn't improve readability (the sorted expression is already clear)
    - It doesn't capture an intermediate computation that's referenced multiple times
    - It adds an extra line and name that readers must track

    **Fix:**
    Inline the `sorted()` call directly in the return statement:
    ```python
    # Combine and sort by timestamp (most recent first)
    return ApprovalsResponse(
        agent_id=self._agent_id,
        approvals=sorted(
            pending_approvals + decided_approvals,
            key=lambda x: x.timestamp,
            reverse=True,
        ),
    )
    ```

    This removes the unnecessary intermediate variable while maintaining clarity. The comment still explains what's being sorted and why.
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [196, 204],  // approvals_list assignment and return
    ],
  },
)
