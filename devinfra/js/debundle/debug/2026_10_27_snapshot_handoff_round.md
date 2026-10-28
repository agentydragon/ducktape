# Snapshot-backed worker and integration round

An independent lane worker exercised the updated handoff instructions on the
public fixture in `skills/shared/examples/fixture`. It received the complete
copied spec, source, graph and engine, plus a staged copy of the worker skill
and its references. It could write only its snapshot's module YAML subtree.
This was a public example, not a downstream large-corpus validation or a check
of the installed/released skills.

## Baseline and result

- Receiving source revision: `1696fd6d3b16df9236fd8f04f82b3b9259af3429`.
- Starting graph SHA-256:
  `d851699fc0b0d1482f94ff7c2b8ff0d9c25cbe3f2f2ba9009fa398ef4313e277`.
- Captured engine SHA-256:
  `3c03cd701a0bc9d0a2ffa6555acffff60ada7a70150fd6ef1b40c03ed7e37576`.
- The worker chose `main/incrementing_value.yaml`, preserving the three binding
  selectors from `values.yaml` and `calculation.yaml`; the merge deleted the
  source YAMLs and added its provenance note.
- Proposed and applied assignment gates passed. Baseline and regenerated full
  pipelines exited 0. Queries using the regenerated graph found three complete
  atomic groups with no missing or unknown bindings.
- The integrator verified its independent receiving snapshot and compared its
  starting graph identity before applying the reviewed YAML delta. Its combined
  pipeline exited 0. Worker and integrator source/generated Node runs exited 0
  with stdout `7\n` and empty stderr.

Raw trial evidence is under `/tmp/debundle-handoff-live/worker/` and
`/tmp/debundle-handoff-live/integration/evidence/` on the trial host. The
continuous acceptance case is
`//devinfra/js/debundle/skills/shared/examples:isolated_edit_test`; it runs the
packaged recipe, removes original spec/source/package inputs before the edit,
checks a recorded stale-graph refusal, and validates the integrated output with
Node. It publishes command evidence as Bazel undeclared outputs.

## Instruction findings

- `modules merge` accepts the selected chunk's module subtree, not
  `--tree-config`. The recipe now takes an explicit `--module-subdir`, supplied
  from the adapter's mapping rather than inferred from the chunk ID.
- The worker was not given a separate uniqueness command. The adapter must
  name the check supplying that proof, including when full pipeline resolution
  already covers it.
- The worker's first verification used combined terminal output instead of
  retaining separate process streams. The handoff contract now explicitly
  includes pre-edit verification evidence; the runnable recipe records its
  verification exit status, stdout and stderr separately.
