//! Regression: a member's `comment:` must survive a rename. The
//! comment map is keyed by the ORIGINAL binding name, but
//! naturalization renames the declaration to the member's export name
//! before comments are attached — without re-keying, any member that
//! has both `comment:` and a rename silently loses its comment.

use debundle_e2e_support::*;

#[test]
fn member_comment_survives_export_name_rename() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function a() { return "ok"; }
console.log(a());
export { a };
"#,
        vec![logical_module(
            "x",
            &[Member::renamed("readable", "a").with_comment("Frobnicates the bazquux.")],
        )],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["// Frobnicates the bazquux.", "function readable("],
        &[],
    );
    assert_entry_output(&fixture, "ok\n");
}
