//! Every command that resolves a selector gives the same answer for the same
//! selector and chunk: `debundle run --dry-run`, `spec validate` (`--spec` and
//! `--source-file`) and `spec match-selector` emit equal outcome records for
//! it — `no_match`, `ambiguous` with the same candidates, `too_broad` — or all
//! resolve it. Each resolving case also pins a matching rule the commands must
//! share: same-spelled locals in sibling blocks, loop heads, `switch` bodies,
//! named function/class expressions and shadowing arrow params are independent
//! bindings; `var` hoists to the enclosing function out of blocks, `catch` and
//! `switch`; property names stay exact; and a `const ANYTHING = <init>`
//! declarator matches its initializer wherever it sits in its statement.

use std::fs;
use std::path::Path;

use debundle_e2e_support::{
    FixtureOpts, Member, assert_module_exports, logical_module, read_selector_outcomes,
    run_dry_run_rejection_fixture, run_fixture, run_match_selector, run_source_only_validate,
    run_spec_validate, write_validate_fixture_spec,
};
use serde_json::{Value, json};

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

fn source_matches_yaml(case: &Case) -> String {
    let indented = case
        .selector
        .lines()
        .map(|line| format!("      {line}"))
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "source_matches:\n  - match: |\n{indented}\n    bindings:\n      - local: {}\n        name: {EXPORT}\n",
        case.local
    )
}

/// The readable name every case claims its target as, in module [`MODULE`].
const EXPORT: &str = "target";
const MODULE: &str = "format";

fn fixture(case: &Case) -> FixtureOpts<'_> {
    FixtureOpts::new(
        case.chunk,
        vec![logical_module(
            MODULE,
            &[Member::source_alpha_target(
                EXPORT,
                case.local,
                case.selector,
            )],
        )],
    )
}

/// The case's outcome record in `outcomes`, if one is listed.
fn target_record(outcomes: &[Value]) -> Option<Value> {
    let mut records = outcomes
        .iter()
        .filter(|record| record["placement"]["entity"]["export"] == EXPORT);
    let record = records.next().cloned();
    assert!(records.next().is_none(), "{outcomes:#?}");
    record
}

fn outcomes(report: &Value) -> &[Value] {
    report["outcomes"]
        .as_array()
        .unwrap_or_else(|| panic!("outcomes must be an array: {report:#}"))
}

/// `spec validate --modules --source-file`: the case's record, if listed.
fn source_only_validate(case: &Case) -> Option<Value> {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("chunk.js");
    write(&source, case.chunk);
    let modules = dir.path().join("modules");
    write(
        &modules.join(format!("{MODULE}.yaml")),
        &source_matches_yaml(case),
    );
    let out = run_source_only_validate(&modules, &source, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    target_record(outcomes(&report))
}

/// `spec validate --spec`, the keep-going pass: the case's record, if listed.
fn spec_validate(case: &Case) -> Option<Value> {
    let fixture = write_validate_fixture_spec(fixture(case));
    let out = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    target_record(outcomes(&report))
}

/// `spec match-selector`: the probe's one record.
fn match_selector(case: &Case) -> Value {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("chunk.js");
    write(&source, case.chunk);
    let report = run_match_selector(
        &source,
        case.selector,
        &["--target-binding", case.local, "--no-slack"],
    );
    let [record] = outcomes(&report) else {
        panic!("match-selector reports one outcome: {report:#}");
    };
    record.clone()
}

/// `record` without the fields a command legitimately lacks or spells its
/// own way.
fn without(record: &Value, fields: &[&str]) -> Value {
    let mut record = record.clone();
    let object = record.as_object_mut().unwrap();
    for field in fields {
        object.remove(*field);
    }
    record
}

fn assert_all_commands_resolve(case: &Case) {
    assert_eq!(
        source_only_validate(case),
        None,
        "spec validate --source-file"
    );
    assert_eq!(spec_validate(case), None, "spec validate --spec");

    let matched = match_selector(case);
    assert_eq!(matched["outcome"]["kind"], "resolved", "{matched:#}");
    assert_eq!(matched["outcome"]["binding"], case.subject, "{matched:#}");

    let fixture = run_fixture(fixture(case));
    assert_module_exports(
        &fixture.out_root,
        &format!("static/app/modules/{MODULE}.js"),
        &[EXPORT],
        &[],
    );
}

/// Every command reports the same outcome record for the case's target,
/// modulo the fields it legitimately lacks: the chunk is a path for the
/// source-only commands, and `match-selector`'s probe has no spec placement.
/// Returns the agreed outcome.
fn assert_all_commands_agree(case: &Case) -> Value {
    let rejected = run_dry_run_rejection_fixture(fixture(case));
    let run = target_record(&read_selector_outcomes(&rejected.report_root))
        .unwrap_or_else(|| panic!("run lists no outcome for the target:\n{}", rejected.stderr));
    assert_eq!(run["chunk"], "static/app", "{run:#}");
    assert_eq!(
        run["placement"],
        json!({
            "logical_module": MODULE,
            "entity": {"export": EXPORT},
            "selector_kind": "source_matches",
        }),
        "{run:#}"
    );

    assert_eq!(
        spec_validate(case).as_ref(),
        Some(&run),
        "spec validate --spec"
    );
    let source_only = source_only_validate(case).expect("spec validate --source-file record");
    assert_eq!(
        without(&source_only, &["chunk"]),
        without(&run, &["chunk"]),
        "spec validate --source-file"
    );
    assert_eq!(
        without(&match_selector(case), &["chunk"]),
        without(&run, &["chunk", "placement"]),
        "spec match-selector"
    );
    run["outcome"].clone()
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
    assert_eq!(
        assert_all_commands_agree(&case),
        json!({"kind": "no_match"})
    );
}

const VAR_HOISTED_OUT_OF_CATCH: Case = Case {
    chunk: r#"function actual(run) {
  try {
    run();
  } catch (e) {
    var a = String(e);
  }
  return a;
}
console.log(actual(() => { throw "boom"; }));
export { actual };
"#,
    selector: r#"function readable(run) {
  try {
    run();
  } catch (error) {
    var message = String(error);
  }
  return message;
}"#,
    local: "readable",
    subject: "actual",
};

const VAR_HOISTED_OUT_OF_SWITCH: Case = Case {
    chunk: r#"function actual(kind) {
  switch (kind) {
    case "a":
      var b = 1;
      break;
    default:
      b = 2;
  }
  return b;
}
console.log(actual("a"), actual("z"));
export { actual };
"#,
    selector: r#"function readable(kind) {
  switch (kind) {
    case "a":
      var value = 1;
      break;
    default:
      value = 2;
  }
  return value;
}"#,
    local: "readable",
    subject: "actual",
};

const NAMED_CLASS_EXPRESSION: Case = Case {
    chunk: r#"const a = () => "outer";
const b = class c {
  static create() {
    return new c();
  }
};
console.log(a(), b.create() instanceof b);
export { a, b };
"#,
    selector: r#"const outer = () => "outer";
const Widget = class outer {
  static create() {
    return new outer();
  }
};"#,
    local: "Widget",
    subject: "b",
};

const ARROW_PARAM_SHADOWS_OUTER: Case = Case {
    chunk: r#"function actual(n, l) {
  return l.map((t) => t.id).concat(n);
}
console.log(actual(0, [{ id: 1 }]).join(","));
export { actual };
"#,
    selector: r#"function readable(value, list) {
  return list.map((value) => value.id).concat(value);
}"#,
    local: "readable",
    subject: "actual",
};

#[test]
fn var_hoists_out_of_catch() {
    assert_all_commands_resolve(&VAR_HOISTED_OUT_OF_CATCH);
}

#[test]
fn var_hoists_out_of_switch() {
    assert_all_commands_resolve(&VAR_HOISTED_OUT_OF_SWITCH);
}

#[test]
fn named_class_expression_name_is_local() {
    assert_all_commands_resolve(&NAMED_CLASS_EXPRESSION);
}

#[test]
fn arrow_param_shadows_outer_binding() {
    assert_all_commands_resolve(&ARROW_PARAM_SHADOWS_OUTER);
}

/// Alpha renaming covers identifiers, never property names: `.id` in the
/// template does not match `.key` in the chunk, in any command.
#[test]
fn property_names_stay_exact_under_alpha() {
    let case = Case {
        chunk: r#"function actual(n, l) {
  return l.map((t) => t.key).concat(n);
}
export { actual };
"#,
        ..ARROW_PARAM_SHADOWS_OUTER
    };
    assert_eq!(
        assert_all_commands_agree(&case),
        json!({"kind": "no_match"})
    );
}

/// A selector matching two declarations names both, in every command.
#[test]
fn ambiguous_selector_lists_the_same_candidates() {
    let case = Case {
        chunk: r#"function a() {
  return "shared";
}
function b() {
  return "shared";
}
console.log(a(), b());
export { a, b };
"#,
        selector: "function f() {\n  return \"shared\";\n}",
        local: "f",
        subject: "a",
    };
    assert_eq!(
        assert_all_commands_agree(&case),
        json!({
            "kind": "ambiguous",
            "candidates": [{"owner": 0, "binding": "a"}, {"owner": 1, "binding": "b"}],
            "truncated": false,
        })
    );
}

/// One place past the candidate cap is `too_broad` in every command, the
/// source-only ones included, which never run the joint solve.
#[test]
fn selector_over_the_candidate_cap_is_too_broad() {
    let chunk = (0..101)
        .map(|index| format!("const a_{index} = f();\n"))
        .collect::<String>();
    let case = Case {
        chunk: chunk.leak(),
        selector: "const x = f();",
        local: "x",
        subject: "a_0",
    };
    assert_eq!(
        assert_all_commands_agree(&case),
        json!({"kind": "too_broad", "count": 101, "limit": 100})
    );
}

/// `const ANYTHING = <init>` holes the declarator's name only: the initializer is
/// the anchor. `c` also declares a `const` inside a single-parameter function
/// and must not match.
const ANYTHING_DECLARATOR_KEEPS_INITIALIZER: Case = Case {
    chunk: r#"function a(n) {
  if (!n) return;
  const e = n.distinctive.leaf?.name;
  return e;
}
function c(n) {
  const t = n * 2;
  return t;
}
console.log(a({ distinctive: { leaf: { name: "x" } } }), c(1));
export { a, c };
"#,
    selector: r#"function readable(ANYTHING) {
  STMT_LIST;
  const ANYTHING = ANYTHING.distinctive.leaf?.name;
  STMT_LIST;
}"#,
    local: "readable",
    subject: "a",
};

/// Minifiers merge consecutive declarations, so an `ANYTHING = <init>`
/// declarator floats: it matches one declarator anywhere in its statement.
const ANYTHING_DECLARATOR_AMONG_OTHERS: Case = Case {
    chunk: r#"function a(n) {
  const e = n.distinctive.leaf?.name, t = 1;
  return e + t;
}
function c(n) {
  const e = n.other, t = 2;
  return e + t;
}
console.log(a({ distinctive: { leaf: { name: "x" } } }), c({ other: "y" }));
export { a, c };
"#,
    selector: r#"function readable(ANYTHING) {
  const ANYTHING = ANYTHING.distinctive.leaf?.name;
  STMT_LIST;
}"#,
    local: "readable",
    subject: "a",
};

#[test]
fn anything_declarator_keeps_its_initializer() {
    assert_all_commands_resolve(&ANYTHING_DECLARATOR_KEEPS_INITIALIZER);
}

#[test]
fn anything_declarator_floats_among_others() {
    assert_all_commands_resolve(&ANYTHING_DECLARATOR_AMONG_OTHERS);
}
