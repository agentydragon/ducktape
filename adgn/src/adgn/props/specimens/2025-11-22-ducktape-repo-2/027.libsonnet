local I = import '../../specimens/lib.libsonnet';

// iss-027: Unnecessary comments stating the obvious

I.issueOneOccurrence(
  rationale= |||
    Two comments state obvious facts about database operations without adding value:

    Line 319: "Call persistence to get ACTUAL ID" - Obviously calling persistence
    (it's right there), obviously getting an actual ID (we're professionals, not
    writing code that invents random numbers).

    Line 346: "Create proposal and get actual database-assigned ID" - Same issue.
    The method names (set_policy, create_policy_proposal) and return types already
    make it clear that these return IDs.

    These comments just add noise. The code is self-documenting. If clarification
    is needed, it should explain WHY we're storing the policy or WHAT the ID will
    be used for, not just repeat what the code obviously does.

    Fix: Delete both comments. The method names and types are sufficient.
  |||,
  properties=['no-useless-docs'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      319,  // "Call persistence to get ACTUAL ID"
      346,  // "Create proposal and get actual database-assigned ID"
    ],
  },
)
