{
  title: 'proposals variable should be inlined in proposals_list resource',
  severity: 'minor',
  category: 'code-style',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [382, 393],
      context: 'proposals = await ...; return ProposalsList(proposals=[... for p in proposals])',
    },
  ],
  description: |||
    In the `proposals_list()` resource handler, the `proposals` variable is
    fetched and immediately consumed in a list comprehension:

    ```python
    async def proposals_list() -> ProposalsList:
        """List all policy proposals with status and timestamps."""
        proposals = await self.persistence.list_policy_proposals(self.agent_id)
        return ProposalsList(
            agent_id=self.agent_id,
            proposals=[
                ProposalDescriptor(
                    id=p.id,
                    status=ProposalStatus(p.status),
                    created_at=p.created_at,
                    decided_at=p.decided_at,
                )
                for p in proposals  # Used once here
            ]
        )
    ```

    The `proposals` variable is not reused - it should be inlined.
  |||,
  recommendation: |||
    Inline the persistence call directly in the list comprehension:

    ```python
    async def proposals_list() -> ProposalsList:
        """List all policy proposals with status and timestamps."""
        return ProposalsList(
            agent_id=self.agent_id,
            proposals=[
                ProposalDescriptor(
                    id=p.id,
                    status=ProposalStatus(p.status),
                    created_at=p.created_at,
                    decided_at=p.decided_at,
                )
                for p in await self.persistence.list_policy_proposals(self.agent_id)
            ]
        )
    ```

    This removes the unnecessary intermediate variable.
  |||,
}
