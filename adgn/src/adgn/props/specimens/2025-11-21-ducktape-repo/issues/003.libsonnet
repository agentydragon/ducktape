local I = import '../../specimens/lib.libsonnet';

// iss-003: Delete comment and inline status variable

I.issueOneOccurrence(
  rationale=|||
    The code has a comment explaining strict validation behavior and an intermediate
    variable that should be inlined.

    **Current code (lines 292-294):**
    ```python
    # Strict mapping; surface invalid data rather than swallowing
    status = ProposalStatus(raw)
    proposals.append(ProposalInfo(id=pid, status=status))
    ```

    **Should be:**
    ```python
    proposals.append(ProposalInfo(id=pid, status=ProposalStatus(raw)))
    ```

    **Why delete the comment:**
    - The comment states "Strict mapping; surface invalid data rather than swallowing"
    - But this is already obvious from the code: `ProposalStatus(raw)` will raise if invalid
    - Pydantic enum validation is strict by default - this isn't doing anything special
    - The comment adds no value beyond what the code already shows
    - If invalid data is passed, Pydantic will raise ValidationError - this is standard behavior

    **Why inline the status variable:**
    - `status` is used exactly once, immediately after creation
    - Variable name doesn't add semantic value beyond `ProposalStatus(raw)`
    - Single-use variable that should be inlined
    - Standard pattern for simple transformations

    **Pattern:**
    This is a common case where a comment explains "what the code does" rather than "why".
    The code is self-documenting - calling `ProposalStatus(raw)` on potentially invalid
    data will raise if it's invalid. No need to comment on standard Pydantic behavior.

    **Comparison with good comments:**
    Good comments explain WHY (business logic, workarounds, non-obvious choices).
    This comment just explains WHAT (validation happens), which is already clear from the code.
  |||,
  properties=['code-style', 'comments', 'simplicity'],
  filesToRanges={
    'adgn/src/adgn/agent/server/runtime.py': [
      [292, 294],  // Comment and status variable to inline
    ],
  },
)
