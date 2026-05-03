//! Init-wrapper splits declarators by hoist-safety.
//!
//! When `materialize_logical_modules` routes a comma-list through
//! the init-wrapper pattern, declarators whose init is a literal
//! (or a tree of literals) stay inline at the destination's top
//! with their original kind, so a cyclic consumer that reads them
//! at its own top sees the value immediately. Non-hoistable
//! initializers (calls, member access, ident references) go through
//! the wrapper: `var X;` placeholder + assignment inside the init
//! function called from the residual entry.
//!
//! The wrapper placeholder is `var`, not `let`: under `let`, a
//! cyclic read before the placeholder line executes TDZ-errors with
//! `ReferenceError: Cannot access 'X' before initialization`. Under
//! `var`, the binding is hoisted to module load and reads as
//! `undefined` until the init runs.
//!
//! Hard to trigger the cyclic read from a single-chunk e2e (the
//! harness's entry calls every `init_<module>()` before evaluating
//! consumer code), so pin the emitted shape directly. The Tana
//! smoke is the integration test for the actual cycle case.

use debundle_e2e_support::*;
use serde_json::json;
use std::fs;

#[test]
fn init_wrapper_keeps_hoistable_inits_inline_and_wraps_unsafe_ones_in_var() {
    // `b = compute()` is a Call init, which forces the init-wrapper
    // pattern across every var-decl in the module. With the split:
    //   - `a = "a"` is hoistable (Lit), stays inline as `const a = "a"`.
    //   - `b = compute()` is unsafe, lowers to `var b;` placeholder
    //     plus `b = compute()` in the init function.
    //   - `z = { ref: a }` is non-hoistable (the Object value is an
    //     Ident, not a literal) — also goes through the wrapper.
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

    // Hoistable: `a = "a"` survives inline as `const a = "a"`.
    assert!(
        mod_x.contains("const a = \"a\""),
        "mod_x.js must keep hoistable Lit init inline as `const a = \"a\"`; got:\n{mod_x}",
    );
    // Unsafe: `b = compute()` is wrapped — placeholder is `var`, not `let`.
    assert!(
        mod_x.contains("var b") && !mod_x.contains("let b"),
        "mod_x.js placeholder for `b` must be `var` (no TDZ risk); got:\n{mod_x}",
    );
    // The wrapper holds the unsafe assignment.
    assert!(
        mod_x.contains("b = compute()"),
        "mod_x.js init wrapper must assign `b = compute()`; got:\n{mod_x}",
    );

    // Behaviour preservation: module still loads + exports the
    // expected values, regardless of split.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_x.js",
        &["a", "b", "z"],
        &[],
    );
    assert_entry_output(&fixture, "a b a\n");
}
