local I = import '../../specimens/lib.libsonnet';

// iss-003: Missing typed stubs for approval_policy tests

I.issueOneOccurrence(
  rationale=|||
    Approval policy tests need typed server stubs following the pattern used in other MCP tests.

    **Status: COMPLETED**

    Created `adgn/src/adgn/mcp/testing/approval_policy_stubs.py` with typed stubs:
    - `ApprovalPolicyServerStub` (reader with decide tool)
    - `ApprovalPolicyProposerServerStub` (create/withdraw proposals)
    - `ApprovalPolicyAdminServerStub` (approve/reject/set policy/validate/reload)

    These stubs follow the ServerStub base class pattern from exec_stubs.py and provide:
    - Type safety for tool arguments and return values
    - IDE completion
    - Clean API without accessing ._mcp_server internals

    Successfully integrated in test_policy_validation_reload.py (see issue 007).

    **Note**: test_policy_resources.py could not be updated as it tests non-existent
    functionality. See issue 011 for details.
  |||,
  properties=['test-quality', 'type-safety', 'maintainability'],
  filesToRanges={
    'adgn/src/adgn/mcp/testing/approval_policy_stubs.py': [
      [1, 53],  // Created stub file
    ],
  },
)
