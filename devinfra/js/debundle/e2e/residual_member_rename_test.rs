//! Logical-module member renames apply to bindings landing in the
//! residual catch-all when the same `logical_modules` entry is pinned
//! at the chunk's `unassigned_mode: catchall_file` target.
//!
//! Before the residual-config unification, the gaffer-side
//! `.yaml.deferred` workflow routed rename ops through a separate
//! `residual_modules` map's `members` field. With `residual_modules`
//! folded into `UnassignedMode::CatchallFile`, the same effect is
//! expressed by declaring a regular `logical_modules` entry at the
//! catchall target: explicit members get their renames applied, and
//! unclaimed top-level decls overflow into the same module (no
//! rename, original binding name).

use debundle_e2e_support::*;

#[test]
fn logical_module_at_catchall_target_renames_and_absorbs_overflow() {
    let opts = FixtureOpts {
        source: r#"function a() { return 1; }
function b() { return 2; }
console.log(a(), b());
export { a, b };
"#,
        // A logical_modules entry pinned at the catchall target.
        // `a` is an explicit member with a rename to `FirstFn`;
        // `b` is unclaimed and overflows into the same module via
        // the `catchall_file` overflow path.
        logical_modules: vec![logical_module(
            "residual/unhandled",
            &[Member::renamed("FirstFn", "a")],
        )],
        chunk_renames: None,
        chunk_id: "static/app",
        unassigned_mode: unassigned_mode_catchall_file(None),
        extra_files: &[],
    };
    let fixture = run_fixture(opts);
    // `a` was renamed to `FirstFn`; `b` is unclaimed overflow and
    // keeps its source name. Both live in the catchall module.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["FirstFn", "b"],
        &["a"],
    );
}
