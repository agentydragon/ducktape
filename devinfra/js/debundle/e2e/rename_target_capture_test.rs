//! Regression: a rename must never be captured by an inner binding of
//! the TARGET name. Renaming `a` -> `b` inside
//! `function f(b) { return a + b; }` would rewrite `a` to `b` and
//! silently capture the parameter (`return b + b`). Soundness over
//! completeness: the pipeline rejects such a spec with a diagnostic
//! instead of emitting a miscompiled body.

use debundle_e2e_support::*;

/// `chunk_renames` path: the rename applies to entry's body, where a
/// nested function binds the target name and reads the source binding.
#[test]
fn chunk_rename_target_captured_by_nested_binding_is_rejected() {
    let opts = FixtureOpts::new(
        r#"var a = "A";
function f(b) {
  return a + b;
}
console.log(f("B"));
export { a, f };
"#,
        vec![],
    )
    .with_chunk_renames(chunk_rename("b", "a"))
    .with_unassigned_mode(unassigned_mode_inline());
    expect_rejection_containing_all(opts, &["captured by a nested binding"]);
}

/// Plan-driven naturalization path: a logical-module member rename
/// (`a` exported as `b`) applies module-wide to the moved body, where
/// `f`'s parameter `b` would capture the renamed reads of `a`.
#[test]
fn module_member_rename_target_captured_by_nested_binding_is_rejected() {
    let opts = FixtureOpts::new(
        r#"var a = "A";
function f(b) {
  return a + b;
}
console.log(f("B"));
export { a, f };
"#,
        vec![logical_module(
            "x",
            &[Member::renamed("b", "a"), Member::new("f")],
        )],
    );
    expect_rejection_containing_all(opts, &["captured by a nested binding"]);
}

/// Sibling collision: the rename target is another top-level binding
/// of the same module body (a destructure sibling pulled along with
/// the claimed binding). Renaming `a` -> `readable` would declare
/// `readable` twice in one pattern.
#[test]
fn module_member_rename_target_colliding_with_sibling_binding_is_rejected() {
    let opts = FixtureOpts::new(
        r#"const { a, readable } = { a: "A", readable: "R" };
console.log(a + readable);
export { a, readable };
"#,
        vec![logical_module("x", &[Member::renamed("readable", "a")])],
    );
    expect_rejection_containing_all(opts, &["collides"]);
}
