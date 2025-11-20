local I = import '../../specimens/lib.libsonnet';

// iss-011: test_policy_resources.py tests non-existent API functionality

I.issueOneOccurrence(
  rationale=|||
    The test file `adgn/tests/mcp/approval_policy/test_policy_resources.py` tests a policy
    CRUD API that was never implemented in the production code.

    **Problem:**
    The test imports and uses types that don't exist in the codebase:
    - `CreatePolicyArgs` - for creating policies via admin tools
    - `UpdatePolicyArgs` - for updating policies
    - `DeletePolicyArgs` - for deleting policies

    These test a full policy CRUD (Create, Read, Update, Delete) API that was apparently
    planned but never implemented. The actual ApprovalPolicyAdminServer only provides:
    - Proposal management: create_proposal, approve_proposal, reject_proposal
    - Policy operations: set_policy, validate_policy, reload_policy

    There is no separate "create_policy", "update_policy", or "delete_policy" tool/functionality.
    The test file appears to be a placeholder or leftover from an earlier design.

    **Evidence:**
    - Test imports CreatePolicyArgs, UpdatePolicyArgs, DeletePolicyArgs (line 11-14)
    - All test classes (TestPolicyListResource, TestPolicyDetailResource, TestCreatePolicyTool,
      TestUpdatePolicyTool, TestDeletePolicyTool, TestPolicyPagination, TestErrorHandling)
      reference these non-existent types
    - The production code uses a different model: policies are managed through proposals
      (create → approve) rather than direct CRUD operations

    **Resolution:**
    Delete this test file entirely. It tests functionality that was never built and would
    require significant production code implementation to make valid. The actual policy
    functionality is tested in test_policy_validation_reload.py and test_proposals_resources.py.

    **Alternative considerations:**
    - If direct policy CRUD is desired, it should be implemented in production code first
    - The test could be kept as a specification/TODO, but it's confusing to have failing
      tests for unimplemented features in the main test suite
    - Better to track this as a feature request in documentation rather than broken tests
  |||,
  properties=['test-validity', 'dead-code', 'technical-debt'],
  filesToRanges={
    'adgn/tests/mcp/approval_policy/test_policy_resources.py': [
      [1, 385],  // Entire file tests non-existent functionality
    ],
  },
)
