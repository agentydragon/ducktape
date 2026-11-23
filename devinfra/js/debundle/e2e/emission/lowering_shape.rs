//! End-to-end behavior-preservation tests for the debundler.
//!
//! Drives the `debundle` CLI through a YAML spec and asserts on the
//! emitted file tree.

use debundle_e2e_support::*;

#[test]
fn emitted_js_files_end_with_one_newline_and_no_blank_line() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const a = 1;
const b = 2;
console.log(a + b);
export { a, b };
"#,
            vec![logical_module("mod_a", &[Member::new("a")])],
        )
        .with_import_order(&["mod_a", "residual"]),
    );

    assert_file_ends_with_single_newline(&fixture.out_root, "static/app/entry.js");
    assert_file_ends_with_single_newline(&fixture.out_root, "static/app/modules/mod_a.js");
    assert_file_ends_with_single_newline(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
    );
    assert_entry_output(&fixture, "3\n");
}
