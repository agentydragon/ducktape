# Supplemental runtime inference validation

The supplemental canonical baseline/candidate comparison remains unresolved.
The former scratch location
`/tmp/debundle-runtime-baseline/corpus/runs/{baseline,candidate-compact}` is not
present in this environment, so this checkout has no evidence that the
comparison completed. Do not treat the prior performance acceptance or a
missing temporary directory as parity evidence.

Resolve the comparison only with preserved run outputs for both candidates.
Compare gate decisions, structured evidence, emitted files, and runtime checks;
report rejected runs separately from successful emission. Keep this plan and
`debug/runtime_transfers.md` until that evidence is available or the comparison
is explicitly superseded.
