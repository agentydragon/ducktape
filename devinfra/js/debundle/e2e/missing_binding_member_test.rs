//! Spec member whose source binding does not exist as a top-level decl.
//!
//! When a `define_logical_module` member claims a source binding that
//! is not a top-level declaration in the chunk, `materialize_logical_modules`
//! has nothing to move. The pipeline keeps emitting (so the
//! destination module still lands with its other, resolvable members),
//! but the build then fails at the end with the full list of
//! unresolved claims — silently letting the binding fall into the
//! residual leaves the named destination module short an export and
//! masks spec errors.
//!
//! Pin the contract:
//! - the binding-name mismatch is rejected as a build error
//! - the error message names the chunk, the module, and the bad
//!   binding so the spec author can fix it
//! - the failure is deferred until after all chunks are processed so
//!   every offender is reported in one pass (not just the first).

use debundle_e2e_support::*;

#[test]
fn missing_binding_member_fails_the_build() {
    let opts = FixtureOpts::new(
        r#"function a() { return 1; }
console.log(a());
export { a };
"#,
        vec![logical_module(
            "mod_x",
            &[Member::renamed("Foo", "a"), Member::renamed("Bar", "b")],
        )],
    );
    let rejected = run_rejection_fixture(opts);
    assert!(
        rejected.stderr.contains("mod_x"),
        "rejection stderr should name the offending module mod_x; got:\n{}",
        rejected.stderr,
    );
    assert!(
        rejected.stderr.contains("`b`"),
        "rejection stderr should name the unresolved binding `b`; got:\n{}",
        rejected.stderr,
    );
}

#[test]
fn multiple_missing_bindings_reported_in_one_pass() {
    // Two unresolved claims across two distinct destination modules.
    // The pipeline must collect both and surface them together
    // rather than failing on the first.
    let opts = FixtureOpts::new(
        r#"function a() { return 1; }
function c() { return 3; }
console.log(a() + c());
export { a, c };
"#,
        vec![
            logical_module(
                "mod_x",
                &[
                    Member::renamed("Foo", "a"),
                    Member::renamed("Bar", "missing_one"),
                ],
            ),
            logical_module(
                "mod_y",
                &[
                    Member::renamed("Baz", "c"),
                    Member::renamed("Qux", "missing_two"),
                ],
            ),
        ],
    );
    let rejected = run_rejection_fixture(opts);
    for needle in ["missing_one", "missing_two", "mod_x", "mod_y"] {
        assert!(
            rejected.stderr.contains(needle),
            "rejection stderr should mention `{needle}`; got:\n{}",
            rejected.stderr,
        );
    }
}
