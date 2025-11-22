local I = import '../../specimens/lib.libsonnet';

// iss-010: pending_count should not be computed separately in approvals_bridge

I.issueOneOccurrence(
  rationale=|||
    The `pending_count` field in `ApprovalsResponse` is redundant because it's derived
    information that can be computed from the already-returned `approvals` list by counting
    items with `status == ApprovalStatus.PENDING`.

    Current implementation manually counts pending approvals while building the list:
    ```python
    pending_count = 0
    # ...
    for call_id, tool_call in pending_map.items():
        approvals_list.append(...)
        pending_count += 1
    ```

    This violates DRY (Don't Repeat Yourself) - the client already has all the information
    needed to compute pending_count from the approvals list.

    Fix: Remove the `pending_count` and `decided_count` fields from `ApprovalsResponse`.
    Clients can compute these values trivially:
    ```python
    pending_count = len([a for a in approvals if a.status == ApprovalStatus.PENDING])
    decided_count = len([a for a in approvals if a.status != ApprovalStatus.PENDING])
    ```

    This simplifies the server code and reduces the chance of count/list mismatch bugs.
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/approvals_bridge.py': [
      38,  // pending_count field
      65,  // pending_count increment
      80,  // decided_count increment
      116, // pending_count in response
    ],
  },
  gap_note=|||
    This pattern deserves a property like "no-redundant-stored-fields": when a field
    stores computed/derived information that can be trivially calculated from other
    fields in the response, it should be removed. This is distinct from
    "no-oneoff-vars-and-trivial-wrappers" which focuses on local variables/helper functions,
    whereas this is about API response design and data duplication.
  |||,
)
