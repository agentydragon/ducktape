# Debundle pipeline and materialization performance

## `debundle run` pipeline

The `debundle run` pipeline wall is a different optimization surface
from residual proposal work. Current unmeasured opportunities:

- **AST-hash codegen cache**: content-address the post-lowering AST and
  reuse SWC emit output when unchanged.
- **Chunk-level incremental rebuild**: hash `(upstream_bytes, spec_slice,
ducktape_version)` per chunk and skip lowering, codegen, and reports
  for unchanged chunks.
- **Opt-in heavy reports**: add `--reports=<list>` so consumers can skip the
  per-chunk reports they do not need (`artifacts/output_layout.rs` lists them).

### Materialize-stage hot-loop optimizations

Ordered by leverage. Re-profile before implementation if the consumer
corpus or pipeline shape has changed materially.

1. **Re-profile / shrink `artifact::write_tree_reports`.** A previous
   profile showed 6.42% Children % here, dominated by serde_json
   pretty-print of `DirectoryManifestIndex` / `DirectoryBoundarySummary`.
   Generated reports now use compact JSON; re-profile before more work,
   then shrink the on-wire shape if this path remains hot.
2. **`vendor::strip::sweep_unreachable_top_level`.** 6.40% Children % in
   the prior profile. Likely amenable to indexed reachability or
   per-chunk caching.
3. **Keep harness emission proportional to the work.** Most remaining
   `emit_browser_harness` cost is `materialize_artifact_scripts` →
   `write_tree_reports` (item 1), not the harness JS emission. Split
   browser-harness generation from non-browser runs where practical, and
   avoid recopying unchanged non-JS assets.
4. **AST visit churn in `prepare_js_chunks`.** SWC parser / lexer /
   `visit_children_with` still occupy ~10–15% summed across many
   sub-2.5%-self entries. No single parser symbol is over the priority
   threshold; revisit after items 1–2.

### Graph pass performance and module boundaries

Tighten before the next large peel loop:

- Keep stage telemetry complete (index build/rebuild, fused AST
  analysis, purity, dependency graph construction, atomic-DAG construction,
  quotient construction, validation, lowering, output writing) — useful
  durations should land in the emitted reports.
- Add focused regression coverage for `ArtifactIndexes` rebuild
  boundaries as more structural artifact mutations are optimized.
- Profile the debundle action around `materialize_logical_modules` and
  `apply_emission_rewrites_in_place`; avoid whole-graph clone/rescan patterns
  where a graph pass or indexed lookup can answer the same question.
- Consider changing per-chunk `file_records` from an ordered vector of
  `(file, role)` pairs into a typed map if output consumers do not depend
  on order. Keep the manifest easy to diff and read.

## Avoid

- Do not replace the maintained condensation order with a cache over stale
  SCCs. Ownership batches can both add and remove quotient edges; keep exact
  overlay handling for removals within an SCC, and compare every fast path with
  the canonical complete gate.
