# Lint a specimen for conformance

Lint one specimen against the authoring rules. Target: `$ARGUMENTS` (a specimen
directory, e.g. `props/specimens/ducktape/2026-07-11-00`, or any file inside it).

Derive the rules from the docs at run time instead of from memory:
`props/specimens/AGENTS.md`, `props/specimens/docs/authoring_guide.md`,
`props/specimens/docs/format_spec.md`, and `props/specimens/docs/quality_checklist.md`.

Check every `issues/*.yaml` file unless asked to check only some. Read the cited
code where it lives: the snapshot's `code/` directory, or for an external
snapshot the `http_archive` its `BUILD.bazel` names. Confirm each cited path and
line range exists there and that the rationale describes only the frozen
snapshot.

Report findings grouped by file, each with the rule it breaks and a concrete
fix. Do not modify files without approval. The snapshot's own
`bbr test //props/specimens/<project>/<YYYY-MM-DD-NN>:test_specimen` checks the schema; the review above covers what the schema cannot.
