local I = import '../../specimens/lib.libsonnet';

I.issueOneOccurrence(
  rationale= |||
    Two files contain TYPE_CHECKING blocks that only contain `pass`, serving no purpose:

    ```python
    if TYPE_CHECKING:
        pass
    ```

    TYPE_CHECKING blocks exist to enable type-only imports that avoid circular dependencies at runtime. A typical use looks like:
    ```python
    if TYPE_CHECKING:
        from module import TypeOnlyNeeded
    ```

    Empty TYPE_CHECKING blocks with only `pass` are dead code - they add noise without providing any functionality. They may have been placeholders that were never filled in, or had imports removed without deleting the block itself.

    **Fix:**
    Delete both empty TYPE_CHECKING blocks:
    - adgn/src/adgn/agent/approvals.py lines 31-32
    - adgn/src/adgn/agent/agent.py lines 43-44

    If type-only imports are needed in the future, they can be added back with actual imports. These empty blocks provide no value.
  |||,
  properties=['no-dead-code'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [31, 32],  // if TYPE_CHECKING: pass
    ],
    'adgn/src/adgn/agent/agent.py': [
      [43, 44],  // if TYPE_CHECKING: pass
    ],
  },
)
