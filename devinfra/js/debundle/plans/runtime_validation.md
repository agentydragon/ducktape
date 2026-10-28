# Supplemental runtime inference validation

The existing-behavior runtime migration landed in
[#12762](https://github.com/agentydragon/ducktape/pull/12762), after the shared
static inference cohort and its runtime prerequisites. Current contracts and
one-way dependency boundaries are documented in `docs/inference.md`.

Completed acceptance:

- All 210 debundle targets pass; exact-head hosted CI passes production Clippy
  and all 183 selected test targets.
- Three alternating optimized pairs on each of four frozen controls stay
  below the 25% CPU/RSS budget. The largest median increases are 18.75% CPU
  and 7.38% peak RSS; emitted behavior and structured evidence match.
- The private Claude replay matches the baseline's rejection across all
  75 graphs and 11 conflict reports, apart from executable provenance.

The supplemental Tana baseline/candidate comparison is still running against
the same frozen source/spec and 58 package mappings. Local artifacts are under
`/tmp/debundle-runtime-baseline/corpus/runs/{baseline,candidate-compact}/tana`.
No completed Tana parity or performance claim is available.

After both runs finish, compare gate decisions, structured evidence and emitted
files, permitting only exact executable/path provenance differences. Record
rejections separately from successful emission and Node checks. Delete this
plan and the active `debug/runtime_transfers.md` investigation when that
comparison is resolved; durable ownership and completion constraints already
live beside the code and in `docs/inference.md`.
