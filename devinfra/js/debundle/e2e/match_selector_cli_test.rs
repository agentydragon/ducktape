//! End-to-end coverage for `debundle spec match-selector`: the prove-gate probe
//! that resolves a candidate `source_match` against a chunk and reports what it
//! binds, whether it is unique, and (by default) how much further it could be
//! holed.

use std::path::PathBuf;

use debundle_e2e_support::{run_match_selector, write_text_file};
use serde_json::{Value, json};

/// The one outcome a match-selector report carries.
fn outcome(report: &Value) -> &Value {
    &report["outcomes"][0]["outcome"]
}

// leftPanel and rightPanel differ only by their string argument; widgetConfig is
// the lone `makeWidget` call; errorState is the lone object literal; computeTotal
// is the lone function declaration.
const CHUNK: &str = r#"const leftPanel = renderPanel("left");
const widgetConfig = makeWidget("widget", 3, theme);
const rightPanel = renderPanel("right");
const errorState = { kind: "error", code: 500, retry: false };
function computeTotal(items) {
  const base = items.length;
  const tax = base * 2;
  return base + tax;
}
"#;

fn fixture() -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("app.js");
    write_text_file(&source, CHUNK);
    (dir, source)
}

#[test]
fn unique_match_reports_the_bound_target() {
    let (_dir, source) = fixture();
    let report = run_match_selector(
        &source,
        "const w = makeWidget(\"widget\", 3, theme);",
        &["--target-binding", "w"],
    );
    assert_eq!(
        report["outcomes"][0]["outcome"],
        json!({
            "kind": "resolved",
            "owner": 1,
            "binding": "widgetConfig",
            "resolved_by": {"by": "own_selector"},
        }),
        "{report:#}"
    );
    assert_eq!(report["counts"], json!({"resolved": 1}));
    assert_eq!(report["outcomes"][0]["severity"], "ok");
    assert_eq!(report["outcomes"][0]["target_binding"], "w");
}

#[test]
fn split_declarator_match_reports_pre_split_body_index() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("app.js");
    write_text_file(
        &source,
        "const runtimeFirst = build(\"left\"), runtimeSecond = build(\"right\");\n\
         const after = initAfter();\n",
    );
    let report = run_match_selector(
        &source,
        "const first = build(\"left\"), second = build(\"right\");",
        &["--target-binding", "second", "--no-slack"],
    );
    assert_eq!(outcome(&report)["kind"], "resolved", "{report:#}");
    assert_eq!(outcome(&report)["owner"], 0);
    assert_eq!(outcome(&report)["binding"], "runtimeSecond");
}

#[test]
fn no_match_is_not_unique() {
    let (_dir, source) = fixture();
    let report = run_match_selector(
        &source,
        "const w = { kind: \"missing\" };",
        &["--target-binding", "w"],
    );
    assert_eq!(outcome(&report)["kind"], "no_match", "{report:#}");
    // Slack is undefined for a non-unique selector.
    assert!(report.get("slack").is_none());
}

#[test]
fn ambiguous_match_lists_candidates_in_body_order() {
    let (_dir, source) = fixture();
    let report = run_match_selector(
        &source,
        "const p = renderPanel(ANYTHING);",
        &["--target-binding", "p"],
    );
    assert_eq!(
        *outcome(&report),
        json!({
            "kind": "ambiguous",
            "candidates": [
                {"owner": 0, "binding": "leftPanel"},
                {"owner": 2, "binding": "rightPanel"},
            ],
            "truncated": false,
            "differentiators": [
                {"owner": 0, "statement": 0, "anchor": "string literal \"left\""},
                {"owner": 2, "statement": 2, "anchor": "string literal \"right\""},
            ],
        }),
        "{report:#}"
    );
}

#[test]
fn over_pinned_selector_reports_holeable_slack() {
    let (_dir, source) = fixture();
    // The callee + arity already single out the one `makeWidget` call, so the
    // pinned literal arguments are unnecessary precision.
    let report = run_match_selector(
        &source,
        "const w = makeWidget(\"widget\", 3, theme);",
        &["--target-binding", "w"],
    );
    assert_eq!(outcome(&report)["kind"], "resolved", "{report:#}");
    let slack = report["slack"].as_array().unwrap();
    assert!(!slack.is_empty(), "expected over-pin slack, got {report}");
    for relaxation in slack {
        let relaxed = relaxation["relaxed_match"].as_str().unwrap();
        // Every slack variant keeps the discriminating `makeWidget` callee; the
        // arguments were the unnecessary precision (holed to ANYTHING or dropped
        // via ARGS).
        assert!(
            relaxed.contains("makeWidget"),
            "relaxed selector should keep the makeWidget anchor: {relaxed}"
        );
    }
}

#[test]
fn minimally_pinned_selector_reports_empty_slack() {
    let (_dir, source) = fixture();
    // The "left" literal is the only thing distinguishing leftPanel from
    // rightPanel; holing it would make the selector ambiguous, so there is no
    // slack to report.
    let report = run_match_selector(
        &source,
        "const p = renderPanel(\"left\");",
        &["--target-binding", "p"],
    );
    assert_eq!(outcome(&report)["kind"], "resolved", "{report:#}");
    assert_eq!(outcome(&report)["binding"], "leftPanel");
    assert!(report["slack"].as_array().unwrap().is_empty());
}

#[test]
fn no_slack_flag_skips_slack_analysis() {
    let (_dir, source) = fixture();
    let report = run_match_selector(
        &source,
        "const w = makeWidget(\"widget\", 3, theme);",
        &["--target-binding", "w", "--no-slack"],
    );
    assert_eq!(outcome(&report)["kind"], "resolved", "{report:#}");
    // With --no-slack the field is omitted even though the selector is unique.
    assert!(report.get("slack").is_none());
}

#[test]
fn slack_drops_an_unneeded_object_property() {
    let (_dir, source) = fixture();
    // errorState is the only object literal, so any one of its keys alone pins
    // it — the others are droppable kvps, not just holeable values.
    let report = run_match_selector(
        &source,
        "const e = { kind: \"error\", code: 500, retry: false };",
        &["--target-binding", "e"],
    );
    assert_eq!(outcome(&report)["kind"], "resolved", "{report:#}");
    assert_eq!(outcome(&report)["binding"], "errorState");
    let slack = report["slack"].as_array().unwrap();
    // A property-drop relaxation removes the `code` kvp entirely (key and value),
    // which value-holing alone could never do.
    assert!(
        slack.iter().any(|relaxation| !relaxation["relaxed_match"]
            .as_str()
            .unwrap()
            .contains("code")),
        "expected a relaxation that drops the `code` property: {report}"
    );
}

#[test]
fn slack_drops_a_statement_from_an_over_pinned_body() {
    let (_dir, source) = fixture();
    // computeTotal is the only function, so its body statements are not needed
    // for uniqueness and collapse to STMT_LIST runs.
    let report = run_match_selector(
        &source,
        "function f(items) { const base = items.length; const tax = base * 2; return base + tax; }",
        &["--target-binding", "f"],
    );
    assert_eq!(outcome(&report)["kind"], "resolved", "{report:#}");
    assert_eq!(outcome(&report)["binding"], "computeTotal");
    let slack = report["slack"].as_array().unwrap();
    assert!(
        slack.iter().any(|relaxation| relaxation["relaxed_match"]
            .as_str()
            .unwrap()
            .contains("STMT_LIST")),
        "expected a relaxation that drops a body statement: {report}"
    );
}

#[test]
fn slack_drops_a_destructure_pattern_property() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("app.js");
    // One object-destructuring statement; the `loadConfig` initializer plus any
    // one destructured key already pin it, so the sibling pattern props are
    // droppable (the destructure analogue of an object-literal property drop).
    write_text_file(
        &source,
        "const lone = initLone();\nconst { primary, secondary, tertiary } = loadConfig();\n",
    );
    let report = run_match_selector(
        &source,
        "const { primary, secondary, tertiary } = loadConfig();",
        &["--target-binding", "primary"],
    );
    assert_eq!(outcome(&report)["kind"], "resolved", "{report:#}");
    assert_eq!(outcome(&report)["binding"], "primary");
    let slack = report["slack"].as_array().unwrap();
    // A pattern-prop drop removes the `secondary` binding from the destructure
    // entirely (not just holing a value), while the target `primary` stays
    // declared — the guard never drops the target's own binding.
    assert!(
        slack.iter().any(|relaxation| {
            let relaxed = relaxation["relaxed_match"].as_str().unwrap();
            relaxed.contains("primary") && !relaxed.contains("secondary")
        }),
        "expected a relaxation that drops a destructure property: {report}"
    );
}

#[test]
fn slack_drops_a_top_level_context_statement() {
    let (_dir, source) = fixture();
    // The `makeWidget` call alone pins widgetConfig, so the trailing `rightPanel`
    // context statement is unnecessary precision and is dropped outright (a
    // top-level STMT_LIST is not honored on the member-resolution path). No error:
    // the guard keeps the target's own declaration, which the matcher requires
    // `target_binding` to name.
    let report = run_match_selector(
        &source,
        "const widgetConfig = makeWidget(\"widget\", 3, theme);\nconst rightPanel = renderPanel(\"right\");",
        &["--target-binding", "widgetConfig"],
    );
    assert_eq!(outcome(&report)["kind"], "resolved", "{report:#}");
    assert_eq!(outcome(&report)["binding"], "widgetConfig");
    let slack = report["slack"].as_array().unwrap();
    // The context-statement drop removes the whole `rightPanel` statement (binding
    // and all) while keeping the `makeWidget` target — distinct from value-holing,
    // which would keep the `rightPanel` binding with its init holed.
    assert!(
        slack.iter().any(|relaxation| {
            let relaxed = relaxation["relaxed_match"].as_str().unwrap();
            relaxed.contains("makeWidget") && !relaxed.contains("rightPanel")
        }),
        "expected a top-level context-statement drop: {report}"
    );
}

#[test]
fn seq_exprs_selector_resolves_a_memoized_hook() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("app.js");
    // One memoized hook and one sibling that never assigns a memo, so the
    // memoized-callback anchor is the only thing the probe needs.
    write_text_file(
        &source,
        "const slots = [];\n\
         function loadLabel(cache, label) {\n\
           let memo;\n\
           return (cache[0] !== label\n\
             ? (memo = async (id) => {\n\
                 const base = `load:${label}`;\n\
                 return `${base}:${id}`;\n\
               }, cache[0] = label, cache[1] = memo)\n\
             : (memo = cache[1])),\n\
             memo;\n\
         }\n\
         function plainLabel(label) {\n\
           return `load:${label}`;\n\
         }\n",
    );
    let report = run_match_selector(
        &source,
        "function readable(cache, label) {\n\
           let memo;\n\
           return (EXPR ? (memo = async (id) => {\n\
             const base = `load:${label}`;\n\
             return `${base}:${id}`;\n\
           }, SEQ_EXPRS) : memo = EXPR), memo;\n\
         }",
        &["--target-binding", "readable"],
    );
    assert_eq!(outcome(&report)["kind"], "resolved", "{report:#}");
    assert_eq!(outcome(&report)["binding"], "loadLabel");
}

#[test]
fn slack_holes_a_comma_sequence_element() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("app.js");
    // The one sequence-valued declaration: any element of it already pins it,
    // so the siblings are droppable — the sequence analogue of dropping an
    // object property.
    write_text_file(
        &source,
        "const leftFlag = readLeft();\n\
         const rightFlag = readRight();\n\
         const sequenced = (leftFlag, \"marker\", rightFlag);\n",
    );
    let report = run_match_selector(
        &source,
        "const sequenced = (leftFlag, \"marker\", rightFlag);",
        &["--target-binding", "sequenced"],
    );
    assert_eq!(outcome(&report)["kind"], "resolved", "{report:#}");
    assert_eq!(outcome(&report)["binding"], "sequenced");
    let slack = report["slack"].as_array().unwrap();
    assert!(
        slack.iter().any(|relaxation| relaxation["relaxed_match"]
            .as_str()
            .unwrap()
            .contains("SEQ_EXPRS")),
        "expected a relaxation that absorbs a sequence element: {report}"
    );
}

#[test]
fn seq_exprs_outside_a_sequence_is_an_invalid_probe() {
    let (_dir, source) = fixture();
    let report = run_match_selector(
        &source,
        "const w = (SEQ_EXPRS);",
        &["--target-binding", "w", "--no-slack"],
    );
    assert_eq!(outcome(&report)["kind"], "invalid", "{report:#}");
    assert!(
        outcome(&report)["error"]
            .as_str()
            .unwrap()
            .contains("run-hole keyword outside a list position"),
        "{report:#}"
    );
}
