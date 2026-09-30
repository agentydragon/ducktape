# Terminology Rename Plan

Remove "factor" terminology in favor of precise graph-theoretic and
descriptive names. "Factorize" is overloaded today: factorization/assembly
produces the authoritative owner partition (`factor_assembly.rs`), while
`peel/factorize.rs` produces advisory planner proposals from the serialized
atomic DAG (surfaced as `debundle modules propose`). Until the rename lands,
docs say which one they mean.

## Graph Vocabulary

Three named graphs, each named by what its vertices represent:

| Graph              | Vertices                                    | Edges                                  | How built                                 |
| ------------------ | ------------------------------------------- | -------------------------------------- | ----------------------------------------- |
| **OwnerGraph**     | Owners (declarations, anonymous statements) | Program deps (EagerUse, LazyUse, etc.) | Source analysis                           |
| **AtomicDAG**      | Atomic units (indivisible SCCs)             | Constraining deps                      | SCC condensation of constraining subgraph |
| **ModuleQuotient** | Modules                                     | Cross-module deps                      | OwnerGraph quotiented by ModuleAssignment |

`ModuleQuotient` keeps its name. The graph is cyclic whenever a spec has a
lazy-only or rejected I-cycle (`ModuleQuotient::sccs` reports its SCCs), so a
`DAG` name would be false, and the peel kernel's `QuotientGraph` /
`IncrementalQuotient` use the same term.

Plus one mapping and one heuristic:

- **ModuleAssignment**: maps each owner to a module. The vertex set of
  the ModuleQuotient without edges is the image of this mapping.
- **Proposals**: advisory move suggestions computed by edge contraction
  over the AtomicDAG (greedy closure of adjacent atomic units).

## Files

| Current                                       | Proposed                                     |
| --------------------------------------------- | -------------------------------------------- |
| `factor_assembly.rs`                          | `assignment_assembly.rs`                     |
| `chunk_factorization.rs`                      | `resolved_chunk.rs`                          |
| `peel/factorize.rs`                           | `peel/propose.rs`                            |
| `e2e/peel_factorize_landability_test.rs`      | `e2e/peel_proposal_landability_test.rs`      |
| `e2e/peel_factorize_extend_anonymous_test.rs` | `e2e/peel_proposal_extend_anonymous_test.rs` |
| `analysis_tests/factorization_validation.rs`  | `analysis_tests/assignment_validation.rs`    |
| `partition.rs`                                | `module_assignment.rs`                       |

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
| `FactorizeContext`           | `ProposalContext`           | Private input bundle of `factorize_with_context`    |
| `PeelFactorizeOptions`       | `PeelProposalOptions`       | CLI args for proposal pass                          |
| `PeelFactorizeReport`        | `PeelProposalReport`        | Result of proposal pass                             |

## Functions

| Current                             | Proposed                            |
| ----------------------------------- | ----------------------------------- |
| `assemble_partition()`              | `assemble_assignment()`             |
| `validate_factorization()`          | `validate_assignment()`             |
| `factorize()`                       | `compute_proposals()`               |
| `factorize_with_context()`          | `compute_proposals_with_context()`  |
| `analyze_peel_factorize()`          | `analyze_peel_proposals()`          |
| `analyze_peel_factorize_on_graph()` | `analyze_peel_proposals_on_graph()` |
| `sort_factorize_diagnostics()`      | `sort_proposal_diagnostics()`       |
| `synthesize_mini_factors()`         | `synthesize_unit_modules()`         |

## External Contract: `unassigned_mode: mini_factors`

`UnassignedMode::MiniFactors` is the spec value `unassigned_mode: { kind:
mini_factors }` (`spec.rs`, `e2e/mini_factors_test.rs`). Spec authors and
downstream specs write it, so renaming it (proposed: `PerAtomicUnit`,
`per_atomic_unit`: one synthetic module per unclaimed atomic unit) is a
spec-format change and takes the same atomic cutover as the JSON keys below.

The phrase "atomic factor unit" in comments and diagnostics
(`factor_assembly.rs`, `atomic_units.rs`, `spec.rs`,
`lowering/materialize/plan_builder.rs`, `lowering/materialize/mod.rs`,
`validation.rs`) becomes "atomic unit", the name `AtomicUnit` already has.

## Keep As-Is

- `OwnerGraph`, `OwnerNode`, `OwnerId`
- `AtomicUnit`, `AtomicGraphReport`, `AtomicUnitReport`, `AtomicUnitEdgeReport`
- `ChunkAnalysis`
- `compute_owner_graph_and_units_with()`, `compute_atomic_units()`
- `ModuleQuotient`, `build_module_quotient()`, `OwnerGraphQuotientReport`,
  `QuotientEdgeReport`, `QuotientSccReport`, and the `module_graph` JSON key
  (see Graph Vocabulary)

## Docs to Update

- `docs/design.md`: references to `ChunkFactorization`, `FactorizationReport`, `validate_factorization`, `factor_assembly`
- `docs/peel_proposer.md`: `FactorizeProposal`, `peel/factorize.rs`, and the "cell" wording
- `docs/cli.md`, `docs/spec_editing.md`, `README.md`, `skills/debundle_intake/SKILL.md`: "factorizer" / "factorization algorithm" wording around `modules propose`
- `docs/wire_format.md`: "the peel factorizer's self-merge bug"
- `ARCHITECTURE_BACKLOG.md`: `validate_factorization`, `ChunkFactorization::build_with`
- `x/graph_planner_factorization.md`: rename or update

## Output Schema (JSON field renames)

These are external API — consumers parse these JSON files. The rename
will be an atomic cutover: update Ducktape and the downstream consumers
together in one commit span, no compatibility shims.

`modules propose` output (from `PeelFactorizeReport`) has no serde renames:
keys follow Rust field names, so renaming the struct fields (`proposals`,
`diagnostics`, etc.) changes JSON keys automatically. Several keys carry the
old vocabulary and need an explicit decision:

| Current JSON key                                          | Proposed JSON key                   | Notes                        |
| --------------------------------------------------------- | ----------------------------------- | ---------------------------- |
| `factorize_proposals` (`ExplainReport`, `peel/plan.rs`)   | `proposals`                         | Also a `limits` section name |
| `factorize_diagnostics` (`ExplainReport`, `peel/plan.rs`) | `proposal_diagnostics`              | Also a `limits` section name |
| `edges_to_other_residual_cells` (`FactorizeProposal`)     | `edges_to_other_residual_classes`   | "cell" vocabulary, see below |
| `other_residual_cells_referenced` (`FactorizeProposal`)   | `other_residual_classes_referenced` | "cell" vocabulary, see below |

Since most report types use default serde (field name = JSON key), the
struct renames above cascade to JSON automatically:

- `FactorizeProposal` fields → `Proposal` fields (no JSON key changes beyond the table: names like `owner_ids`, `binding_ids`, `landable_today` are already clean)
- `FactorizeDiagnosticReport` fields → `ProposalDiagnostic` fields (same)
- `FactorizeDiagnosticReason` variants use `#[serde(rename_all = "snake_case")]` → already produce `exceeds_size_cap`. No change needed.

## "Cell" Vocabulary

The proposer's older parallel IR called each proposed owner group a "cell".
The IR is gone (`QuotientGraph` calls them classes: `ClassId`, `ClassData`),
but the word survives in field names and wire keys
(`edges_to_other_residual_cells`, `other_residual_cells_referenced`), in
comments and docs across `peel/factorize.rs`, `peel/quotient.rs`,
`docs/peel_proposer.md`, `docs/design.md` and `docs/cli.md`, and in the
`landability_notes` string `reads other residual cells; land the referenced
cells first or together`. `peel/factorize.rs` pins the substring
`other residual cells` in a unit test. The rename to "class" touches the
wire keys in the table above and that note text.

## BUILD.bazel Targets

- `e2e/peel_factorize_landability_test` → `e2e/peel_proposal_landability_test`
- `e2e/peel_factorize_extend_anonymous_test` → `e2e/peel_proposal_extend_anonymous_test`
- File references in `:analysis` and `:peel` targets update with file renames
- No target name changes needed for `:analysis`, `:peel` themselves (they're named by concern)
