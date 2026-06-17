//! Regression: renaming a binding must not rewrite shorthand object
//! property keys or destructure pattern keys along with the binding.
//!
//! `{ a }` (object literal shorthand) reads binding `a` under property
//! key `a`; renaming the binding `a` -> `readable` must emit
//! `{ a: readable }` (key preserved), not `{ readable }` (the property
//! key silently changes). Likewise `const { a } = obj` declares binding
//! `a` from property `a`; the rename must emit
//! `const { a: readable } = obj` — `const { readable } = obj` reads
//! `obj.readable` instead.

use debundle_e2e_support::*;
use std::fs;

/// Both shorthand positions in one source: the destructure pattern
/// declaring `a` and an object-literal shorthand read of `a`. A
/// key-rewriting rename turns the runtime output into
/// `undefinedundefined`; the key-preserving emit keeps `AA`.
#[test]
fn chunk_rename_expands_shorthand_preserving_property_keys() {
    let opts = FixtureOpts::new(
        r#"const { a } = { a: "A" };
function f(o) {
  return o.a;
}
console.log(f({ a }) + a);
export { a };
"#,
        vec![],
    )
    .with_chunk_renames(chunk_rename("readable", "a"))
    .with_unassigned_mode(unassigned_mode_inline());
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "AA\n");
    // Both the pattern (`const { a: readable } = ...`) and the literal
    // (`f({ a: readable })`) must expand shorthand with the original
    // key. Count occurrences so one expanded site can't mask the other.
    let code = fs::read_to_string(fixture.out_root.join("static/app/entry.js")).unwrap();
    let expanded = code.matches("a: readable").count();
    assert!(
        expanded >= 2,
        "expected both shorthand sites expanded to `a: readable`; found {expanded} in:\n{code}",
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &["export { readable as a }"],
        &["{ readable }"],
    );
}

/// The same key-preservation contract on the naturalization path: a
/// logical-module member rename (`a` exported as `readable`) rewrites
/// the moved body via the plan-driven naturalizer, which must also
/// expand shorthand instead of changing keys.
#[test]
fn module_member_rename_expands_shorthand_preserving_property_keys() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const { a } = { a: "A" };
function get() {
  return { a }.a + a;
}
console.log(get());
export { a, get };
"#,
        vec![logical_module(
            "x",
            &[Member::renamed("readable", "a"), Member::new("get")],
        )],
    ));

    assert_entry_output(&fixture, "AA\n");
    let code = fs::read_to_string(fixture.out_root.join("static/app/modules/x.js")).unwrap();
    let expanded = code.matches("a: readable").count();
    assert!(
        expanded >= 2,
        "expected both shorthand sites expanded to `a: readable`; found {expanded} in:\n{code}",
    );
}
