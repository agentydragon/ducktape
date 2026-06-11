//! End-to-end coverage for `source_match` syntactic holes.
//!
//! Expression holes are selector-local identifier expressions prefixed with
//! `EXPR_`. Statement holes are selector-local bare expression statements
//! prefixed with `STMT_`.

use debundle_e2e_support::*;

#[test]
fn member_source_match_expr_prefix_holes_match_arbitrary_expression_subtrees() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const actual = Math.max(Number.parseInt("7", 10), [1, 2, 3].length);
console.log(actual);
export { actual };
"#,
        vec![logical_module(
            "calc",
            &[Member::source_alpha_with_syntactic_holes(
                "calc_value",
                r#"const readable = Math.max(EXPR_LEFT, EXPR_RIGHT);"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "7\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &["calc_value"],
        &["actual"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &[
            "Math.max",
            r#"Number.parseInt("7", 10)"#,
            "].length",
            "const calc_value",
        ],
        &[],
    );
}

#[test]
fn member_source_match_many_expr_holes_match_positionally() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const actual = [
  1 + 2,
  Number.parseInt("4", 10),
  ({ x: 5 }).x,
  [6, 7].length,
  Math.max(8, 9),
  true ? 10 : 11,
];
console.log(actual.length);
export { actual };
"#,
        vec![logical_module(
            "calc",
            &[Member::source_alpha_with_syntactic_holes(
                "calc_value",
                r#"const readable = [
  EXPR_A,
  EXPR_B,
  EXPR_C,
  EXPR_D,
  EXPR_E,
  EXPR_F,
];"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "6\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &[
            "1 + 2",
            r#"Number.parseInt("4", 10)"#,
            "x: 5",
            "}).x",
            "7",
            "].length",
            "Math.max(8, 9)",
            "true ? 10 : 11",
        ],
        &[],
    );
}

#[test]
fn binding_group_source_match_expr_prefix_holes_match_each_target_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var first = 1 + 2, second = Number.parseInt("4", 10);
console.log(first + second);
export { first, second };
"#,
        vec![logical_module_with_binding_groups(
            "pair",
            &[],
            &[BindingGroup::source_alpha_with_syntactic_holes(
                r#"var left = EXPR_LEFT, right = EXPR_RIGHT;"#,
                &[("left", "first_value"), ("right", "second_value")],
            )],
        )],
    ));

    assert_entry_output(&fixture, "7\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/pair.js",
        &["first_value", "second_value"],
        &["first", "second"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/pair.js",
        &[
            "var first_value = 1 + 2",
            r#"var second_value = Number.parseInt("4", 10)"#,
        ],
        &[],
    );
}

#[test]
fn anonymous_source_match_stmt_prefix_hole_matches_arbitrary_nested_statement() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"if (true) {
  console.log("setup");
  console.log("done");
}
const marker = "ready";
export { marker };
"#,
        vec![logical_module_with_anon_alpha_syntactic_holes(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  STMT_SETUP;
  console.log("done");
}"#,
        )],
    ));

    assert_entry_output(&fixture, "setup\ndone\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/init.js",
        &[
            r#"console.log("setup")"#,
            r#"console.log("done")"#,
            "const marker",
        ],
        &[],
    );
}

#[test]
fn anonymous_source_match_stmt_prefix_holes_still_reject_ambiguous_matches() {
    let opts = FixtureOpts::new(
        r#"if (true) {
  console.log("first");
  console.log("done");
}
const marker = "ready";
if (true) {
  console.log("second");
  console.log("done");
}
export { marker };
"#,
        vec![logical_module_with_anon_alpha_syntactic_holes(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  STMT_SETUP;
  console.log("done");
}"#,
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::init",
            "ambiguous",
            "STMT_SETUP",
            r#"console.log("done")"#,
        ],
    );
}
