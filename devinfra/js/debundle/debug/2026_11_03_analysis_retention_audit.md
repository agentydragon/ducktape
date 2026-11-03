# Bound provider AST lifetime in Analyze

## Evidence

The completed public-engine Analyze invocation for `c7a6e4b1962bd84997995abc9a679c4a6300a06e` (#14092) reported a Claude peak of 8,125,001,728 bytes, above its inherited 7,500,000,000-byte budget. The same invocation reported 3,505,973,200 bytes for Tana. The temporary `EstimatedMemory` value remains 15,000,000,000 bytes. These are action-reported results from [invocation 2240e60a-39da-4ccc-b6b2-5324566b9fae](https://app.buildbuddy.io/invocation/2240e60a-39da-4ccc-b6b2-5324566b9fae); they do not attribute the peak to a particular retained object.

## Source-grounded lifetime change

`analyze_program` reparses unretained entries so the cross-module purity pass can inspect their bodies. The resulting `ReparsedEntry` values also provide the syntax contexts needed by the analysis-artifact codec. Before this change, those ASTs remained live through parallel per-chunk fact analysis because context ordinals were built only after every chunk's facts had been collected.

The code now builds the same `ContextOrdinals` table, over the same modules and in the same order, immediately after cross-module analysis, then drops the re-parsed entries before per-chunk analysis. `ContextOrdinals` stores context IDs and their ordinals, not AST references. Cross-module results are owned, so their later consumers do not depend on the re-parsed ASTs.

This shortens the AST lifetime without changing analysis scope, facts, context numbering, fallback behavior, or artifact schema. The change has not been memory-profiled. The reported invocation does not measure this source change, and no memory savings are claimed. It does not change the inherited budget or temporary estimate.

## Validation

The tested source was `origin/devel` `ca373dc4ad67a162a26b986106f327ea26875811` plus this patch. The focused command was:

```sh
nix develop -c bbr test //devinfra/js/debundle:lowering_test //devinfra/js/debundle/e2e:staged_analysis_test --test_output=errors
```

Both targets passed in invocation [ad9c9e0e-bcbb-413e-ba25-3f8574e73b86](https://app.buildbuddy.io/invocation/ad9c9e0e-bcbb-413e-ba25-3f8574e73b86). `lowering_test` was served from cache; `staged_analysis_test` executed and passed. The staged CLI comparison checks that analyzing to an artifact and consuming it produces the same reports as direct analysis. No new test was added.
