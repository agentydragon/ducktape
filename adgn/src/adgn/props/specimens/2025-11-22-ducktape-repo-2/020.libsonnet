{
  title: 'Useless TYPE_CHECKING blocks with only pass',
  severity: 'minor',
  category: 'code-quality',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [31, 32],
      context: 'if TYPE_CHECKING:\\n    pass',
    },
    {
      path: 'adgn/src/adgn/agent/agent.py',
      lines: [43, 44],
      context: 'if TYPE_CHECKING:\\n    pass',
    },
  ],
  description: |||
    Two files contain TYPE_CHECKING blocks that only contain `pass`:

    **adgn/src/adgn/agent/approvals.py (lines 31-32):**
    ```python
    if TYPE_CHECKING:
        pass
    ```

    **adgn/src/adgn/agent/agent.py (lines 43-44):**
    ```python
    if TYPE_CHECKING:
        pass
    ```

    These blocks serve no purpose. TYPE_CHECKING is meant for type-only imports
    to avoid circular dependencies at runtime, e.g.:

    ```python
    if TYPE_CHECKING:
        from module import TypeOnlyNeeded
    ```

    An empty TYPE_CHECKING block with only `pass` does nothing and should be deleted.
  |||,
  recommendation: |||
    Delete both useless TYPE_CHECKING blocks:

    **adgn/src/adgn/agent/approvals.py** - remove lines 31-32
    **adgn/src/adgn/agent/agent.py** - remove lines 43-44

    If type-only imports are needed in the future, they can be added back.
    For now, these blocks just add noise.
  |||,
}
