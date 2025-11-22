local I = import '../../specimens/lib.libsonnet';

// iss-011: Do not silently swallow config read/parse errors

I.issueOneOccurrence(
  rationale=|||
    Do not silently swallow config read/parse errors.

    `load_config` iterates a list of candidate config files and attempts to read/parse the first that exists. On a read or JSON-parse error it currently swallows the exception and continues to the next candidate, which silently discards explicit user intent when `--config` is provided and hides real problems in configuration files.

    Broken candidate files are rare and likely indicate a problem the user should see. Let these failures surface instead of skipping to another candidate.

    Before (lines 46–51):

    ```python
    for cand in candidates:
        if cand.is_file():
            try:
                return cand, json.loads(cand.read_text())
            except Exception:
                pass
    return None, {}
    ```

    After (recommended): either let the exception propagate so the program fails loudly, or catch and re-raise with context (do not silently continue):

    Option A — fail fast (preferred):
    ```python
    for cand in candidates:
        if cand.is_file():
            return cand, json.loads(cand.read_text())
    return None, {}
    ```

    Option B — preserve context and fail loud:
    ```python
    for cand in candidates:
        if cand.is_file():
            try:
                return cand, json.loads(cand.read_text())
            except (OSError, json.JSONDecodeError) as e:
                raise RuntimeError(f"Failed reading config {cand}: {e}") from e
    return None, {}
    ```

    Either option makes configuration problems visible to users and avoids silently violating explicit `--config` intent.
  |||,
  properties=['python/no-swallowing-errors'],
  filesToRanges={
    'pyright_watch_report.py': [[46, 51]],
  },
)
