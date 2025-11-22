local I = import '../../specimens/lib.libsonnet';

I.issueOneOccurrence(
  rationale= |||
    The ApprovalItem model has a `timestamp` field whose meaning is ambiguous:

    ```python
    class ApprovalItem(BaseModel):
        """A single approval (pending or decided)."""
        call_id: str
        tool_call: ToolCall
        status: ApprovalStatus
        reason: str | None = None
        timestamp: datetime  # What does this represent?
    ```

    Looking at usage patterns reveals inconsistent semantics:
    - **Pending approvals** (line 167): `timestamp=datetime.now()` - uses current time when building the list
    - **Decided approvals** (line 189): `timestamp=record.decision.decided_at` - uses the decision time

    The field name "timestamp" doesn't clarify what event it's timestamping:
    - Is it when the tool call was requested?
    - When the approval decision was made?
    - When the approval item was last updated?

    For decided approvals it's explicitly the decision time (`decided_at`), but for pending approvals it's just "now" which is actually neither the request time nor a decision time. This semantic inconsistency makes the field unclear and potentially misleading.

    **Fix:**
    Rename to be more specific about what is being timestamped. Options include:
    - `updated_at` - if it represents last update time for both states
    - Split into `requested_at` and `decided_at` fields where decided_at is nullable
    - Use a union type with status-specific semantics

    The name should make it clear what temporal event is being recorded, and the semantics should be consistent across both pending and decided states.
  |||,
  properties=['self-describing-names', 'truthfulness'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [79, 85],  // ApprovalItem class definition with timestamp field
      167,       // timestamp=datetime.now() for pending
      189,       // timestamp=record.decision.decided_at for decided
    ],
  },
)
