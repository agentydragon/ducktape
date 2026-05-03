//! Cross-module dependency wiring tests. Black-box: runs `debundle`
//! with a JSONC spec and asserts on emitted modules + runtime equivalence.

use debundle_e2e_support::*;

#[test]
fn extracted_module_imports_unowned_helper_from_residual() {
    // Spec claims only `b` for mod_x. Its helper `a` is unclaimed,
    // so post Phase 1.7 (no closure pass), `a` stays in the
    // residual entry; mod_x imports it.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const a = x => "h:" + x;
const b = x => a(x);
console.log(b("y"));
export { b };
"#,
        vec![logical_module("x", &[Member::new("b")])],
    ));
    assert_module_exports(&fixture.out_root, "static/app/modules/x.js", &["b"], &[]);
    // mod_x imports `a` from residual rather than carrying its
    // declaration locally — the explicit spec is the only routing,
    // closure no longer pulls helpers along.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["import { a }"],
        &["a = "],
    );
    assert_entry_output(&fixture, "h:y\n");
}

#[test]
fn explicit_modules_share_a_residual_helper_via_imports() {
    // Without closure, helper `r` (unclaimed by either explicit
    // module) stays in residual. Both `inner` (owns `s`, which
    // calls `r`) and `outer` (owns `t`, which calls `s` and `r`)
    // import `r` from residual.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
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
    ));
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/inner.js",
        &["s"],
        &["r", "t"],
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
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
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
    ));
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
