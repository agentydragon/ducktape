local I = import '../../specimens/lib.libsonnet';

// iss-001: Test fixture and constant duplication in approval policy tests

I.issueOneOccurrence(
  rationale=|||
    The test file `test_proposals_resources.py` has significant duplication that should be
    refactored into fixtures and constants:

    **Policy content duplication:**
    - The "allow" policy (class ApprovalPolicy with decision="allow") is duplicated 3+ times
      across tests (lines 32-36, 60-64, 86-90, 159-163, 176)
    - The "deny_abort" policy appears at lines 127-131
    - The "deny_continue" policy appears at lines 164-168
    - No shared policy constants exist in the codebase for these common test policies

    **Client instantiation duplication:**
    - `ApprovalPolicyProposerServer(engine=approval_engine)` created in 6 different tests
    - `ApprovalPolicyAdminServer(engine=approval_engine)` created in 3 tests
    - `ApprovalPolicyServer(approval_engine)` (reader) created in 6 tests
    - Each wrapped individually with `make_typed_mcp` context manager

    **Recommended fixes:**
    1. Create module-level constants for common policy strings (POLICY_ALLOW, POLICY_DENY_ABORT,
       POLICY_DENY_CONTINUE)
    2. Create pytest fixtures or fixture factories for creating typed clients:
       - `proposer_client` fixture that yields configured proposer client
       - `admin_client` fixture that yields configured admin client
       - `reader_client` fixture that yields configured reader client
       - Or a fixture factory like `make_policy_client(role)` that returns appropriate client
    3. This reduces test boilerplate and makes tests focus on behavior rather than setup

    The duplication makes tests harder to maintain - changes to policy format or client
    initialization patterns require updates in many places.
  |||,
  properties=['test-quality', 'dry-principle', 'maintainability'],
  filesToRanges={
    'adgn/tests/mcp/approval_policy/test_proposals_resources.py': [
      [25, 25],   // proposer creation
      [32, 36],   // POLICY_ALLOW duplication 1
      [44, 44],   // reader creation
      [59, 59],   // proposer creation
      [60, 64],   // POLICY_ALLOW duplication 2
      [71, 71],   // reader creation
      [86, 90],   // POLICY_ALLOW duplication 3
      [93, 93],   // proposer creation
      [99, 99],   // admin creation
      [106, 106], // reader creation
      [127, 131], // POLICY_DENY_ABORT
      [134, 134], // proposer creation
      [140, 140], // admin creation
      [147, 147], // reader creation
      [159, 168], // POLICY_ALLOW + POLICY_DENY_CONTINUE
      [171, 171], // proposer creation
      [181, 181], // admin creation
      [187, 187], // reader creation
      [204, 204], // reader creation
    ],
  },
)
