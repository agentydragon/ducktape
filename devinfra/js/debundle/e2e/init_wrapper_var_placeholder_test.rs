//! Init-wrapper placeholder declarations must use `var`, not `let`.
//!
//! When `materialize_logical_modules` routes a comma-list through the
//! init-wrapper pattern (`let X; ...; function init() { X = "..."; }`)
//! a circular module-load can read `X` before the placeholder line
//! has executed. Under `let`, that's `ReferenceError: Cannot access
//! 'X' before initialization`. Under `var`, the binding is hoisted
//! and reads as `undefined` — semantically harmless in cases where
//! consumers only access the binding lazily inside function bodies
//! (the common case).
//!
//! Hard to trigger TDZ from a single-chunk e2e (the harness's entry
//! body calls `init_<module>()` before evaluating any consumer code,
//! so the placeholder is always reached first). Pin the var-vs-let
//! choice on the emitted file directly; the Tana smoke is the
//! integration test for the actual cycle case.

use debundle_e2e_support::*;
use serde_json::json;
use std::fs;

#[test]
fn init_wrapper_emits_var_placeholders_for_routed_bindings() {
    // `b = compute()` is a Call init, which forces the init-wrapper
    // pattern across every declarator in the comma-list. After the
    // rewrite the destination has `<var-or-let> a; <var-or-let> b;
    // <var-or-let> z;` plus `function __dt_generated_init__mod_x() {
    // a = "a"; b = compute(); z = { ref: a }; }`. Pin the placeholder
    // kind to `var` so a circular load doesn't TDZ.
    let opts = FixtureOpts::new(
        r#"function compute() { return "b"; }
const a = "a", b = compute(), z = { ref: a };
console.log(a, b, z.ref);
export { a, b, z };
"#,
        vec![json!({
            "id": "logical__mod_x",
            "operation": "define_logical_module",
            "selector": { "chunkId": "static/app" },
            "target": { "path": "mod_x" },
            "members": [
                {
                    "id": "m_a",
                    "name": "a",
                    "selector": { "binding": { "name": "a" } },
                },
                {
                    "id": "m_b",
                    "name": "b",
                    "selector": { "binding": { "name": "b" } },
                },
                {
                    "id": "m_z",
                    "name": "z",
                    "selector": { "binding": { "name": "z" } },
                },
            ],
        })],
    );
    let fixture = run_logical_modules_e2e_fixture(opts);

    let mod_x = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_x.js"))
        .expect("read mod_x.js");

    // Pin the placeholder shape on `a` (the simplest binding). The
    // identical match against the assignment line below distinguishes
    // the placeholder from the init-wrapper assignment.
    assert!(
        mod_x.contains("var a;") || mod_x.contains("var a,"),
        "mod_x.js placeholder must be `var a;` (or comma-grouped); got:\n{mod_x}",
    );
    assert!(
        !mod_x.contains("let a;") && !mod_x.contains("let a,"),
        "mod_x.js placeholder must NOT use `let` (TDZ risk on cycles); got:\n{mod_x}",
    );

    // Behaviour preservation: module still loads + exports the
    // expected values via the init wrapper.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_x.js",
        &["a", "b", "z"],
        &[],
    );
    assert_entry_output(&fixture, "a b a\n");
}
