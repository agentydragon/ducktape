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
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const a = 1, b = 2, c = a + b;
const z = "z";
console.log(c);
export { c, z };
"#,
        vec![logical_module("x", &[Member::new("c")])],
    ));
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
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
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
    ));
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
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const q = () => "a";
const b = ({ a: c = q } = {}) => c();
console.log(b({}), b({ a: () => "b" }));
export { q, b };
"#,
        vec![logical_module(
            "x",
            &[Member::renamed("alpha", "q"), Member::renamed("beta", "b")],
        )],
    ));
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
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"class A { static label() { return "a"; } }
function b() { return A.label(); }
console.log(b());
export { A, b };
"#,
        vec![logical_module("x", &[Member::new("A"), Member::new("b")])],
    ));
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
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"var A = ((B) => { B.X = "x"; B.Y = "y"; return B; })(A || {});
function b() { return A.X; }
console.log(b());
export { b };
"#,
        vec![logical_module("x", &[Member::new("A"), Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["var A = ", "function b()"],
        &[],
    );
    assert_entry_output(&fixture, "x\n");
}

// --- Emit shape -----------------------------------------------------------

#[test]
fn emits_extracted_decls_inline_in_their_module() {
    // The runtime side effect references `c` (residual), not the
    // extracted bindings; mod_x carries the original `const`/
    // `function` declarations as-is.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const a = 1;
function b() { return a; }
const c = b();
console.log(c);
export { c };
"#,
        vec![logical_module("x", &[Member::new("a"), Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["const a = 1;", "function b()"],
        &[],
    );
    assert_entry_output(&fixture, "1\n");
}

#[test]
fn emits_top_level_effects_inline_in_extracted_module() {
    // The initializer of `a` has a side effect (the comma expression).
    // Source-order emit lands the const inline.
    //
    // We don't assert on entry stdout here: source-order emit
    // evaluates mod_x at link time (before any entry top-level code
    // runs), which is semantically different from the original
    // chunk's "evaluate inline at this source ordinal". Specs that
    // care about exact side-effect interleaving need G' edge
    // tracking, which is a known limitation (see DESIGN.md "Side-
    // effect ordering").
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"globalThis.log = "";
const a = (globalThis.log += "a", 1);
console.log(globalThis.log, a);
export { a };
"#,
        vec![logical_module("x", &[Member::new("a")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["const a = (globalThis.log"],
        &[],
    );
}

// --- Rejections ------------------------------------------------------------

#[test]
fn rejects_extraction_with_a_propagated_final_name_collision() {
    // Two members both renamed to "a" — one from the variable `a`, one from
    // function `b`. The extractor should refuse before emitting.
    expect_logical_modules_e2e_rejection(
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
