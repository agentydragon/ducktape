local I = import '../../specimens/lib.libsonnet';

// iss-014: Proposals list building should use list comprehension in runtime.py

I.issueOneOccurrence(
  rationale=|||
    The `proposals` list is built imperatively using a for loop with `append()`:

    ```python
    proposals: list[ProposalInfo] = []
    # Load proposals from persistence policy store
    if self._persistence is not None and self.agent_id:
        rows = await self._persistence.list_policy_proposals(self.agent_id)
        for r in rows:
            pid = str(r.id)
            raw = str(r.status)
            proposals.append(ProposalInfo(id=pid, status=ProposalStatus(raw)))
    ```

    This is non-idiomatic Python. List comprehensions are the Pythonic way to transform
    an iterable into a list.

    Fix - refactor to use a list comprehension:

    ```python
    # Load proposals from persistence policy store
    proposals: list[ProposalInfo] = []
    if self._persistence is not None and self.agent_id:
        rows = await self._persistence.list_policy_proposals(self.agent_id)
        proposals = [
            ProposalInfo(id=str(r.id), status=ProposalStatus(str(r.status)))
            for r in rows
        ]
    ```

    Or even more concisely using conditional expression:
    ```python
    proposals = (
        [
            ProposalInfo(id=str(r.id), status=ProposalStatus(str(r.status)))
            for r in await self._persistence.list_policy_proposals(self.agent_id)
        ]
        if self._persistence is not None and self.agent_id
        else []
    )
    ```

    This eliminates:
    - Intermediate variables (`pid`, `raw`)
    - Manual list initialization and append
    - The imperative loop pattern
  |||,
  properties=['python/modern-python-idioms', 'no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/src/adgn/agent/server/runtime.py': [
      [267, 274], // proposals list building with for loop
    ],
  },
)
