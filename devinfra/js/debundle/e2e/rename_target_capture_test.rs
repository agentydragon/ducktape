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
        r#"var source_value = "A";
function f(taken_name) {
  return source_value + taken_name;
}
console.log(f("B"));
export { source_value, f };
"#,
        vec![],
    )
    .with_chunk_renames(chunk_rename("taken_name", "source_value"))
    .with_unassigned_mode(unassigned_mode_inline());
    expect_rejection_containing_all(opts, &["source_value", "taken_name"]);
}

/// Plan-driven naturalization path: a logical-module member rename
/// (`a` exported as `b`) applies module-wide to the moved body, where
/// `f`'s parameter `b` would capture the renamed reads of `a`.
#[test]
fn module_member_rename_target_captured_by_nested_binding_is_rejected() {
    let opts = FixtureOpts::new(
        r#"var source_value = "A";
function f(taken_name) {
  return source_value + taken_name;
}
console.log(f("B"));
export { source_value, f };
"#,
        vec![logical_module(
            "x",
            &[
                Member::renamed("taken_name", "source_value"),
                Member::new("f"),
            ],
        )],
    );
    expect_rejection_containing_all(opts, &["source_value", "taken_name"]);
}

/// Sibling collision: the rename target is another top-level binding
/// of the same module body (a destructure sibling pulled along with
/// the claimed binding). Renaming `origin_name` -> `sibling_name` would
/// declare `sibling_name` twice in one pattern.
#[test]
fn module_member_rename_target_colliding_with_sibling_binding_is_rejected() {
    let opts = FixtureOpts::new(
        r#"const { origin_name, sibling_name } = { origin_name: "A", sibling_name: "R" };
console.log(origin_name + sibling_name);
export { origin_name, sibling_name };
"#,
        vec![logical_module(
            "x",
            &[Member::renamed("sibling_name", "origin_name")],
        )],
    );
    expect_rejection_containing_all(opts, &["origin_name", "sibling_name"]);
}
