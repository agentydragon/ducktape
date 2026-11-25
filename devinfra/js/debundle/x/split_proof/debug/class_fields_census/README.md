# Class-field allocation census

This investigation compares the tree before #20001 (`4413445e`) with the
identical tree committed as `f689082d`. The [N4 results](../../PLAN.md#proof-track)
report the counts and their limits.

`probe.patch.gz` contains a unified diff adding temporary test targets. The census uses `LinkedInput`'s
source-site numbering and linked unit lowerer, then runs `Expr.allocOnly` on
successfully lowered declaration initializers. It records source ordinals both
before and after the predicate change, alongside rejected and non-executable
units. Imported names come from the same `ModuleInput.importDecl` parser. The
probe uses the empty host profile for the debundle cases, `ecmascript` for the
library bundles, and `browser-page` for Excalidraw. Instance and static field
controls each gain one unit; a control with an effectful static initializer is
refused during lowering because its callee is an unsupported global. The probe
does not check cell ownership or claim that a qualifying unit is a valid pivot
in a particular linked state.

The debundle sources are the adapter's single-input case directories: it skips
multi-input cases and reports parse errors. The certificate comparison covers
all expected-accept requests in the saved diagnostic cohort, including the
`strict-*` cases. It runs the normal compiled checker, tries structural mode
first, and checks successful output from disk and against Node. The library and
Excalidraw checks use their existing pinned certificate tests.

To reproduce in clean checkouts at the two revisions, apply `probe.patch.gz` in
each checkout. `bbr` uploads new files only after `git add -N`:

```sh
gzip -dc /path/to/probe.patch.gz | git apply
git add -N devinfra/js/debundle/x/split_proof/lean/ClassFieldCensus.lean \
  devinfra/js/debundle/x/split_proof/integration/excalidraw/class_field_census_tmp.mjs
p=//devinfra/js/debundle/x/split_proof
# On the pre-#20001 checkout only, add --test_env=CENSUS_BASELINE=1.
bbr test --jobs=4 "$p/integration/excalidraw:class_field_census_tmp"
bbr test --jobs=4 "$p/debundle_cases:class_field_certificates_tmp" \
  "$p/debundle_cases:class_field_other_certificates_tmp" \
  "$p/integration/library_splits:library_certificate_test" \
  "$p/integration/excalidraw:excalidraw_scroll_certificate"
```

The census writes `class-field-census.json` to undeclared test outputs, with
one record per source and per-unit allocation ordinals. Each debundle test shard
writes `certification.jsonl`. Compare the `afterOrdinals` sets for the same
source on the two revisions and the `outcome` and structural `mode` by case ID.
The source names and AST ordinals, not just total counts, must agree. The
instrumented targets are diagnostics and are not part of the splitter.
