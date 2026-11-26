# Class-field and blocker census

This investigation compares the tree before #20001 (`4413445e`) with the
identical tree committed as `f689082d`. The [N4 results](../../PLAN.md#proof-track)
report the allocation counts and certificate comparison. The [blocker census](blockers.md)
ranks overlapping syntax features and their combinations.

The first probe omitted opaque-capture annotations. The corrected probe reparses
sources with `includeOpaqueCaptures: true`, as the compiled checker's module
input does. It uses the `LinkedInput.censusUnits` routine from #20006 at commit
`3025b087`, including fallback to retained opaque units, and runs
`Stmt.allocs` and `Stmt.inert` on each result. It does not link providers or
check whether an individual allocation is a valid pivot in a particular state.
The probe uses the empty host profile for the debundle cases, `ecmascript` for
the library bundles, and `browser-page` for Excalidraw; its positive controls
cover ordinary instance and static fields. Do not compare these numbers with a
census using the test-only Node recording profile.

The debundle sources are the adapter's single-input case directories: it skips
multi-input cases and reports parse errors. The certificate comparison covers
all expected-accept requests in the saved diagnostic cohort, including the
`strict-*` cases. It runs the normal compiled checker, tries structural mode
first, and checks successful output from disk and against Node. The library and
Excalidraw checks use their existing pinned certificate tests.

`probe.patch.gz` is a unified diff adding temporary test targets. It also adds
the unmerged #20006 census routine to the two pinned revisions. In clean
checkouts at those revisions, apply it and mark the three new files for upload:

```sh
gzip -dc /path/to/probe.patch.gz | git apply
git add -N devinfra/js/debundle/x/split_proof/lean/ClassFieldCensus.lean \
  devinfra/js/debundle/x/split_proof/integration/excalidraw/class_field_census_tmp.mjs \
  devinfra/js/debundle/x/split_proof/integration/excalidraw/class_features_tmp.mjs
p=//devinfra/js/debundle/x/split_proof
# On the pre-#20001 checkout only, add --test_env=CENSUS_BASELINE=1.
bbr test --jobs=4 "$p/integration/excalidraw:class_field_census_tmp"
bbr test --jobs=4 "$p/debundle_cases:class_field_certificates_tmp" \
  "$p/debundle_cases:class_field_other_certificates_tmp" \
  "$p/integration/excalidraw:excalidraw_scroll_certificate"
```

The historical library certificate target has since been retired; the current
`library_smoke_test` runs only the original Acorn and semver runtime probes.

The census writes `class-blockers.json` to undeclared test outputs. The archived
[blockers.json.gz](blockers.json.gz) is the after-#20001 output; it records each
class-bearing source unit, every selected syntax feature, standard-global names,
eager and deferred class references, allocation and inert status, and lowering
errors. Each debundle certificate test shard writes `certification.jsonl`.
Compare the `afterOrdinals` sets for the same source on the two revisions and
the certificate `outcome` and structural `mode` by case ID. The source names
and AST ordinals, not just total counts, must agree. The instrumented targets
are diagnostics and are not part of the splitter.
