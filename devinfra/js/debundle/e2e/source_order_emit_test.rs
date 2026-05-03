//! A realizable spec emits source-order ESM modules: each
//! emitted module is a plain ESM file with imports, declarations
//! in source order with their original kind (`const`/`let`/
//! `class`/`function`), and exports. The ESM linker handles
//! cross-module init order through the dep graph.

use debundle_e2e_support::*;
use std::fs;

#[test]
fn realizable_spec_emits_source_order_modules() {
    // `A = f()` / `B = g()` are call-initialised consts; source-
    // order emit lands them inline as `const A = f();` /
    // `const B = g();` in their respective modules with cross-
    // module imports as needed.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"function f() { return "a"; }
function g() { return "b"; }
const A = f();
const B = g();
console.log(A, B);
export { A, B };
"#,
        vec![
            logical_module("mod_a", &[Member::new("A")]),
            logical_module("mod_b", &[Member::new("B")]),
        ],
    ));

    let mod_a = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_a.js"))
        .expect("read mod_a.js");
    let mod_b = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_b.js"))
        .expect("read mod_b.js");

    // Each owned binding emits as a source-order `const` with
    // its original initializer in place.
    assert!(
        mod_a.contains("const A = f()"),
        "mod_a.js must keep `const A = f()` inline; got:\n{mod_a}",
    );
    assert!(
        mod_b.contains("const B = g()"),
        "mod_b.js must keep `const B = g()` inline; got:\n{mod_b}",
    );

    // Behaviour preservation.
    assert_entry_output(&fixture, "a b\n");
}
