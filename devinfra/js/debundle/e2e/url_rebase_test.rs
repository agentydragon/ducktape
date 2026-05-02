//! URL-rebasing tests: when a function containing a relative URL literal
//! moves from the entry into a nested module, the URL string must be
//! rewritten so it still resolves to the same target.

use debundle_e2e_support::*;

#[test]
fn rebases_worker_constructor_url_to_runtime_relative_module_url() {
    let fixture = run_logical_modules_e2e_fixture(
        "rebases worker constructor URL to runtime-relative module URL",
        FixtureOpts::new(
            r#"function a() { return new Worker("./b.js"); }
console.log(typeof a);
export { a };
"#,
            vec![logical_module("x", &[Member::new("a")])],
        ),
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &[r#"new Worker(new URL("../b.js", import.meta.url))"#],
        &[],
    );
    assert_entry_output(&fixture, "function\n");
}

#[test]
fn rebases_dynamic_import_specifiers_to_runtime_relative_paths() {
    let fixture = run_logical_modules_e2e_fixture(
        "rebases dynamic import specifiers to runtime-relative paths",
        FixtureOpts::new(
            r#"async function a() { const m = await import("./b.js"); return m.x; }
console.log(typeof a);
export { a };
"#,
            vec![logical_module("x", &[Member::new("a")])],
        ),
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &[r#"await import("../b.js")"#],
        &[],
    );
    assert_entry_output(&fixture, "function\n");
}
