# Staged analysis and incremental application

The existing analysis identity and Bazel reuse contract is in
<../docs/bazel_integration.md>; native-premise source identity is specified in
<../SPEC.md#native-integrity-inputs> and <../docs/wire_format.md>.

## Current boundary

`debundle_analysis` already shares one `.analysis` artifact among apply
targets with the same canonical projection. Its header binds the analysis
engine, projection, and parser-input digests; its body stores the program-wide
summary once and the per-chunk facts. [`analyze_program`](../staged_analysis/mod.rs)
prepares source, resolves the selected static-import closure, computes the
cross-module summary, then computes each selected chunk's facts. Apply in
[`materialize_logical_modules`](../lowering/mod.rs) still prepares source,
reconstructs AST contexts, and resolves selectors; it checks the completed
projection before consuming the restored facts. This is
semantic-analysis reuse, not a parse-free apply cache.

The saved fact codec in
[`analysis_artifact.rs`](../staged_analysis/analysis_artifact.rs) reconstructs
source contexts against freshly parsed ASTs. [`SourceWithAst`](../artifacts/artifact.rs)
is a read-only in-process representation: mutable AST access promotes it and
invalidates the retained source pairing, and analysis releases it when those
contexts are no longer needed. Persistent caches must therefore store stable
facts and source identities, not SWC ASTs, `Id`s, spans, or context ordinals.

## Next architecture

Retain the existing whole-analysis artifact as the compatibility boundary while
introducing explicit immutable stage results:

1. **Chunk-local source facts:** capture parser-derived imports, exports, shallow
   runtime facts, and local source-bound evidence under a versioned identity
   containing the analysis-engine closure, canonical chunk and file identity,
   exact parser-input digests, and every loader, vendor, or package input read
   by that stage. A cache hit may avoid recomputing these facts; it does not
   avoid parsing where selector resolution or AST-context reconstruction still
   needs the AST.
2. **Program-wide join:** resolve export/import chains, callable and purity
   summaries, native integrity, and loader/reentry premises from the exact
   participating chunk facts and semantic projection. The key records the
   selected static-import closure and its resolved edges. Whole-bundle checks,
   including blockers derived from chunks outside that closure, also bind the
   complete bundle manifest; they must not inherit a closure-only key.
3. **Per-chunk analysis:** derive each chunk's analysis facts from its local
   facts, the accepted program-summary identity, and that chunk's exact
   projected premises. Start by recomputing this join and all dependent chunk
   facts when any global input changes. Reuse only unchanged local facts until
   dependency-level invalidation has its own proof.
4. **Application:** keep selector resolution and its joint solve, ownership
   planning, lowering, gate validation, and whole-program emission rewrites
   downstream of source analysis. Their identity includes the selected chunks,
   module assignments, renames, ownership subset, graph options, vendor
   resolution, and cross-chunk references they consume. Do not cache emitted
   files by chunk name alone: final imports and rewrites can depend on other
   chunks.

For every stage, a missing cache entry is a miss. An entry with a different
schema, engine, source manifest, projection, or dependency identity is never
accepted. Keep canonical projection equality, parser-input checks, provenance,
and native-integrity validation mandatory; cache presence grants no authority.
Only add a finer invalidation edge when the stage's complete read set is
explicit and a changed provider or import edge invalidates every dependent
result.

## Work and acceptance gates

Keep each step independently reviewable and land it only after its own evidence:

1. Profile representative maintained corpora with the existing optimized
   profiler workflow. Record cold and warm runs, end-to-end latency, peak RSS,
   artifact bytes, and action/cache behavior for an unchanged build, a local
   source edit, a provider or import-edge edit, a premise/vendor edit, and a
   module-layout-only edit. Do not add ad hoc timing code or claim a speedup
   from a synthetic workload or cache-hit count alone.
2. Extract explicit local-fact and program-join boundaries without persistent
   reuse. Compare the staged path with fresh analysis for facts, diagnostics,
   gate verdicts, emitted JavaScript, and requested reports/graphs.
3. Add versioned reuse for chunk-local facts. Exercise additions, removals,
   source edits, changed loaders/packages, and engine/schema changes; verify
   each produces a miss for every affected fact and preserves hits for
   unaffected local inputs. A corrupt or incomplete entry must trigger a
   clean recompute or a clear failure, never partial acceptance.
4. Consider reuse of global summaries, per-chunk analysis, or applied outputs
   in separate changes only after the prior invalidation map is complete.
   Compare cache-on output with a cold recompute byte-for-byte where the format
   is deterministic, and structurally otherwise. Retain only stages whose
   repeatable end-to-end benefit for the observed edit mix exceeds lookup,
   encoding, storage, and memory costs without worsening cold builds.

Per-chunk S3 actions and a separate vendor-strip action remain deferred until
these measurements show that their added action and artifact costs pay back.
