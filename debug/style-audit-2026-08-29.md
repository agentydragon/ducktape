# `STYLE.md` audit follow-up

Audit scope: committed `HEAD` `9ae5a4fe3`, checked in an isolated clone on 2026-08-29.
This note records style debt; it does not claim remediation.

- Markdown has at least 429 unlabeled fenced blocks (MD040) across 140 active
  non-vendored/non-fixture files. The pre-commit hook only checks `cluster/` and
  `website/`.
- At least 41 local links use the prohibited `[path](path)` form.
- Typed Pydantic objects are converted to dictionaries internally in
  `openai_utils/text_extraction.py` and `openai_utils/model.py`.
- Broad exceptions are silently swallowed in
  `skills/freecad/examples/freecad_helpers.py` and
  `skills/reverse_engineer/evals/x/task.py`.
- `aiquota/AGENTS.md` does not begin with the sibling `README.md` transclusion.
- Grab-bag modules remain, including `devinfra/js/debundle/live_proxy/core.py`,
  `devinfra/wt/testing/utils.py`, and `py_detectors/utils.py`.
- Multiple entry points return numeric status codes and call `sys.exit(main())`,
  contrary to the `main() -> None` convention.
- Dynamic attribute probing remains in `mcp_infra/display/rich_display.py`; the
  file itself records a TODO to replace it with typed dispatch.

The original audit also found Gazelle drift in nine BUILD files and parser warnings
for two Python files. That finding no longer reproduces on the current `origin/devel`,
which contains the intervening Gazelle convergence commits.
