local I = import '../../specimens/lib.libsonnet';

// iss-030: ProposalStatus(p.status) redundant conversion suggests type inconsistency

I.issueOneOccurrence(
  rationale= |||
    Both resource handlers convert p.status and got.status to ProposalStatus:

    Line 388: status=ProposalStatus(p.status)
    Line 405: status=ProposalStatus(got.status)

    This conversion is necessarily redundant:

    Case 1: If p.status and got.status are already ProposalStatus, then
    ProposalStatus(p.status) is a no-op that should be p.status directly.

    Case 2: If p.status is a different type (e.g., string or database enum),
    this indicates a type inconsistency that should be fixed upstream.

    Similar to finding 024 (ApprovalOutcome vs ApprovalStatus), this suggests
    ProposalStatus might have a duplicate in the persistence layer, requiring
    conversion at the boundary.

    Fix options:
    1. If already ProposalStatus: remove conversion, use status=p.status
    2. If persistence returns different type: unify types - make persistence
       return ProposalStatus directly, OR move conversion into persistence
       layer's model so it returns objects with ProposalStatus already set
    3. Most likely: duplicate enums that should be unified

    This is a type correctness issue - types should match at boundaries
    without runtime conversion.
  |||,
  properties=['type-correctness-and-specificity'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      388,  // proposals_list: status=ProposalStatus(p.status)
      405,  // proposal_detail: status=ProposalStatus(got.status)
    ],
  },
)
