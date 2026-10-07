# Lowering ownership

- `mod.rs`: artifact-wide orchestration and the public lowering entry point.
- `materialize/`: resolve requests and prepare accepted per-chunk plans.
- `lower.rs`: partition a chunk, construct its residual entry, and dispatch module emission.
- `module_output.rs`: emit one logical module's imports, body rewrites, comments, and metadata. Its input contains only
  that module's selected exports and imported re-exports, not the vectors for every module in the chunk.
- `imports/`: reference planning, source resolution, directive construction, and vendor consultation.
- `rename_ledger.rs`, `naturalize.rs`, `visitors.rs`: collect, seal, and apply rename decisions.
- `body_facts.rs`, `chunk_ast.rs`, `scope_names.rs`: AST facts used by the planning and emission phases.

Import internal helpers from their owning module rather than through a root prelude. The root is an orchestrator, not a
namespace of every implementation dependency.
