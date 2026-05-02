//! End-to-end behavior-preservation tests for the debundler.
//!
//! Drives the `debundle` CLI through a JSONC spec and asserts on the
//! emitted file tree.

use debundle_e2e_support::*;

// --- Behavior preservation -------------------------------------------------

#[test]
fn preserves_source_order_evaluation_across_split_declarator_fragments() {
    // Single declaration with cross-fragment dep `c = a + b`. Selecting `c`
    // forces the closure to pull `a` and `b` into the module while `z` stays
    // in residual.
    let fixture = run_logical_modules_e2e_fixture(
        "preserves source-order evaluation across split declarator fragments",
        FixtureOpts::new(
            r#"const a = 1, b = 2, c = a + b;
const z = "z";
console.log(c);
export { c, z };
"#,
            vec![logical_module("x", &[Member::new("c")])],
        ),
    );
    assert_module_exports(&fixture.out_root, "static/app/modules/x.js", &["c"], &[]);
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &[],
        &["c"],
    );
    assert_entry_output(&fixture, "3\n");
}

#[test]
fn preserves_function_declaration_hoisting_across_modules() {
    let fixture = run_logical_modules_e2e_fixture(
        "preserves function declaration hoisting across modules",
        FixtureOpts::new(
            r#"function a() { return b(); }
const c = a();
function b() { return "b"; }
console.log(c);
export { c };
"#,
            vec![
                logical_module("helper", &[Member::new("a"), Member::new("b")]),
                logical_module("consumer", &[Member::new("c")]),
            ],
        ),
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/helper.js",
        &["a"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/consumer.js",
        &["c"],
        &[],
    );
    assert_entry_output(&fixture, "b\n");
}

#[test]
fn preserves_default_references_after_readable_and_explicit_renames() {
    let fixture = run_logical_modules_e2e_fixture(
        "preserves default references after readable and explicit renames",
        FixtureOpts::new(
            r#"const q = () => "a";
const b = ({ a: c = q } = {}) => c();
console.log(b({}), b({ a: () => "b" }));
export { q, b };
"#,
            vec![logical_module(
                "x",
                &[Member::renamed("alpha", "q"), Member::renamed("beta", "b")],
            )],
        ),
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["alpha", "beta"],
        &[],
    );
    assert_entry_output(&fixture, "a b\n");
}

#[test]
fn extracts_a_class_declaration_without_changing_runtime() {
    let fixture = run_logical_modules_e2e_fixture(
        "extracts a class declaration without changing runtime",
        FixtureOpts::new(
            r#"class A { static label() { return "a"; } }
function b() { return A.label(); }
console.log(b());
export { A, b };
"#,
            vec![logical_module("x", &[Member::new("A"), Member::new("b")])],
        ),
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["class A", "function b()"],
        &[],
    );
    assert_entry_output(&fixture, "a\n");
}

#[test]
fn lowers_ts_enum_style_self_referencing_var_declarations_correctly() {
    let fixture = run_logical_modules_e2e_fixture(
        "lowers TS-enum-style self-referencing var declarations correctly",
        FixtureOpts::new(
            r#"var A = ((B) => { B.X = "x"; B.Y = "y"; return B; })(A || {});
function b() { return A.X; }
console.log(b());
export { b };
"#,
            vec![logical_module("x", &[Member::new("A"), Member::new("b")])],
        ),
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["var A = ", "function b()"],
        &[],
    );
    assert_entry_output(&fixture, "x\n");
}

// --- Module structure: plain-import vs. init-wrapper -----------------------

#[test]
fn emits_a_plain_import_without_an_init_wrapper_for_a_pure_module() {
    // The runtime side effect references `c` (residual), not the extracted
    // bindings, so nothing gets attached to the module's init wrapper.
    let fixture = run_logical_modules_e2e_fixture(
        "emits a plain import without an init wrapper for a pure module",
        FixtureOpts::new(
            r#"const a = 1;
function b() { return a; }
const c = b();
console.log(c);
export { c };
"#,
            vec![logical_module("x", &[Member::new("a"), Member::new("b")])],
        ),
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["const a = 1;", "function b()"],
        &["__dt_generated_init__x"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &[],
        &["__dt_generated_init__x"],
    );
    assert_entry_output(&fixture, "1\n");
}

#[test]
fn emits_an_init_wrapper_when_the_extracted_module_has_top_level_effects() {
    // The initializer of `a` has a side effect (the comma expression),
    // forcing the extractor to emit an init wrapper rather than a plain const.
    let fixture = run_logical_modules_e2e_fixture(
        "emits an init wrapper when the extracted module has top-level effects",
        FixtureOpts::new(
            r#"globalThis.log = "";
const a = (globalThis.log += "a", 1);
console.log(globalThis.log, a);
export { a };
"#,
            vec![logical_module("x", &[Member::new("a")])],
        ),
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["export function __dt_generated_init__x"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &["__dt_generated_init__x();"],
        &[],
    );
    assert_entry_output(&fixture, "a 1\n");
}

// --- Rejections ------------------------------------------------------------

#[test]
fn rejects_extraction_with_a_propagated_final_name_collision() {
    // Two members both renamed to "a" — one from the variable `a`, one from
    // function `b`. The extractor should refuse before emitting.
    expect_logical_modules_e2e_rejection(
        "rejects extraction with a propagated final-name collision",
        FixtureOpts::new(
            r#"const a = 1;
function b() { return a; }
console.log(b());
export { b };
"#,
            vec![logical_module(
                "x",
                &[Member::new("a"), Member::renamed("a", "b")],
            )],
        ),
        &[
            "propagated final name collision",
            "conflicts with existing top-level binding",
            "duplicate binding name",
            "duplicate exported logical names",
        ],
    );
}
