//! A realizable spec emits source-order ESM modules with no
//! init-wrapper scaffolding.
//!
//! Each emitted module is a plain ESM file: imports,
//! declarations in source order with their original kind
//! (`const`/`let`/`class`/`function`), exports. The ESM linker
//! handles cross-module init order through the dep graph. There
//! is no `__dt_generated_init__` symbol, no idempotency flag,
//! no `var X; X = ...` placeholder pattern. This test pins that
//! shape on a simple split where every initializer is a function
//! call.

use debundle_e2e_support::*;
use std::fs;

#[test]
fn realizable_spec_emits_source_order_modules_without_init_wrappers() {
    // `A = f()` / `B = g()` are call-initialised consts. Source-
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

    // No init-wrapper scaffolding survives.
    for (label, src) in [("mod_a.js", &mod_a), ("mod_b.js", &mod_b)] {
        assert!(
            !src.contains("__dt_generated_init__"),
            "{label} must not contain `__dt_generated_init__`; source-order \
             emit replaces the init wrapper. got:\n{src}",
        );
        assert!(
            !src.contains("__dt_inited_"),
            "{label} must not contain idempotency flags; got:\n{src}",
        );
    }

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

    // Behaviour preservation under source-order emit.
    assert_entry_output(&fixture, "a b\n");
}
