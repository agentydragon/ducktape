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

#[test]
fn define_residual_module_members_apply_renames() {
    let opts = FixtureOpts {
        source: r#"function a() { return 1; }
function b() { return 2; }
console.log(a(), b());
export { a, b };
"#,
        // No logical modules — every binding stays in residual. The
        // residual carries a renaming member entry for `a`.
        logical_modules: vec![],
        residual: Some(residual_module(
            "residual/unhandled",
            &[Member::renamed("FirstFn", "a")],
        )),
        chunk_renames: None,
        chunk_id: "static/app",
        include_residual: false,
        unassigned_mode: None,
        extra_files: &[],
    };
    let fixture = run_fixture(opts);
    // `a` was renamed to `FirstFn`; `b` is unmentioned and keeps its
    // source name. Both live in the residual catch-all.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["FirstFn", "b"],
        &["a"],
    );
}
