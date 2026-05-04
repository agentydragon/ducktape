//! Spec-defined renames apply to bindings staying in the residual module.
//!
//! The gaffer-side `.yaml.deferred` workflow routes its rename ops
//! through `define_residual_module`'s `members` field instead of a
//! `define_logical_module`. Bindings remain owned by the residual
//! catch-all (no module pull) but get a public name applied to them.
//! Pin the contract: when a residual op carries a member with `name`
//! set, the residual module must export that binding under the new
//! name.

use debundle_e2e_support::*;
use serde_json::json;

#[test]
fn define_residual_module_members_apply_renames() {
    let opts = FixtureOpts {
        source: r#"function a() { return 1; }
function b() { return 2; }
console.log(a(), b());
export { a, b };
"#,
        // No `define_logical_module` — every binding stays in residual.
        // The residual op carries a renaming member entry for `a`. We
        // construct the residual op explicitly here (instead of letting
        // `include_residual: true` produce a memberless one).
        operations: vec![json!({
            "id": "logical__residual_unhandled",
            "operation": "define_residual_module",
            "selector": { "chunkId": "static/app" },
            "target": { "path": "residual/unhandled" },
            "members": [
                {
                    "id": "rename_a",
                    "name": "FirstFn",
                    "selector": { "binding": { "name": "a" } },
                },
            ],
        })],
        chunk_id: "static/app",
        include_residual: false,
        extra_files: &[],
    };
    let fixture = run_logical_modules_e2e_fixture(opts);
    // `a` was renamed to `FirstFn`; `b` is unmentioned and keeps its
    // source name. Both live in the residual catch-all.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["FirstFn", "b"],
        &["a"],
    );
}
