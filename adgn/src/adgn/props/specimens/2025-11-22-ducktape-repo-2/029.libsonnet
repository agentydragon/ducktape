local I = import '../../specimens/lib.libsonnet';

// iss-029: proposals variable should be inlined in proposals_list resource

I.issueOneOccurrence(
  rationale= |||
    In the proposals_list() resource handler (lines 382-393), the proposals variable
    is fetched and immediately consumed in a list comprehension without reuse:

    proposals = await self.persistence.list_policy_proposals(self.agent_id)
    return ProposalsList(
        agent_id=self.agent_id,
        proposals=[... for p in proposals]  # Used once here
    )

    The proposals variable is not reused and should be inlined directly in the
    list comprehension.

    Fix: Inline the persistence call:

    return ProposalsList(
        agent_id=self.agent_id,
        proposals=[
            ProposalDescriptor(...)
            for p in await self.persistence.list_policy_proposals(self.agent_id)
        ]
    )

    This removes the unnecessary intermediate variable.
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [382, 393],  // proposals_list resource with one-off proposals variable
    ],
  },
)
