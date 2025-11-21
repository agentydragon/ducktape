local I = import '../../specimens/lib.libsonnet';

// iss-019: Use Path.read_text() instead of open() with manual read

I.issueOneOccurrence(
  rationale=|||
    The code uses `open()` with manual read when `Path.read_text()` is cleaner and more idiomatic.

    **Current code (lines 52-53):**
    ```python
    with open(python_file) as f:
        code = f.read()
    ```

    **Should be:**
    ```python
    code = python_file.read_text()
    ```

    **Why Path.read_text() is better:**
    - `python_file` is already a `Path` object (line 47 signature shows `Path`)
    - `Path.read_text()` handles encoding automatically (defaults to locale encoding)
    - More concise - one line instead of two
    - Consistent with line 174 which already uses `output_file.write_text(ts_code)`
    - No need for manual context manager
    - More idiomatic modern Python

    **Context preservation:**
    The current code executes the file with `exec(code, namespace)` at line 54, so the code
    string is still needed. This is not about removing the intermediate variable, just using
    the cleaner Path API.
  |||,
  properties=['code-style', 'readability', 'consistency'],
  filesToRanges={
    'adgn/scripts/generate_frontend_code.py': [
      [52, 53],  // open() with manual read instead of Path.read_text()
      [174, 174], // Line already using write_text() for comparison
    ],
  },
)
