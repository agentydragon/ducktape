local I = import '../../specimens/lib.libsonnet';

// iss-024: PolicyProposalInfo proposal_uri field is computable from id and should be removed

I.issueOneOccurrence(
  rationale=|||
    The `proposal_uri` field in `PolicyProposalInfo` can be trivially computed from the `id` field
    and should be removed to avoid redundancy.

    **Current code (lines 171-178):**
    ```python
    class PolicyProposalInfo(BaseModel):
        """Policy proposal metadata with URI to full content."""

        id: str
        status: ProposalStatus
        created_at: datetime
        decided_at: datetime | None = None
        proposal_uri: str  # URI to access full proposal content in policy server
    ```

    **Why this is problematic:**

    1. **Trivially computable**: The `proposal_uri` follows a deterministic pattern from `id`:
       ```
       proposal_uri = f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/{id}"
       ```
       or similar pattern. Clients can compute this in one line.

    2. **Redundant information**: Storing both `id` and `proposal_uri` violates DRY principle when
       one can be derived from the other.

    3. **Inconsistency risk**: If the URI pattern changes, must update both the construction logic
       AND this field. Easy to get out of sync.

    4. **Bloats payloads**: Every `PolicyProposalInfo` object carries this redundant URI string,
       wasting bandwidth when listing many proposals.

    5. **Inconsistent approach**: The codebase uses IDs as the primary identifier elsewhere, not URIs.
       Mixing both creates confusion about which is the canonical identifier.

    **Recommended fix:**

    Remove `proposal_uri` field:

    ```python
    class PolicyProposalInfo(BaseModel):
        """Policy proposal metadata with URI to full content."""

        id: str
        status: ProposalStatus
        created_at: datetime
        decided_at: datetime | None = None
    ```

    **Client-side change** (if clients need the URI):
    ```python
    # Construct URI from ID
    proposal_uri = f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/{proposal.id}"
    ```

    Or define a helper function/property if needed frequently.

    **Benefits:**
    - Single source of truth for URI patterns
    - Smaller response payloads
    - No risk of URI being out of sync with ID
    - Consistent with using IDs as primary identifiers
    - Less code to maintain

    **Note:**
    The alternative mentioned by the user (consistently using URIs instead of IDs everywhere in MCP
    protocol) would make things quite complicated and is not recommended. The simpler solution is
    to use IDs and let clients construct URIs when needed.
  |||,
  properties=['redundancy', 'data-modeling', 'dry-principle'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [178, 178],  // Redundant proposal_uri field
    ],
  },
)
