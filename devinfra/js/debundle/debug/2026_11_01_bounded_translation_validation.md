# Bounded edge-TV diagnostics

## Measured issue

An existing canonical application report was 97,222,194 bytes (92.72 MiB):
320,519 distinct conflict-edge rows repeatedly rendered only 995 first and
915 later statement/module records. Compacting that full array alone still
costs 66,445,418 bytes. The default report needs bounded examples and exact
totals; complete evidence remains available explicitly. No post-change
canonical size or runtime improvement is claimed here.

## Transfer and retention boundary

The existing classification loop and emitted-order sweep emit each result
through one visitor boundary. Every result updates exact category totals and
conflict totals by dependency kind. The complete legacy conflict-pair set keeps
the previous report-ordinal mapping and feeds the existing effect-TV comparison.
Only diagnostic examples stop at twenty. Acceptance uses complete counters;
neither the gate oracle nor effect comparison uses sample length or emptiness.
No edge eligibility, loader ordering, source scope, premise, purity or gate
criterion changes.

`translation_validation.json` is a distinct bounded view: each evidence list
has `total`, `examples` and `truncated`. The optional
`--full-report translation-validation` sink writes every checker result to
`translation_validation.full.jsonl`; it does not run another checker or retain
a second full row vector. `--full-report-chunk` filters only sinks. Complete
diagnostic selection and graph selection are independent. Bazel forwards these
flags only to Pipeline, retaining the same Analyze action inputs and outputs.

The shared JSONL sink propagates final flush errors before a sibling final
summary is written. Its metadata advertises the export capability, without
claiming an unrequested file exists. Existing effect-conflict JSONL uses the
same sink. Atomic/cycle reports, stderr, entry banners and GATE_REJECTED
semantics are preserved. The exact pair set may still grow with real conflicts;
this change bounds expanded diagnostic rows, not all semantic memory.

## Validation

- Base `eba35ca9ad298fd0b76f8b810d8f04d28b43bfe9` production plus the same neutral
  CLI fixture failed with “default report retained more than20 full conflict
  rows,” before reaching the new export flag. This is the unbounded-default
  regression, not an unknown-option failure.
  [Expected failing control](https://app.buildbuddy.io/invocation/208ce77c-c2fa-4af6-9a5a-904c0181708c).
- Seven targets passed: TV differential, effect-TV, staged analysis, lowering,
  gate, pipeline and CLI tests. Controls cover complete rows beyond the sample,
  exact legacy-pair coverage, runtime output, invalid specs, invocation records,
  explicit/unknown/dry-run sink selection, final-flush failure and direct/staged
  equivalence. A multi-chunk filter comparison preserves every other output
  byte, including facts, census, summaries and emitted JavaScript.
  [Focused checks](https://app.buildbuddy.io/invocation/d013ebd0-8dde-4442-af67-f52f6c24351b).
- Six pipeline rule tests passed, including full-report shell quoting,
  report-only action flags, unchanged action inputs/output groups and retained
  reading-only status handling. The same command named gate, lowering, pipeline,
  CLI and output-layout libraries for normal Clippy/Rustfmt aspects.
  [Rule and library checks](https://app.buildbuddy.io/invocation/49964436-59ca-4a67-9a79-96a56b5b720e).

Canonical replay and hosted CI are separate evidence. Full export-reentry
presentation is a subsequent change; its semantic/staged report is untouched.
