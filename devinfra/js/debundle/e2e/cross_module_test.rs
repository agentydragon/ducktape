//! Cross-module dependency wiring tests. Black-box: runs `debundle_rust`
//! with a JSONC spec and asserts on emitted modules + runtime equivalence.

use debundle_e2e_support::*;

#[test]
fn closes_an_extracted_module_over_its_helper_dependencies() {
    // Selecting only `b`. Its helper `a` must be pulled into the module file
    // (as an internal binding, not exported) and removed from residual.
    let fixture = run_logical_modules_e2e_fixture(
        "closes an extracted module over its helper dependencies",
        FixtureOpts::new(
            "const a = x => \"h:\" + x;\n\
             const b = x => a(x);\n\
             console.log(b(\"y\"));\n\
             export { b };\n",
            vec![logical_module("x", &[Member::var("b")])],
        ),
    );
    assert_module_exports(&fixture.out_root, "static/app/modules/x.js", &["b"], &[]);
    assert_module_source(&fixture.out_root, "static/app/modules/x.js", &["a = "], &[]);
    assert_entry_output(&fixture, "h:y\n");
}

#[test]
fn duplicates_a_shared_bootstrap_dependency_into_each_named_module() {
    let fixture = run_logical_modules_e2e_fixture(
        "duplicates a shared bootstrap dependency into each named module",
        FixtureOpts::new(
            "const q = \"a\";\n\
             function r() { return q; }\n\
             function s() { return \"b\" + r(); }\n\
             function t() { return s() + r(); }\n\
             console.log(t());\n\
             export { t, s };\n",
            vec![
                logical_module("inner", &[Member::func("s")]),
                logical_module("outer", &[Member::func("t")]),
            ],
        ),
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/inner.js",
        &["r", "s"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/outer.js",
        &["t"],
        &["r", "s"],
    );
    assert_entry_output(&fixture, "baa\n");
}

#[test]
fn imports_renamed_dependencies_across_split_declarators() {
    let fixture = run_logical_modules_e2e_fixture(
        "imports renamed dependencies across split declarators",
        FixtureOpts::new(
            "const q = o => o.a, r = o => o.b;\n\
             const s = o => q(o) ?? r(o);\n\
             console.log(s({ a: null, b: \"c\" }));\n\
             export { s };\n",
            vec![
                logical_module(
                    "provider",
                    &[Member::renamed_var("u", "q"), Member::renamed_var("v", "r")],
                ),
                logical_module("consumer", &[Member::renamed_var("w", "s")]),
            ],
        ),
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/provider.js",
        &["u", "v"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/consumer.js",
        &["w"],
        &["u"],
    );
    assert_generated_module_after_entry_script(
        &fixture.out_root,
        "const { w } = await import(\"./static/app/modules/consumer.js\");\n\
         console.log(w({ a: null, b: \"d\" }));\n",
        "d\n",
    );
    assert_entry_output(&fixture, "c\n");
}
