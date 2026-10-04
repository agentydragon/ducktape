//! Every command that resolves a selector gives the same answer for the same
//! selector and chunk, because they all call one resolve: `debundle run
//! --dry-run`, `spec validate` (`--spec` and `--source-file`), `spec
//! match-selector`, the `synthesize-selectors` proof and the graph-backed
//! commands (`describe` and the edit gate). They emit equal outcome records
//! for it — `no_match`, `ambiguous` with the same candidates, `too_broad`,
//! resolved by elimination — or all resolve it. `match-selector` resolves its
//! probe alone, so a selector unique only by elimination is ambiguous there;
//! the graph-backed commands resolve only `source_matches[]` and anonymous
//! statements, so one unique only by a relational member's claim is ambiguous
//! to them.
//!
//! Routing is checked once per outcome; other matching rules use a real
//! pipeline run and Node output instead of repeating the command cross-product:
//! same-spelled locals in sibling blocks, loop heads, `switch` bodies, named
//! function/class expressions and shadowing arrow params are independent
//! bindings; `var` hoists to the enclosing function out of blocks, `catch` and
//! `switch`; property names stay exact; and a `const ANYTHING = <init>`
//! declarator matches its initializer wherever it sits in its statement.

use std::fs;

use debundle_e2e_support::{
    BindingGroup, Fixture, FixtureOpts, GraphFixture, Member, assert_entry_output,
    assert_generated_module_after_entry_script, assert_module_source, logical_module,
    logical_module_with_binding_groups, outcomes, parse_stdout_json, read_selector_outcomes,
    run_dry_run_fixture, run_dry_run_rejection_fixture, run_fixture, run_match_selector,
    run_source_only_validate, run_spec_validate, run_synthesize_selectors, write_text_file,
    write_validate_fixture_spec,
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

/// `spec validate --modules --source-file`: the case's record, if listed.
fn source_only_validate(case: &Case) -> Option<Value> {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("chunk.js");
    write_text_file(&source, case.chunk);
    let modules = dir.path().join("modules");
    write_text_file(
        &modules.join(format!("{MODULE}.yaml")),
        &source_matches_yaml(case),
    );
    let out = run_source_only_validate(&modules, &source, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    export_record(outcomes(&report), EXPORT)
}

/// `spec validate --spec`, the keep-going pass: the case's record, if listed.
fn spec_validate(case: &Case) -> Option<Value> {
    let fixture = write_validate_fixture_spec(fixture(case));
    let out = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    export_record(outcomes(&report), EXPORT)
}

/// `spec match-selector`: the probe's one record.
fn match_selector(case: &Case) -> Value {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("chunk.js");
    write_text_file(&source, case.chunk);
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

fn assert_all_commands_resolve(case: &Case, expected_output: &str) -> Fixture {
    assert_eq!(
        source_only_validate(case),
        None,
        "spec validate --source-file"
    );
    assert_eq!(spec_validate(case), None, "spec validate --spec");

    let matched = match_selector(case);
    assert_eq!(matched["outcome"]["kind"], "resolved", "{matched:#}");
    assert_eq!(matched["outcome"]["binding"], case.subject, "{matched:#}");

    assert_selector_runs(case, expected_output)
}

fn assert_selector_runs(case: &Case, expected_output: &str) -> Fixture {
    let fixture = run_fixture(fixture(case));
    assert_entry_output(&fixture, expected_output);
    // Runtime identity pins which binding was selected, not merely that the
    // whole entry still runs with an arbitrary export renamed to `target`.
    assert_generated_module_after_entry_script(
        &fixture,
        &format!(
            "const entry = await import('./static/app/entry.js');\n\
             const selected = await import('./static/app/modules/{MODULE}.js');\n\
             console.log(typeof selected.{EXPORT} === 'function' && selected.{EXPORT} === entry[{}]);",
            serde_json::to_string(case.subject).unwrap(),
        ),
        "true\n",
    );
    fixture
}

fn rejected_outcome(case: &Case) -> Value {
    let rejected = run_dry_run_rejection_fixture(fixture(case));
    let run = export_record(&read_selector_outcomes(&rejected.report_root), EXPORT)
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

    run
}

/// Every command reports the same outcome record for the case's target,
/// modulo the fields it legitimately lacks: the chunk is a path for the
/// source-only commands, and `match-selector`'s probe has no spec placement.
/// Returns the agreed outcome.
fn assert_all_commands_agree(case: &Case) -> Value {
    let run = rejected_outcome(case);
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
    let fixture = assert_all_commands_resolve(&SIBLING_BLOCKS, "L|R\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/format.js",
        &["function target", "out.push(a)", "out.push(b)"],
        &["function actual", "function readable"],
    );
}

#[test]
fn sibling_loop_heads_are_independent() {
    assert_selector_runs(&SIBLING_LOOPS, "-6\n");
}

#[test]
fn switch_body_is_its_own_scope() {
    let fixture = assert_selector_runs(&SWITCH_SCOPE, "L outer\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/format.js",
        &[
            "function target",
            r#"const a = "outer""#,
            "const b = input.left",
        ],
        &["function actual", "function readable"],
    );
}

#[test]
fn named_function_expression_name_is_local() {
    let fixture = assert_selector_runs(&NAMED_FUNCTION_EXPRESSION, "outer inner\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/format.js",
        &["const target", "function c", "c(n - 1)"],
        &["function outer"],
    );
}

#[test]
fn var_hoists_out_of_its_block() {
    assert_selector_runs(&VAR_HOISTED_OUT_OF_BLOCK, "4\n");
}

/// Hoisting is a constraint, not only a permission: the `var` bound inside the
/// block is the one returned after it, so a chunk returning something else
/// must not match.
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
    assert_no_match_nearest_actual(&rejected_outcome(&case)["outcome"]);
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
    assert_selector_runs(&VAR_HOISTED_OUT_OF_CATCH, "boom\n");
}

#[test]
fn var_hoists_out_of_switch() {
    assert_selector_runs(&VAR_HOISTED_OUT_OF_SWITCH, "1 2\n");
}

#[test]
fn named_class_expression_name_is_local() {
    assert_selector_runs(&NAMED_CLASS_EXPRESSION, "outer true\n");
}

#[test]
fn arrow_param_shadows_outer_binding() {
    assert_selector_runs(&ARROW_PARAM_SHADOWS_OUTER, "1,0\n");
}

/// Alpha renaming covers identifiers, never property names: `.id` in the
/// template does not match `.key` in the chunk, in any command.
/// A near miss names the one unclaimed declaration, `actual`, as closest.
fn assert_no_match_nearest_actual(outcome: &Value) {
    assert_eq!(outcome["kind"], "no_match", "{outcome:#}");
    assert_eq!(
        outcome["nearest_unclaimed"][0]["bindings"],
        json!(["actual"]),
        "{outcome:#}"
    );
}

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
    assert_no_match_nearest_actual(&assert_all_commands_agree(&case));
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
            // Alike in themselves, each is set apart by its neighbor: only
            // `a` is followed, and only `b` preceded, by a `"shared"` function.
            "differentiators": [
                {"owner": 0, "statement": 1, "anchor": "string literal \"shared\""},
                {"owner": 1, "statement": 0, "anchor": "string literal \"shared\""},
            ],
        })
    );
}

/// One place past the candidate cap is `too_broad` in every command.
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
    assert_selector_runs(&ANYTHING_DECLARATOR_KEEPS_INITIALIZER, "x 2\n");
}

#[test]
fn anything_declarator_floats_among_others() {
    assert_selector_runs(&ANYTHING_DECLARATOR_AMONG_OTHERS, "x1 y2\n");
}

/// `Either` matches `first` and `second`; `Other` matches only `second`, so
/// `Either` is unique only because `Other` claimed its alternative.
const ELIMINATION_CHUNK: &str = r#"function first() {
  return "shared";
}
function second() {
  return "other";
}
function third(value) {
  return value.trim();
}
console.log(first(), second(), third(" ok "));
"#;
const EITHER: &str = "function f() {\n  return EXPR;\n}";
const OTHER: &str = "function g() {\n  return \"other\";\n}";

fn elimination_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        ELIMINATION_CHUNK,
        vec![
            logical_module(
                "elimination/either",
                &[Member::source_alpha_target("Either", "f", EITHER)],
            ),
            logical_module(
                "elimination/other",
                &[Member::source_alpha_target("Other", "g", OTHER)],
            ),
            logical_module("elimination/third", &[Member::renamed("Third", "third")]),
        ],
    )
}

fn elimination_tree() -> GraphFixture {
    let mut modules = Vec::new();
    for (path, name, local, selector) in [
        ("elimination/either.yaml", "Either", "f", EITHER),
        ("elimination/other.yaml", "Other", "g", OTHER),
    ] {
        modules.push((path, format!(
            "source_matches: [{{match: {selector:?}, bindings: [{{local: {local}, name: {name}}}]}}]"
        )));
    }
    modules.push((
        "elimination/third.yaml",
        "members: [{name: Third, selector: {binding: {name: third}}}]".to_string(),
    ));
    let modules: Vec<_> = modules
        .iter()
        .map(|(path, yaml)| (*path, yaml.as_str()))
        .collect();
    GraphFixture::new(ELIMINATION_CHUNK, &modules)
}

/// Every command that resolves a spec resolves `Either` jointly, by
/// elimination, with the same warning record; `match-selector`, which asks
/// about one selector on its own, finds it ambiguous. The graph-backed
/// commands (`describe` through `peel`, and the edit gate) claim `first` for
/// it the way `run` does.
#[test]
fn resolution_by_elimination_is_shared_by_every_spec_command() {
    let fixture = run_dry_run_fixture(elimination_fixture());
    let run = export_record(&read_selector_outcomes(&fixture.report_root), "Either")
        .unwrap_or_else(|| panic!("run lists no outcome for Either:\n{}", fixture.stderr));
    assert_eq!(
        run["outcome"],
        json!({
            "kind": "resolved",
            "owner": 0,
            "binding": "first",
            "resolved_by": {
                "by": "elimination",
                "claimers": [{"logical_module": "elimination/other", "entity": {"export": "Other"}}],
            },
        }),
        "{run:#}"
    );

    let spec = write_validate_fixture_spec(elimination_fixture());
    let out = run_spec_validate(&spec.spec_path, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    assert_eq!(
        outcomes(&report),
        std::slice::from_ref(&run),
        "spec validate --spec"
    );

    let tree = elimination_tree();
    let source = tree.source_path();
    let modules = &tree.modules;
    let out = run_source_only_validate(modules, &source, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    let [source_only] = outcomes(&report) else {
        panic!("spec validate --source-file lists one outcome: {report:#}");
    };
    assert_eq!(
        without(source_only, &["chunk"]),
        without(&run, &["chunk"]),
        "spec validate --source-file"
    );

    let probe = run_match_selector(&source, EITHER, &["--target-binding", "f", "--no-slack"]);
    let [probe] = outcomes(&probe) else {
        panic!("match-selector reports one outcome: {probe:#}");
    };
    assert_eq!(
        probe["outcome"],
        json!({
            "kind": "ambiguous",
            "candidates": [{"owner": 0, "binding": "first"}, {"owner": 1, "binding": "second"}],
            "truncated": false,
            "differentiators": [
                {"owner": 0, "statement": 0, "anchor": "string literal \"shared\""},
                {"owner": 1, "statement": 1, "anchor": "string literal \"other\""},
            ],
        }),
        "spec match-selector"
    );

    let describe = tree.command(&["describe", "elimination/either", "--format", "json"]);
    assert!(describe.status.success(), "{describe:?}");
    let describe: Value = serde_json::from_slice(&describe.stdout).unwrap();
    assert_eq!(describe["owner_ids"], json!(["owner:0"]), "describe");

    // The edit gate resolves every module's claims before accepting an edit.
    let unassign = tree.command(&["bindings", "unassign", "third"]);
    let stderr = String::from_utf8_lossy(&unassign.stderr);
    assert!(unassign.status.success(), "edit gate: {stderr}");
    assert!(
        stderr.contains("resolved by elimination"),
        "edit gate: {stderr}"
    );
    tree.assert_runs("shared other ok\n");
}

/// `Either` matches `first` and `second`; the relational `Other` pins
/// `second` as the function reading `.other`, so `Either` is unique only
/// because a relational claim took its alternative.
const RELATIONAL_ELIMINATION_CHUNK: &str = r#"const marker = { other: "o" };
function first() {
  return "shared";
}
function second() {
  return marker.other;
}
console.log(first(), second());
"#;

/// `run` and `spec validate --source-file` resolve `Either` by elimination
/// against the relational `Other`. The graph-backed commands resolve only
/// `source_matches[]` and anonymous statements, so to them `Either` is
/// ambiguous.
#[test]
fn relational_claims_eliminate_only_in_spec_wide_commands() {
    let fixture = run_dry_run_fixture(FixtureOpts::new(
        RELATIONAL_ELIMINATION_CHUNK,
        vec![
            logical_module(
                "elimination/either",
                &[Member::source_alpha_target("Either", "f", EITHER)],
            ),
            logical_module(
                "elimination/other",
                &[Member::reads_member(
                    "Other",
                    "other",
                    None,
                    Some("function_declaration"),
                )],
            ),
            logical_module("elimination/marker", &[Member::renamed("Marker", "marker")]),
        ],
    ));
    let run = export_record(&read_selector_outcomes(&fixture.report_root), "Either")
        .unwrap_or_else(|| panic!("run lists no outcome for Either:\n{}", fixture.stderr));
    assert_eq!(
        run["outcome"],
        json!({
            "kind": "resolved",
            "owner": 1,
            "binding": "first",
            "resolved_by": {
                "by": "elimination",
                "claimers": [{"logical_module": "elimination/other", "entity": {"export": "Other"}}],
            },
        }),
        "{run:#}"
    );

    let either =
        format!("source_matches: [{{match: {EITHER:?}, bindings: [{{local: f, name: Either}}]}}]");
    let tree = GraphFixture::new(
        RELATIONAL_ELIMINATION_CHUNK,
        &[
            ("elimination/either.yaml", &either),
            (
                "elimination/other.yaml",
                "members: [{name: Other, selector: {reads_member: {member: other, kind: function_declaration}}}]",
            ),
            (
                "elimination/marker.yaml",
                "members: [{name: Marker, selector: {binding: {name: marker}}}]",
            ),
        ],
    );
    let source = tree.source_path();
    let modules = &tree.modules;
    let out = run_source_only_validate(modules, &source, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    let [source_only] = outcomes(&report) else {
        panic!("spec validate --source-file lists one outcome: {report:#}");
    };
    assert_eq!(
        without(source_only, &["chunk"]),
        without(&run, &["chunk"]),
        "spec validate --source-file"
    );

    for args in [
        &["describe", "elimination/either"][..],
        &["bindings", "unassign", "marker"][..],
    ] {
        let out = tree.command(args);
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(!out.status.success(), "{args:?}: {stderr}");
        assert!(
            stderr.contains("source_matches[] is ambiguous"),
            "{args:?}: {stderr}"
        );
    }
    tree.assert_runs("shared o\n");
}

/// The record of the export `export_name` in `outcomes`, if one is listed.
fn export_record(outcomes: &[Value], export_name: &str) -> Option<Value> {
    let mut records = outcomes
        .iter()
        .filter(|record| record["placement"]["entity"]["export"] == export_name);
    let record = records.next().cloned();
    assert!(records.next().is_none(), "{outcomes:#?}");
    record
}

/// `synthesize-selectors` proves a selector with the resolve every command
/// uses: what it writes resolves on its own selector in `match-selector` and
/// is clean in `spec validate --source-file`.
#[test]
fn synthesized_selector_resolves_in_every_command() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("chunk.js");
    write_text_file(
        &source,
        r#"function actual(input) {
  return input.trim() + "-suffix";
}
function other(input) {
  return input.trim();
}
export { actual, other };
"#,
    );
    let modules = dir.path().join("modules");
    let module_file = modules.join(format!("{MODULE}.yaml"));
    write_text_file(
        &module_file,
        &format!("members:\n  - name: {EXPORT}\n    selector: {{ binding: {{ name: actual }} }}\n"),
    );
    let out = run_synthesize_selectors(
        &modules,
        &[
            "--source-file",
            source.to_str().unwrap(),
            "--item",
            &format!("{MODULE}:{EXPORT}"),
            "--apply",
            "--format",
            "json",
        ],
    );
    let report = parse_stdout_json(&out);
    assert_eq!(report["summary"]["changed_candidates"], 1, "{report:#}");

    let module: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&module_file).unwrap()).unwrap();
    let claim = &module["source_matches"][0];
    let selector = claim["match"].as_str().expect("a synthesized match");
    // A binding whose local is its name is written as the bare name.
    let binding = &claim["bindings"][0];
    let local = binding["local"]
        .as_str()
        .or(binding.as_str())
        .expect("a local");

    let probe = run_match_selector(
        &source,
        selector,
        &["--target-binding", local, "--no-slack"],
    );
    let [probe] = outcomes(&probe) else {
        panic!("match-selector reports one outcome: {probe:#}");
    };
    assert_eq!(
        probe["outcome"],
        json!({"kind": "resolved", "owner": 0, "binding": "actual", "resolved_by": {"by": "own_selector"}}),
        "{selector}"
    );

    let out = run_source_only_validate(&modules, &source, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    assert!(outcomes(&report).is_empty(), "{report:#}");
}

/// An import specifier declares no top-level owner, so a template matching
/// one is invalid in every command rather than claiming the import.
#[test]
fn import_specifier_match_is_invalid() {
    let case = Case {
        chunk: "import { dep } from \"external-pkg\";\nconsole.log(dep);\n",
        selector: "import { dep } from \"external-pkg\";",
        local: "dep",
        subject: "dep",
    };
    assert_eq!(
        assert_all_commands_agree(&case),
        json!({
            "kind": "invalid",
            "error": "projection_owner_mapping_error: source_match candidate at body index 0 \
                      binding `dep` does not map to an owner-graph node",
        })
    );
}

/// Each binding of a multi-binding `source_matches` entry is its own entity:
/// both matches of the template share `key`'s declaration, so `anchorKey`
/// resolves while `reader` is ambiguous between the two functions.
#[test]
fn multi_binding_template_decides_each_binding() {
    const CHUNK: &str = r#"const k = "anchor-key";
function a() { return k + 1; }
function b() { return k + 1; }
console.log(a(), b());
export { a, b };
"#;
    const TEMPLATE: &str = r#"const key = "anchor-key";
STMT_LIST;
function f() { return key + 1; }"#;
    let reader_ambiguous = json!({
        "kind": "ambiguous",
        "candidates": [{"owner": 1, "binding": "a"}, {"owner": 2, "binding": "b"}],
        "truncated": false,
        "differentiators": [
            {"owner": 1, "statement": 0, "anchor": "string literal \"anchor-key\""},
            {"owner": 2, "statement": 1, "anchor": "number literal 1"},
        ],
    });

    let rejected = run_dry_run_rejection_fixture(FixtureOpts::new(
        CHUNK,
        vec![logical_module_with_binding_groups(
            MODULE,
            &[],
            &[BindingGroup::source_alpha(
                TEMPLATE,
                &[("key", "anchorKey"), ("f", "reader")],
            )],
        )],
    ));
    let run = read_selector_outcomes(&rejected.report_root);
    assert_eq!(export_record(&run, "anchorKey"), None, "{run:#?}");
    assert_eq!(
        export_record(&run, "reader").expect("reader record")["outcome"],
        reader_ambiguous
    );

    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("chunk.js");
    write_text_file(&source, CHUNK);
    let modules = dir.path().join("modules");
    let indented = TEMPLATE
        .lines()
        .map(|line| format!("      {line}"))
        .collect::<Vec<_>>()
        .join("\n");
    write_text_file(
        &modules.join(format!("{MODULE}.yaml")),
        &format!(
            "source_matches:\n  - match: |\n{indented}\n    bindings:\n      - local: key\n        name: anchorKey\n      - local: f\n        name: reader\n"
        ),
    );
    let out = run_source_only_validate(&modules, &source, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    assert_eq!(
        export_record(outcomes(&report), "anchorKey"),
        None,
        "{report:#}"
    );
    assert_eq!(
        export_record(outcomes(&report), "reader").expect("reader record")["outcome"],
        reader_ambiguous
    );
}

#[test]
fn candidate_limit_counts_places_not_wildcard_alignments() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("chunk.js");
    let calls: String = (0..101).map(|i| format!("f{i}();\n")).collect();
    let chunk = format!("const a = 1; {calls} const b = 2;");
    write_text_file(&source, &chunk);
    // A binding group has one placement tuple but 101 free-name bindings.
    // Two claimed bindings select the module-level sequence matcher; a single
    // claimed binding uses contiguous windows, not top-level STMT_LIST gaps.
    let modules = dir.path().join("modules");
    write_text_file(
        &modules.join("selected.yaml"),
        "source_matches:\n  - match: |\n      const first = 1; STMT_LIST; anchor(); STMT_LIST; const second = 2;\n    bindings: [first, second]\n",
    );
    let output = run_source_only_validate(&modules, &source, &["--format", "json"]);
    assert!(
        output.status.success(),
        "{}\n{}",
        output.stdout,
        output.stderr
    );
    let report: Value = serde_json::from_str(&output.stdout).unwrap();
    assert!(outcomes(&report).is_empty(), "{report:#}");
}

#[test]
fn selector_commands_share_source_path_selection_without_changing_env_support() {
    use debundle_e2e_support::debundler_path;
    use std::process::Command;

    let root = tempfile::tempdir().unwrap();
    write_text_file(
        &root.path().join("chunk.js"),
        "const a = 1; console.log(a);",
    );
    write_text_file(
        &root.path().join("modules/chosen.yaml"),
        "members: [{name: A, selector: {binding: {name: a}}}]",
    );
    let absolute_source = root.path().join("chunk.js");
    for (verb, extras, optional_source, source_env) in [
        ("validate", vec!["--modules", "modules"], false, false),
        ("selector-debt", vec!["--modules", "modules"], true, true),
        (
            "synthesize-selectors",
            vec!["--modules", "modules", "--item", "chosen:A"],
            false,
            false,
        ),
        (
            "match-selector",
            vec![
                "--match",
                "const value = 1;",
                "--target-binding",
                "value",
                "--no-slack",
            ],
            false,
            true,
        ),
    ] {
        let run = |args: &[&str], env: bool| {
            let mut command = Command::new(debundler_path());
            command
                .current_dir(root.path())
                .args(["spec", verb])
                .args(&extras)
                .args(args)
                .env_remove("DEBUNDLE_SOURCE_ROOT");
            if env {
                command.env("DEBUNDLE_SOURCE_ROOT", root.path());
            }
            command.output().unwrap()
        };
        for args in [
            vec!["--source-file", "chunk.js"],
            vec!["--source-root", ".", "--chunk", "chunk.js"],
            vec!["--source-file", "chunk.js", "--source-root", "unused-root"],
            vec!["--source-file", absolute_source.to_str().unwrap()],
        ] {
            let out = run(&args, false);
            assert!(
                out.status.success(),
                "{verb} {args:?}: {}",
                String::from_utf8_lossy(&out.stderr)
            );
        }
        if source_env {
            let out = run(&["--chunk", "chunk.js"], true);
            assert!(
                out.status.success(),
                "{verb}: {}",
                String::from_utf8_lossy(&out.stderr)
            );
        }
        let out = run(&[], false);
        assert_eq!(
            out.status.success(),
            optional_source,
            "{verb}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        for (args, diagnostic) in [
            (
                vec!["--source-file", "chunk.js", "--chunk", "chunk.js"],
                "not both",
            ),
            (
                vec!["--chunk", "chunk.js"],
                "--chunk requires --source-root",
            ),
        ] {
            let out = run(&args, false);
            assert!(!out.status.success(), "{verb} {args:?}");
            assert!(
                String::from_utf8_lossy(&out.stderr).contains(diagnostic),
                "{verb}: {}",
                String::from_utf8_lossy(&out.stderr)
            );
        }
    }
}

#[test]
fn selector_debt_ndjson_preserves_json_payloads_and_section_order() {
    use debundle_e2e_support::run_debundle;

    let root = tempfile::tempdir().unwrap();
    for name in ["a", "b"] {
        write_text_file(
            &root.path().join(format!("ui/{name}.yaml")),
            &format!(
                "members: [{{selector: {{binding: {{name: {name}}}}}}}]\nsource_matches: [{{match: 'const value = 1;', bindings: [value]}}]"
            ),
        );
    }
    let run = |format| {
        let out = run_debundle(&[
            "spec",
            "selector-debt",
            "--modules",
            root.path().to_str().unwrap(),
            "--group-module-depth",
            "1",
            "--format",
            format,
        ]);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        out.stdout
    };
    let report: Value = serde_json::from_slice(&run("json")).unwrap();
    let rows: Vec<Value> = String::from_utf8(run("ndjson"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let mut expected = Vec::new();
    for (field, section) in [
        ("name_only", "name_only"),
        ("repeated_source_match", "repeated_source_match"),
        ("name_only_module_groups", "name_only_module_group"),
        ("drifted_bindings", "drifted_binding"),
        ("source_aware_near_ambiguous", "source_aware_near_ambiguous"),
        ("source_aware_repeated_exact", "source_aware_repeated_exact"),
        (
            "source_aware_binding_group_suggestions",
            "source_aware_binding_group_suggestion",
        ),
    ] {
        for mut row in report[field].as_array().unwrap().iter().cloned() {
            row["section"] = json!(section);
            expected.push(row);
        }
    }
    let mut summary = report["summary"].clone();
    summary["section"] = json!("summary");
    expected.push(summary);
    assert_eq!(rows, expected);
}
