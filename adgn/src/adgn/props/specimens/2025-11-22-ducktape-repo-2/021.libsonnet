{
  title: 'ApprovalItem.timestamp field name is ambiguous',
  severity: 'minor',
  category: 'naming',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [79, 85],
      context: 'ApprovalItem class with timestamp field',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [167],
      context: 'timestamp=datetime.now() for pending approvals',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [189],
      context: 'timestamp=record.decision.decided_at for decided approvals',
    },
  ],
  description: |||
    The ApprovalItem model has a `timestamp` field that is ambiguous:

    ```python
    class ApprovalItem(BaseModel):
        """A single approval (pending or decided)."""
        call_id: str
        tool_call: ToolCall
        status: ApprovalStatus
        reason: str | None = None
        timestamp: datetime  # <-- What does this timestamp represent?
    ```

    Looking at usage:
    - **Pending approvals** (line 167): `timestamp=datetime.now()` - current time
    - **Decided approvals** (line 189): `timestamp=record.decision.decided_at` - decision time

    The field name "timestamp" doesn't clarify:
    - Time of tool call request?
    - Time of decision?
    - Time of last update?

    For decided approvals it's `decided_at`, but for pending it's just "now".
    This semantic inconsistency makes the field name unclear.
  |||,
  recommendation: |||
    Rename `timestamp` to be more specific about what it represents.

    **Option 1: If it represents "last updated time":**
    ```python
    class ApprovalItem(BaseModel):
        ...
        updated_at: datetime  # When this approval was last updated
    ```

    **Option 2: If it represents "decision time or request time":**
    ```python
    class ApprovalItem(BaseModel):
        ...
        decided_at: datetime | None  # When decided, or None if pending
        requested_at: datetime  # When the approval was requested
    ```

    **Option 3: Use status-specific semantics:**
    - For pending: `created_at` (when approval was requested)
    - For decided: `decided_at` (when decision was made)

    Rename to match the actual semantics of what's being timestamped.
  |||,
}
