# Staged pipeline

Split `debundle run` into an analysis stage and a run stage, so an edit that
does not change what analysis reads reuses a cached analysis artifact. Stage
outputs are Bazel action outputs, keyed by content and shared through the
remote cache. There is no in-process or ad-hoc on-disk cache. This plan burns
down as its PRs land.

## Problem

Agents iterate on specs. Most edits change module assignment, co-location or
`commuting_statements`, none of which change chunk analysis, yet every run
recomputes all of it. Measured on `devel` `a7520fb884` (`-c opt`, local host,
gaffer-private `origin/main` `7b2d174f` specs; [§ Measurements](#measurements)):

| Input                             | Wall to end of materialize | Analysis share                   | Peak RSS |
| --------------------------------- | -------------------------- | -------------------------------- | -------- |
| Tana (1 chunk plus vendor)        | 259–265 s                  | 238–263 s (S2 84–97, S3 154–166) | 4.6 GiB  |
| Claude CLI (76 chunks), full emit | 81.5 s (86.7 s total)      | 66 s (S2 40, S3 26 wall)         | 8.6 GiB  |

## Stage map

Stage names follow the code, not the original proposal. "Selector solve" is
`prepare_logical_chunk` → `solve_projected_chunks` → `claim_prepared_chunk`
(`lowering/mod.rs`).

| Stage                          | Code                                                                                                                                                                                                           | Reads                                                                                                                               | Tana                         | Claude                               |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- | ------------------------------------ |
| S0 prepare                     | `pipeline.rs::prepare_transform`: parse every chunk, `build_vendor_resolution_plan`                                                                                                                            | sources, `inputs`, vendor marks, package roots                                                                                      | 1.6 s                        | 3.8 s                                |
| S1 structural + selector solve | `collect_chunk_source_facts`, selector solve                                                                                                                                                                   | source; all module YAML (anchors couple modules)                                                                                    | 3.4 s                        | 1.3 s                                |
| S2 program-wide                | `export_policies`, `export_reentry_reports` (loader derivation, closure blockers), `cross_module::collect_cross_module_imported_purities` (provider execution indexes, callable summaries, premise resolution) | sources, `host_behavior`, `program_premises`, `chunk_export_purity`, premise lists, vendor plan; today also assignment (C1, C3, C4) | 0.2 s + 84–97 s              | 7.5 s + 40 s                         |
| S3 per-chunk analysis          | `finish_logical_chunk` → `complete_chunk_structural_with_inputs` → `analyze_chunk_with_policy` (one `IterativeInference` context: interpreter and purity)                                                      | chunk source, S2 output, annotation premises, analysis half of `chunk_analysis_options`                                             | 154–166 s                    | 26 s wall, 58 s CPU                  |
| S4 graph and gate              | `compute_dependency_graph_and_atomic_groups_with`, rebind folds, `builder.finalize`, `AssignmentAnalysis`, `validate_and_emit_reports`                                                                         | S3 facts, `commuting_statements`, `dataflow_aware_s_chain`, assignment                                                              | 4.1–4.7 s graph + 4.0 s gate | 7.4 s + 1.8 s (`cli`, critical path) |
| S5 lower and emit              | `lower_chunk`, naturalize, emission rewrites (including vendor strip), prune, `ReconstructionPlan`, `write_js_tree`                                                                                            | S4 placement, `chunk_renames`, output config                                                                                        | 5.0 s lower; emit see below  | 0.3 s + 5.2 s                        |

S1 and S2 run once per program; S3, S4 and the lowering half of S5 run per
chunk in parallel, so Claude's wall is its `cli` chunk.

The vendor strip (`vendor/strip.rs::analyze_prune_items`) runs a sixth,
separate analysis: `analyze_vendor_prune_chunk`, the full interpreter over the
partially swapped vendor chunk with default inputs, after materialize. On the
Tana `--emit-on-rejection` run it was still running single-threaded at 25 min
(`Executor::callback_values` hot) when the harness killed it. It is a pure
function of the vendor chunk after its own rewrites and the vendor plan, so it
is cacheable the same way; its runtime is an analysis-performance bug either
way.

## Spec keys by stage

S1 means "resolved by the selector solve". Keys absent from every stage
(`note`, `comment` outside output) must be excluded from projections or they
cause spurious cache misses.

| Key                                                                                                                                                                   | Stages                   | Notes                                                                                         |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ | --------------------------------------------------------------------------------------------- |
| `inputs.{input_root,js_list_path}`                                                                                                                                    | S0                       | Source identity; every stage keys on it.                                                      |
| `inputs.embedded_loaders_path`                                                                                                                                        | S0, S2                   | Loader derivation.                                                                            |
| `host_behavior.{synchronous_module_loader, loader_result_*, synchronous_load_*, reentry_free_*_modules, reentry_free_embedded_loaders}`                               | S2                       | Export-reentry policy; copied into S4 reports only.                                           |
| `host_behavior.reentry_free_operations`                                                                                                                               | S3                       |                                                                                               |
| `host_behavior.predefined_globals`                                                                                                                                    | S2, S3                   | Read by two stages.                                                                           |
| `program_premises.*`                                                                                                                                                  | S2                       | Reports only after S2.                                                                        |
| `vendor` marks                                                                                                                                                        | S0, S2, S5               | S2 needs replacement packages and closure blockers.                                           |
| `swap_vendor_chunks.*`, `write_js_tree`, `emit_browser_harness`, `materialize_logical_modules.report_out_dir`                                                         | S5                       | Output only.                                                                                  |
| `logical_modules` key set, `chunk_renames` key set, `unassigned_mode` key set                                                                                         | S0 (chunk set), S2 scope | Which chunks are analysed (C7).                                                               |
| `materialize_logical_modules.prune_other_chunks`                                                                                                                      | S2 scope                 | Changes the S2 universe.                                                                      |
| `materialize_logical_modules.{file,target_dir}`                                                                                                                       | S1, S5                   | Request paths; analysis sees destinations by index only.                                      |
| members' selectors, `source_matches`, `anonymous_statements[].source_match`                                                                                           | S1, S4, S5               | The assignment. Results leak into S2/S3 today (C1, C3, C4).                                   |
| `annotations.{purity, initializer_purity, effect, pure_members, no_sync_callback_members, retained_record_factory}`                                                   | S1, S2 (`pure_new`), S3  | Analysis premises stored in module YAML; destination never read (C2, C4).                     |
| `anonymous_statements[].no_reentry_call`                                                                                                                              | S1, S3                   | Premise stored in module YAML (C2).                                                           |
| `chunk_renames.<c>.members`                                                                                                                                           | S4, S5                   |                                                                                               |
| `chunk_renames.<c>.annotations`                                                                                                                                       | S3                       | Same shape as member annotations.                                                             |
| `unassigned_mode`                                                                                                                                                     | S1, S4                   | Never read by S2/S3.                                                                          |
| `chunk_export_purity.*`                                                                                                                                               | S2                       | Reaches S3 through summaries. `ContractAudit::probe` already varies only this key in-process. |
| `chunk_analysis_options.<c>.{value_inputs, native_operation_inputs, native_call_identity_inputs, argument_inputs}`                                                    | S2, S3                   | Resolved in S2; `resolved_member` selectors (C3). Cleared before graph build.                 |
| `chunk_analysis_options.<c>.no_accessor_install_callables`                                                                                                            | S2                       |                                                                                               |
| `chunk_analysis_options.<c>.{local_property_effects, trusted_dataflow_summaries, deferred_global_publications, non_reentrant_global_membership, admission_overrides}` | S3                       | `admission_overrides` is source-only and cheap.                                               |
| `chunk_analysis_options.<c>.commuting_statements`                                                                                                                     | S2 (resolve), S4         | A gate premise routed through `AnalysisAssumptions` (C5).                                     |
| `chunk_analysis_options.<c>.dataflow_aware_s_chain`                                                                                                                   | S4                       |                                                                                               |

Keys read by more than one stage: `predefined_globals`, `vendor`,
`logical_modules`, `annotations.purity`, every input contract list,
`commuting_statements`, `chunk_renames`, `unassigned_mode`,
`materialize_logical_modules.file`. Only the couplings below block caching;
the rest are reads by both an analysis stage and a later stage, which is fine.

The dependency graph does not read the assignment:
`compute_dependency_graph_and_atomic_groups_with(facts, options, commuting)`
gets no builder or placement, and rebind folds, per-atomic-group synthesis and
`AssignmentAnalysis` all run after it.

## Couplings and cuts

**C1 — `certify_stable_set_spreads` reads true assignment.**
`cross_module_purity.rs:959` passes `explicit_binding_destinations` (binding
`Id` → destination module index) and certification requires the seed, each
intervening `Set` constructor and the spread to share one destination
(`facts/set_iterables.rs`); `materialize/mod.rs` re-checks it against final
placement. Cut: certify on "same original declaration" only and emit the
interval bindings as a must-co-locate constraint the gate checks. This is a
semantic change: a crossing interval becomes a gate rejection instead of an
analysis error. Both canonical inputs have empty `stable_set_spreads`, so
acceptance there cannot change.

**C2 — member and chunk-rename annotations, `no_reentry_call`.** Keyed by
readable name inside a module; the selector solve maps them to binding names
(`collect_member_analysis_assumptions`, `collect_chunk_rename_analysis_assumptions`).
No destination is read: moving a member leaves the resulting
`AnalysisAssumptions` identical. Cut: the projection (below) carries the
resolved `{chunk → binding → annotation}` map, not module paths.

**C3 — `resolved_member` premise selectors.** Native-operation, call-identity
and commuting premises may name `resolved_member: { module, name }`, resolved
through `resolved_member_sites` (`lowering/cross_module.rs` around lines 1042
and 1251). Renaming or moving the member changes the premise's meaning. Cut:
the projection resolves each to `(chunk, body_index, binding)`; S2 never sees
module paths. Optionally a later codemod rewrites `resolved_member` to
`source_match` and retires the variant. All three entries that `devel` now
reports inert in the gaffer `origin/main` specs (Tana
`static/index-DI2GynTv[16]`, two Claude `apiErrors` entries) use
`resolved_member`; worth checking that resolution is correct while touching it.

**C4 — `declared_pure_new` reaches S2** from `purity: pure_new` annotations
(`lowering/mod.rs` → `cross_module.rs:629`). Same shape as C2; covered by the
same projection.

**C5 — `commuting_statements` is a gate premise routed through analysis.** It
is resolved in S2, carried in `AnalysisAssumptions`, and read only by graph
build (`chunk_analysis/mod.rs`). Cut: resolve it in S4 against the chunk's
structural facts, pass it only to graph build, and delete the field from
`AnalysisAssumptions` so the type proves S3 cannot read it. The
commuting-statements diagnostic path moves with it.

**C6 — spec paths and whole-spec provenance in analysis output.** S2 evidence
embeds `author_input_spec_path(provenance)`; `spec_location` strings and
`ProofDependencies` flow into S3 results; `GraphProvenance` records every
authored YAML. Any artifact embedding them is keyed by the whole spec. Cut:
evidence names a premise by a path-independent id (premise kind, chunk,
index); report writers in S4/S5 render the location. The artifact header
carries only its own key.

**C7 — the analysed chunk set comes from assignment key sets.** Cut: the
projection lists the analysed chunks and `prune_other_chunks` explicitly.

**C8 — selector anchors couple modules within S1.** `cross_ref.anchor`,
`reads_member.object` and similar name other members, so the selector solve
reads all module YAML. It only affects the projection step's runtime, which is
seconds; its output is what must be canonical.

### Other hidden couplings

- **SWC hygiene.** `Id = (Atom, SyntaxContext)`; a `SyntaxContext` is an index
  into process-local `GLOBALS`, and chunks are parsed in parallel, so numbering
  differs between processes. This killed the previous attempt
  (`docs/lessons_learned/cross_process_stage_b.md`); see
  [§ Artifact](#artifact) for the encoding that avoids it.
- **Spans.** `BytePos` depends on `SourceMap` registration order. Premise
  matching compares spans (`input.call_span == call.span`), so artifacts store
  file-relative offsets.
- **One `IterativeInference` context per chunk.** Purity, function effects and
  the interpreter share it (design.md § Purity and synchronous execution share
  one context). It never crosses the S3 boundary: the artifact holds the
  settled `StatementFacts`, not the context.
- **S2 keeps per-chunk premise resolutions.** `CrossModulePurities` carries
  each chunk's resolved `value_inputs`, `native_operation_inputs`,
  `argument_inputs` beside the summaries; S3 for chunk _c_ reads only its
  slice plus its imports' summaries.
- **Existing partial reuse.** `ContractAudit` keeps the S0 preparation and
  reruns from `chunk_export_purity` in-process; `cli/edit_gate.rs` gates
  post-edit assignments against a saved `dependency_graph.json` without a run.
  The staged run complements both: it produces the lowered tree.

## Artifact

What crosses the boundary: S2's `CrossModulePurities` plus export-reentry
reports, and per chunk S3's `ChunkFactAnalysis`. Not the dependency graph: it
depends on `commuting_statements` (an S4 key), is 3–4× larger than the facts
it is built from, and rebuilding it from loaded facts takes 4.2 s on Tana and
reproduces it exactly (JSON equality on the Tana main chunk and 75 Claude
chunks).

### Measurements

Prototype: `Serialize`/`Deserialize` added to about 250 analysis-crate types.
`StatementFacts`, `CrossModulePurities` and `AnalysisInputs` derive cleanly
with everything they contain; the few types that cannot (`&'static` builtin
rows, `BinaryOp`, `petgraph` maps) are not in the artifact. Also timers at stage boundaries, and an in-run dump that writes,
re-reads and deserializes each value. JSON is impossible for the facts
(`BTreeMap<Id, _>` keys); `postcard` serializes but cannot deserialize the
existing serde attributes (`#[serde(tag = …)]`, `flatten`,
`skip_serializing_if` need a self-describing format), so its load numbers are
not usable. MessagePack (`rmp-serde`, `to_vec_named`) round-trips everything.

| Artifact                           | JSON                   | MessagePack                              | postcard | zstd -3 of MessagePack (compress, decompress) |
| ---------------------------------- | ---------------------- | ---------------------------------------- | -------- | --------------------------------------------- |
| Tana S2 `CrossModulePurities`      | 19.6 MB, 35–47 ms load | 16.2 MB, 22–29 ms load                   | 5.9 MB   | 0.67 MB                                       |
| Tana S3 facts (main chunk)         | n/a                    | 212 MB, 0.3 s ser, 1.4–1.5 s load        | 124 MB   | 3.7 MB (0.07 s, 0.06 s)                       |
| Tana dependency graph (not cached) | 899 MB                 | 682 MB, 2.2 s load                       | 202 MB   |                                               |
| Claude S2 `CrossModulePurities`    | 75 MB, 0.24 s          | 64 MB, 0.17 s load                       | 29 MB    | 3.0 MB                                        |
| Claude S3 facts (76 chunks)        | n/a                    | 563 MB, 4.1 s load summed (`cli` 387 MB) | 295 MB   | 11.4 MB (0.37 s all)                          |
| Claude per-chunk `AnalysisInputs`  | 2.16 GB                | 1.77 GB                                  |          |                                               |

The last row is why S3's input is the S2 artifact once, not a per-chunk copy
of its derived `AnalysisInputs`.

Identifier canonicalization, measured: numbering every `SyntaxContext` by
first occurrence in a pre-order identifier walk of the chunk AST takes 0.25 s
on Tana (22,878 contexts) and covered every context of the 171k `Id`s checked
in `declared`, `local_bindings`, `reads`, `calls` and `rebinds`, including
closure-local ones; every Claude chunk had zero misses as well.

### Format choice

- **Encoding:** `rmp-serde` with named fields on the existing derives, zstd-3
  framed. Load is 1.5 s for Tana's largest artifact; the compressed artifact
  is 4–15 MB. A dedicated positional wire format (`postcard` plus interned
  atoms) would load faster but needs parallel wire types for about 250 types;
  not worth it at 1.5 s.
- **No versioning.** The engine binary digest is in every key, so the schema
  may change in any PR; serde derives on internal types carry no
  compatibility burden.
- **Hygiene:** `SyntaxContext` written as its first-occurrence ordinal in the
  chunk; the run stage re-parses (it parses anyway in S0), walks, and maps
  ordinals back. This is the "hygiene replay" the lessons-learned note costed
  as substantial, reduced to one walk because the resolver's scope structure,
  not its numbering, is deterministic. Inner-scope contexts keep their own
  ordinal, so the shadowing hazard in that note does not arise.
- **Spans:** offsets relative to the chunk's file start.
- **Header:** engine sha256, projection digest, per-chunk source sha256 (the
  `GraphProvenance` `chunk_sources` shape), chunk list.

## Projected agent loop

Run stage given a cache hit: S0, S1 and selector solve, artifact load,
context remap, S4, S5. Numbers are measured stage times summed, not a
measured end-to-end run of the split.

| Edit                                                                        | Tana                                                      | Claude                                                     |
| --------------------------------------------------------------------------- | --------------------------------------------------------- | ---------------------------------------------------------- |
| Today, any edit                                                             | ~260 s to end of materialize                              | 87 s                                                       |
| Assignment, co-location, module rename, selector rewrite resolving the same | ~20 s (1.6 + 3.4 + 1.5 + 0.3 + 4.5 + 4.0 + 5.0) plus emit | ~23 s (3.8 + 1.3 + 2.8 + 7.4 + 1.8 + 0.3 + 5.2)            |
| `commuting_statements`, `dataflow_aware_s_chain` (after C5)                 | same as assignment                                        | same as assignment                                         |
| Premise edits (annotations, input lists, `chunk_export_purity`, host keys)  | full analysis; no gain                                    | full analysis; per-chunk S3 actions recover most (phase 2) |

In Bazel add the projection action (S0 + S1 + selector solve, about 5 s) and
action overhead. Tana's emit time on an accepted spec is not in the table: the
`--emit-on-rejection` run did not finish (vendor strip, above).

## CLI

```text
debundle spec project-analysis --tree-config … --tree-modules … --tree-vendor-marks …
    --tree-source-root … [--package-root …] --out analysis_projection.json
debundle analyze --projection analysis_projection.json --tree-source-root …
    [--package-root …] --out analysis.dba
debundle run <today's flags> [--analysis analysis.dba]
```

- `analyze` takes no spec tree flags: the projection is its only spec input.
- `run --analysis` recomputes the projection and source digests from its own
  flags and refuses on mismatch, naming the differing projection keys. Under
  Bazel this is redundant; for an agent running `run --analysis` locally
  against an artifact fetched once, it turns a stale artifact into an error.
- `run` without `--analysis` keeps today's behavior, implemented as
  project → analyze → run in-process, so there is one code path.

## Bazel

`debundle_pipeline` (`pipeline.bzl`) becomes three actions; callers do not
change:

| Mnemonic                  | Inputs                                          | Output                                          | Notes                                                                                                                                       |
| ------------------------- | ----------------------------------------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `DebundleProjectAnalysis` | spec tree, `input_data`, package roots, binary  | `<name>.analysis_projection.json`               | Reruns on every spec edit; seconds.                                                                                                         |
| `DebundleAnalyze`         | projection, `input_data`, package roots, binary | `<name>.dba`                                    | Key unchanged by assignment edits → remote-cache hit across agents and machines. Keeps the big `exec_properties` (Claude peaks at 8.6 GiB). |
| `DebundlePipeline`        | today's inputs plus `<name>.dba`                | today's `<name>.out`, `dependency_graphs` group | Exit-3 handling and `no-sandbox` unchanged. Can request smaller executors.                                                                  |

- Content addressing gives the cutoff: an assignment edit changes the spec
  glob, so `DebundleProjectAnalysis` reruns, writes identical bytes, and
  `DebundleAnalyze` hits the action cache.
- Output sizes are small: 4–15 MB compressed, against today's 1.1–1.4 GB
  `<name>.out`, which RBE already accepts within `--remote_timeout=10m`.
- Local loop: `bazel build :<name>_analysis --remote_download_outputs=toplevel`
  (after `--config=rbe`), then `debundle run --analysis` locally on each edit.
- Phase 2, only if premise edits dominate: S2 as one action and S3 as one
  action per chunk, with the S2 artifact split per consumer chunk (its own
  resolved premises plus its imports' summaries) so an edit that changes no
  exported summary cuts off before the chunks. Vendor strip analysis as its
  own action keyed by the vendor plan.

## Cache-key correctness

A stage may read only what its key covers. Enforced, not documented:

1. **Process boundary.** `analyze` parses no YAML but the projection and
   accepts no spec-tree flag. On RBE the action sees only declared inputs.
   Locally (`no-sandbox`), `InputCapture` already records every read; `analyze`
   fails if a read falls outside the projection, the source root and the
   package roots, and writes the digests it read into the header.
2. **Types.** `AnalysisProjection` is the only spec-derived type in S2 and S3
   signatures. `MaterializeSpecInputs` splits into `AnalysisProjection` and the
   assignment half; `DependencyGraphOptions` splits into analysis and graph
   options; `commuting_statements` leaves `AnalysisAssumptions`; S2 no longer
   receives `explicit_binding_destinations` or `resolved_member_sites`. The
   `analysis` crate does not depend on the `gate` crate or any assignment type.
3. **Canonical projection.** Sorted keys, no paths outside the source root, no
   `note`/`comment`. Tests: moving a member between modules, renaming a
   module, editing `commuting_statements` or rewriting a selector to an
   equivalent one leaves the projection bytes identical; adding an annotation
   or premise changes them.
4. **Deterministic artifact.** No `SyntaxContext` numbers, `BytePos` values,
   absolute paths or provenance in the artifact. Test: analyze twice in fresh
   processes with different thread counts, compare bytes; and the cross-
   `Globals` round trip the lessons-learned note says does not exist.
5. **Equivalence.** `run` equals `analyze` + `run --analysis` byte-for-byte
   on the whole e2e corpus and both canonical inputs (tree, reports, exit code).

## Migration order

Each PR is byte-identical on the e2e suite and both canonical inputs unless
it says otherwise.

1. **Split graph build out of analysis (C5, types).** `compute_chunk_analysis`
   becomes `analyze_chunk` plus `build_chunk_graph(facts, GraphOptions,
commuting)`; `DependencyGraphOptions` splits into analysis and graph
   halves; `commuting_statements` moves out of `AnalysisAssumptions` (still
   resolved in S2).
2. **Resolve `commuting_statements` in S4**, against the chunk's structural
   facts, with its diagnostics.
3. **`AnalysisProjection` (C2, C3, C4, C7).** Computed after the selector
   solve; S2 and S3 consume only it; `resolved_member` premises resolved to
   `(chunk, body_index, binding)`.
4. **Stable set spreads as a gate constraint (C1).** Not byte-identical on
   e2e: the crossing-interval case moves from an analysis error to a gate
   rejection. Canonical inputs unaffected (no stable set spreads).
5. **Path-independent evidence (C6).**
6. **Serializable artifact.** Serde derives, `rmp-serde` (new dependency)
   and `zstd` (already in `Cargo.Bazel.lock` transitively), context and
   span canonicalization, round-trip and determinism tests.
7. **CLI**: `spec project-analysis`, `analyze`, `run --analysis`, equivalence
   tests. Graduate the boundary to docs/design.md § Pipeline split and update
   the lessons-learned note.
8. **`pipeline.bzl`** three actions. gaffer-private repins separately.
9. Phase 2 (per-chunk actions, vendor strip) only if measured edit mix asks
   for it.

PRs 1, 2, 5 and 6 are independent of each other; 3 needs 2 for the commuting
selectors; 7 needs all before it.

## Interaction with other work

- **Abstract interpreter** (<abstract_interpreter.md>). Step 4c keeps purity
  and the interpreter in one per-chunk context; that context is internal to
  S3. Step 9 moves gate consumption of `commuting_statements` onto effect
  sets: the gate then reads effect sets from the artifact, which carries them
  in `StatementFacts`, and PR 2 puts commuting resolution where step 9 needs
  it. Artifact schema churn from any step is free (engine digest in the key).
- **Analysis performance.** Faster S2/S3 shrinks the cold path and does not
  change the boundary. Work that shares state between S2 and S3 (for example
  reusing S2's prepared execution indexes in S3) must pass it as a value in
  the S2 artifact, not as shared in-process state.
- **Dependency-graph slimming.** Changes `dependency_graph.json`, which the run
  stage still writes; the artifact caches facts, not the graph, so the two do
  not interact.

## Not in scope

- An in-process or ad-hoc local cache.
- Incremental analysis within a chunk.
- The prototype: it is a throwaway patch (serde derives on 62 files, timers,
  dump code), not kept under `x/`; PR 6 redoes the derives properly.
