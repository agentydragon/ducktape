//! `TransformSpec.chunk_renames` applies in-place renames to bindings
//! staying in entry's body without creating an explicit `Logical(R)`
//! module that would form a 2-module SCC with `ResidualEntry`.
//!
//! ## Background — the bug this op was added to fix
//!
//! Previously, the gaffer-side `.yaml.deferred` workflow stuffed its
//! rename ops into `residualModules[chunkId].members` so the
//! residual-member-rename path applied them. That works mechanically
//! but the residual-member-rename path needs a `Logical(R)` module,
//! which the validator treats as a distinct node from
//! `ModuleId::ResidualEntry`. When the chunk interleaves orphan
//! top-level statements (`console.log(x)` etc., owner =
//! `ResidualEntry`) with side-effecting initializers on residual-
//! owned decls (owner = `Logical(R)`), the S-edge ordering loop
//! produces a 2-module SCC. Both nodes emit into files that evaluate
//! in the chunk's same init phase — the cycle is a validator
//! artifact, not a real evaluation hazard, but the gate still
//! rejects.
//!
//! ## What `chunk_renames` does
//!
//! Carries `members[]` with rename info, like a `LogicalModule`, but
//! doesn't create a logical module. The materializer collects the
//! renames into a `binding_name -> export_name` map and the lowerer
//! applies them in-place during entry-body emission for any binding
//! *not* claimed by a logical module. Bindings claimed by a logical
//! module take their rename from the module plan; the
//! `chunk_renames` entry (if any) is dropped for those.
//!
//! With no `Logical(R)` to interleave against, the orphan stmts and
//! unowned decls share `ResidualEntry` ownership; no cycle.

use debundle_e2e_support::*;
use serde_json::json;

#[test]
fn chunk_renames_renames_residual_bindings_in_entry() {
    let opts = FixtureOpts {
        // S1 (orphan-S):       console.log("before")
        // S2 (decl with S):    let x = (..., "x-value")  -- stays in entry
        // S3 (orphan-R+S):     console.log(x)            -- reads x at-init
        //
        // No `residualModules` entry; `x` stays in `ResidualEntry`-land.
        // `chunkRenames` renames `x` -> `payload`. The lowerer rewrites
        // the in-entry references; the export statement carries the
        // new name.
        source: r#"console.log("before");
let x = (globalThis.__touched = true, "x-value");
console.log(x);
export { x };
"#,
        logical_modules: vec![],
        residual: None,
        chunk_renames: Some(json!({
            "id": "chunk_renames__static_app",
            "members": [
                {
                    "name": "payload",
                    "selector": { "binding": { "name": "x" } },
                },
            ],
        })),
        chunk_id: "static/app",
        include_residual: false,
        extra_files: &[],
    };
    let fixture = run_logical_modules_e2e_fixture(opts);
    // The chunk evaluates: orphan logs "before", x's init runs (sets
    // globalThis.__touched and binds "x-value"), orphan logs the
    // value. With the in-place rename, the entry source emits
    // `let payload = ...` and `console.log(payload)`; runtime
    // semantics are unchanged.
    assert_entry_output(&fixture, "before\nx-value\n");
    // Pin the in-entry rename: entry.js should reference the new
    // name and not the original binding.
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &[
            "let payload =",
            "console.log(payload)",
            "export { payload as x }",
        ],
        &["let x =", "console.log(x)"],
    );
}
