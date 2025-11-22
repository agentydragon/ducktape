{
  title: 'Unnecessary comments stating the obvious',
  severity: 'minor',
  category: 'code-quality',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [319],
      context: '# Call persistence to get ACTUAL ID',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [346],
      context: '# Create proposal and get actual database-assigned ID',
    },
  ],
  description: |||
    Two comments state obvious facts about database operations:

    **Line 319:**
    ```python
    # Call persistence to get ACTUAL ID
    self._policy_id = await self.persistence.set_policy(self.agent_id, content=source)
    ```

    **Line 346:**
    ```python
    # Create proposal and get actual database-assigned ID
    new_id = await self.persistence.create_policy_proposal(...)
    ```

    These comments add no value:
    1. "Call persistence" - obviously we're calling persistence, it's right there
    2. "get ACTUAL ID" / "actual database-assigned ID" - DUH. Obviously. We're professionals.
       We don't write bullshit code that invents random numbers or lies.

    The method names (`set_policy`, `create_policy_proposal`) and return types
    already make it clear that these return IDs. The comments just add noise.
  |||,
  recommendation: |||
    Delete both comments:

    **Before:**
    ```python
    # Call persistence to get ACTUAL ID
    self._policy_id = await self.persistence.set_policy(self.agent_id, content=source)
    ```

    **After:**
    ```python
    self._policy_id = await self.persistence.set_policy(self.agent_id, content=source)
    ```

    **Before:**
    ```python
    # Create proposal and get actual database-assigned ID
    new_id = await self.persistence.create_policy_proposal(...)
    ```

    **After:**
    ```python
    new_id = await self.persistence.create_policy_proposal(...)
    ```

    The code is self-documenting. If clarification is needed, it should explain
    *why* we're storing the policy or *what* the ID will be used for, not just
    repeat what the code obviously does.
  |||,
}
