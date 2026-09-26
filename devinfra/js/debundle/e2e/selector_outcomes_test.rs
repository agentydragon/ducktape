//! Selector outcomes decided around the joint solve: a selector the shape
//! matcher places too widely is `too_broad` without a solve, and one that is
//! unique only because other selectors claimed its alternatives is `resolved`
//! by elimination, with a warning.

use debundle_e2e_support::{
    FixtureOpts, Member, assert_fail_fast_stops_at_first_outcome, find_outcome, logical_module,
    logical_module_with_anon, logical_module_with_anon_alpha, read_selector_outcomes,
    run_dry_run_fixture, run_dry_run_rejection_fixture, run_spec_validate,
    write_validate_fixture_spec,
};
use serde_json::{Value, json};

/// 101 identical `const a_i = f();` declarations, one past the candidate cap,
/// beside one distinctive function.
fn too_broad_fixture_source() -> String {
    let mut source = (0..101)
        .map(|index| format!("const a_{index} = f();\n"))
        .collect::<String>();
    source.push_str(
        r#"function keepMe(value) {
  return value.trim();
}
console.log(keepMe(" ok "));
"#,
    );
    source
}

fn too_broad_fixture(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "broad/everything",
                &[Member::source_alpha("TooBroad", "const x = f();")],
            ),
            logical_module(
                "keep/me",
                &[Member::source_alpha(
                    "KeepMe",
                    "function k(value) {\n  return value.trim();\n}",
                )],
            ),
            // A name pin on `keepMe`: the duplicate claim it draws is the
            // witness that the other selector still resolved.
            logical_module("witness/keep", &[Member::renamed("KeepPin", "keepMe")]),
        ],
    )
}

#[test]
fn selector_over_candidate_cap_is_too_broad_and_others_still_resolve() {
    let source = too_broad_fixture_source();
    let rejected = run_dry_run_rejection_fixture(too_broad_fixture(&source));
    assert!(
        rejected
            .stderr
            .contains("matches 101 places (limit 100); anchor it more specifically"),
        "{}",
        rejected.stderr
    );
    let outcomes = read_selector_outcomes(&rejected.report_root);

    let broad = find_outcome(&outcomes, "too_broad", "TooBroad");
    assert_eq!(broad["severity"], "error", "{broad:#}");
    assert_eq!(
        broad["outcome"],
        json!({"kind": "too_broad", "count": 101, "limit": 100})
    );
    let duplicate = outcomes
        .iter()
        .find(|record| record["outcome"]["kind"] == "duplicate_claim")
        .unwrap_or_else(|| panic!("missing duplicate-claim witness: {outcomes:#?}"));
    assert_eq!(duplicate["outcome"]["binding"], "keepMe");
    assert_eq!(outcomes.len(), 2, "{outcomes:#?}");

    let validate = validate_json(too_broad_fixture(&source));
    assert_eq!(validate["counts"]["too_broad"], 1, "{validate:#}");
}

/// A too-broad selector is rejected before the solve that finds the
/// duplicate claim, so fail-fast stops at it.
#[test]
fn fail_fast_stops_at_too_broad_selector() {
    let source = too_broad_fixture_source();
    let line = assert_fail_fast_stops_at_first_outcome(|| too_broad_fixture(&source), "too_broad");
    assert!(line.contains("as `TooBroad`"), "{line}");
}

/// `Either` matches both functions; `Other` matches only `second`. `Either`
/// resolves to `first` solely because `Other` claimed its alternative.
fn elimination_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"function first() {
  return "shared";
}
function second() {
  return "other";
}
function third(value) {
  return value.trim();
}
console.log(first(), second(), third(" ok "));
"#,
        vec![
            logical_module(
                "elimination/either",
                &[Member::source_alpha(
                    "Either",
                    "function f() {\n  return EXPR;\n}",
                )],
            ),
            logical_module(
                "elimination/other",
                &[Member::source_alpha(
                    "Other",
                    "function g() {\n  return \"other\";\n}",
                )],
            ),
            logical_module(
                "elimination/third",
                &[Member::source_alpha(
                    "Third",
                    "function t(value) {\n  return value.trim();\n}",
                )],
            ),
        ],
    )
}

#[test]
fn selector_unique_only_by_elimination_warns_and_run_succeeds() {
    let fixture = run_dry_run_fixture(elimination_fixture());
    let outcomes = read_selector_outcomes(&fixture.report_root);

    let either = find_outcome(&outcomes, "resolved", "Either");
    assert_eq!(either["severity"], "warning", "{either:#}");
    assert_eq!(
        either["outcome"],
        json!({
            "kind": "resolved",
            "owner": 0,
            "binding": "first",
            "resolved_by": {
                "by": "elimination",
                "claimers": [{"logical_module": "elimination/other", "entity": {"export": "Other"}}],
            },
        })
    );
    // `Other` and `Third` are unique on their own candidates: no warning.
    assert_eq!(outcomes.len(), 1, "{outcomes:#?}");
    assert!(
        fixture.stderr.contains(
            "[warning: resolved] static/app::elimination/either as `Either` \
             (source_matches[].bindings[`f`]): resolved by elimination to `first` (body[0]): \
             its other matches are claimed by `Other` in elimination/other"
        ),
        "{}",
        fixture.stderr
    );

    let validate = validate_json(elimination_fixture());
    assert_eq!(validate["counts"]["resolved"], 1, "{validate:#}");
    assert_eq!(validate["outcomes"][0], *either, "{validate:#}");
}

/// Anonymous statements are kept distinct like members: `either` matches both
/// statements, `other` only the second, so `either` is unique only because
/// `other` claimed its alternative.
fn anonymous_elimination_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"console.log("first");
console.log("other");
"#,
        vec![
            logical_module_with_anon_alpha("anonymous/either", &[], "console.log(EXPR);"),
            logical_module_with_anon_alpha("anonymous/other", &[], r#"console.log("other");"#),
        ],
    )
}

#[test]
fn anonymous_statement_unique_only_by_elimination_warns() {
    let fixture = run_dry_run_fixture(anonymous_elimination_fixture());
    let outcomes = read_selector_outcomes(&fixture.report_root);
    let [either] = outcomes.as_slice() else {
        panic!("one elimination warning: {outcomes:#?}");
    };
    assert_eq!(
        either["placement"],
        json!({
            "logical_module": "anonymous/either",
            "entity": {"anonymous_statement": 0},
            "selector_kind": "anonymous_statements.source_match",
        }),
        "{either:#}"
    );
    assert_eq!(either["severity"], "warning", "{either:#}");
    assert_eq!(
        either["outcome"],
        json!({
            "kind": "resolved",
            "owner": 0,
            "resolved_by": {
                "by": "elimination",
                "claimers": [{"logical_module": "anonymous/other", "entity": {"anonymous_statement": 0}}],
            },
        })
    );

    let validate = validate_json(anonymous_elimination_fixture());
    assert_eq!(validate["outcomes"], json!([either]), "{validate:#}");
}

const RENAMED_METHOD: &str = r#"class a {
  open() {
    return 1;
  }
  close() {
    return 2;
  }
}
class b {
  start() {
    return 3;
  }
}
console.log(new a().open(), new b().start());
"#;

/// `close` was renamed upstream from the `shut` the selector still names.
const WIDGET: &str =
    "class Widget {\n  open() {\n    STMT_LIST;\n  }\n  shut() {\n    STMT_LIST;\n  }\n}";

fn renamed_method_fixture(
    extra: Vec<debundle_e2e_support::LogicalModuleEntry>,
) -> FixtureOpts<'static> {
    let mut modules = vec![logical_module(
        "widgets/widget",
        &[Member::source_alpha("Widget", WIDGET)],
    )];
    modules.extend(extra);
    FixtureOpts::new(RENAMED_METHOD, modules)
}

fn nearest_bindings(outcome: &Value) -> Vec<Value> {
    outcome["outcome"]["nearest_unclaimed"]
        .as_array()
        .map(|near| near.iter().map(|near| near["bindings"].clone()).collect())
        .unwrap_or_default()
}

/// A `no_match` template lists the unclaimed statements it comes closest to,
/// with where each diverges, in `run` and `spec validate` alike.
#[test]
fn no_match_lists_the_nearest_unclaimed_statements() {
    let rejected = run_dry_run_rejection_fixture(renamed_method_fixture(Vec::new()));
    let outcomes = read_selector_outcomes(&rejected.report_root);
    let widget = find_outcome(&outcomes, "no_match", "Widget");
    assert_eq!(nearest_bindings(widget)[0], json!(["a"]), "{widget:#}");
    assert!(
        rejected
            .stderr
            .contains("nearest unclaimed: body[0] declaring `a`"),
        "{}",
        rejected.stderr
    );

    let validate = validate_json(renamed_method_fixture(Vec::new()));
    assert_eq!(validate["outcomes"], json!([widget]), "{validate:#}");
}

/// A statement another entity claimed is never offered, however close.
#[test]
fn nearest_unclaimed_skips_claimed_statements() {
    let rejected = run_dry_run_rejection_fixture(renamed_method_fixture(vec![logical_module(
        "widgets/other",
        &[Member::renamed("Other", "a")],
    )]));
    let outcomes = read_selector_outcomes(&rejected.report_root);
    let widget = find_outcome(&outcomes, "no_match", "Widget");
    assert!(
        !nearest_bindings(widget).contains(&json!(["a"])),
        "{widget:#}"
    );
}

/// Nothing close enough: the record has no `nearest_unclaimed`.
#[test]
fn no_match_without_a_near_statement_lists_none() {
    let rejected = run_dry_run_rejection_fixture(FixtureOpts::new(
        "console.log(1);\n",
        vec![logical_module(
            "widgets/widget",
            &[Member::source_alpha("Widget", WIDGET)],
        )],
    ));
    let outcomes = read_selector_outcomes(&rejected.report_root);
    let widget = find_outcome(&outcomes, "no_match", "Widget");
    assert_eq!(widget["outcome"], json!({"kind": "no_match"}), "{widget:#}");
}

/// `alpha` and `beta` differ in their own literal; `gamma` and `delta` are
/// identical, so only the statement before each sets it apart.
const DIFFERENTIATED: &str = r#"function alpha() {
  return format("first");
}
function beta() {
  return format("second");
}
const marker = "gamma-marker";
function gamma() {
  return format("same");
}
function delta() {
  return format("same");
}
function format(value) {
  return value.trim();
}
console.log(alpha(), beta(), gamma(), delta(), marker);
"#;

fn differentiated_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        DIFFERENTIATED,
        vec![logical_module(
            "formatters/any",
            &[Member::source_alpha(
                "AnyFormatter",
                "function f() {\n  return format(EXPR);\n}",
            )],
        )],
    )
}

/// An `ambiguous` template names, per candidate, the anchor that sets it apart
/// from the other candidates: its own when it has one, else one in an
/// adjacent statement.
#[test]
fn ambiguous_lists_each_candidates_differentiator() {
    let rejected = run_dry_run_rejection_fixture(differentiated_fixture());
    let outcomes = read_selector_outcomes(&rejected.report_root);
    let any = find_outcome(&outcomes, "ambiguous", "AnyFormatter");
    assert_eq!(
        any["outcome"]["differentiators"],
        json!([
            {"owner": 0, "statement": 0, "anchor": "string literal \"first\""},
            {"owner": 1, "statement": 1, "anchor": "string literal \"second\""},
            {"owner": 3, "statement": 2, "anchor": "string literal \"gamma-marker\""},
            {"owner": 4, "statement": 3, "anchor": "string literal \"same\""},
        ]),
        "{any:#}"
    );
    assert!(
        rejected.stderr.contains(
            "set apart body[0] by its string literal \"first\", body[1] by its string literal \
             \"second\", body[3] by its string literal \"gamma-marker\" in the preceding body[2], \
             body[4] by its string literal \"same\" in the preceding body[3]"
        ),
        "{}",
        rejected.stderr
    );

    let validate = validate_json(differentiated_fixture());
    assert_eq!(validate["outcomes"], json!([any]), "{validate:#}");
}

/// Candidates alike in themselves and in their neighbors have no
/// differentiator, and the report says so.
#[test]
fn ambiguous_without_a_differentiator_says_so() {
    let rejected = run_dry_run_rejection_fixture(FixtureOpts::new(
        r#"noop();
work("same");
noop();
work("same");
noop();
function noop() {}
function work(value) {
  return value;
}
"#,
        vec![logical_module_with_anon(
            "effects/work",
            &[],
            &["work(\"same\");"],
        )],
    ));
    let outcomes = read_selector_outcomes(&rejected.report_root);
    let [work] = outcomes.as_slice() else {
        panic!("one ambiguous outcome: {outcomes:#?}");
    };
    assert_eq!(
        work["outcome"],
        json!({"kind": "ambiguous", "candidates": [{"owner": 1}, {"owner": 3}], "truncated": false}),
        "{work:#}"
    );
    assert!(
        rejected
            .stderr
            .contains("no candidate has an anchor the others lack"),
        "{}",
        rejected.stderr
    );
}

fn validate_json(opts: FixtureOpts<'_>) -> Value {
    let fixture = write_validate_fixture_spec(opts);
    let out = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(
        out.status.success(),
        "spec validate exited non-zero: stderr={}",
        out.stderr
    );
    serde_json::from_str(&out.stdout)
        .unwrap_or_else(|err| panic!("parse validate json: {err}\nstdout:\n{}", out.stdout))
}
