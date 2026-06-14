//! End-to-end coverage for `source_match` syntactic holes.
//!
//! Single-node holes:
//! - Expression holes (`EXPR` / `EXPR_name`) are selector-local
//!   identifier expressions; they match one arbitrary expression subtree.
//! - Statement holes (`STMT` / `STMT_name`) are selector-local bare
//!   expression statements; they match exactly one statement.
//! - `ANYTHING` is anonymous parse-position sugar: in an expression
//!   position it behaves like `EXPR`, as a bare statement like `STMT`,
//!   as a non-declarator binding pattern like an anonymous pattern hole,
//!   as a variable declarator like `DECLARATORS`, as an object-literal
//!   shorthand property like an anonymous property-list hole, and as a
//!   no-init class field like `CLASS_REST`.
//!
//! The bare single-node keyword matches independently at every occurrence;
//! the named form binds for cross-occurrence equality.
//!
//! List holes (variable-length):
//! - `ARGS` / `ARGS_name` in a call or `new` argument list absorbs a run
//!   of arguments (including an empty run) — e.g. match a stable
//!   important argument without spelling noisy generated siblings.
//! - `STMT_LIST` / `STMT_LIST_name;` in a block body absorbs a run of
//!   statements (including an empty run) — e.g. a method body you don't
//!   want to pin.
//! - `CLASS_REST;` as a class field absorbs a run of class members —
//!   e.g. "match this class by these members, ignore the rest".
//! - `DECLARATORS` / `DECLARATORS_name = null` in a variable declaration
//!   absorbs a run of declarators — e.g. match a few stable entries in a
//!   wider `const` list without spelling unrelated siblings.
//!
//! List-hole suffixes are labels for readability; they do not bind the
//! absorbed run for equality.
//!
//! Several list holes may appear in one block or class body: they split
//! the pinned statements/members into an ordered subsequence with gaps,
//! so a selector can bracket a few stable members with `CLASS_REST;`
//! holes and match any class that contains them in that order.

use debundle_e2e_support::*;

#[test]
fn member_source_match_alpha_all_allows_name_reuse_in_sibling_function_scopes() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function actual(items) {
  return items
    .map((x) => {
      const y = [];
      y.push(x.label);
      return y.join(":");
    })
    .filter((x) => x !== "")
    .map((x) => x.toUpperCase())
    .join(",");
}
console.log(actual([{ label: "left" }, { label: "right" }]));
export { actual };
"#,
        vec![logical_module(
            "format",
            &[Member::source_alpha(
                "format_items",
                r#"function readable(items) {
  return items
    .map((item) => {
      const lines = [];
      lines.push(item.label);
      return lines.join(":");
    })
    .filter((line) => line !== "")
    .map((line) => line.toUpperCase())
    .join(",");
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "LEFT,RIGHT\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/format.js",
        &["function format_items", ".filter", ".map"],
        &["function actual", "function readable"],
    );
}

#[test]
fn member_source_match_alpha_all_with_holes_allows_name_reuse_in_sibling_function_scopes() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function actual(items) {
  return items
    .map((x) => x.label.trim())
    .filter((x) => x !== "")
    .map((x) => x.toUpperCase())
    .join(",");
}
console.log(actual([{ label: " left " }, { label: " right " }]));
export { actual };
"#,
        vec![logical_module(
            "format",
            &[Member::source_alpha(
                "format_items",
                r#"function readable(items) {
  return items
    .map((item) => EXPR)
    .filter((line) => line !== "")
    .map((line) => line.toUpperCase())
    .join(",");
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "LEFT,RIGHT\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/format.js",
        &["function format_items", ".filter", ".map"],
        &["EXPR", "function actual", "function readable"],
    );
}

#[test]
fn member_source_match_treats_object_shorthand_as_explicit_same_name_property() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function actual(apiMode, enabled) {
  return { apiMode, enabled };
}
console.log(JSON.stringify(actual("preview", true)));
export { actual };
"#,
        vec![logical_module(
            "config",
            &[Member::source_alpha(
                "makeConfig",
                r#"function readable(apiMode, enabled) {
  return { apiMode: apiMode, enabled: enabled };
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "{\"apiMode\":\"preview\",\"enabled\":true}\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/config.js",
        &["function makeConfig", "return {", "apiMode,", "enabled"],
        &["function actual", "function readable"],
    );
}

#[test]
fn member_source_match_object_property_hole_skips_arbitrary_key_values() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function makeValue(label) {
  return label.toUpperCase();
}
const actual = {
  requiredKey: makeValue("required"),
  generatedAlpha: makeValue("alpha"),
  nested: { inner: makeValue("nested") },
  ...{ spreadValue: makeValue("spread") },
  anotherKey: makeValue("another"),
};
console.log(actual.requiredKey, actual.anotherKey, actual.spreadValue);
export { actual };
"#,
        vec![logical_module(
            "config",
            &[Member::source_alpha_target(
                "config_object",
                "readable",
                r#"const readable = {
  requiredKey: EXPR,
  OBJECT_PROPS_GENERATED,
  anotherKey: EXPR,
};"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "REQUIRED ANOTHER SPREAD\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/config.js",
        &["config_object"],
        &["actual"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/config.js",
        &[
            "const config_object",
            "generatedAlpha",
            "spreadValue",
            "anotherKey",
        ],
        &["OBJECT_PROPS_GENERATED", "readable"],
    );
}

#[test]
fn member_source_match_anything_object_property_hole_skips_arbitrary_key_values() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function makeValue(label) {
  return label.toUpperCase();
}
const actual = {
  requiredKey: makeValue("required"),
  generatedAlpha: makeValue("alpha"),
  nested: { inner: makeValue("nested") },
  ...{ spreadValue: makeValue("spread") },
  anotherKey: makeValue("another"),
};
console.log(actual.requiredKey, actual.anotherKey, actual.spreadValue);
export { actual };
"#,
        vec![logical_module(
            "config",
            &[Member::source_alpha_target(
                "config_object",
                "readable",
                r#"const readable = {
  requiredKey: ANYTHING,
  ANYTHING,
  anotherKey: ANYTHING,
};"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "REQUIRED ANOTHER SPREAD\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/config.js",
        &["config_object"],
        &["actual"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/config.js",
        &[
            "const config_object",
            "generatedAlpha",
            "spreadValue",
            "anotherKey",
        ],
        &["ANYTHING", "readable"],
    );
}

#[test]
fn source_match_anything_object_key_reports_unsupported_position() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const actual = { mode: "runtime" };
console.log(actual.mode);
export { actual };
"#,
            vec![logical_module(
                "config",
                &[Member::source_alpha(
                    "makeConfig",
                    r#"const readable = { ANYTHING: "runtime" };"#,
                )],
            )],
        ),
        &[
            "ANYTHING",
            "unsupported",
            "object property key",
            "key: ANYTHING",
        ],
    );
}

#[test]
fn member_source_match_treats_destructure_shorthand_as_explicit_same_name_property() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function actual({ apiMode, enabled }) {
  return `${apiMode}:${enabled ? "on" : "off"}`;
}
console.log(actual({ apiMode: "preview", enabled: true }));
export { actual };
"#,
        vec![logical_module(
            "config",
            &[Member::source_alpha(
                "describeConfig",
                r#"function readable({ apiMode: apiMode, enabled: enabled }) {
  return `${apiMode}:${enabled ? "on" : "off"}`;
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "preview:on\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/config.js",
        &["function describeConfig", "{ apiMode, enabled }"],
        &["function actual", "function readable"],
    );
}

#[test]
fn member_source_match_anything_pattern_skips_destructuring_shape() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function actual({ value, ignored }) {
  return `stable:${value}`;
}
console.log(actual({ value: "ok", ignored: "noise" }));
export { actual };
"#,
        vec![logical_module(
            "config",
            &[Member::source_alpha(
                "readConfig",
                r#"function readable(ANYTHING) {
  return `stable:${ANYTHING}`;
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "stable:ok\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/config.js",
        &["readConfig"],
        &["actual"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/config.js",
        &["function readConfig", "{ value, ignored }"],
        &["ANYTHING", "readable"],
    );
}

#[test]
fn member_source_match_expr_prefix_holes_match_arbitrary_expression_subtrees() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const actual = Math.max(Number.parseInt("7", 10), [1, 2, 3].length);
console.log(actual);
export { actual };
"#,
        vec![logical_module(
            "calc",
            &[Member::source_alpha(
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
fn member_source_match_anything_matches_expression_subtrees() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const actual = Number.parseInt("8", 10) + [1, 2, 3].length;
console.log(actual);
export { actual };
"#,
        vec![logical_module(
            "calc",
            &[Member::source_alpha(
                "calc_value",
                r#"const readable = ANYTHING + ANYTHING;"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "11\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &["calc_value"],
        &["actual"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &["Number.parseInt", "].length", "const calc_value"],
        &["ANYTHING", "readable"],
    );
}

#[test]
fn member_source_match_string_literal_regex_predicate_matches_string_literal_value() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeStyle = "WidgetShell-42";
console.log(runtimeStyle);
export { runtimeStyle };
"#,
        vec![logical_module(
            "styles/shell",
            &[Member::source_alpha(
                "shellStyle",
                r#"const readableStyle = STR_LITERAL_MATCHING_RE("^WidgetShell-[0-9]+$");"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "WidgetShell-42\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/styles/shell.js",
        &["const shellStyle = \"WidgetShell-42\""],
        &["STR_LITERAL_MATCHING_RE"],
    );
}

#[test]
fn member_source_match_string_literal_regex_predicate_rejects_non_matching_literal() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const runtimeStyle = "PanelShell-42";
console.log(runtimeStyle);
export { runtimeStyle };
"#,
            vec![logical_module(
                "styles/shell",
                &[Member::source_alpha(
                    "shellStyle",
                    r#"const readableStyle = STR_LITERAL_MATCHING_RE("^WidgetShell-[0-9]+$");"#,
                )],
            )],
        ),
        &[
            "styles/shell",
            "did not match any top-level declaration",
            "STR_LITERAL_MATCHING_RE",
            "WidgetShell",
        ],
    );
}

#[test]
fn member_source_match_string_literal_regex_predicate_rejects_ambiguous_literals() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const runtimePrimaryStyle = "WidgetShell-1";
const runtimeSecondaryStyle = "WidgetShell-2";
console.log(runtimePrimaryStyle, runtimeSecondaryStyle);
export { runtimePrimaryStyle, runtimeSecondaryStyle };
"#,
            vec![logical_module(
                "styles/shell",
                &[Member::source_alpha(
                    "shellStyle",
                    r#"const readableStyle = STR_LITERAL_MATCHING_RE("^WidgetShell-[0-9]+$");"#,
                )],
            )],
        ),
        &[
            "styles/shell",
            "ambiguous",
            "STR_LITERAL_MATCHING_RE",
            "WidgetShell",
        ],
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
            &[Member::source_alpha(
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
fn member_source_match_argument_list_holes_skip_unimportant_arguments() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function joinParts(...parts) {
  return parts.join("|");
}
function ignoredPart(label) {
  return `ignored:${label}`;
}
const importantValue = "important";
const actual = joinParts("stable", ignoredPart("left"), importantValue, ignoredPart("right"));
console.log(actual);
export { actual };
"#,
        vec![logical_module(
            "joined",
            &[Member::source_alpha(
                "joinedValue",
                r#"const selectedValue = joinParts("stable", ARGS_BEFORE, importantValue, ARGS_AFTER);"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "stable|ignored:left|important|ignored:right\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/joined.js",
        &["joinedValue"],
        &["actual"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/joined.js",
        &[
            "ignoredPart(\"left\")",
            "importantValue",
            "ignoredPart(\"right\")",
        ],
        &["ARGS_BEFORE", "ARGS_AFTER"],
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
            &[BindingGroup::source_alpha(
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
fn binding_group_source_match_string_literal_regex_predicates_match_each_target_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimePrimary = "Token-primary-101",
  runtimeSecondary = "Token-secondary-202";
console.log(`${runtimePrimary}:${runtimeSecondary}`);
export { runtimePrimary, runtimeSecondary };
"#,
        vec![logical_module_with_binding_groups(
            "styles/tokens",
            &[],
            &[BindingGroup::source_alpha(
                r#"const primaryToken = STR_LITERAL_MATCHING_RE("^Token-primary-[0-9]+$"),
  secondaryToken = STR_LITERAL_MATCHING_RE("^Token-secondary-[0-9]+$");"#,
                &[
                    ("primaryToken", "primaryStyleToken"),
                    ("secondaryToken", "secondaryStyleToken"),
                ],
            )],
        )],
    ));

    assert_entry_output(&fixture, "Token-primary-101:Token-secondary-202\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/styles/tokens.js",
        &[
            "const primaryStyleToken = \"Token-primary-101\"",
            "secondaryStyleToken = \"Token-secondary-202\"",
        ],
        &["STR_LITERAL_MATCHING_RE"],
    );
}

#[test]
fn binding_group_source_match_string_literal_regex_range_exports_style_object() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeFirstClassName = "Widget-module_first__a1B-2";
const runtimeSecondClassName = "Widget-module_second__Z9_x";
const runtimeStyles = {
  first: runtimeFirstClassName,
  second: runtimeSecondClassName,
};
console.log(runtimeStyles.first, runtimeStyles.second);
export { runtimeFirstClassName, runtimeSecondClassName, runtimeStyles };
"#,
        vec![logical_module_with_binding_groups(
            "styles/widget",
            &[],
            &[BindingGroup::source_alpha(
                r#"const firstClassName = STR_LITERAL_MATCHING_RE("^Widget-module_first__[A-Za-z0-9_-]+$");
const secondClassName = STR_LITERAL_MATCHING_RE("^Widget-module_second__[A-Za-z0-9_-]+$");
const styles = { first: firstClassName, second: secondClassName };"#,
                &[
                    ("firstClassName", "firstClassName"),
                    ("secondClassName", "secondClassName"),
                    ("styles", "styles"),
                ],
            )],
        )],
    ));

    assert_entry_output(
        &fixture,
        "Widget-module_first__a1B-2 Widget-module_second__Z9_x\n",
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/styles/widget.js",
        &["firstClassName", "secondClassName", "styles"],
        &[
            "runtimeFirstClassName",
            "runtimeSecondClassName",
            "runtimeStyles",
        ],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/styles/widget.js",
        &[
            r#"const firstClassName = "Widget-module_first__a1B-2""#,
            r#"const secondClassName = "Widget-module_second__Z9_x""#,
            "const styles = {",
            "first: firstClassName",
            "second: secondClassName",
        ],
        &["STR_LITERAL_MATCHING_RE"],
    );
}

#[test]
fn binding_group_source_match_range_ignores_comments_and_exports_subset() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeFirstClassName = "Widget-module_first__c0m";
// Deliberately between declarations; comments should not become selector anchors.
const runtimeSecondClassName = "Widget-module_second__m3nt";
/* Another ignored comment before the aggregate object. */
const runtimeStyles = {
  first: runtimeFirstClassName,
  second: runtimeSecondClassName,
};
console.log(runtimeStyles.first, runtimeStyles.second);
export { runtimeFirstClassName, runtimeSecondClassName, runtimeStyles };
"#,
        vec![logical_module_with_binding_groups(
            "styles/commented-widget",
            &[],
            &[BindingGroup::source_alpha(
                r#"const firstClassName = STR_LITERAL_MATCHING_RE("^Widget-module_first__[A-Za-z0-9_-]+$");
const secondClassName = STR_LITERAL_MATCHING_RE("^Widget-module_second__[A-Za-z0-9_-]+$");
const styles = { first: firstClassName, second: secondClassName };"#,
                &[("styles", "styles")],
            )],
        )],
    ));

    assert_entry_output(
        &fixture,
        "Widget-module_first__c0m Widget-module_second__m3nt\n",
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/styles/commented-widget.js",
        &["styles"],
        &["firstClassName", "secondClassName", "runtimeStyles"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/styles/commented-widget.js",
        &["const styles = {", "first:", "second:"],
        &[
            "STR_LITERAL_MATCHING_RE",
            "Deliberately between declarations",
        ],
    );
}

#[test]
fn binding_group_source_match_range_matches_exported_const_declarations() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"export const runtimeFirstClassName = "Widget-module_first__exp1";
export const runtimeSecondClassName = "Widget-module_second__exp2";
export const runtimeStyles = {
  first: runtimeFirstClassName,
  second: runtimeSecondClassName,
};
console.log(runtimeStyles.first, runtimeStyles.second);
"#,
        vec![logical_module_with_binding_groups(
            "styles/exported-widget",
            &[],
            &[BindingGroup::source_alpha(
                r#"export const firstClassName = STR_LITERAL_MATCHING_RE("^Widget-module_first__[A-Za-z0-9_-]+$");
export const secondClassName = STR_LITERAL_MATCHING_RE("^Widget-module_second__[A-Za-z0-9_-]+$");
export const styles = { first: firstClassName, second: secondClassName };"#,
                &[
                    ("firstClassName", "firstClassName"),
                    ("secondClassName", "secondClassName"),
                    ("styles", "styles"),
                ],
            )],
        )],
    ));

    assert_entry_output(
        &fixture,
        "Widget-module_first__exp1 Widget-module_second__exp2\n",
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/styles/exported-widget.js",
        &["firstClassName", "secondClassName", "styles"],
        &[
            "runtimeFirstClassName",
            "runtimeSecondClassName",
            "runtimeStyles",
        ],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/styles/exported-widget.js",
        &[
            r#"const firstClassName = "Widget-module_first__exp1""#,
            r#"const secondClassName = "Widget-module_second__exp2""#,
            "const styles = {",
        ],
        &["STR_LITERAL_MATCHING_RE"],
    );
}

#[test]
fn binding_group_source_match_range_matches_mixed_declaration_kinds() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeFirstClassName = "Widget-module_first__mix1";
const runtimeSecondClassName = "Widget-module_second__mix2";
function runtimeMakeStyles(first, second) {
  return { first, second };
}
const runtimeStyles = runtimeMakeStyles(runtimeFirstClassName, runtimeSecondClassName);
console.log(runtimeStyles.first, runtimeStyles.second);
export {
  runtimeFirstClassName,
  runtimeSecondClassName,
  runtimeMakeStyles,
  runtimeStyles,
};
"#,
        vec![logical_module_with_binding_groups(
            "styles/mixed-widget",
            &[],
            &[BindingGroup::source_alpha(
                r#"const firstClassName = STR_LITERAL_MATCHING_RE("^Widget-module_first__[A-Za-z0-9_-]+$");
const secondClassName = STR_LITERAL_MATCHING_RE("^Widget-module_second__[A-Za-z0-9_-]+$");
function makeStyles(first, second) {
  return { first, second };
}
const styles = makeStyles(firstClassName, secondClassName);"#,
                &[("makeStyles", "makeStyles"), ("styles", "styles")],
            )],
        )],
    ));

    assert_entry_output(
        &fixture,
        "Widget-module_first__mix1 Widget-module_second__mix2\n",
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/styles/mixed-widget.js",
        &["makeStyles", "styles"],
        &[
            "firstClassName",
            "secondClassName",
            "runtimeMakeStyles",
            "runtimeStyles",
        ],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/styles/mixed-widget.js",
        &[
            "import { runtimeFirstClassName, runtimeSecondClassName }",
            "function makeStyles",
            "const styles = makeStyles",
        ],
        &["STR_LITERAL_MATCHING_RE"],
    );
}

#[test]
fn binding_group_comments_emit_for_each_target_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var first = 1 + 2, second = Number.parseInt("4", 10);
console.log(first + second);
export { first, second };
"#,
        vec![logical_module_with_binding_groups(
            "pair",
            &[],
            &[BindingGroup::source_alpha(
                r#"var left = EXPR_LEFT, right = EXPR_RIGHT;"#,
                &[("left", "first_value"), ("right", "second_value")],
            )
            .with_comments(&[
                ("left", "First selected value."),
                ("right", "Second selected value."),
            ])],
        )],
    ));

    assert_entry_output(&fixture, "7\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/pair.js",
        &[
            "// First selected value.",
            "var first_value = 1 + 2",
            "// Second selected value.",
            r#"var second_value = Number.parseInt("4", 10)"#,
        ],
        &[],
    );
}

#[test]
fn binding_group_comments_emit_for_adopted_names() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const primary = 10, secondary = 20;
console.log(primary + secondary);
export { primary, secondary };
"#,
        vec![logical_module_with_binding_groups(
            "settings",
            &[],
            &[BindingGroup::source_alpha_adopt_all(
                r#"const primary = EXPR_PRIMARY, secondary = EXPR_SECONDARY;"#,
            )
            .with_comments(&[
                ("primary", "Primary selected value."),
                ("secondary", "Secondary selected value."),
            ])],
        )],
    ));

    assert_entry_output(&fixture, "30\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/settings.js",
        &[
            "// Primary selected value.",
            "const primary = 10",
            "// Secondary selected value.",
            "secondary = 20",
        ],
        &[],
    );
}

#[test]
fn binding_group_comments_reject_unknown_selector_local_key() {
    let opts = FixtureOpts::new(
        r#"const primary = 10, secondary = 20;
console.log(primary + secondary);
export { primary, secondary };
"#,
        vec![logical_module_with_binding_groups(
            "settings",
            &[],
            &[BindingGroup::source_alpha_adopt_names(
                r#"const primary = EXPR_PRIMARY, secondary = EXPR_SECONDARY;"#,
                &["primary"],
            )
            .with_comments(&[
                ("primary", "Primary selected value."),
                ("secondary", "This binding is not exported by the group."),
            ])],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::settings",
            "binding_groups[].comments",
            "not exported by the group",
            "secondary",
        ],
    );
}

#[test]
fn member_source_match_declarator_holes_select_binding_from_wider_const_list() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimePrefix = "prefix",
  runtimeFormat = (value) => String(value).toUpperCase(),
  runtimeLabels = new Map([
    ["left", "Left"],
    ["right", "Right"],
  ]),
  runtimeRead = (key) => runtimeLabels.get(key) ?? runtimeFormat(key),
  runtimeSuffix = "suffix";
console.log(runtimePrefix, runtimeRead("left"), runtimeSuffix);
export { runtimePrefix, runtimeFormat, runtimeLabels, runtimeRead, runtimeSuffix };
"#,
        vec![logical_module(
            "display",
            &[Member::source_alpha_target(
                "readDisplayLabel",
                "readDisplayLabel",
                r#"const DECLARATORS_BEFORE = null,
  formatDisplayLabel = EXPR_FORMAT,
  displayLabels = new Map([
    ["left", "Left"],
    ["right", "Right"],
  ]),
  readDisplayLabel = EXPR_READ,
  DECLARATORS_AFTER = null;"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "prefix Left suffix\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/display.js",
        &["readDisplayLabel"],
        &["runtimeRead"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/display.js",
        &["const readDisplayLabel", "runtimeLabels.get"],
        &["runtimePrefix", "runtimeSuffix", "DECLARATORS"],
    );
}

#[test]
fn member_source_match_anything_declarator_selects_binding_from_wider_const_list() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimePrefix = "prefix",
  runtimeBuild = (value) => `build:${value}`,
  runtimeRead = (value) => runtimeBuild(value).toUpperCase(),
  runtimeSuffix = "suffix";
console.log(runtimePrefix, runtimeRead("one"), runtimeSuffix);
export { runtimePrefix, runtimeBuild, runtimeRead, runtimeSuffix };
"#,
        vec![logical_module(
            "display",
            &[Member::source_alpha_target(
                "readDisplayValue",
                "readDisplayValue",
                r#"const ANYTHING = null,
  buildDisplayValue = (value) => `build:${value}`,
  readDisplayValue = (value) => buildDisplayValue(value).toUpperCase(),
  ANYTHING = null;"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "prefix BUILD:ONE suffix\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/display.js",
        &["readDisplayValue"],
        &["runtimeRead"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/display.js",
        &["const readDisplayValue", ".toUpperCase()"],
        &["runtimePrefix", "runtimeSuffix", "ANYTHING"],
    );
}

#[test]
fn binding_group_declarator_holes_extract_multiple_bindings_and_skip_holes_for_adopt_names() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimePrefix = "prefix",
  runtimeFormat = (value) => String(value).toUpperCase(),
  runtimeLabels = new Map([
    ["left", "Left"],
    ["right", "Right"],
  ]),
  runtimeRead = (key) => runtimeLabels.get(key) ?? runtimeFormat(key),
  runtimeSuffix = "suffix";
console.log(runtimePrefix, runtimeRead("left"), runtimeFormat("ok"), runtimeSuffix);
export { runtimePrefix, runtimeFormat, runtimeLabels, runtimeRead, runtimeSuffix };
"#,
        vec![logical_module_with_binding_groups(
            "display",
            &[],
            &[BindingGroup::source_alpha_adopt_all(
                r#"const DECLARATORS_BEFORE = null,
  formatDisplayLabel = EXPR_FORMAT,
  displayLabels = new Map([
    ["left", "Left"],
    ["right", "Right"],
  ]),
  readDisplayLabel = EXPR_READ,
  DECLARATORS_AFTER = null;"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "prefix Left OK suffix\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/display.js",
        &["displayLabels", "formatDisplayLabel", "readDisplayLabel"],
        &["runtimeFormat", "runtimeLabels", "runtimeRead"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/display.js",
        &[
            "const formatDisplayLabel",
            "const displayLabels",
            "const readDisplayLabel",
        ],
        &["runtimePrefix", "runtimeSuffix", "DECLARATORS"],
    );
}

#[test]
fn binding_group_trailing_declarator_hole_absorbs_later_declarators() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const selectedA = () => "a",
  selectedB = () => "b",
  laterC = () => "c";
console.log(selectedA() + selectedB() + laterC());
export { selectedA, selectedB, laterC };
"#,
        vec![logical_module_with_binding_groups(
            "bridges",
            &[],
            &[BindingGroup::source_alpha(
                r#"const selectedA = EXPR_A,
  selectedB = EXPR_B,
  DECLARATORS_AFTER = null;"#,
                &[("selectedA", "recordBridge"), ("selectedB", "replayBridge")],
            )],
        )],
    ));

    assert_entry_output(&fixture, "abc\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/bridges.js",
        &["recordBridge", "replayBridge"],
        &["selectedA", "selectedB"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/bridges.js",
        &["const recordBridge", "const replayBridge"],
        &["laterC", "DECLARATORS_AFTER"],
    );
}

#[test]
fn binding_group_declarator_holes_extract_adjacent_arrows_at_start_middle_and_end() {
    let leading_fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeBuild = (value) => `build:${value}`,
  runtimeRead = (value) => runtimeBuild(value).toUpperCase(),
  runtimeTrailingHelper = () => "tail";
console.log(runtimeRead("one"), runtimeTrailingHelper());
export { runtimeBuild, runtimeRead, runtimeTrailingHelper };
"#,
        vec![logical_module_with_binding_groups(
            "leading",
            &[],
            &[BindingGroup::source_alpha(
                r#"const buildSelected = (value) => `build:${value}`,
  readSelected = (value) => buildSelected(value).toUpperCase(),
  DECLARATORS_AFTER = null;"#,
                &[
                    ("buildSelected", "buildValue"),
                    ("readSelected", "readValue"),
                ],
            )],
        )],
    ));
    assert_entry_output(&leading_fixture, "BUILD:ONE tail\n");
    assert_module_exports(
        &leading_fixture.out_root,
        "static/app/modules/leading.js",
        &["buildValue", "readValue"],
        &["runtimeBuild", "runtimeRead", "runtimeTrailingHelper"],
    );
    assert_module_source(
        &leading_fixture.out_root,
        "static/app/modules/leading.js",
        &["const buildValue", "const readValue"],
        &["runtimeTrailingHelper", "DECLARATORS_AFTER"],
    );

    let middle_fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeLeadingHelper = () => "head",
  runtimeBuild = (value) => `build:${value}`,
  runtimeRead = (value) => runtimeBuild(value).toUpperCase(),
  runtimeTrailingHelper = () => "tail";
console.log(runtimeLeadingHelper(), runtimeRead("two"), runtimeTrailingHelper());
export { runtimeLeadingHelper, runtimeBuild, runtimeRead, runtimeTrailingHelper };
"#,
        vec![logical_module_with_binding_groups(
            "middle",
            &[],
            &[BindingGroup::source_alpha_adopt_all(
                r#"const DECLARATORS_BEFORE = null,
  buildSelected = (value) => `build:${value}`,
  readSelected = (value) => buildSelected(value).toUpperCase(),
  DECLARATORS_AFTER = null;"#,
            )],
        )],
    ));
    assert_entry_output(&middle_fixture, "head BUILD:TWO tail\n");
    assert_module_exports(
        &middle_fixture.out_root,
        "static/app/modules/middle.js",
        &["buildSelected", "readSelected"],
        &[
            "runtimeLeadingHelper",
            "runtimeBuild",
            "runtimeRead",
            "runtimeTrailingHelper",
        ],
    );
    assert_module_source(
        &middle_fixture.out_root,
        "static/app/modules/middle.js",
        &["const buildSelected", "const readSelected"],
        &[
            "runtimeLeadingHelper",
            "runtimeTrailingHelper",
            "DECLARATORS_BEFORE",
            "DECLARATORS_AFTER",
        ],
    );

    let trailing_fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeLeadingHelper = () => "head",
  runtimeBuild = (value) => `build:${value}`,
  runtimeRead = (value) => runtimeBuild(value).toUpperCase();
console.log(runtimeLeadingHelper(), runtimeRead("three"));
export { runtimeLeadingHelper, runtimeBuild, runtimeRead };
"#,
        vec![logical_module_with_binding_groups(
            "trailing",
            &[],
            &[BindingGroup::source_alpha_adopt_names(
                r#"const DECLARATORS_BEFORE = null,
  buildSelected = (value) => `build:${value}`,
  readSelected = (value) => buildSelected(value).toUpperCase();"#,
                &["buildSelected", "readSelected"],
            )],
        )],
    ));
    assert_entry_output(&trailing_fixture, "head BUILD:THREE\n");
    assert_module_exports(
        &trailing_fixture.out_root,
        "static/app/modules/trailing.js",
        &["buildSelected", "readSelected"],
        &["runtimeLeadingHelper", "runtimeBuild", "runtimeRead"],
    );
    assert_module_source(
        &trailing_fixture.out_root,
        "static/app/modules/trailing.js",
        &["const buildSelected", "const readSelected"],
        &["runtimeLeadingHelper", "DECLARATORS_BEFORE"],
    );
}

#[test]
fn declarator_hole_miss_reports_best_anchored_var_decl_candidate() {
    let opts = FixtureOpts::new(
        r#"function operation(kind, value) {
  return `${kind}:${value}`;
}
function helper(value) {
  return value;
}
const unrelatedOperation = operation("alpha", "small"),
  unrelatedFreeze = Object.freeze({ ready: true });
const clusterPrefix = helper("prefix"),
  firstSelected = operation("alpha", "first"),
  clusterMiddle = helper("middle"),
  secondSelected = operation("beta", "second"),
  clusterSuffix = helper("suffix");
console.log(firstSelected, secondSelected);
export { firstSelected, secondSelected };
"#,
        vec![logical_module_with_binding_groups(
            "operations",
            &[],
            &[BindingGroup::source_alpha(
                r#"const DECLARATORS = null,
  firstSelected = operation("alpha", EXPR),
  DECLARATORS = null,
  secondSelected = operation("gamma", EXPR),
  DECLARATORS = null;"#,
                &[
                    ("firstSelected", "firstOperation"),
                    ("secondSelected", "secondOperation"),
                ],
            )],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::operations",
            "binding_groups[].source_match",
            "did not match",
            "Nearest variable declaration candidates:",
            "declares `clusterPrefix`, `firstSelected`, `clusterMiddle`, `secondSelected`, `clusterSuffix`",
            "declarators: `clusterPrefix = helper(...)`, `firstSelected = operation(...)`",
            "matched 1/2 pinned declarators in order",
            "selector pinned declarator #3 `secondSelected = operation(...)` was not found in order",
            "remaining candidate declarators: `clusterMiddle = helper(...)`, `secondSelected = operation(...)`",
        ],
    );
}

#[test]
fn declarator_hole_miss_reports_missing_between_hole_and_target_binding_guidance() {
    let opts = FixtureOpts::new(
        r#"function buildItem(label) {
  return { label };
}
function helperItem(label) {
  return { label };
}
const leadingHelper = helperItem("lead"),
  selectedA = buildItem("a"),
  skippedHelper = helperItem("middle"),
  selectedB = buildItem("b"),
  selectedC = buildItem("c"),
  trailingHelper = helperItem("tail");
console.log(selectedA.label, selectedB.label, selectedC.label);
export { selectedA, selectedB, selectedC };
"#,
        vec![logical_module(
            "selected_values",
            &[Member::source_alpha_target(
                "selectedB",
                "selectedB",
                r#"const DECLARATORS_BEFORE = null,
  selectedA = buildItem("a"),
  selectedB = buildItem("b"),
  selectedC = buildItem("c"),
  DECLARATORS_AFTER = null;"#,
            )],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::selected_values",
            "target_binding `selectedB`",
            "did not match any top-level declaration",
            "matched 3/3 pinned declarators in order",
            "candidate has unmatched declarator(s) between selector declarator #1 `selectedA = buildItem(...)` and #2 `selectedB = buildItem(...)`: `skippedHelper = helperItem(...)`",
            "Add a `DECLARATORS_* = null` pseudo-declarator between those pinned declarators",
            "`target_binding` resolves one selector-local binding for the current export",
            "use one `binding_groups` entry",
        ],
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
        vec![logical_module_with_anon_alpha(
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
        vec![logical_module_with_anon_alpha(
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

#[test]
fn anonymous_source_match_stmt_list_hole_absorbs_contiguous_statements() {
    // `STMT_LIST_BODY;` as the whole block body absorbs the three
    // statements, so the selector matches the `if` regardless of body.
    let fixture = run_fixture(FixtureOpts::new(
        r#"if (true) {
  console.log("a");
  console.log("b");
  console.log("c");
}
const marker = "ready";
export { marker };
"#,
        vec![logical_module_with_anon_alpha(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  STMT_LIST_BODY;
}"#,
        )],
    ));

    assert_entry_output(&fixture, "a\nb\nc\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/init.js",
        &[
            r#"console.log("a")"#,
            r#"console.log("b")"#,
            r#"console.log("c")"#,
            "const marker",
        ],
        // The selector's placeholder name never appears in the output;
        // the original statements were spliced in verbatim.
        &["STMT_LIST_BODY"],
    );
}

#[test]
fn anonymous_source_match_stmt_list_hole_absorbs_empty_run() {
    // A trailing `STMT_LIST_TAIL;` matches a block that has only the
    // pinned prefix statement — the hole absorbs zero statements.
    let fixture = run_fixture(FixtureOpts::new(
        r#"if (true) {
  console.log("only");
}
const marker = "ready";
export { marker };
"#,
        vec![logical_module_with_anon_alpha(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  console.log("only");
  STMT_LIST_TAIL;
}"#,
        )],
    ));

    assert_entry_output(&fixture, "only\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/init.js",
        &[r#"console.log("only")"#, "const marker"],
        &["STMT_LIST_TAIL"],
    );
}

#[test]
fn member_source_match_class_rest_hole_selects_class_ignoring_other_members() {
    // Pin the class by its constructor (body hole) and let `CLASS_REST;`
    // absorb `increment` and `reset`. The whole class still moves — the
    // hole is only in the selector, not the output.
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Counter {
  constructor() {
    this.value = 0;
  }
  increment() {
    this.value += 1;
    return this.value;
  }
  reset() {
    this.value = 0;
  }
}
const counter = new Counter();
console.log(counter.increment());
export { Counter };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Counter",
                r#"class K {
  constructor() {
    STMT_LIST_CTOR;
  }
  CLASS_REST;
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "1\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["Counter"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        // The full class moved, members and all.
        &["class", "increment", "reset"],
        &["CLASS_REST", "STMT_LIST_CTOR"],
    );
}

#[test]
fn member_source_match_anything_class_member_selects_class_ignoring_other_members() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"class RuntimeCounter {
  constructor(start) {
    this.value = start;
  }
  increment() {
    this.value += 1;
  }
  label() {
    return `count:${this.value}`;
  }
  reset() {
    this.value = 0;
  }
}
const counter = new RuntimeCounter(4);
counter.increment();
console.log(counter.label());
export { RuntimeCounter };
"#,
        vec![logical_module(
            "counter",
            &[Member::source_alpha(
                "Counter",
                r#"class Counter {
  ANYTHING;
  label() {
    ANYTHING;
  }
  ANYTHING;
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "count:5\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/counter.js",
        &["Counter"],
        &["RuntimeCounter"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/counter.js",
        &["class Counter", "increment()", "reset()"],
        &["ANYTHING"],
    );
}

#[test]
fn member_source_match_class_skeleton_rejects_ambiguous_match() {
    // The skeleton `class K { run() { STMT_LIST } CLASS_REST }` matches
    // both `Alpha` and `Beta`; ambiguous matches stay hard errors.
    let opts = FixtureOpts::new(
        r#"class Alpha {
  run() {
    return 1;
  }
}
class Beta {
  run() {
    return 2;
  }
}
console.log(new Alpha().run() + new Beta().run());
export { Alpha };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Selected",
                r#"class K {
  run() {
    STMT_LIST_BODY;
  }
  CLASS_REST;
}"#,
            )],
        )],
    );

    expect_rejection_containing_all(opts, &["static/app::shapes", "ambiguous"]);
}

#[test]
fn member_source_match_class_rest_hole_pins_member_order() {
    // CLASS_REST is positional: members pinned before the hole must be
    // the candidate's leading members in the same order. Listing `b`
    // before `a` does not match a class whose first members are `a`
    // then `b`, so resolution finds no match.
    let opts = FixtureOpts::new(
        r#"class Counter {
  a() {
    return 1;
  }
  b() {
    return 2;
  }
}
console.log(new Counter().a());
export { Counter };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Selected",
                r#"class K {
  b() {
    STMT_LIST_B;
  }
  a() {
    STMT_LIST_A;
  }
  CLASS_REST;
}"#,
            )],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::shapes",
            "did not match",
            "Nearest class candidates:",
            "declares `Counter`",
            "selector class pinned member `a`",
        ],
    );
}

#[test]
fn class_source_match_miss_reports_best_anchored_class_candidate() {
    // The selector has all of the intended class's method anchors in order,
    // but one method body is too exact. Diagnostics should point at that class
    // and body mismatch, not at a later unrelated class that merely misses an
    // anchor method.
    let opts = FixtureOpts::new(
        r#"class CatalogCache {
  field = new Map();
  constructor() {
    this.ready = true;
  }
  refreshEntriesNow(scope, filter) {
    if (filter.enabled) {
      this.loadBatch(scope, filter);
    }
  }
  loadBatch(scope, filter) {
    return scope.prefix + filter.kind;
  }
  lookupEntryByKey(key, record) {
    return key + record.id;
  }
  dropEntryByKey(key, record) {
    return key;
  }
}
class LaterCatalog {
  configure() {
    return true;
  }
  loadBatch(scope, filter) {
    return scope.prefix + filter.kind;
  }
  lookupEntryByKey(key, record) {
    return key + record.id;
  }
  dropEntryByKey(key, record) {
    return key;
  }
}
console.log(new CatalogCache().lookupEntryByKey("a", { id: "b" }));
export { CatalogCache };
"#,
        vec![logical_module(
            "catalog",
            &[Member::source_alpha(
                "CatalogCache",
                r#"class K {
  CLASS_REST;
  refreshEntriesNow(scope, filter) {
    if (filter.active) {
      this.loadBatch(scope, filter);
    }
  }
  CLASS_REST;
  loadBatch(scope, filter) {
    STMT_LIST;
  }
  CLASS_REST;
  lookupEntryByKey(key, record) {
    STMT_LIST;
  }
  CLASS_REST;
  dropEntryByKey(key, record) {
    STMT_LIST;
  }
  CLASS_REST;
}"#,
            )],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::catalog",
            "did not match",
            "Nearest class candidates:",
            "declares `CatalogCache`",
            "members: `field`, `constructor`, `refreshEntriesNow`",
            "matched 4/4 pinned member names in order",
            "class member `refreshEntriesNow` matched by name",
            "declares `LaterCatalog`",
            "matched 0/4 pinned member names in order",
        ],
    );
}

#[test]
fn anonymous_expr_holes_match_independent_subtrees() {
    // The bare keyword `EXPR` is anonymous: the two occurrences match
    // *different* expressions. A named hole `EXPR_X` repeated would
    // instead force the two arguments to be equal.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const actual = Math.max(Number.parseInt("7", 10), [1, 2, 3].length);
console.log(actual);
export { actual };
"#,
        vec![logical_module(
            "calc",
            &[Member::source_alpha(
                "calc_value",
                r#"const readable = Math.max(EXPR, EXPR);"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "7\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &["Math.max", "const calc_value"],
        &["EXPR"],
    );
}

#[test]
fn anonymous_stmt_hole_matches_one_arbitrary_statement() {
    // The bare keyword `STMT` matches exactly one statement, with no
    // suffix to mint — the anonymous single-statement form.
    let fixture = run_fixture(FixtureOpts::new(
        r#"if (true) {
  console.log("setup");
  console.log("done");
}
const marker = "ready";
export { marker };
"#,
        vec![logical_module_with_anon_alpha(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  STMT;
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
        &["STMT"],
    );
}

#[test]
fn anonymous_stmt_list_and_class_rest_holes_need_no_minted_names() {
    // Bare `STMT_LIST` and bare `CLASS_REST` select the class with no
    // suffixes to invent.
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Counter {
  constructor() {
    this.value = 0;
  }
  increment() {
    this.value += 1;
    return this.value;
  }
}
const counter = new Counter();
console.log(counter.increment());
export { Counter };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Counter",
                r#"class K {
  constructor() {
    STMT_LIST;
  }
  CLASS_REST;
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "1\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["Counter"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["class", "increment"],
        &["STMT_LIST", "CLASS_REST"],
    );
}

#[test]
fn member_source_match_class_rest_holes_bracket_interior_member() {
    // Two `CLASS_REST;` holes bracket a single pinned member, so the
    // selector matches a class by an interior member it contains: the
    // leading hole absorbs `a`, the trailing hole absorbs `c`, and `b`
    // is pinned in between. (Previously a second `CLASS_REST` was a hard
    // "ambiguous, never matches"; it is now an ordered-subsequence gap.)
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Counter {
  a() {
    return 1;
  }
  b() {
    return 2;
  }
  c() {
    return 3;
  }
}
console.log(new Counter().b());
export { Counter };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Counter",
                r#"class K {
  CLASS_REST;
  b() {
    STMT_LIST_B;
  }
  CLASS_REST;
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "2\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["Counter"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        // The whole class moved; the bracketing holes are selector-only.
        &["class", "a()", "b()", "c()"],
        &["CLASS_REST", "STMT_LIST_B"],
    );
}

#[test]
fn member_source_match_interleaved_class_rest_holes_match_ordered_members() {
    // Two pinned members separated by a `CLASS_REST;` hole match a class
    // that contains them in that order with other members interspersed:
    // `open` (after `setup`) then `close` (after `tick`). This is the
    // ordered-subset fingerprint — pin a few stable members, ignore the
    // rest.
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Widget {
  setup() {
    return 0;
  }
  open() {
    return 1;
  }
  tick() {
    return 2;
  }
  close() {
    return 3;
  }
}
console.log(new Widget().open() + new Widget().close());
export { Widget };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Widget",
                r#"class K {
  CLASS_REST;
  open() {
    STMT_LIST_O;
  }
  CLASS_REST;
  close() {
    STMT_LIST_C;
  }
  CLASS_REST;
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "4\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["Widget"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["setup()", "open()", "tick()", "close()"],
        &["CLASS_REST"],
    );
}

#[test]
fn member_source_match_interleaved_class_rest_holes_enforce_order() {
    // The same `Widget`, but the selector pins `close` before `open`.
    // Ordered-subsequence matching keeps source order, so pinning them
    // in the wrong order matches nothing — it is not an unordered
    // "contains both somewhere" match.
    let opts = FixtureOpts::new(
        r#"class Widget {
  setup() {
    return 0;
  }
  open() {
    return 1;
  }
  tick() {
    return 2;
  }
  close() {
    return 3;
  }
}
console.log(new Widget().open());
export { Widget };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Selected",
                r#"class K {
  CLASS_REST;
  close() {
    STMT_LIST_C;
  }
  CLASS_REST;
  open() {
    STMT_LIST_O;
  }
  CLASS_REST;
}"#,
            )],
        )],
    );

    expect_rejection_containing_all(opts, &["static/app::shapes", "did not match"]);
}

#[test]
fn anonymous_source_match_multiple_stmt_list_holes_bracket_pinned_statements() {
    // Three `STMT_LIST_*;` holes bracket two pinned statements inside a
    // block: the holes absorb the `a`/`b`/`c` logs, leaving `pinned1`
    // then `pinned2` matched in order.
    let fixture = run_fixture(FixtureOpts::new(
        r#"if (true) {
  console.log("a");
  console.log("pinned1");
  console.log("b");
  console.log("pinned2");
  console.log("c");
}
const marker = "ready";
export { marker };
"#,
        vec![logical_module_with_anon_alpha(
            "init",
            &[Member::new("marker")],
            r#"if (true) {
  STMT_LIST_HEAD;
  console.log("pinned1");
  STMT_LIST_MID;
  console.log("pinned2");
  STMT_LIST_TAIL;
}"#,
        )],
    ));

    assert_entry_output(&fixture, "a\npinned1\nb\npinned2\nc\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/init.js",
        &[
            r#"console.log("a")"#,
            r#"console.log("pinned1")"#,
            r#"console.log("pinned2")"#,
            "const marker",
        ],
        &["STMT_LIST_HEAD", "STMT_LIST_MID", "STMT_LIST_TAIL"],
    );
}

#[test]
fn non_trailing_class_rest_hole_keeps_later_identifiers_aligned() {
    // Regression guard for the alpha-identifier bijection: a leading
    // `CLASS_REST` absorbs `helper`, whose param/body identifiers do not
    // desync the `run(value) { return value * 2 }` member that follows.
    // (Under the old global alpha-canonicalization the absorbed `helper`
    // identifiers shifted the numbering and this failed to match.)
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Counter {
  helper(seed) {
    return seed + 1;
  }
  run(value) {
    return value * 2;
  }
}
const counter = new Counter();
console.log(counter.run(5));
export { Counter };
"#,
        vec![logical_module(
            "shapes",
            &[Member::source_alpha(
                "Counter",
                r#"class K {
  CLASS_REST;
  run(value) {
    return value * 2;
  }
}"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "10\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["Counter"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shapes.js",
        &["class", "helper", "run"],
        &["CLASS_REST"],
    );
}

#[test]
fn single_node_hole_keeps_later_identifiers_aligned() {
    // The same bijection guard for single-node holes: `EXPR` absorbs a
    // multi-identifier subtree, and the `limit` argument after it still
    // matches by alpha-correspondence rather than by absolute position.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const limit = 4;
const alpha = 1, beta = 2, gamma = 3;
const total = Math.max(Math.min(alpha, beta, gamma), limit);
console.log(total);
export { total };
"#,
        vec![logical_module(
            "calc",
            &[Member::source_alpha(
                "calc_total",
                r#"const readable = Math.max(EXPR, limit);"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "4\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/calc.js",
        &["Math.max", "const calc_total"],
        &["EXPR"],
    );
}

#[test]
fn unsupported_reserved_hole_names_fail_before_generic_no_match() {
    let universal = run_dry_run_rejection_fixture(FixtureOpts::new(
        r#"const actual = computeTotal(1, 2);
console.log(actual);
export { actual };
"#,
        vec![logical_module(
            "calc",
            &[Member::source_alpha(
                "total",
                r#"const readable = ANYTHING_FUTURE;"#,
            )],
        )],
    ));
    assert!(
        universal.stderr.contains("unsupported selector capability"),
        "stderr:\n{}",
        universal.stderr
    );
    assert!(
        universal.stderr.contains("ANYTHING_FUTURE"),
        "stderr:\n{}",
        universal.stderr
    );
    assert!(
        !universal
            .stderr
            .contains("did not match any top-level declaration"),
        "stderr:\n{}",
        universal.stderr
    );

    let object_gap = run_dry_run_rejection_fixture(FixtureOpts::new(
        r#"const actual = { stable: 1, generated: 2, other: 3 };
console.log(actual.stable + actual.other);
export { actual };
"#,
        vec![logical_module(
            "objects",
            &[Member::source_alpha(
                "selected",
                r#"const readable = { stable: EXPR, ANYTHING_FUTURE, other: EXPR };"#,
            )],
        )],
    ));
    assert!(
        object_gap
            .stderr
            .contains("unsupported selector capability"),
        "stderr:\n{}",
        object_gap.stderr
    );
    assert!(
        object_gap.stderr.contains("ANYTHING_FUTURE"),
        "stderr:\n{}",
        object_gap.stderr
    );
    assert!(
        !object_gap
            .stderr
            .contains("did not match any top-level declaration"),
        "stderr:\n{}",
        object_gap.stderr
    );
}
