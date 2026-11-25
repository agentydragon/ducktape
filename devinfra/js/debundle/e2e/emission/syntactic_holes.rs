//! End-to-end coverage for `source_match` syntactic holes.
//!
//! Single-run fixtures covered by the corpus runner live in
//! `e2e/cases/emission/syntactic_holes/`.
//!
//! Single-node holes:
//! - Expression holes (`EXPR` / `EXPR_label`) are selector-local identifier
//!   expressions; they match one arbitrary expression subtree.
//! - Statement holes (`STMT` / `STMT_label`) are selector-local bare expression
//!   statements; they match exactly one statement.
//! - `ANYTHING` is anonymous parse-position sugar: in an expression
//!   position it behaves like `EXPR`, as a bare statement like `STMT`,
//!   as a non-declarator binding pattern like an anonymous pattern hole,
//!   as a variable declarator like `DECLARATORS`, as an object-literal
//!   shorthand property like an anonymous property-list hole, and as a
//!   no-init class field like an anonymous class-member-list hole.
//!
//! Hole labels are readability-only: every occurrence matches independently,
//! even when two occurrences use the same suffix.
//!
//! List holes (variable-length):
//! - `ARGS` / `ARGS_name` in a call or `new` argument list absorbs a run
//!   of arguments (including an empty run) — e.g. match a stable
//!   important argument without spelling noisy generated siblings.
//! - `STMT_LIST` / `STMT_LIST_name;` in a block body absorbs a run of
//!   statements (including an empty run) — e.g. a method body you don't
//!   want to pin.
//! - `ANYTHING;` as a no-init class field absorbs a run of class members —
//!   e.g. "match this class by these members, ignore the rest".
//! - `case CASE_REST:` as an empty switch case absorbs a run of
//!   `case`/`default` clauses — e.g. "match this switch by these
//!   discriminating cases, ignore the rest".
//! - `DECLARATORS` / `DECLARATORS_name = null` in a variable declaration
//!   absorbs a run of declarators — e.g. match a few stable entries in a
//!   wider `const` list without spelling unrelated siblings.
//! - `SEQ_EXPRS` / `SEQ_EXPRS_name` as an element of a comma-sequence
//!   expression absorbs a run of sequence elements — e.g. pin a memoized hook
//!   on its callback without spelling the cache writes a rebuild adds to the
//!   tail of its `return (test ? (memo = …, write, write) : memo = …), memo;`.
//!
//! List-hole suffixes are labels for readability; they do not bind the
//! absorbed run for equality.
//!
//! Several list holes may appear in one block or class body: they split
//! the pinned statements/members into an ordered subsequence with gaps,
//! so a selector can bracket a few stable members with `ANYTHING;`
//! holes and match any class that contains them in that order.

use debundle_e2e_support::*;

// Keep each semantic case and its Node/assertion checks separate; only the
// one-member spec envelope is shared.
fn member_fixture<'a>(source: &'a str, module: &str, member: Member) -> FixtureOpts<'a> {
    FixtureOpts::new(source, vec![logical_module(module, &[member])])
}

fn anonymous_init_fixture<'a>(source: &'a str, selector: &str) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![logical_module_with_anon_alpha(
            "init",
            &[Member::new("marker")],
            &[selector],
        )],
    )
}

const THREE_STATEMENT_BLOCK_SOURCE: &str = r#"if (true) {
  console.log("a");
  console.log("b");
  console.log("c");
}
const marker = "ready";
export { marker };
"#;

const COMPUTE_TOTAL_SOURCE: &str = r#"function computeTotal(a, b) {
  return a + b;
}
const actual = computeTotal(1, 2);
console.log(actual);
export { actual };
"#;

#[test]
fn member_source_match_treats_object_shorthand_as_explicit_same_name_property() {
    let fixture = run_fixture(member_fixture(
        r#"function actual(apiMode, enabled) {
  return { apiMode, enabled };
}
console.log(JSON.stringify(actual("preview", true)));
export { actual };
"#,
        "config",
        Member::source_alpha(
            "makeConfig",
            r#"function readable(apiMode, enabled) {
  return { apiMode: apiMode, enabled: enabled };
}"#,
        ),
    ));

    assert_entry_output(&fixture, "{\"apiMode\":\"preview\",\"enabled\":true}\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/config.js",
        &["function makeConfig", "return {", "apiMode,", "enabled"],
        &["function actual", "function readable"],
    );
}

const HANDLER_SIBLINGS: &str = r#"const handlerA = makeHandler({ route: { method: "GET" } }),
  handlerB = makeHandler({ route: { method: "POST" } });
function makeHandler(config) {
  return () => config.route.method;
}
console.log(handlerA(), handlerB());
export { handlerA, handlerB };
"#;

// Two same-arity declarators in one `const a = …, b = …` comma-list that differ
// only in a deeply-nested value are each pinned, uniquely, by a single-declarator
// `source_match` asserting that nested anchor — the resolver matches the
// single-declarator needle against each declarator of the source_unit, so the nested
// `"GET"` / `"POST"` distinguishes the otherwise-identical siblings. Each resolves
// to its own module.
#[test]
fn member_source_match_comma_list_siblings_disambiguated_by_nested_value() {
    let fixture = run_fixture(
        FixtureOpts::new(
            HANDLER_SIBLINGS,
            vec![
                logical_module(
                    "get_route",
                    &[Member::source_alpha_target(
                        "get_handler",
                        "readable",
                        r#"const readable = makeHandler({ route: { method: "GET" } });"#,
                    )],
                ),
                logical_module(
                    "post_route",
                    &[Member::source_alpha_target(
                        "post_handler",
                        "readable",
                        r#"const readable = makeHandler({ route: { method: "POST" } });"#,
                    )],
                ),
            ],
        )
        .with_import_order(&["get_route", "post_route", "residual"]),
    );

    assert_entry_output(&fixture, "GET POST\n");
    // each sibling resolved to its own module, distinguished only by the nested
    // method value.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/get_route.js",
        &["get_handler"],
        &["handlerA"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/get_route.js",
        &["const get_handler", r#"method: "GET""#],
        &[r#"method: "POST""#, "readable"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/post_route.js",
        &["post_handler"],
        &["handlerB"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/post_route.js",
        &["const post_handler", r#"method: "POST""#],
        &[r#"method: "GET""#, "readable"],
    );
}

#[test]
fn comma_list_siblings_without_the_nested_anchor_are_ambiguous() {
    let outcome = expect_selector_outcome(
        member_fixture(
            HANDLER_SIBLINGS,
            "routes",
            Member::source_alpha_target(
                "handler",
                "readable",
                "const readable = makeHandler({ route: { method: EXPR } });",
            ),
        ),
        "ambiguous",
        "routes",
    );
    let candidates: Vec<&str> = outcome["outcome"]["candidates"]
        .as_array()
        .unwrap()
        .iter()
        .map(|candidate| candidate["binding"].as_str().unwrap())
        .collect();
    assert_eq!(candidates, ["handlerA", "handlerB"], "{outcome:#}");
}

#[test]
fn member_source_match_treats_destructure_shorthand_as_explicit_same_name_property() {
    let fixture = run_fixture(member_fixture(
        r#"function actual({ apiMode, enabled }) {
  return `${apiMode}:${enabled ? "on" : "off"}`;
}
console.log(actual({ apiMode: "preview", enabled: true }));
export { actual };
"#,
        "config",
        Member::source_alpha(
            "describeConfig",
            r#"function readable({ apiMode: apiMode, enabled: enabled }) {
  return `${apiMode}:${enabled ? "on" : "off"}`;
}"#,
        ),
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
fn literal_regex_holes_select_and_emit_the_matching_value() {
    for (source, selector, output, declaration, import_order) in [
        (
            r#"const runtimeStyle = "WidgetShell-42";
console.log(runtimeStyle);
export { runtimeStyle };
"#,
            r#"const readableStyle = STR_LITERAL_MATCHING_RE("^WidgetShell-[0-9]+$");"#,
            "WidgetShell-42\n",
            r#"const shellStyle = "WidgetShell-42""#,
            &["styles/shell"][..],
        ),
        (
            r#"const decoyStyle = "f";
const runtimeStyle = "fo";
console.log(runtimeStyle);
export { runtimeStyle };
"#,
            r#"const readableStyle = STR_LITERAL_MATCHING_RE("^foo*");"#,
            "fo\n",
            r#"const shellStyle = "fo""#,
            &["residual", "styles/shell"][..],
        ),
    ] {
        let fixture = run_fixture(
            member_fixture(
                source,
                "styles/shell",
                Member::source_alpha("shellStyle", selector),
            )
            .with_import_order(import_order),
        );
        assert_entry_output(&fixture, output);
        assert_module_source(
            &fixture.out_root,
            "static/app/modules/styles/shell.js",
            &[declaration],
            &["STR_LITERAL_MATCHING_RE"],
        );
    }
}

#[test]
fn literal_regex_holes_preserve_no_match_and_ambiguity_diagnostics() {
    for (source, selector, kind) in [
        (
            r#"const decoyStyle = "foo";
const runtimeStyle = "fo";
console.log(runtimeStyle);
export { decoyStyle, runtimeStyle };
"#,
            r#"const readableStyle = STR_LITERAL_MATCHING_RE("^foo*");"#,
            "ambiguous",
        ),
        (
            r#"const runtimeStyle = "PanelShell-42";
console.log(runtimeStyle);
export { runtimeStyle };
"#,
            r#"const readableStyle = STR_LITERAL_MATCHING_RE("^WidgetShell-[0-9]+$");"#,
            "no_match",
        ),
        (
            r#"const runtimePrimaryStyle = "WidgetShell-1";
const runtimeSecondaryStyle = "WidgetShell-2";
console.log(runtimePrimaryStyle, runtimeSecondaryStyle);
export { runtimePrimaryStyle, runtimeSecondaryStyle };
"#,
            r#"const readableStyle = STR_LITERAL_MATCHING_RE("^WidgetShell-[0-9]+$");"#,
            "ambiguous",
        ),
    ] {
        let outcome = expect_selector_outcome(
            member_fixture(
                source,
                "styles/shell",
                Member::source_alpha("shellStyle", selector),
            ),
            kind,
            "styles/shell",
        );
        assert_eq!(outcome["selector_preview"], selector);
    }
}

#[test]
fn member_source_match_many_expr_holes_match_positionally() {
    let fixture = run_fixture(member_fixture(
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
        "calc",
        Member::source_alpha(
            "calc_value",
            r#"const readable = [
  EXPR_A,
  EXPR_B,
  EXPR_C,
  EXPR_D,
  EXPR_E,
  EXPR_F,
];"#,
        ),
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
    assert_line_directly_above(
        &fixture.out_root,
        "static/app/modules/pair.js",
        "// First selected value.",
        "var first_value = 1 + 2",
    );
    assert_line_directly_above(
        &fixture.out_root,
        "static/app/modules/pair.js",
        "// Second selected value.",
        r#"var second_value = Number.parseInt("4", 10)"#,
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
    assert_line_directly_above(
        &fixture.out_root,
        "static/app/modules/settings.js",
        "// Primary selected value.",
        "const primary = 10",
    );
    assert_line_directly_above(
        &fixture.out_root,
        "static/app/modules/settings.js",
        "// Secondary selected value.",
        "const secondary = 20",
    );
}

#[test]
fn source_match_comments_reject_unknown_annotation_key() {
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

    expect_rejection_containing_all(opts, &["static/app::settings", "secondary"]);
}

#[test]
fn source_match_declarator_holes_extract_multiple_bindings_and_skip_holes_for_adopt_names() {
    let fixture = run_fixture(
        FixtureOpts::new(
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
        )
        .with_import_order(&["display", "residual"]),
    );

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
fn binding_group_declarator_holes_extract_adjacent_arrows_at_start_middle_and_end() {
    for (module, source, group, output, exports, excluded_exports, excluded_source) in [
        (
            "leading",
            r#"const runtimeBuild = (value) => `build:${value}`,
  runtimeRead = (value) => runtimeBuild(value).toUpperCase(),
  runtimeTrailingHelper = () => "tail";
console.log(runtimeRead("one"), runtimeTrailingHelper());
export { runtimeBuild, runtimeRead, runtimeTrailingHelper };
"#,
            BindingGroup::source_alpha(
                r#"const buildSelected = (value) => `build:${value}`,
  readSelected = (value) => buildSelected(value).toUpperCase(),
  DECLARATORS_AFTER = null;"#,
                &[
                    ("buildSelected", "buildValue"),
                    ("readSelected", "readValue"),
                ],
            ),
            "BUILD:ONE tail\n",
            &["buildValue", "readValue"][..],
            &["runtimeBuild", "runtimeRead", "runtimeTrailingHelper"][..],
            &["runtimeTrailingHelper", "DECLARATORS_AFTER"][..],
        ),
        (
            "middle",
            r#"const runtimeLeadingHelper = () => "head",
  runtimeBuild = (value) => `build:${value}`,
  runtimeRead = (value) => runtimeBuild(value).toUpperCase(),
  runtimeTrailingHelper = () => "tail";
console.log(runtimeLeadingHelper(), runtimeRead("two"), runtimeTrailingHelper());
export { runtimeLeadingHelper, runtimeBuild, runtimeRead, runtimeTrailingHelper };
"#,
            BindingGroup::source_alpha_adopt_all(
                r#"const DECLARATORS_BEFORE = null,
  buildSelected = (value) => `build:${value}`,
  readSelected = (value) => buildSelected(value).toUpperCase(),
  DECLARATORS_AFTER = null;"#,
            ),
            "head BUILD:TWO tail\n",
            &["buildSelected", "readSelected"][..],
            &[
                "runtimeLeadingHelper",
                "runtimeBuild",
                "runtimeRead",
                "runtimeTrailingHelper",
            ][..],
            &[
                "runtimeLeadingHelper",
                "runtimeTrailingHelper",
                "DECLARATORS_BEFORE",
                "DECLARATORS_AFTER",
            ][..],
        ),
        (
            "trailing",
            r#"const runtimeLeadingHelper = () => "head",
  runtimeBuild = (value) => `build:${value}`,
  runtimeRead = (value) => runtimeBuild(value).toUpperCase();
console.log(runtimeLeadingHelper(), runtimeRead("three"));
export { runtimeLeadingHelper, runtimeBuild, runtimeRead };
"#,
            BindingGroup::source_alpha_adopt_names(
                r#"const DECLARATORS_BEFORE = null,
  buildSelected = (value) => `build:${value}`,
  readSelected = (value) => buildSelected(value).toUpperCase();"#,
                &["buildSelected", "readSelected"],
            ),
            "head BUILD:THREE\n",
            &["buildSelected", "readSelected"][..],
            &["runtimeLeadingHelper", "runtimeBuild", "runtimeRead"][..],
            &["runtimeLeadingHelper", "DECLARATORS_BEFORE"][..],
        ),
    ] {
        let fixture = run_fixture(
            FixtureOpts::new(
                source,
                vec![logical_module_with_binding_groups(module, &[], &[group])],
            )
            .with_import_order(&[module, "residual"]),
        );
        let path = format!("static/app/modules/{module}.js");
        assert_entry_output(&fixture, output);
        assert_module_exports(&fixture.out_root, &path, exports, excluded_exports);
        let declarations: Vec<_> = exports.iter().map(|name| format!("const {name}")).collect();
        let declarations: Vec<_> = declarations.iter().map(String::as_str).collect();
        assert_module_source(&fixture.out_root, &path, &declarations, excluded_source);
    }
}

#[test]
fn declarator_hole_miss_between_hole_and_target_binding_reports_light_no_match() {
    let opts = member_fixture(
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
        "selected_values",
        Member::source_alpha_target(
            "selectedB",
            "selectedB",
            r#"const DECLARATORS_BEFORE = null,
  selectedA = buildItem("a"),
  selectedB = buildItem("b"),
  selectedC = buildItem("c"),
  DECLARATORS_AFTER = null;"#,
        ),
    );

    let outcome = expect_selector_outcome(opts, "no_match", "selected_values");
    assert_eq!(outcome["target_binding"], "selectedB");
    let preview = outcome["selector_preview"].as_str().unwrap();
    for declarator in [
        "selectedA = buildItem",
        "selectedB = buildItem",
        "selectedC = buildItem",
    ] {
        assert!(preview.contains(declarator), "{preview}");
    }
}

#[test]
fn anonymous_source_match_stmt_prefix_holes_still_reject_ambiguous_matches() {
    let opts = anonymous_init_fixture(
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
        r#"if (true) {
  STMT_SETUP;
  console.log("done");
}"#,
    );

    let outcome = expect_selector_outcome(opts, "ambiguous", "init");
    let preview = outcome["selector_preview"].as_str().unwrap();
    for fragment in ["STMT_SETUP", r#"console.log("done")"#] {
        assert!(preview.contains(fragment), "{preview}");
    }
}

#[test]
fn member_source_match_class_member_hole_pins_member_order() {
    // The class-member hole is positional: members pinned before it must be
    // the candidate's leading members in the same order. Listing `b`
    // before `a` does not match a class whose first members are `a`
    // then `b`, so resolution finds no match.
    let opts = member_fixture(
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
        "shapes",
        Member::source_alpha(
            "Selected",
            r#"class K {
  b() {
    STMT_LIST_B;
  }
  a() {
    STMT_LIST_A;
  }
  ANYTHING;
}"#,
        ),
    );

    expect_selector_outcome(opts, "no_match", "shapes");
}

#[test]
fn member_source_match_interleaved_class_member_holes_enforce_order() {
    // The same `Widget`, but the selector pins `close` before `open`.
    // Ordered-subsequence matching keeps source order, so pinning them
    // in the wrong order matches nothing — it is not an unordered
    // "contains both somewhere" match.
    let opts = member_fixture(
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
        "shapes",
        Member::source_alpha(
            "Selected",
            r#"class K {
  ANYTHING;
  close() {
    STMT_LIST_C;
  }
  ANYTHING;
  open() {
    STMT_LIST_O;
  }
  ANYTHING;
}"#,
        ),
    );

    expect_selector_outcome(opts, "no_match", "shapes");
}

#[test]
fn suffixed_hole_labels_are_cosmetic() {
    let universal = run_fixture(
        member_fixture(
            COMPUTE_TOTAL_SOURCE,
            "calc",
            Member::source_alpha("total", r#"const readable = ANYTHING_FUTURE;"#),
        )
        .with_import_order(&["calc", "residual"]),
    );
    assert_entry_output(&universal, "3\n");

    let object_gap = run_fixture(
        member_fixture(
            r#"const actual = { stable: 1, generated: 2, other: 3 };
console.log(actual.stable + actual.other);
export { actual };
"#,
            "objects",
            Member::source_alpha(
                "selected",
                r#"const readable = { stable: EXPR, ANYTHING_FUTURE, other: EXPR };"#,
            ),
        )
        .with_import_order(&["objects"]),
    );
    assert_entry_output(&object_gap, "4\n");

    let named_expr = run_fixture(
        member_fixture(
            COMPUTE_TOTAL_SOURCE,
            "calc",
            Member::source_alpha(
                "total",
                r#"const readable = computeTotal(EXPR_FUTURE, EXPR);"#,
            ),
        )
        .with_import_order(&["calc", "residual"]),
    );
    assert_entry_output(&named_expr, "3\n");

    let empty_expr_label = run_fixture(
        member_fixture(
            r#"const actual = Math.min(10, 4);
console.log(actual);
export { actual };
"#,
            "calc_empty_label",
            Member::source_alpha("total", r#"const readable = Math.min(EXPR_, EXPR);"#),
        )
        .with_import_order(&["calc_empty_label"]),
    );
    assert_entry_output(&empty_expr_label, "4\n");

    let named_stmt = run_fixture(
        member_fixture(
            r#"function setup() {}
function actual() {
  setup();
  return 1;
}
console.log(actual());
export { actual };
"#,
            "calc",
            Member::source_alpha(
                "total",
                r#"function readable() {
  STMT_FUTURE;
  return 1;
}"#,
            ),
        )
        .with_import_order(&["calc", "residual"]),
    );
    assert_entry_output(&named_stmt, "1\n");
}

// `ANYTHING`-vs-keyword redundancy proofs (de-risk "language simplification")
//
// `ANYTHING` is a *run-absorbing* list hole in object-property,
// object-pattern-property, class-member, and declarator position. In a
// call/`new` argument position (`is_run_hole_carrier` has no `ANYTHING`
// fallback) and a block-statement position (`statement_list_hole_name` has no
// `ANYTHING` fallback), a bare `ANYTHING` is a *single-node* hole — `EXPR`
// resp. `STMT` — so it is NOT interchangeable with `ARGS` / `STMT_LIST`. A
// `case CASE_REST:` clause has no `ANYTHING` spelling at all. `DECLARATORS` is
// the one run hole that keeps a typed spelling alongside `ANYTHING`; the
// declarator tests above pin both spellings.
//
// The matched pairs below pin the other claims against a representative subject.

#[test]
fn args_run_absorber_is_not_redundant_with_anything_single_arg() {
    // Subject call has two arguments. `joinParts(ARGS)` absorbs the run and
    // matches; `joinParts(ANYTHING)` is a single-expression hole, so it
    // requires arity 1 and does NOT match a 2-argument call.
    let subject = r#"function joinParts(...parts) {
  return parts.join("|");
}
const actual = joinParts("alpha", "beta");
console.log(actual);
export { actual };
"#;

    // ARGS: run-absorber matches the two-arg call.
    let with_args = run_fixture(
        member_fixture(
            subject,
            "joined",
            Member::source_alpha("joinedValue", r#"const selectedValue = joinParts(ARGS);"#),
        )
        .with_import_order(&["joined", "residual"]),
    );
    assert_entry_output(&with_args, "alpha|beta\n");
    assert_module_exports(
        &with_args.out_root,
        "static/app/modules/joined.js",
        &["joinedValue"],
        &["actual"],
    );

    // ANYTHING in the same position is a single EXPR: arity 1 != 2, no match.
    expect_selector_outcome(
        member_fixture(
            subject,
            "joined",
            Member::source_alpha(
                "joinedValue",
                r#"const selectedValue = joinParts(ANYTHING);"#,
            ),
        )
        .with_import_order(&["residual"]),
        "no_match",
        "joined",
    );
}

#[test]
fn stmt_list_run_absorber_is_not_redundant_with_anything_single_stmt() {
    // The `if` block has three statements. `{ STMT_LIST; }` absorbs the run
    // and matches; `{ ANYTHING; }` is a single-statement hole (arity 1 != 3)
    // and does NOT match.
    let subject = THREE_STATEMENT_BLOCK_SOURCE;

    // STMT_LIST: run-absorber matches the three-statement block.
    let with_stmt_list = run_fixture(anonymous_init_fixture(
        subject,
        r#"if (true) {
  STMT_LIST;
}"#,
    ));
    assert_entry_output(&with_stmt_list, "a\nb\nc\n");

    // ANYTHING as a block statement is a single STMT: arity 1 != 3, no match.
    expect_selector_outcome(
        anonymous_init_fixture(
            subject,
            r#"if (true) {
  ANYTHING;
}"#,
        ),
        "no_match",
        "init",
    );
}
