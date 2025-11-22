local I = import '../../specimens/lib.libsonnet';

// iss-006: Explicit --config flag silently skipped if broken

I.issueOneOccurrence(
  rationale=|||
    If `load_config` is run with explicit `--config=<file>`, it will *silently skip it* if it's broken and instead potentially use other autodiscovered candidates, which (A) *discards user intent* despite the *explicit flags*, and (B) does so *silently*, without announcing any kind of error.

    `load_config` does:

    ```python
    candidates: list[Path] = []
    if config_path:
        candidates.append(config_path)  # <- from explicit commandline arg
    # ... add other candidates ...
    for cand in candidates:
      if cand.is_file():
          try:
              return cand, json.loads(cand.read_text())
          except Exception:
              pass
    ```

    If user explicitly sets `--config` and it fails to load, this silently skips it and moves on to other candidates, explicitly *and silently* violating user intent.

    If explicitly provided `--config` is not present or fails to load, code must fail fast and surface the error.
    Fallback discovery as in specimen would only be acceptable as "friendly default" when no explicit `--config` passed.
    (Motivating scary example: imagine a PII-holding server, with `--config=explicit_config.json`, `explicit_config.json` having `{"dangerous_pii_exposing_debug_switch"=false}` (type) and silently discovered fallback `random_debug_developer_config.json` setting it to `true`).
  |||,
  properties=['python/no-swallowing-errors', 'early-bailout'],
  filesToRanges={
    'pyright_watch_report.py': [
      12,  // config_path argument
      46,  // load_config function
    ],
  },
)
