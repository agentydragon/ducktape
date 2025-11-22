local I = import '../../specimens/lib.libsonnet';

// iss-031: Duplicated "get proposal or raise KeyError" pattern should be extracted

I.issueOneOccurrence(
  rationale= |||
    The "get proposal or raise KeyError if None" pattern appears twice:

    Lines 357-358 (approve_proposal):
    if (got := await self.persistence.get_policy_proposal(...)) is None:
        raise KeyError(str(proposal_id))

    Lines 399-401 (proposal_detail):
    got = await self.persistence.get_policy_proposal(...)
    if got is None:
        raise KeyError(f"Proposal {id} not found")

    This is code duplication. Both:
    1. Call get_policy_proposal()
    2. Check if result is None
    3. Raise KeyError with the proposal ID

    The "get or None" version (get_policy_proposal) might not be used anywhere
    without this immediate None check. If that's the case, the persistence
    method itself should raise.

    Fix options:
    1. Preferred: Add get_policy_proposal_or_raise() to persistence layer that
       raises KeyError instead of returning None
    2. Alternative: Add local helper method _get_proposal_or_raise()
    3. Check if nullable version is actually needed - if never called without
       the None check, delete it and make the main method raise

    This simplifies call sites to: got = await persistence.get_policy_proposal_or_raise(...)
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [357, 358],  // approve_proposal: get + None check + raise
      [399, 401],  // proposal_detail: get + None check + raise
    ],
  },
  gap_note= |||
    This finding illustrates a pattern that could be a property: "extract-repeated-guards"
    or "consolidate-validation-patterns".

    When the same validation/guard pattern (get + None check + raise) appears at
    multiple call sites, extract it into:
    - A helper method at the appropriate layer (preferred for API methods)
    - A local helper (for internal use only)
    - Or strengthen the underlying API to handle the case (e.g., non-nullable variant)

    Related to DRY and "no-oneoff-vars-and-trivial-wrappers" but specifically
    about consolidating guard/validation patterns that repeat across call sites.
  |||,
)
