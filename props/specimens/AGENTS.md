@README.md

## Agent Instructions

Read only the specimen docs needed for the task:

- Format/schema questions: <docs/format_spec.md>
- Authoring guidance: <docs/authoring_guide.md>
- Pre-commit review checklist: <docs/quality_checklist.md>
- Build and DB sync mechanics: <docs/build_and_sync.md>
- Labeled `match_file_restriction` examples: <docs/only_matchable_labels.md>

Specimens are immutable training data. Do not update issue files to say an issue
was fixed, completed, or superseded; create a new snapshot for a later commit
instead.

When authoring or editing a snapshot:

- Finish the investigation before writing labels; no open questions in issue
  files.
- Keep one logical issue type per YAML file, grouping repeated occurrences in
  `occurrences`.
- Cite only files and line ranges present in the frozen snapshot.
- Validate with `bbr test //props/core:test_production_specimens`.
