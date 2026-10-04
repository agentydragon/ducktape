//! End-to-end coverage for `source_match` syntactic holes.
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

const SETUP_BLOCK_SOURCE: &str = r#"if (true) {
  console.log("setup");
  console.log("done");
}
const marker = "ready";
export { marker };
"#;

const THREE_STATEMENT_BLOCK_SOURCE: &str = r#"if (true) {
  console.log("a");
  console.log("b");
  console.log("c");
}
const marker = "ready";
export { marker };
"#;

const SINGLE_STATEMENT_BLOCK_SOURCE: &str = r#"if (true) {
  console.log("only");
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
fn member_source_match_alpha_all_allows_name_reuse_in_sibling_function_scopes() {
    let fixture = run_fixture(member_fixture(
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
        "format",
        Member::source_alpha(
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
        ),
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
    let fixture = run_fixture(member_fixture(
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
        "format",
        Member::source_alpha(
            "format_items",
            r#"function readable(items) {
  return items
    .map((item) => EXPR)
    .filter((line) => line !== "")
    .map((line) => line.toUpperCase())
    .join(",");
}"#,
        ),
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

#[test]
fn member_source_match_anything_object_property_hole_skips_arbitrary_key_values() {
    let fixture = run_fixture(member_fixture(
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
        "config",
        Member::source_alpha_target(
            "config_object",
            "readable",
            r#"const readable = {
  requiredKey: ANYTHING,
  ANYTHING,
  anotherKey: ANYTHING,
};"#,
        ),
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

// A concise arrow whose body is a parenthesized object literal
// (`(props) => ({ … })`) is the idiomatic component/factory shape; the returned
// object is the stable, re-minify-proof anchor. The selector pins the factory by
// its returned object's distinctive `kind` key (with `ANYTHING` absorbing the
// noisy generated members), end to end through the lowering pipeline.
#[test]
fn member_source_match_arrow_returning_object_literal_selects_factory() {
    let fixture = run_fixture(member_fixture(
        r#"const makeWidget = (props) => ({
  kind: "widget",
  render: () => props.label.toUpperCase(),
  dispose() {},
});
console.log(makeWidget({ label: "ok" }).render());
export { makeWidget };
"#,
        "factory",
        Member::source_alpha_target(
            "widget_factory",
            "readable",
            r#"const readable = (props) => ({
  kind: "widget",
  ANYTHING,
});"#,
        ),
    ));

    assert_entry_output(&fixture, "OK\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/factory.js",
        &["widget_factory"],
        &["makeWidget"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/factory.js",
        &[
            "const widget_factory",
            // the concise arrow object body survives lowering (the `({` shows the
            // object-expression body paren was kept, not turned into a block).
            "({",
            r#"kind: "widget""#,
            "render",
        ],
        &["ANYTHING", "readable"],
    );
}

// A function whose body is a parenthesized sequence/assignment expression — the
// esbuild/TypeScript `__decorate` shape `(applyDecorators(t, d), t)` — is pinned
// by that distinctive sequence body, end to end. The `Seq` and its inner call
// survive parsing/lowering (parens are transparent grouping), so the selector
// asserts the structure rather than the minified name.
#[test]
fn member_source_match_parenthesized_sequence_body_selects_helper() {
    let fixture = run_fixture(member_fixture(
        r#"function applyDecorators(target, decorators) {
  for (const decorate of decorators) decorate(target);
}
function decorate(target, decorators) {
  return (applyDecorators(target, decorators), target);
}
const tagged = decorate({ tags: [] }, [(t) => t.tags.push("a")]);
console.log(tagged.tags.join(","));
export { decorate };
"#,
        "helpers",
        Member::source_alpha(
            "decorate_helper",
            r#"function readable(target, decorators) {
  return (applyDecorators(target, decorators), target);
}"#,
        ),
    ));

    assert_entry_output(&fixture, "a\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/helpers.js",
        &[
            "function decorate_helper",
            // the sequence body survives lowering, inner call + trailing return
            // value intact.
            "applyDecorators(target, decorators)",
        ],
        &["readable"],
    );
}

// A long array-literal initializer is pinned by `ARRAY_ELEMENTS` anchoring on its
// few stable elements (the leading and trailing entries) while the run hole
// absorbs the noisy middle, end to end — instead of over-pinning every element.
#[test]
fn member_source_match_array_elements_hole_anchors_stable_endpoints() {
    let fixture = run_fixture(member_fixture(
        r#"const palette = [
  "black",
  "slate",
  "gray",
  "silver",
  "white",
];
console.log(palette[0], palette[palette.length - 1]);
export { palette };
"#,
        "theme",
        Member::source_alpha_target(
            "color_palette",
            "readable",
            r#"const readable = ["black", ARRAY_ELEMENTS, "white"];"#,
        ),
    ));

    assert_entry_output(&fixture, "black white\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/theme.js",
        &["color_palette"],
        &["palette"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/theme.js",
        &[
            "const color_palette",
            // the full array (every original element) is emitted; the selector
            // anchored only the endpoints, the run hole absorbed the middle.
            r#""black""#,
            r#""gray""#,
            r#""white""#,
        ],
        &["ARRAY_ELEMENTS", "readable"],
    );
}

// A React Compiler memoized hook keeps its cache writes in one comma sequence
// after the memo assignment: one write per dependency plus the memo write.
// `SEQ_EXPRS` absorbs that tail, so the selector pins the hook by its memoized
// callback and survives a rebuild that changes the dependency count. Here: two
// writes.
#[test]
fn member_source_match_seq_exprs_hole_anchors_a_memoized_hook() {
    let fixture = run_fixture(member_fixture(
        r#"const slots = [];
function loadLabel(cache, label) {
  let memo;
  return (cache[0] !== label
    ? (memo = async (id) => {
        const base = `load:${label}`;
        return `${base}:${id}`;
      }, cache[0] = label, cache[1] = memo)
    : (memo = cache[1])),
    memo;
}
// Same callback, but no assignment to memo: it must not match the selector.
function otherLabel(cache, label) {
  let memo;
  return (cache[0] !== label
    ? (async (id) => {
        const base = `load:${label}`;
        return `${base}:${id}`;
      }, cache[0] = label, cache[1] = memo)
    : (memo = cache[1])), memo;
}
loadLabel(slots, "first")("a").then((value) => console.log(value));
export { loadLabel };
"#,
        "resource",
        Member::source_alpha_target(
            "label_resource",
            "readable",
            r#"function readable(cache, label) {
  let memo;
  return (EXPR ? (memo = async (id) => {
    const base = `load:${label}`;
    return `${base}:${id}`;
  }, SEQ_EXPRS) : memo = EXPR), memo;
}"#,
        ),
    ));

    assert_entry_output(&fixture, "load:first:a\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/resource.js",
        &["label_resource"],
        &["loadLabel", "otherLabel"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/resource.js",
        // the whole hook, cache writes and all; the hole absorbed them in the
        // selector only.
        &["function label_resource", "load:", "cache[0]"],
        &["SEQ_EXPRS", "readable"],
    );
}

// The same selector against a hook whose dependency count grew: five writes
// instead of two. The selector names no cache slot, so it still resolves
// uniquely — the reason `SEQ_EXPRS` exists.
#[test]
fn member_source_match_seq_exprs_hole_survives_a_dependency_count_change() {
    let fixture = run_fixture(member_fixture(
        r#"const slots = [];
function loadLabel(cache, label) {
  let memo;
  return (cache[0] !== label || cache[1] !== label || cache[2] !== label
    ? (memo = async (id) => {
        const base = `load:${label}`;
        return `${base}:${id}`;
      }, cache[0] = label, cache[1] = label, cache[2] = label, cache[3] = label,
      cache[4] = memo)
    : (memo = cache[4])),
    memo;
}
loadLabel(slots, "first")("a").then((value) => console.log(value));
export { loadLabel };
"#,
        "resource",
        Member::source_alpha_target(
            "label_resource",
            "readable",
            r#"function readable(cache, label) {
  let memo;
  return (EXPR ? (memo = async (id) => {
    const base = `load:${label}`;
    return `${base}:${id}`;
  }, SEQ_EXPRS) : memo = EXPR), memo;
}"#,
        ),
    ));

    assert_entry_output(&fixture, "load:first:a\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/resource.js",
        &["label_resource"],
        &["loadLabel"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/resource.js",
        &["function label_resource", "load:", "cache[4]"],
        &["SEQ_EXPRS", "readable"],
    );
}

// Two `SEQ_EXPRS` holes in one sequence: the element pinned between them is
// matched in order and each hole absorbs the run on its side (including none),
// so the sibling sequence without that literal is not claimed.
#[test]
fn member_source_match_two_seq_exprs_holes_bracket_a_pinned_element() {
    let fixture = run_fixture(member_fixture(
        r#"function runPipeline(flags) {
  return (flags.on, "marker", 1, 2, flags.off);
}
function otherPipeline(flags) {
  return (flags.on, 1, 2, flags.off);
}
console.log(runPipeline({ on: "left", off: "right" }));
console.log(otherPipeline({ on: "left", off: "other" }));
export { runPipeline };
"#,
        "pipeline",
        Member::source_alpha_target(
            "marked_pipeline",
            "readable",
            r#"function readable(flags) {
  return (SEQ_EXPRS, "marker", SEQ_EXPRS);
}"#,
        ),
    ));

    assert_entry_output(&fixture, "right\nother\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/pipeline.js",
        &["marked_pipeline"],
        &["runPipeline", "otherPipeline"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/pipeline.js",
        &["function marked_pipeline", r#""marker""#, "flags.off"],
        &["SEQ_EXPRS", "readable"],
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
// single-declarator needle against each declarator of the owner, so the nested
// `"GET"` / `"POST"` distinguishes the otherwise-identical siblings. Each resolves
// to its own module.
#[test]
fn member_source_match_comma_list_siblings_disambiguated_by_nested_value() {
    let fixture = run_fixture(FixtureOpts::new(
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
    ));

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
    expect_rejection_containing_all(
        member_fixture(
            HANDLER_SIBLINGS,
            "routes",
            Member::source_alpha_target(
                "handler",
                "readable",
                "const readable = makeHandler({ route: { method: EXPR } });",
            ),
        ),
        &["ambiguous", "handlerA", "handlerB"],
    );
}

#[test]
fn source_match_anything_object_key_reports_unsupported_position() {
    expect_rejection_containing_all(
        member_fixture(
            r#"const actual = { mode: "runtime" };
console.log(actual.mode);
export { actual };
"#,
            "config",
            Member::source_alpha("makeConfig", r#"const readable = { ANYTHING: "runtime" };"#),
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
fn member_source_match_anything_pattern_skips_destructuring_shape() {
    let fixture = run_fixture(member_fixture(
        r#"function actual({ value, ignored }) {
  return `stable:${value}`;
}
console.log(actual({ value: "ok", ignored: "noise" }));
export { actual };
"#,
        "config",
        Member::source_alpha(
            "readConfig",
            r#"function readable(ANYTHING) {
  return `stable:${ANYTHING}`;
}"#,
        ),
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
fn member_source_match_anything_matches_expression_subtrees() {
    let fixture = run_fixture(member_fixture(
        r#"const actual = Number.parseInt("8", 10) + [1, 2, 3].length;
console.log(actual);
export { actual };
"#,
        "calc",
        Member::source_alpha("calc_value", r#"const readable = ANYTHING + ANYTHING;"#),
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
fn literal_regex_holes_select_and_emit_the_matching_value() {
    for (source, selector, output, declaration) in [
        (
            r#"const runtimeStyle = "WidgetShell-42";
console.log(runtimeStyle);
export { runtimeStyle };
"#,
            r#"const readableStyle = STR_LITERAL_MATCHING_RE("^WidgetShell-[0-9]+$");"#,
            "WidgetShell-42\n",
            r#"const shellStyle = "WidgetShell-42""#,
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
        ),
    ] {
        let fixture = run_fixture(member_fixture(
            source,
            "styles/shell",
            Member::source_alpha("shellStyle", selector),
        ));
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
    for (source, selector, outcome, anchor) in [
        (
            r#"const decoyStyle = "foo";
const runtimeStyle = "fo";
console.log(runtimeStyle);
export { decoyStyle, runtimeStyle };
"#,
            r#"const readableStyle = STR_LITERAL_MATCHING_RE("^foo*");"#,
            "ambiguous",
            "foo",
        ),
        (
            r#"const runtimeStyle = "PanelShell-42";
console.log(runtimeStyle);
export { runtimeStyle };
"#,
            r#"const readableStyle = STR_LITERAL_MATCHING_RE("^WidgetShell-[0-9]+$");"#,
            "did not match any top-level declaration",
            "WidgetShell",
        ),
        (
            r#"const runtimePrimaryStyle = "WidgetShell-1";
const runtimeSecondaryStyle = "WidgetShell-2";
console.log(runtimePrimaryStyle, runtimeSecondaryStyle);
export { runtimePrimaryStyle, runtimeSecondaryStyle };
"#,
            r#"const readableStyle = STR_LITERAL_MATCHING_RE("^WidgetShell-[0-9]+$");"#,
            "ambiguous",
            "WidgetShell",
        ),
    ] {
        expect_rejection_containing_all(
            member_fixture(
                source,
                "styles/shell",
                Member::source_alpha("shellStyle", selector),
            ),
            &["styles/shell", outcome, "STR_LITERAL_MATCHING_RE", anchor],
        );
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
fn member_source_match_argument_list_holes_skip_unimportant_arguments() {
    let fixture = run_fixture(member_fixture(
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
        "joined",
        Member::source_alpha(
            "joinedValue",
            r#"const selectedValue = joinParts("stable", ARGS_BEFORE, importantValue, ARGS_AFTER);"#,
        ),
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
fn grouped_source_matches_string_literal_regex_predicates_match_each_target_binding() {
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
fn grouped_source_matches_string_literal_regex_range_exports_style_object() {
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
fn grouped_source_matches_range_ignores_comments_and_exports_subset() {
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
fn grouped_source_matches_range_with_outer_stmt_list_holes_stays_native() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const ignoredBefore = "before";
const runtimeFirst = "alpha";
function runtimeSecond() {
  return `${runtimeFirst}:beta`;
}
const ignoredAfter = "after";
console.log(runtimeSecond(), ignoredBefore, ignoredAfter);
export { ignoredBefore, runtimeFirst, runtimeSecond, ignoredAfter };
"#,
        vec![logical_module_with_binding_groups(
            "selected/range",
            &[],
            &[BindingGroup::source_alpha(
                r#"STMT_LIST_HEAD;
const first = "alpha";
function second() {
  return `${first}:beta`;
}
STMT_LIST_TAIL;"#,
                &[("first", "first"), ("second", "second")],
            )],
        )],
    ));

    assert_entry_output(&fixture, "alpha:beta before after\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/selected/range.js",
        &["first", "second"],
        &["ignoredBefore", "ignoredAfter"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/selected/range.js",
        &["const first", "function second", "alpha"],
        &["ignoredBefore", "ignoredAfter", "STMT_LIST"],
    );
}

#[test]
fn grouped_source_matches_range_with_internal_stmt_list_hole_stays_native() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeFirst = "alpha";
const ignoredMiddle = "middle";
function runtimeSecond() {
  return `${runtimeFirst}:beta`;
}
console.log(runtimeSecond(), ignoredMiddle);
export { runtimeFirst, ignoredMiddle, runtimeSecond };
"#,
        vec![logical_module_with_binding_groups(
            "selected/gapped-range",
            &[],
            &[BindingGroup::source_alpha(
                r#"const first = "alpha";
STMT_LIST_MIDDLE;
function second() {
  return `${first}:beta`;
}"#,
                &[("first", "first"), ("second", "second")],
            )],
        )],
    ));

    assert_entry_output(&fixture, "alpha:beta middle\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/selected/gapped-range.js",
        &["first", "second"],
        &["ignoredMiddle"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/selected/gapped-range.js",
        &["const first", "function second", "alpha"],
        &["ignoredMiddle", "STMT_LIST"],
    );
}

#[test]
fn grouped_source_matches_range_matches_exported_const_declarations() {
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
fn grouped_source_matches_range_matches_mixed_declaration_kinds() {
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

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::settings",
            "annotations key `secondary` does not match",
            "secondary",
        ],
    );
}

#[test]
fn member_source_match_declarator_holes_select_binding_from_wider_const_list() {
    let fixture = run_fixture(member_fixture(
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
        "display",
        Member::source_alpha_target(
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
        ),
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
    let fixture = run_fixture(member_fixture(
        r#"const runtimePrefix = "prefix",
  runtimeBuild = (value) => `build:${value}`,
  runtimeRead = (value) => runtimeBuild(value).toUpperCase(),
  runtimeSuffix = "suffix";
console.log(runtimePrefix, runtimeRead("one"), runtimeSuffix);
export { runtimePrefix, runtimeBuild, runtimeRead, runtimeSuffix };
"#,
        "display",
        Member::source_alpha_target(
            "readDisplayValue",
            "readDisplayValue",
            r#"const ANYTHING = ANYTHING,
  buildDisplayValue = (value) => `build:${value}`,
  readDisplayValue = (value) => buildDisplayValue(value).toUpperCase(),
  ANYTHING = ANYTHING;"#,
        ),
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
fn source_match_declarator_holes_extract_multiple_bindings_and_skip_holes_for_adopt_names() {
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
        let fixture = run_fixture(FixtureOpts::new(
            source,
            vec![logical_module_with_binding_groups(module, &[], &[group])],
        ));
        let path = format!("static/app/modules/{module}.js");
        assert_entry_output(&fixture, output);
        assert_module_exports(&fixture.out_root, &path, exports, excluded_exports);
        let declarations: Vec<_> = exports.iter().map(|name| format!("const {name}")).collect();
        let declarations: Vec<_> = declarations.iter().map(String::as_str).collect();
        assert_module_source(&fixture.out_root, &path, &declarations, excluded_source);
    }
}

#[test]
fn declarator_hole_miss_reports_light_no_match() {
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
            "source_matches",
            "firstSelected",
            "did not match",
            "firstSelected = operation",
            "secondSelected = operation",
        ],
    );
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

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::selected_values",
            "source_matches[].bindings[`selectedB`]",
            "did not match any top-level declaration",
            "selectedA = buildItem",
            "selectedB = buildItem",
            "selectedC = buildItem",
        ],
    );
}

#[test]
fn anonymous_source_match_stmt_prefix_hole_matches_arbitrary_nested_statement() {
    let fixture = run_fixture(anonymous_init_fixture(
        SETUP_BLOCK_SOURCE,
        r#"if (true) {
  STMT_SETUP;
  console.log("done");
}"#,
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
fn anonymous_source_match_stmt_list_hole_absorbs_empty_run() {
    // A trailing `STMT_LIST_TAIL;` matches a block that has only the
    // pinned prefix statement — the hole absorbs zero statements.
    let fixture = run_fixture(anonymous_init_fixture(
        SINGLE_STATEMENT_BLOCK_SOURCE,
        r#"if (true) {
  console.log("only");
  STMT_LIST_TAIL;
}"#,
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
fn member_source_match_class_member_hole_selects_class_ignoring_other_members() {
    // Pin the class by its constructor (body hole) and let `ANYTHING;`
    // absorb `increment` and `reset`. The whole class still moves — the
    // hole is only in the selector, not the output.
    let fixture = run_fixture(member_fixture(
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
        "shapes",
        Member::source_alpha(
            "Counter",
            r#"class K {
  constructor() {
    STMT_LIST_CTOR;
  }
  ANYTHING;
}"#,
        ),
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
        &["ANYTHING", "STMT_LIST_CTOR"],
    );
}

#[test]
fn member_source_match_case_rest_hole_selects_switch_ignoring_other_cases() {
    // Pin the function by one discriminating `case "go":` arm and let the
    // `case CASE_REST_*:` holes absorb the surrounding cases. The whole
    // function still moves — the holes are only in the selector.
    let fixture = run_fixture(member_fixture(
        r#"function dispatch(kind) {
  switch (kind) {
    case "alpha":
      return 1;
    case "beta":
      return 2;
    case "go":
      return 42;
    case "delta":
      return 4;
    default:
      return 0;
  }
}
console.log(dispatch("go"));
export { dispatch };
"#,
        "router",
        Member::source_alpha(
            "dispatch",
            r#"function readable(ANYTHING) {
  switch (ANYTHING) {
    case CASE_REST_BEFORE:
    case "go":
      STMT_LIST_GO;
    case CASE_REST_AFTER:
  }
}"#,
        ),
    ));

    assert_entry_output(&fixture, "42\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/router.js",
        &["dispatch"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/router.js",
        // The full switch moved, every case and all.
        &[
            "function dispatch",
            "switch",
            "\"alpha\"",
            "\"go\"",
            "default",
        ],
        &["CASE_REST", "STMT_LIST_GO"],
    );
}

#[test]
fn member_source_match_anything_class_member_selects_class_ignoring_other_members() {
    let fixture = run_fixture(member_fixture(
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
        "counter",
        Member::source_alpha(
            "Counter",
            r#"class Counter {
  ANYTHING;
  label() {
    ANYTHING;
  }
  ANYTHING;
}"#,
        ),
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
    // The skeleton `class K { run() { STMT_LIST } ANYTHING; }` matches
    // both `Alpha` and `Beta`; ambiguous matches stay hard errors.
    let opts = member_fixture(
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
        "shapes",
        Member::source_alpha(
            "Selected",
            r#"class K {
  run() {
    STMT_LIST_BODY;
  }
  ANYTHING;
}"#,
        ),
    );

    expect_rejection_containing_all(opts, &["static/app::shapes", "ambiguous"]);
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

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::shapes",
            "did not match",
            "class K",
            "STMT_LIST_A",
        ],
    );
}

#[test]
fn class_source_match_miss_reports_light_no_match() {
    // The selector has all of the intended class's method anchors in order,
    // but one method body is too exact. Production diagnostics should stay
    // light and report the failed selector instead of running a second
    // near-miss matcher.
    let opts = member_fixture(
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
        "catalog",
        Member::source_alpha(
            "CatalogCache",
            r#"class K {
  ANYTHING;
  refreshEntriesNow(scope, filter) {
    if (filter.active) {
      this.loadBatch(scope, filter);
    }
  }
  ANYTHING;
  loadBatch(scope, filter) {
    STMT_LIST;
  }
  ANYTHING;
  lookupEntryByKey(key, record) {
    STMT_LIST;
  }
  ANYTHING;
  dropEntryByKey(key, record) {
    STMT_LIST;
  }
  ANYTHING;
}"#,
        ),
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::catalog",
            "did not match",
            "refreshEntriesNow",
            "filter.active",
        ],
    );
}

#[test]
fn anonymous_expr_holes_match_independent_subtrees() {
    // Labels are cosmetic: the two `EXPR_VALUE` occurrences match different
    // expressions, not a shared equality binding.
    let fixture = run_fixture(member_fixture(
        r#"const actual = Math.max(Number.parseInt("7", 10), [1, 2, 3].length);
console.log(actual);
export { actual };
"#,
        "calc",
        Member::source_alpha(
            "calc_value",
            r#"const readable = Math.max(EXPR_VALUE, EXPR_VALUE);"#,
        ),
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
fn member_source_match_class_member_holes_bracket_interior_member() {
    // Two `ANYTHING;` holes bracket a single pinned member, so the
    // selector matches a class by an interior member it contains: the
    // leading hole absorbs `a`, the trailing hole absorbs `c`, and `b`
    // is pinned in between. (Previously a second class-member hole was a hard
    // "ambiguous, never matches"; it is now an ordered-subsequence gap.)
    let fixture = run_fixture(member_fixture(
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
        "shapes",
        Member::source_alpha(
            "Counter",
            r#"class K {
  ANYTHING;
  b() {
    STMT_LIST_B;
  }
  ANYTHING;
}"#,
        ),
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
        &["ANYTHING", "STMT_LIST_B"],
    );
}

#[test]
fn member_source_match_interleaved_class_member_holes_match_ordered_members() {
    // Two pinned members separated by an `ANYTHING;` hole match a class
    // that contains them in that order with other members interspersed:
    // `open` (after `setup`) then `close` (after `tick`). This is the
    // ordered-subset fingerprint — pin a few stable members, ignore the
    // rest.
    let fixture = run_fixture(member_fixture(
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
        "shapes",
        Member::source_alpha(
            "Widget",
            r#"class K {
  ANYTHING;
  open() {
    STMT_LIST_O;
  }
  ANYTHING;
  close() {
    STMT_LIST_C;
  }
  ANYTHING;
}"#,
        ),
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
        &["ANYTHING"],
    );
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

    expect_rejection_containing_all(opts, &["static/app::shapes", "did not match"]);
}

#[test]
fn anonymous_source_match_multiple_stmt_list_holes_bracket_pinned_statements() {
    // Three `STMT_LIST_*;` holes bracket two pinned statements inside a
    // block: the holes absorb the `a`/`b`/`c` logs, leaving `pinned1`
    // then `pinned2` matched in order.
    let fixture = run_fixture(anonymous_init_fixture(
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
        r#"if (true) {
  STMT_LIST_HEAD;
  console.log("pinned1");
  STMT_LIST_MID;
  console.log("pinned2");
  STMT_LIST_TAIL;
}"#,
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
fn non_trailing_class_member_hole_keeps_later_identifiers_aligned() {
    // Regression guard for the alpha-identifier bijection: a leading
    // The class-member hole absorbs `helper`, whose param/body identifiers do not
    // desync the `run(value) { return value * 2 }` member that follows.
    // (Under the old global alpha-canonicalization the absorbed `helper`
    // identifiers shifted the numbering and this failed to match.)
    let fixture = run_fixture(member_fixture(
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
        "shapes",
        Member::source_alpha(
            "Counter",
            r#"class K {
  ANYTHING;
  run(value) {
    return value * 2;
  }
}"#,
        ),
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
        &["ANYTHING"],
    );
}

#[test]
fn single_node_hole_keeps_later_identifiers_aligned() {
    // The same bijection guard for single-node holes: `EXPR` absorbs a
    // multi-identifier subtree, and the `limit` argument after it still
    // matches by alpha-correspondence rather than by absolute position.
    let fixture = run_fixture(member_fixture(
        r#"const limit = 4;
const alpha = 1, beta = 2, gamma = 3;
const total = Math.max(Math.min(alpha, beta, gamma), limit);
console.log(total);
export { total };
"#,
        "calc",
        Member::source_alpha("calc_total", r#"const readable = Math.max(EXPR, limit);"#),
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
fn suffixed_hole_labels_are_cosmetic() {
    let universal = run_fixture(member_fixture(
        COMPUTE_TOTAL_SOURCE,
        "calc",
        Member::source_alpha("total", r#"const readable = ANYTHING_FUTURE;"#),
    ));
    assert_entry_output(&universal, "3\n");

    let object_gap = run_fixture(member_fixture(
        r#"const actual = { stable: 1, generated: 2, other: 3 };
console.log(actual.stable + actual.other);
export { actual };
"#,
        "objects",
        Member::source_alpha(
            "selected",
            r#"const readable = { stable: EXPR, ANYTHING_FUTURE, other: EXPR };"#,
        ),
    ));
    assert_entry_output(&object_gap, "4\n");

    let named_expr = run_fixture(member_fixture(
        COMPUTE_TOTAL_SOURCE,
        "calc",
        Member::source_alpha(
            "total",
            r#"const readable = computeTotal(EXPR_FUTURE, EXPR);"#,
        ),
    ));
    assert_entry_output(&named_expr, "3\n");

    let empty_expr_label = run_fixture(member_fixture(
        r#"const actual = Math.min(10, 4);
console.log(actual);
export { actual };
"#,
        "calc_empty_label",
        Member::source_alpha("total", r#"const readable = Math.min(EXPR_, EXPR);"#),
    ));
    assert_entry_output(&empty_expr_label, "4\n");

    let named_stmt = run_fixture(member_fixture(
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
    ));
    assert_entry_output(&named_stmt, "1\n");
}

// ---------------------------------------------------------------------------
// `ANYTHING`-vs-keyword redundancy proofs (de-risk "language simplification")
//
// `ANYTHING` is a *run-absorbing* list hole in object-property,
// object-pattern-property, class-member, and declarator position. In a
// call/`new` argument position (`argument_list_hole_name` has no `ANYTHING`
// fallback) and a block-statement position (`statement_list_hole_name` has no
// `ANYTHING` fallback), a bare `ANYTHING` is a *single-node* hole — `EXPR`
// resp. `STMT` — so it is NOT interchangeable with `ARGS` / `STMT_LIST`. A
// `case CASE_REST:` clause has no `ANYTHING` spelling at all. `DECLARATORS` is
// the one run hole that keeps a typed spelling alongside `ANYTHING`; the
// declarator tests above pin both spellings.
//
// The matched pairs below pin the other claims against a representative subject.
// ---------------------------------------------------------------------------

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
    let with_args = run_fixture(member_fixture(
        subject,
        "joined",
        Member::source_alpha("joinedValue", r#"const selectedValue = joinParts(ARGS);"#),
    ));
    assert_entry_output(&with_args, "alpha|beta\n");
    assert_module_exports(
        &with_args.out_root,
        "static/app/modules/joined.js",
        &["joinedValue"],
        &["actual"],
    );

    // ANYTHING in the same position is a single EXPR: arity 1 != 2, no match.
    expect_rejection_containing_all(
        member_fixture(
            subject,
            "joined",
            Member::source_alpha(
                "joinedValue",
                r#"const selectedValue = joinParts(ANYTHING);"#,
            ),
        ),
        &[
            "static/app::joined",
            "did not match any top-level declaration",
        ],
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
    expect_rejection_containing_all(
        anonymous_init_fixture(
            subject,
            r#"if (true) {
  ANYTHING;
}"#,
        ),
        &["static/app::init", "did not match"],
    );
}

/// Two `source_matches[]` entries in one module whose templates both spell the
/// same function: one binds the function, the other binds the `WeakMap` that a
/// multi-declarator `const` declares just before it.
#[test]
fn grouped_source_matches_sharing_a_statement_bind_each_target() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const limit = 10, cache = new WeakMap();
function lookup(n, e) {
  let t = cache.get(n);
  if (!t) {
    t = { [e]: limit };
    cache.set(n, t);
  }
  return t[e];
}
console.log(lookup({}, "x"));
export { lookup };
"#,
        vec![logical_module_with_binding_groups(
            "cache",
            &[],
            &[
                BindingGroup::source_alpha(
                    r#"function getCached(n, e) {
  let t = cache.get(n);
  STMT_LIST;
}"#,
                    &[("getCached", "getCached")],
                ),
                BindingGroup::source_alpha(
                    r#"const DECLARATORS, propertyCache = new WeakMap();
function getCached(n, e) {
  let t = propertyCache.get(n);
  STMT_LIST;
}"#,
                    &[("propertyCache", "propertyCache")],
                ),
            ],
        )],
    ));

    assert_entry_output(&fixture, "10\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/cache.js",
        &["const propertyCache = new WeakMap()", "function getCached"],
        &["const propertyCache = 10"],
    );
}

/// A multi-statement member selector whose target sits after a `DECLARATORS`
/// run binds the declarator the run leaves pinned, not the first one.
#[test]
fn multi_statement_member_target_after_declarators_run_binds_its_declarator() {
    let fixture = run_fixture(member_fixture(
        r#"const limit = 10, cache = new WeakMap();
function lookup(n) {
  return cache.get(n) ?? limit;
}
console.log(lookup({}));
export { lookup };
"#,
        "cache",
        Member::source_alpha_target(
            "propertyCache",
            "propertyCache",
            r#"const DECLARATORS, propertyCache = new WeakMap();
function lookupCached(n) {
  return propertyCache.get(n) ?? ANYTHING;
}"#,
        ),
    ));

    assert_entry_output(&fixture, "10\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/cache.js",
        &["const propertyCache = new WeakMap()"],
        &["const propertyCache = 10"],
    );
}

/// A floating `ANYTHING = <init>` declarator declares nothing in the selector
/// but absorbs a declarator in the chunk; the pinned target after it still
/// binds its own declarator.
#[test]
fn target_after_floating_anything_declarator_binds_its_declarator() {
    let fixture = run_fixture(member_fixture(
        r#"const cache = new WeakMap(), limit = 10;
console.log(cache instanceof WeakMap, limit);
export { cache, limit };
"#,
        "limits",
        Member::source_alpha_target(
            "maxEntries",
            "maxEntries",
            r#"const ANYTHING = new WeakMap(), maxEntries = 10;"#,
        ),
    ));

    assert_entry_output(&fixture, "true 10\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/limits.js",
        &["const maxEntries = 10"],
        &["const maxEntries = new WeakMap()"],
    );
}
