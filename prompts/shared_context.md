# Shared Context for Code Quality Scans

Two files own the durable rules, so this one stays short and every scan starts from the same
place without restating them:

- [Scan Prompt Philosophy](PHILOSOPHY.md) - how to run a scan: gather every candidate with
  automation, verify each in context, fix only what verification confirms, and weigh a tool's
  recall against its precision to decide how much manual reading a pattern needs.
- [STYLE.md](../STYLE.md) - the house rules that make something a finding. Each file in
  [scans/](scans/README.md) operationalizes one of them; the
  [repository parsimony goal](../x/goals/parsimony.md) says why removing it is worth the change.

## Before proposing a change

- Read the library's own types before casting them or probing with `hasattr`. The OpenAI SDK,
  Pydantic and SQLAlchemy ship usable annotations, and
  [library_type_misuse.md](scans/library_type_misuse.md) shows where to look them up. Parse with
  `Model.model_validate` rather than `TypeAdapter`.
- Prefer a function that raises over a getter that returns `None` when it cannot do its job.
- Search for an existing helper before adding one.
- Keep a comment or docstring only when it says something the code cannot;
  [useless_comments_and_docs.md](scans/useless_comments_and_docs.md) weighs that.

## Running the tools

- The scanners in `scans/` are stdlib-only Python exposed as Bazel targets: run
  `bb run //prompts/scans:<name>_bin -- [directory]` from the repository root.
- `ruff` and mypy run as Bazel aspects, so a proposed fix is checked by the same tools CI
  applies to every other change.
