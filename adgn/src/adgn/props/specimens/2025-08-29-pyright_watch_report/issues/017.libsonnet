local I = import '../../specimens/lib.libsonnet';

// iss-017: Condense and de-duplicate config printing

I.issueOneOccurrence(
  rationale=|||
    Condense and de-duplicate config printing.

    The code prints the config path with an if/else that can be expressed more concisely without losing clarity. A single expression using `or` is shorter and avoids branching noise.

    Before:
    ```python
    if cfg_file:
        print(f"config: {cfg_file}")
    else:
        print("config: <not found, using defaults>")
    ```

    After (shorter):
    ```python
    print(f"config: {cfg_file or '<not found, using defaults>'}")
    ```

    This is a readability-focused micro-refactor: it reduces branching for a simple, readable output and keeps intent clear.
  |||,
  properties=['python/modern-python-idioms'],
  filesToRanges={
    'pyright_watch_report.py': [[259, 262]],
  },
)
