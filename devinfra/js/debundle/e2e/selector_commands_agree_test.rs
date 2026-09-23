//! Every command that resolves a selector gives the same answer for the same
//! selector and chunk: `debundle run`, `spec validate --source-file` and
//! `spec match-selector`. Each case also pins an `alpha_all` scoping rule the
//! commands must share: same-spelled locals in sibling blocks, loop heads,
//! `switch` bodies and named function expressions are independent bindings,
//! while `var` hoists to the enclosing function.

use std::fs;
use std::path::Path;

use debundle_e2e_support::{
    FixtureOpts, Member, assert_module_exports, logical_module, run_fixture, run_match_selector,
    run_source_only_validate,
};
use serde_json::Value;

struct Case {
    /// Chunk source; declares and exports the target.
    chunk: &'static str,
    /// The `source_match` template.
    selector: &'static str,
    /// Template-local name of the target binding.
    local: &'static str,
    /// Minified name the target has in the chunk.
    subject: &'static str,
}

const SIBLING_BLOCKS: Case = Case {
    chunk: r#"function actual(input) {
  const out = [];
  { const { value: a } = input.left; out.push(a); }
  { const { value: b } = input.right; out.push(b); }
  return out.join("|");
}
console.log(actual({ left: { value: "L" }, right: { value: "R" } }));
export { actual };
"#,
    selector: r#"function readable(input) {
  const out = [];
  { const { value } = input.left; out.push(value); }
  { const { value } = input.right; out.push(value); }
  return out.join("|");
}"#,
    local: "readable",
    subject: "actual",
};

const SIBLING_LOOPS: Case = Case {
    chunk: r#"function actual(rows) {
  let total = 0;
  for (let a = 0; a < rows.length; a++) total += rows[a];
  for (let b = rows.length - 1; b >= 0; b--) total -= rows[b] * 2;
  return total;
}
console.log(actual([1, 2, 3]));
export { actual };
"#,
    selector: r#"function readable(rows) {
  let total = 0;
  for (let index = 0; index < rows.length; index++) total += rows[index];
  for (let index = rows.length - 1; index >= 0; index--) total -= rows[index] * 2;
  return total;
}"#,
    local: "readable",
    subject: "actual",
};

const SWITCH_SCOPE: Case = Case {
    chunk: r#"function actual(input) {
  const a = "outer";
  switch (input.kind) {
    case "left":
      const b = input.left;
      return b;
  }
  return a;
}
console.log(actual({ kind: "left", left: "L" }), actual({ kind: "right", left: "R" }));
export { actual };
"#,
    selector: r#"function readable(input) {
  const value = "outer";
  switch (input.kind) {
    case "left":
      const value = input.left;
      return value;
  }
  return value;
}"#,
    local: "readable",
    subject: "actual",
};

const NAMED_FUNCTION_EXPRESSION: Case = Case {
    chunk: r#"const a = () => "outer";
const b = function c(n) {
  return n <= 0 ? "inner" : c(n - 1);
};
console.log(a(), b(1));
export { a, b };
"#,
    selector: r#"const outer = () => "outer";
const wrapper = function outer(n) {
  return n <= 0 ? "inner" : outer(n - 1);
};"#,
    local: "wrapper",
    subject: "b",
};

const VAR_HOISTED_OUT_OF_BLOCK: Case = Case {
    chunk: r#"function actual(n) {
  if (n) { var a = n * 2; }
  return a;
}
console.log(actual(2));
export { actual };
"#,
    selector: r#"function readable(n) {
  if (n) { var doubled = n * 2; }
  return doubled;
}"#,
    local: "readable",
    subject: "actual",
};

fn write(path: &Path, body: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, body).unwrap();
}

fn source_matches_yaml(case: &Case, export_name: &str) -> String {
    let indented = case
        .selector
        .lines()
        .map(|line| format!("      {line}"))
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "source_matches:\n  - match: |\n{indented}\n    bindings:\n      - local: {}\n        name: {export_name}\n",
        case.local
    )
}

/// `spec validate --source-file` diagnostics for the case's selector.
fn validate(case: &Case) -> Value {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("chunk.js");
    write(&source, case.chunk);
    let modules = dir.path().join("modules");
    write(
        &modules.join("format.yaml"),
        &source_matches_yaml(case, "target"),
    );
    let out = run_source_only_validate(&modules, &source, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    serde_json::from_str(&out.stdout).unwrap()
}

/// `spec match-selector` matches for the case's selector.
fn match_selector(case: &Case) -> Value {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("chunk.js");
    write(&source, case.chunk);
    run_match_selector(
        &source,
        case.selector,
        &["--target-binding", case.local, "--no-slack"],
    )
}

fn assert_all_commands_resolve(case: &Case) {
    let validated = validate(case);
    assert_eq!(
        validated["total"], 0,
        "spec validate --source-file: {validated:#}"
    );

    let matched = match_selector(case);
    assert_eq!(matched["unique"], true, "spec match-selector: {matched:#}");
    assert_eq!(
        matched["matches"][0]["binding_name"], case.subject,
        "{matched:#}"
    );

    let fixture = run_fixture(FixtureOpts::new(
        case.chunk,
        vec![logical_module(
            "format",
            &[Member::source_alpha_target(
                "target",
                case.local,
                case.selector,
            )],
        )],
    ));
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/format.js",
        &["target"],
        &[],
    );
}

#[test]
fn sibling_block_consts_are_independent() {
    assert_all_commands_resolve(&SIBLING_BLOCKS);
}

#[test]
fn sibling_loop_heads_are_independent() {
    assert_all_commands_resolve(&SIBLING_LOOPS);
}

#[test]
fn switch_body_is_its_own_scope() {
    assert_all_commands_resolve(&SWITCH_SCOPE);
}

#[test]
fn named_function_expression_name_is_local() {
    assert_all_commands_resolve(&NAMED_FUNCTION_EXPRESSION);
}

#[test]
fn var_hoists_out_of_its_block() {
    assert_all_commands_resolve(&VAR_HOISTED_OUT_OF_BLOCK);
}

/// Hoisting is a constraint, not only a permission: the `var` bound inside the
/// block is the one returned after it, so a chunk returning something else
/// matches in no command.
#[test]
fn hoisted_var_must_stay_consistent() {
    let case = Case {
        chunk: r#"function actual(n) {
  if (n) { var a = n * 2; }
  return other;
}
export { actual };
"#,
        ..VAR_HOISTED_OUT_OF_BLOCK
    };
    let validated = validate(&case);
    assert_eq!(
        validated["counts"]["unresolved_selector"], 1,
        "{validated:#}"
    );
    let matched = match_selector(&case);
    assert_eq!(matched["unique"], false, "{matched:#}");
    assert!(
        matched["matches"].as_array().unwrap().is_empty(),
        "{matched:#}"
    );
}
