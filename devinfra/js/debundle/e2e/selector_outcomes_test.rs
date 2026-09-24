//! Selector outcomes decided around the joint solve: a selector the shape
//! matcher places too widely is `too_broad` without a solve, and one that is
//! unique only because other selectors claimed its alternatives is `resolved`
//! by elimination, with a warning.

use debundle_e2e_support::{
    FixtureOpts, Member, assert_fail_fast_stops_at_first_outcome, find_outcome, logical_module,
    read_selector_outcomes, run_dry_run_fixture, run_dry_run_rejection_fixture, run_spec_validate,
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
