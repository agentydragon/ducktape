local I = import '../../specimens/lib.libsonnet';

// iss-032: ApprovalHub and ApprovalPolicyEngine methods should be inlined into MCP tools/resources

I.issueOneOccurrence(
  rationale= |||
    Several methods in ApprovalHub and ApprovalPolicyEngine are called ONLY by their
    corresponding MCP tools/resources, and nowhere else in production code:

    1. ApprovalHub.resolve() - only called by approve/reject tools (lines 142, 148)
    2. ApprovalPolicyEngine.set_policy() - only called by set_policy tool (lines 316, 322)
    3. ApprovalPolicyEngine.create_proposal() - only called by create_proposal tool (lines 337, 349)
    4. ApprovalPolicyEngine.approve_proposal() - only called by approve_proposal tool (lines 351, 366)
    5. ApprovalPolicyEngine.reject_proposal() - only called by reject_proposal tool (lines 367, 370)

    These are unnecessary abstractions - the methods exist solely to be called by
    their corresponding MCP tool, with no other callers.

    Fix: Inline these methods directly into their MCP tool/resource implementations.

    Example for ApprovalHub.resolve():

    Before:
    def resolve(self, call_id: str, decision: ...) -> None:
        pending = self._pending.pop(call_id, None)
        ...

    @self.tool()
    async def approve(...):
        self.resolve(call_id, decision)

    After:
    @self.tool()
    async def approve(...):
        # Inline resolve logic
        pending = self._pending.pop(call_id, None)
        ...

    Benefits:
    - Removes unnecessary indirection
    - Makes tool implementation self-contained and easier to understand
    - Reduces method count in the class
    - Clearer that this is the ONLY place this logic is used

    Note: Methods like await_decision(), get_policy(), load_policy(), and self_check()
    should NOT be inlined - they're called externally by production code.
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers', 'no-dead-code'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [142, 148],  // ApprovalHub.resolve() definition
      [316, 322],  // ApprovalPolicyEngine.set_policy() definition
      [337, 349],  // ApprovalPolicyEngine.create_proposal() definition
      [351, 366],  // ApprovalPolicyEngine.approve_proposal() definition
      [367, 370],  // ApprovalPolicyEngine.reject_proposal() definition
    ],
  },
)
