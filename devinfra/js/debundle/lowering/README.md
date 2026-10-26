# Lowering ownership

- `mod.rs`: artifact-wide orchestration and the public lowering entry point.
- `materialize/`: resolve requests and prepare accepted per-chunk plans.
- `placement.rs`: project binding and whole-statement destinations from the validated assignment and check linkage metadata agrees.
- `lower.rs`: lower the source units according to that immutable placement, construct the residual entry, and dispatch module emission.
  Its plan requires a `ValidatedAssignment` from the shared gate; an unchecked
  assignment cannot enter lowering.
- `module_output.rs`: emit one logical module's imports, body rewrites, comments, and metadata.
  Its input contains only that module's selected exports and imported re-exports, not the
  vectors for every module in the chunk.
- `imports/`: reference planning, source resolution, directive construction, and vendor consultation.
- `rename_ledger.rs`, `naturalize.rs`, `visitors.rs`: collect, seal, and apply rename decisions.
- `body_facts.rs`, `chunk_ast.rs`, `scope_names.rs`: AST facts used by the planning and emission phases.

Import internal helpers from their owning module rather than through a root prelude.
The root is an orchestrator, not a namespace of every implementation dependency.
