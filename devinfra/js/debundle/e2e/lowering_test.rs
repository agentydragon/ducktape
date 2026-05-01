//! End-to-end behavior-preservation tests for the debundler.
//!
//! Drives the `debundle_rust` CLI through a JSONC spec and asserts on the
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
            "const a = 1, b = 2, c = a + b;\n\
             const z = \"z\";\n\
             console.log(c);\n\
             export { c, z };\n",
            vec![logical_module("x", &[Member::var("c")])],
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
            "function a() { return b(); }\n\
             const c = a();\n\
             function b() { return \"b\"; }\n\
             console.log(c);\n\
             export { c };\n",
            vec![
                logical_module("helper", &[Member::func("a"), Member::func("b")]),
                logical_module("consumer", &[Member::var("c")]),
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
            "const q = () => \"a\";\n\
             const b = ({ a: c = q } = {}) => c();\n\
             console.log(b({}), b({ a: () => \"b\" }));\n\
             export { q, b };\n",
            vec![logical_module(
                "x",
                &[
                    Member::renamed_var("alpha", "q"),
                    Member::renamed_var("beta", "b"),
                ],
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
            "class A { static label() { return \"a\"; } }\n\
             function b() { return A.label(); }\n\
             console.log(b());\n\
             export { A, b };\n",
            vec![logical_module(
                "x",
                &[Member::class("A"), Member::func("b")],
            )],
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
            "var A = ((B) => { B.X = \"x\"; B.Y = \"y\"; return B; })(A || {});\n\
             function b() { return A.X; }\n\
             console.log(b());\n\
             export { b };\n",
            vec![logical_module("x", &[Member::var("A"), Member::func("b")])],
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
            "const a = 1;\n\
             function b() { return a; }\n\
             const c = b();\n\
             console.log(c);\n\
             export { c };\n",
            vec![logical_module("x", &[Member::var("a"), Member::func("b")])],
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
            "globalThis.log = \"\";\n\
             const a = (globalThis.log += \"a\", 1);\n\
             console.log(globalThis.log, a);\n\
             export { a };\n",
            vec![logical_module("x", &[Member::var("a")])],
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
            "const a = 1;\n\
             function b() { return a; }\n\
             console.log(b());\n\
             export { b };\n",
            vec![logical_module(
                "x",
                &[Member::var("a"), Member::renamed_func("a", "b")],
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
