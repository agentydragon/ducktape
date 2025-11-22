{
  title: 'ProposalStatus(p.status) redundant conversion suggests type inconsistency',
  severity: 'medium',
  category: 'type-design',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [388],
      context: 'status=ProposalStatus(p.status) in proposals_list',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [405],
      context: 'status=ProposalStatus(got.status) in proposal_detail',
    },
  ],
  description: |||
    Both resource handlers convert `p.status` and `got.status` to ProposalStatus:

    **proposals_list() - line 388:**
    ```python
    ProposalDescriptor(
        id=p.id,
        status=ProposalStatus(p.status),  # Conversion
        created_at=p.created_at,
        decided_at=p.decided_at,
    )
    ```

    **proposal_detail() - line 405:**
    ```python
    return ProposalDetail(
        id=got.id,
        status=ProposalStatus(got.status),  # Conversion
        created_at=got.created_at,
        decided_at=got.decided_at,
        content=got.content,
    )
    ```

    This conversion is necessarily redundant:

    **Case 1: They're the same type**
    If `p.status` and `got.status` are already ProposalStatus, then
    `ProposalStatus(p.status)` is a no-op that should be `p.status`.

    **Case 2: They're different types**
    If `p.status` is a different type (e.g., string or database enum),
    this indicates a type inconsistency that should be fixed upstream.

    **Similar to finding 024 (ApprovalOutcome vs ApprovalStatus):**
    This suggests ProposalStatus might have a duplicate in the persistence layer,
    requiring conversion at the boundary.
  |||,
  recommendation: |||
    **Investigate the type of p.status and got.status:**

    1. If they're already ProposalStatus, remove the conversion:
       ```python
       status=p.status,  # Direct assignment
       ```

    2. If they're strings or a different enum, unify the types:
       - Make persistence return ProposalStatus directly, OR
       - If there's a legitimate reason for different types (e.g., database
         representation vs API), document this and ensure the conversion is
         explicit and validated (don't silently convert)

    3. If this is a database type conversion:
       Consider moving the conversion into the persistence layer's model,
       so it returns objects with ProposalStatus already set:

       ```python
       # In persistence layer
       @dataclass
       class ProposalRecord:
           id: int
           status: ProposalStatus  # Already converted from DB
           created_at: datetime
           decided_at: datetime | None
           content: str
       ```

    Then the resource handlers become:
    ```python
    status=p.status,  # No conversion needed
    ```

    **Most likely:** This is the same issue as ApprovalOutcome/ApprovalStatus (finding 024)
    - there are duplicate enums that should be unified.
  |||,
}
