//! Small synthetic regressions for selector choices that remain stable under
//! harmless source edits, including minified-name changes.

use std::path::Path;

use debundle_e2e_support::{
    parse_stdout_json, run_match_selector, run_synthesize_selectors, write_text_file,
};

fn synthesized_match(root: &Path, source: &str) -> String {
    let source_file = root.join("source.js");
    write_text_file(&source_file, source);
    let modules = root.join("modules");
    write_text_file(
        &modules.join("app/example.yaml"),
        "members:\n  - name: Selected\n    selector:\n      binding:\n        name: a\n",
    );
    let result = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source_file.to_str().unwrap(),
            "--item",
            "app/example:Selected",
            "--format",
            "json",
        ],
    );
    let report = parse_stdout_json(&result);
    assert_eq!(
        report["candidates"][0]["action"], "would_change",
        "{report}"
    );
    report["candidates"][0]["match_source"]
        .as_str()
        .unwrap()
        .to_owned()
}

fn assert_matches_rebuilt_source(original: &str, rebuilt: &str, expected_binding: &str) {
    let dir = tempfile::tempdir().unwrap();
    let selector = synthesized_match(dir.path(), original);
    let rebuilt_file = dir.path().join("rebuilt.js");
    write_text_file(&rebuilt_file, rebuilt);
    let result = run_match_selector(
        &rebuilt_file,
        &selector,
        &["--target-binding", "Selected", "--no-slack"],
    );
    assert_eq!(
        result["outcomes"][0]["outcome"]["binding"], expected_binding,
        "selector did not survive a source edit:\n{selector}\n{result}"
    );
}

#[test]
fn prefers_role_marker_to_mutable_tooltip() {
    let original = r#"
const a = memo(function (props) {
  if (props.hidden) return null;
  return jsx(View, { children: jsxs(Box, { children: [jsx(Button, { tooltip: "Open editor" }), jsx(Editor, { "data-role": "document-editor", children: props.content })] }) });
});
const b = memo(function (props) {
  if (props.hidden) return null;
  return jsx(View, { children: jsxs(Box, { children: [jsx(Button, { tooltip: "Open preview" }), jsx(Editor, { "data-role": "document-preview", children: props.content })] }) });
});
"#;
    let rebuilt = original
        .replace("const a =", "const c =")
        .replace("const b =", "const d =")
        .replace("Open editor", "Edit item");
    assert_matches_rebuilt_source(original, &rebuilt, "c");
}

#[test]
fn uses_array_run_holes_around_distinctive_element() {
    let original = r#"
function a(flag) {
  return flag ? [] : [sectionOne, sectionTwo, { description: "Review drafts", handler: openDrafts }, sectionThree, sectionFour];
}
function b(flag) {
  return flag ? [] : [sectionOne, sectionTwo, { description: "Review archives", handler: openArchives }, sectionThree, sectionFour];
}
"#;
    let rebuilt = original
        .replace("function a(", "function c(")
        .replace("function b(", "function d(")
        .replace(
            "[sectionOne, sectionTwo, { description: \"Review drafts\"",
            "[extraBefore, sectionOne, sectionTwo, { description: \"Review drafts\"",
        )
        .replace(
            "handler: openDrafts }, sectionThree, sectionFour]",
            "handler: openDrafts }, sectionThree, sectionFour, extraAfter]",
        );
    assert_matches_rebuilt_source(original, &rebuilt, "c");
}

#[test]
fn keeps_enum_label_while_holing_numeric_values() {
    let original = r#"
var a = ((x) => (x[x.Open = 7] = "Open", x[x.Close = 9] = "Close", x[x.Share = 13] = "Share", x))(a || {});
var b = ((x) => (x[x.Open = 2] = "Open", x[x.Close = 5] = "Close", x[x.Delete = 11] = "Delete", x))(b || {});
"#;
    let rebuilt = r#"
var c = ((y) => (y[y.Open = 17] = "Open", y[y.Close = 19] = "Close", y[y.Share = 23] = "Share", y))(c || {});
var d = ((y) => (y[y.Open = 2] = "Open", y[y.Close = 5] = "Close", y[y.Delete = 11] = "Delete", y))(d || {});
"#;
    assert_matches_rebuilt_source(original, rebuilt, "c");
}

#[test]
fn follows_registration_use_instead_of_a_neighbor() {
    let original = r#"
function a(x) { return x.trim(); }
function b(x) { return x.trim(); }
register("primary", a);
register("secondary", b);
"#;
    let rebuilt = r#"
register("secondary", d);
function d(x) { return x.trim(); }
register("primary", c);
function c(x) { return x.trim(); }
"#;
    assert_matches_rebuilt_source(original, rebuilt, "c");
}

#[test]
fn anchors_a_deep_value_without_pinning_its_wrapper_path() {
    let original = r#"
const a = memo(function () { return wrap1(wrap2(wrap3(wrap4(wrap5(wrap6(wrap7("Open details"))))))); });
const b = memo(function () { return wrap1(wrap2(wrap3(wrap4(wrap5(wrap6(wrap7("Open summary"))))))); });
"#;
    let rebuilt = r#"
const c = memo(function () { return different(wrap7("Open details")); });
const d = memo(function () { return different(wrap7("Open summary")); });
"#;
    let dir = tempfile::tempdir().unwrap();
    let selector = synthesized_match(dir.path(), original);
    assert!(selector.contains("AST_CONTAINS_STRING"), "{selector}");
    let rebuilt_file = dir.path().join("rebuilt.js");
    write_text_file(&rebuilt_file, rebuilt);
    let result = run_match_selector(
        &rebuilt_file,
        &selector,
        &["--target-binding", "Selected", "--no-slack"],
    );
    assert_eq!(result["outcomes"][0]["outcome"]["binding"], "c", "{result}");
}
