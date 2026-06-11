# Debundle Data Shape Smells

Audit of data structures and data flow in the debundle pipeline.

## Root cause

The pipeline uses a single mutable artifact passed through every stage, with fields
stamped as stages run. The fix is not splitting types while keeping the mutation
pattern — it's making each stage a pure function that takes inputs and returns outputs,
with no shared mutable state.

## Fixed (Phases 1-7)

| #   | Smell                                                                                   | Fix                                                                                                                                                              |
| --- | --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- | ----------------------------------- |
| 1   | `JsPipelineArtifact` was a mutable pipeline envelope with misleading name               | Renamed to `ChunkBundle`                                                                                                                                         |
| 6   | `ChunkManifest` mixed analysis, decomposition, and write-time data                      | Split into `ChunkAnalysis` (analysis), `ChunkDecompositionOutput` (decomposition), `ChunkManifest` (write-time assembly via `from_analysis`)                     |
| 7   | `ArtifactManifest` was a grab-bag accumulator with `empty()`                            | Constructed once in `write_tree.rs` with all data explicit; `empty()` deleted                                                                                    |
| 8   | `ArtifactCounts.selected_module_lowerings` was redundant                                | Removed                                                                                                                                                          |
| 9   | `FileMetadata` was all-optional grab-bag                                                | Required fields (`chunk_id`, `chunk_file`, `role`, `source_path`); dead `output_path` removed; `generated_stage` → `generated_by_selected_module_lowering: bool` |
| 11  | Imperative `try_fold` in `prepare_chunks`                                               | Replaced with validation loop + `map`/`collect`                                                                                                                  |
| 12  | Imperative loop in `swap_vendor_chunks`                                                 | Replaced with `collect::<Result<_>>() + retain_chunks()`                                                                                                         |
| 14  | `update_root_manifest` / `prune_artifact_to_chunk_ids` were stamp functions             | Deleted / simplified                                                                                                                                             |
| 15  | `write_tree` clone-then-stamp                                                           | `ArtifactManifest` constructed from explicit args; `WriteTreeInput` struct replaces 7 params; return type simplified to `Result<()>`                             |
| 16  | `emit_harness` read from `root_manifest.chunks`                                         | Takes `chunk_records` as explicit arg                                                                                                                            |
| 17  | `pipeline.rs` read `selected_module_lowerings` from root manifest                       | Reads from `materialize_result.selected_lowerings` directly                                                                                                      |
| 19  | `MaterializeLogicalModulesResult` carried artifact back with stamped decomposition data | `selected_lowerings` and `module_count` flow through result struct                                                                                               |
| 21  | Forged `Default` impls on metrics types                                                 | `Default` removed from `OutputMetrics`, `DecompositionMetrics`, etc.                                                                                             |
| 22  | Serialization via intermediate string allocation                                        | All manifest sites use `serde_json::to_writer_pretty`                                                                                                            |
| 23  | `.map(                                                                                  | \_                                                                                                                                                               | ())`discarded`WriteJsTreeManifest` return | Return type changed to `Result<()>` |
| 24  | `too_many_arguments` suppression on `write_js_tree`                                     | `WriteTreeInput` struct bundles parameters                                                                                                                       |
| 25  | Type names didn't match semantics after refactor                                        | `ChunkManifest` → `ChunkAnalysis`, `WrittenChunkManifest` → `ChunkManifest`, field `manifest` → `analysis`                                                       |

## Fixed (Phases A-E)

| Smell                                                                 | Fix                                                                                                                                           |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `ChunkArtifact.decomposition: Option<ChunkDecompositionOutput>` stamp | Field removed; decomposition flows as `HashMap<ChunkId, ChunkDecompositionOutput>` via `MaterializeLogicalModulesResult` and `WriteTreeInput` |
| `artifact.chunks` — `mem::take` and reassign                          | `materialize_logical_modules` takes `ChunkBundle` by value, returns new `ChunkBundle`; no `mem::take`                                         |
| `ChunkMetadata.module_extraction_state: None` → `Some(...)`           | Field and `ModuleExtractionState` struct removed entirely (dead code)                                                                         |
| `ChunkAnalysis` post-creation mutation (`entry_file`, `files`)        | Struct update syntax (`..base_analysis.unwrap_or_else(...)`) replaces `let mut` + overwrite                                                   |
| `JsFile.body` in-place mutation                                       | `JsFile::into_rendered_source()` consuming transformation; `prepare_chunks` uses remove/render/insert                                         |

## Acceptable patterns (not stamp-driven)

### `JsChunk` file bag: `remove_file` + `insert_file` in `rewrite_specifiers.rs` and `vendor.rs`

Both stages extract files for rayon parallel processing, then re-insert results. This is
driven by parallelism requirements, not by a stamp pattern — the remove/insert is a
consequence of needing to move file data into parallel tasks. No fix needed.

## Remaining TODO

### TODO: `ChunkBundle` ownership ping-pong

Ownership still passes through every stage via return: `artifact = result.artifact`.
Could be cleaner with a builder or consuming pipeline, but each stage is now a pure
function so the remaining smell is cosmetic.

### TODO: `ChunkMetadata.source_path: Option<String>` is always `Some`

Every construction site sets `source_path: Some(...)`. The `Option` is used for
`and_then` chaining convenience in `artifact.rs:723`. Consider making it `String`
and adjusting the callers.

### TODO: `LoadedJsChunks` is a partially-constructed artifact

`Vec<Option<JsChunk>>` with holes. Constructed empty with `Default`, filled iteratively.
Less urgent without the ChunkTable elimination.

### TODO: Pipeline ordering — `generated_by_selected_module_lowering` flag

`generated_by_selected_module_lowering` exists solely so `rewrite_chunk_entry_specifiers`
can skip specifier rewriting on files synthesized by the lowering stage. This flag wouldn't
be needed if specifier rewriting ran _before_ lowering. Investigate whether reordering the
pipeline stages eliminates the need for the flag entirely.

### DEFERRED: `ChunkTable` / `ChunkId(usize)` interned IDs

`ChunkId(usize)` is an interned identifier: 8 bytes, `Copy`, cheap to hash.
Replacing with `String` would mean 24-byte heap-allocated keys, string comparison on every
HashMap lookup, and `BTreeMap` O(log n) vs `Vec` O(1). The interned-ID pattern is standard
in compilers/analyzers and arguably correct here. Revisit only if the duplication becomes
actively harmful.
