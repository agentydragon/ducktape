//! Cross-module dependency wiring tests. Black-box: runs `debundle`
//! with a JSONC spec and asserts on emitted modules + runtime equivalence.

use debundle_e2e_support::*;

#[test]
fn closes_an_extracted_module_over_its_helper_dependencies() {
    // Selecting only `b`. Its helper `a` must be pulled into the module file
    // (as an internal binding, not exported) and removed from residual.
    let fixture = run_logical_modules_e2e_fixture(
        "closes an extracted module over its helper dependencies",
        FixtureOpts::new(
            r#"const a = x => "h:" + x;
const b = x => a(x);
console.log(b("y"));
export { b };
"#,
            vec![logical_module("x", &[Member::new("b")])],
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
            r#"const q = "a";
function r() { return q; }
function s() { return "b" + r(); }
function t() { return s() + r(); }
console.log(t());
export { t, s };
"#,
            vec![
                logical_module("inner", &[Member::new("s")]),
                logical_module("outer", &[Member::new("t")]),
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
            r#"const q = o => o.a, r = o => o.b;
const s = o => q(o) ?? r(o);
console.log(s({ a: null, b: "c" }));
export { s };
"#,
            vec![
                logical_module(
                    "provider",
                    &[Member::renamed("u", "q"), Member::renamed("v", "r")],
                ),
                logical_module("consumer", &[Member::renamed("w", "s")]),
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
        r#"const { w } = await import("./static/app/modules/consumer.js");
console.log(w({ a: null, b: "d" }));
"#,
        "d\n",
    );
    assert_entry_output(&fixture, "c\n");
}
