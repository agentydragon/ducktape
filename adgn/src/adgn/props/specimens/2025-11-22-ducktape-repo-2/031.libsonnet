{
  title: 'Duplicated "get proposal or raise KeyError" pattern should be extracted',
  severity: 'minor',
  category: 'code-duplication',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [357, 358],
      context: 'approve_proposal: if (got := ...) is None: raise KeyError',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [399, 401],
      context: 'proposal_detail: got = ...; if got is None: raise KeyError',
    },
  ],
  description: |||
    The "get proposal or raise KeyError if None" pattern appears twice:

    **approve_proposal() - lines 357-358:**
    ```python
    if (got := await self.persistence.get_policy_proposal(self.agent_id, proposal_id)) is None:
        raise KeyError(str(proposal_id))
    ```

    **proposal_detail() resource - lines 399-401:**
    ```python
    got = await self.persistence.get_policy_proposal(self.agent_id, id)
    if got is None:
        raise KeyError(f"Proposal {id} not found")
    ```

    This is code duplication. Both:
    1. Call `get_policy_proposal()`
    2. Check if result is None
    3. Raise KeyError with the proposal ID

    The "get or None" version (`get_policy_proposal`) might not even be used
    anywhere without this immediate None check. If that's the case, the
    persistence method itself should raise.
  |||,
  recommendation: |||
    **Option 1 (Preferred): Add a non-nullable method to persistence**

    Add a method that raises instead of returning None:

    ```python
    # In persistence layer
    async def get_policy_proposal_or_raise(
        self, agent_id: AgentID, proposal_id: int
    ) -> ProposalRecord:
        """Get policy proposal, raising KeyError if not found."""
        proposal = await self.get_policy_proposal(agent_id, proposal_id)
        if proposal is None:
            raise KeyError(f"Proposal {proposal_id} not found for agent {agent_id}")
        return proposal
    ```

    Then simplify call sites:

    **approve_proposal():**
    ```python
    got = await self.persistence.get_policy_proposal_or_raise(self.agent_id, proposal_id)
    # Self-check the proposal program before activation
    self.self_check(got.content)
    ```

    **proposal_detail():**
    ```python
    got = await self.persistence.get_policy_proposal_or_raise(self.agent_id, id)
    return ProposalDetail(
        id=got.id,
        status=ProposalStatus(got.status),
        ...
    )
    ```

    **Option 2: If nullable version is actually used, add local helper**

    If there are legitimate uses of the nullable `get_policy_proposal()`, add
    a local helper method:

    ```python
    async def _get_proposal_or_raise(self, proposal_id: int) -> ProposalRecord:
        """Get proposal or raise KeyError if not found."""
        got = await self.persistence.get_policy_proposal(self.agent_id, proposal_id)
        if got is None:
            raise KeyError(f"Proposal {proposal_id} not found")
        return got
    ```

    **Check if nullable version is actually needed:**
    If `get_policy_proposal()` is never called without immediately checking for
    None and raising, then it should just raise internally and the nullable
    version should be deleted.
  |||,
}
