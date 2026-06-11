# Terminology Rename Plan

Remove "factor" terminology in favor of precise graph-theoretic and
descriptive names.

## Graph Vocabulary

Three named graphs, each named by what its vertices represent:

| Graph          | Vertices                                    | Edges                                  | How built                                 |
| -------------- | ------------------------------------------- | -------------------------------------- | ----------------------------------------- |
| **OwnerGraph** | Owners (declarations, anonymous statements) | Program deps (EagerUse, LazyUse, etc.) | Source analysis                           |
| **AtomicDAG**  | Atomic units (indivisible SCCs)             | Constraining deps                      | SCC condensation of constraining subgraph |
| **ModuleDAG**  | Modules                                     | Cross-module deps                      | OwnerGraph quotiented by ModuleAssignment |

Plus one mapping and one heuristic:

- **ModuleAssignment**: maps each owner to a module. The vertex set of
  the ModuleDAG without edges is the image of this mapping.
- **Proposals**: advisory move suggestions computed by edge contraction
  over the AtomicDAG (greedy closure of adjacent atomic units).

## Files

| Current                                  | Proposed                                |
| ---------------------------------------- | --------------------------------------- |
| `factor_assembly.rs`                     | `partition_assembly.rs`                 |
| `chunk_factorization.rs`                 | `resolved_chunk.rs`                     |
| `peel/factorize.rs`                      | `peel/propose.rs`                       |
| `e2e/peel_factorize_landability_test.rs` | `e2e/peel_proposal_landability_test.rs` |
| `partition.rs`                           | `module_assignment.rs`                  |

## Structs and Enums

| Current                      | Proposed                    | Notes                                               |
| ---------------------------- | --------------------------- | --------------------------------------------------- |
| `Partition`                  | `ModuleAssignment`          | Owner → module mapping                              |
| `ChunkFactorization`         | `ResolvedChunk`             | Analysis + assignment + quotient + validation       |
| `AssemblyOutcome`            | `AssignmentOutcome`         | Result of claim resolution                          |
| `FactorizationReport`        | `AssignmentReport`          | Validation result (cycles, conflicts, linker order) |
| `FactorizeProposal`          | `Proposal`                  | One advisory move suggestion                        |
| `FactorizeDiagnosticReport`  | `ProposalDiagnostic`        | Why a closure didn't become a proposal              |
| `FactorizeDiagnosticReason`  | `ProposalDiagnosticReason`  | Enum of diagnostic reasons                          |
| `FactorizeSizeDistributions` | `ProposalSizeDistributions` | Size histogram                                      |
| `FactorizeSizeBucketCount`   | `ProposalSizeBucketCount`   | One histogram bucket                                |
| `PeelFactorizeOptions`       | `PeelProposalOptions`       | CLI args for proposal pass                          |
| `PeelFactorizeReport`        | `PeelProposalReport`        | Result of proposal pass                             |
| `ModuleQuotient`             | `ModuleDAG`                 | Inter-module dependency graph                       |
| `OwnerGraphQuotientReport`   | `ModuleDAGReport`           | Serialized quotient                                 |
| `QuotientEdgeReport`         | `ModuleDAGEdgeReport`       | Edge in serialized quotient                         |
| `QuotientSccReport`          | `ModuleDAGSccReport`        | SCC in serialized quotient                          |

## Functions

| Current                        | Proposed                      |
| ------------------------------ | ----------------------------- |
| `assemble_partition()`         | `assemble_assignment()`       |
| `validate_factorization()`     | `validate_assignment()`       |
| `build_module_quotient()`      | `build_module_dag()`          |
| `factorize()`                  | `compute_proposals()`         |
| `analyze_peel_factorize()`     | `analyze_peel_proposals()`    |
| `sort_factorize_diagnostics()` | `sort_proposal_diagnostics()` |

## Keep As-Is

- `OwnerGraph`, `OwnerNode`, `OwnerId`
- `AtomicUnit`, `AtomicGraphReport`, `AtomicUnitReport`, `AtomicUnitEdgeReport`
- `ChunkAnalysis`
- `compute_owner_graph_and_units()`, `compute_atomic_units()`

## Docs to Update

- `docs/design.md`: ~15 references to `ChunkFactorization`, `FactorizationReport`, `validate_factorization`, `factor_assembly`
- `TODO.md`: `peel/factorize.rs` reference
- `README.md`: any `ChunkFactorization` references
- `x/graph_planner_factorization.md`: rename or update
- `CLI_DOGFOOD.md`: any factorize references

## Output Schema (JSON field renames)

These are external API — consumers parse these JSON files. The rename
will be an atomic cutover: update `ducktape` and `gaffer-private`
together in one commit span, no compatibility shims.

`owner_graph.json` (from `OwnerGraphReport`):

| Current JSON key                                                         | Proposed JSON key                 | Notes                                                        |
| ------------------------------------------------------------------------ | --------------------------------- | ------------------------------------------------------------ |
| `module_graph` (`#[serde(rename = "module_graph")]` on `quotient` field) | `module_dag`                      | Currently has a serde rename from Rust field name `quotient` |
| (nested object under `module_graph`)                                     | Same struct, no key change within | The object keys (`nodes`, `edges`, `sccs`) stay the same     |

`plan-work` output (from `PeelFactorizeReport`):

| Current JSON key | Proposed JSON key                               | Notes                                                                                         |
| ---------------- | ----------------------------------------------- | --------------------------------------------------------------------------------------------- |
| (top-level)      | No serde renames — keys follow Rust field names | Renaming the struct fields (`proposals`, `diagnostics`, etc.) changes JSON keys automatically |

Since most report types use default serde (field name = JSON key), the
struct renames above cascade to JSON automatically:

- `FactorizeProposal` fields → `Proposal` fields (no JSON key changes — field names like `owner_ids`, `binding_ids`, `landable_today` are already clean)
- `FactorizeDiagnosticReport` fields → `ProposalDiagnostic` fields (same)
- `FactorizeDiagnosticReason` variants use `#[serde(rename_all = "snake_case")]` → already produce `exceeds_size_cap`, `no_exact_repair`, etc. No change needed.

The only JSON key that needs an explicit serde rename change:

- `module_graph` → `module_dag` in `OwnerGraphReport`

## BUILD.bazel Targets

- `e2e/peel_factorize_landability_test` → `e2e/peel_proposal_landability_test`
- File references in `:analysis` and `:peel` targets update with file renames
- No target name changes needed for `:analysis`, `:peel` themselves (they're named by concern)
