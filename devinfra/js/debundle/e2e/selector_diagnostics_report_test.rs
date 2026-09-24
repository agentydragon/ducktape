//! `debundle run --dry-run` keep-going: every selector that does not resolve
//! gets an outcome record in `selector_diagnostics.json` and a line in the
//! stderr report, and none of them takes the chunk's other selectors down.
//! With `--fail-fast` the first of them stops the run.

use debundle_e2e_support::{
    BindingGroup, FixtureOpts, Member, assert_fail_fast_stops_at_first_outcome, find_outcome,
    logical_module, logical_module_with_anon, logical_module_with_anon_alpha_many,
    logical_module_with_binding_groups, read_selector_outcomes, run_dry_run_rejection_fixture,
};
use serde_json::{Value, json};

#[test]
fn keep_going_writes_machine_readable_selector_outcomes() {
    let missing_selector = r#"function selectedFormatter(value) {
  return value.toLowerCase();
}"#;
    let ambiguous_selector = r#"function repeatedHelper() {
  return "shared";
}"#;
    let opts = FixtureOpts::new(
        r#"function renderCard(value) {
  return value.trim();
}
function decoratePrimary() {
  return "shared";
}
function decorateSecondary() {
  return "shared";
}
console.log(renderCard(" ok "), decoratePrimary(), decorateSecondary());
export { renderCard, decoratePrimary, decorateSecondary };
"#,
        vec![
            logical_module(
                "diagnostics/missing",
                &[Member::source_alpha("MissingFormatter", missing_selector)],
            ),
            logical_module("owners/card", &[Member::new("renderCard")]),
            logical_module(
                "duplicates/card",
                &[Member::renamed("renderCardAgain", "renderCard")],
            ),
            logical_module(
                "diagnostics/ambiguous",
                &[Member::source_alpha("AmbiguousHelper", ambiguous_selector)],
            ),
            logical_module_with_anon("diagnostics/anon", &[], &["console.warn(\"absent\");"]),
        ],
    );

    let rejected = run_dry_run_rejection_fixture(opts);
    for required in [
        "4 selector outcome(s): no_match=2, ambiguous=1, duplicate_claim=1",
        "[no_match] static/app::diagnostics/missing as `MissingFormatter`",
        "[no_match] static/app::diagnostics/anon anonymous_statements[0]",
        "[ambiguous] static/app::diagnostics/ambiguous as `AmbiguousHelper`",
        "[duplicate_claim]",
    ] {
        assert!(
            rejected.stderr.contains(required),
            "stderr missing {required:?}:\n{}",
            rejected.stderr
        );
    }

    let outcomes = read_selector_outcomes(&rejected.report_root);
    assert_eq!(outcomes.len(), 4, "{outcomes:#?}");

    let missing = find_outcome(&outcomes, "no_match", "MissingFormatter");
    assert_eq!(missing["chunk"], "static/app");
    assert_eq!(
        missing["placement"],
        json!({
            "logical_module": "diagnostics/missing",
            "entity": {"export": "MissingFormatter"},
            "selector_kind": "source_matches",
        })
    );
    assert_eq!(missing["target_binding"], "selectedFormatter");
    assert_eq!(missing["severity"], "error");
    assert!(
        missing["selector_preview"]
            .as_str()
            .unwrap()
            .contains("selectedFormatter"),
        "{missing:#}"
    );

    let ambiguous = find_outcome(&outcomes, "ambiguous", "AmbiguousHelper");
    assert_eq!(
        ambiguous["outcome"],
        json!({
            "kind": "ambiguous",
            "candidates": [
                {"owner": 1, "binding": "decoratePrimary"},
                {"owner": 2, "binding": "decorateSecondary"},
            ],
            "truncated": false,
            "differentiators": [
                {"owner": 1, "statement": 0, "anchor": "property access `.trim`"},
                {"owner": 2, "statement": 1, "anchor": "string literal \"shared\""},
            ],
        })
    );

    let duplicate = outcomes
        .iter()
        .find(|record| record["outcome"]["kind"] == "duplicate_claim")
        .expect("duplicate claim outcome");
    assert_eq!(duplicate["outcome"]["binding"], "renderCard");
    let mut sites = [
        duplicate["placement"]["logical_module"].as_str().unwrap(),
        duplicate["outcome"]["claimed_by"]["logical_module"]
            .as_str()
            .unwrap(),
    ];
    sites.sort_unstable();
    assert_eq!(sites, ["duplicates/card", "owners/card"], "{duplicate:#}");

    let anon = find_anon_outcome(&outcomes, "console.warn");
    assert_eq!(anon["outcome"]["kind"], "no_match");
    assert_eq!(
        anon["placement"],
        json!({
            "logical_module": "diagnostics/anon",
            "entity": {"anonymous_statement": 0},
            "selector_kind": "anonymous_statements.source_match",
        })
    );
}

/// A selector the matcher places nowhere is reported on its own and never
/// enters the solve, so it cannot take the chunk's other selectors down with it.
#[test]
fn keep_going_unmatched_selector_does_not_cascade() {
    let missing_selector = r#"function missingFormatter(value) {
  return value.toLowerCase();
}"#;
    let valid_selector = r#"function keepMe(value) {
  return value.trim();
}"#;
    let opts = FixtureOpts::new(
        r#"function keepMe(value) {
  return value.trim();
}
console.log(keepMe(" ok "));
export { keepMe };
"#,
        vec![
            logical_module(
                "diagnostics/root",
                &[Member::source_alpha("MissingFormatter", missing_selector)],
            ),
            logical_module(
                "diagnostics/cascade",
                &[Member::source_alpha("KeepMe", valid_selector)],
            ),
        ],
    );

    let outcomes = keep_going_outcomes(opts);
    find_outcome(&outcomes, "no_match", "MissingFormatter");
    assert_eq!(outcomes.len(), 1, "{outcomes:#?}");
}

/// Two pairs of selectors each compete for the one declaration both members
/// of the pair match, under the injectivity `all_different`, beside an
/// independent selector whose binding a name pin also claims.
fn conflicts_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"function alphaOne() {
  return "alpha";
}
function betaOne() {
  return "beta";
}
function gammaOne(value) {
  return value.trim();
}
console.log(alphaOne(), betaOne(), gammaOne(" ok "));
export { alphaOne, betaOne, gammaOne };
"#,
        vec![
            logical_module(
                "conflicts/alpha_left",
                &[Member::source_alpha(
                    "AlphaLeft",
                    "function left() {\n  return \"alpha\";\n}",
                )],
            ),
            logical_module(
                "conflicts/alpha_right",
                &[Member::source_alpha(
                    "AlphaRight",
                    "function right() {\n  return \"alpha\";\n}",
                )],
            ),
            logical_module(
                "conflicts/beta_left",
                &[Member::source_alpha(
                    "BetaLeft",
                    "function left() {\n  return \"beta\";\n}",
                )],
            ),
            logical_module(
                "conflicts/beta_right",
                &[Member::source_alpha(
                    "BetaRight",
                    "function right() {\n  return \"beta\";\n}",
                )],
            ),
            logical_module(
                "independent/gamma",
                &[Member::source_alpha(
                    "Gamma",
                    "function g(value) {\n  return value.trim();\n}",
                )],
            ),
            // A name pin on `gammaOne`: the duplicate claim it draws is the
            // witness that the independent selector resolved.
            logical_module("witness/gamma", &[Member::renamed("GammaPin", "gammaOne")]),
        ],
    )
}

/// Each pair is its own contradiction: its selectors name each other, and
/// neither pair takes down the other or the independent selector.
#[test]
fn keep_going_localizes_each_selector_conflict() {
    let outcomes = keep_going_outcomes(conflicts_fixture());
    for (export_name, partner, partner_module) in [
        ("AlphaLeft", "AlphaRight", "conflicts/alpha_right"),
        ("AlphaRight", "AlphaLeft", "conflicts/alpha_left"),
        ("BetaLeft", "BetaRight", "conflicts/beta_right"),
        ("BetaRight", "BetaLeft", "conflicts/beta_left"),
    ] {
        let record = find_outcome(&outcomes, "conflict", export_name);
        assert_eq!(
            record["outcome"]["with"],
            json!([{"logical_module": partner_module, "entity": {"export": partner}}]),
            "{record:#}"
        );
    }
    let duplicate = outcomes
        .iter()
        .find(|record| record["outcome"]["kind"] == "duplicate_claim")
        .unwrap_or_else(|| panic!("missing duplicate-claim witness: {outcomes:#?}"));
    assert_eq!(duplicate["outcome"]["binding"], "gammaOne");
    assert_eq!(outcomes.len(), 5, "{outcomes:#?}");
}

#[test]
fn fail_fast_stops_at_the_first_conflict() {
    assert_fail_fast_stops_at_first_outcome(conflicts_fixture, "conflict");
}

/// Two selectors, each matching two identical functions of its own.
#[test]
fn fail_fast_stops_at_the_first_ambiguous_selector() {
    assert_fail_fast_stops_at_first_outcome(
        || {
            FixtureOpts::new(
                r#"function sharedOne() {
  return "shared";
}
function sharedTwo() {
  return "shared";
}
function twiceOne() {
  return "twice";
}
function twiceTwo() {
  return "twice";
}
console.log(sharedOne(), sharedTwo(), twiceOne(), twiceTwo());
export { sharedOne, sharedTwo, twiceOne, twiceTwo };
"#,
                vec![
                    logical_module(
                        "ambiguous/shared",
                        &[Member::source_alpha(
                            "Shared",
                            "function s() {\n  return \"shared\";\n}",
                        )],
                    ),
                    logical_module(
                        "ambiguous/twice",
                        &[Member::source_alpha(
                            "Twice",
                            "function t() {\n  return \"twice\";\n}",
                        )],
                    ),
                ],
            )
        },
        "ambiguous",
    );
}

#[test]
fn keep_going_unmatched_anonymous_statement_does_not_cascade() {
    let opts = FixtureOpts::new(
        r#"console.log("present");
"#,
        vec![logical_module_with_anon_alpha_many(
            "diagnostics/anon",
            &[],
            &[r#"console.warn("missing");"#, r#"console.log("present");"#],
        )],
    );

    let outcomes = keep_going_outcomes(opts);
    let missing = find_anon_outcome(&outcomes, "console.warn");
    assert_eq!(missing["outcome"]["kind"], "no_match", "{missing:#}");
    assert_eq!(outcomes.len(), 1, "{outcomes:#?}");
}

#[test]
fn keep_going_reports_unmatched_source_match_group_per_target_binding() {
    let opts = FixtureOpts::new(
        r#"const presentLeft = 1, presentRight = 2;
console.log(presentLeft, presentRight);
export { presentLeft, presentRight };
"#,
        vec![logical_module_with_binding_groups(
            "diagnostics/group",
            &[],
            &[BindingGroup::source_alpha(
                r#"const missingLeft = "missing-left", missingRight = "missing-right";"#,
                &[
                    ("missingLeft", "ExportedLeft"),
                    ("missingRight", "ExportedRight"),
                ],
            )],
        )],
    );

    let outcomes = keep_going_outcomes(opts);
    for (export_name, target_binding) in [
        ("ExportedLeft", "missingLeft"),
        ("ExportedRight", "missingRight"),
    ] {
        let record = find_outcome(&outcomes, "no_match", export_name);
        assert_eq!(record["placement"]["selector_kind"], "source_matches");
        assert_eq!(record["target_binding"], target_binding);
    }
}

#[test]
fn keep_going_reports_canonical_source_match_group_failure() {
    let opts = FixtureOpts::new(
        r#"const present = 1;
console.log(present);
export { present };
"#,
        vec![logical_module_with_binding_groups(
            "diagnostics/unsupported-group",
            &[],
            &[BindingGroup::source_alpha(
                r#"const left = makeLeft();
STMT_LIST;
const right = makeRight();"#,
                &[("left", "ExportedLeft"), ("right", "ExportedRight")],
            )],
        )],
    );

    let rejected = run_dry_run_rejection_fixture(opts);
    assert!(
        rejected
            .stderr
            .contains("source_matches[].bindings[`left`]"),
        "human report should name the canonical source match:\n{}",
        rejected.stderr
    );
    let outcomes = read_selector_outcomes(&rejected.report_root);
    for (export_name, target_binding) in [("ExportedLeft", "left"), ("ExportedRight", "right")] {
        let record = find_outcome(&outcomes, "no_match", export_name);
        assert_eq!(record["placement"]["selector_kind"], "source_matches");
        assert_eq!(record["target_binding"], target_binding);
    }
}

fn keep_going_outcomes(opts: FixtureOpts<'_>) -> Vec<Value> {
    read_selector_outcomes(&run_dry_run_rejection_fixture(opts).report_root)
}

fn find_anon_outcome<'a>(outcomes: &'a [Value], preview_needle: &str) -> &'a Value {
    outcomes
        .iter()
        .find(|record| {
            record["placement"]["selector_kind"] == "anonymous_statements.source_match"
                && record["selector_preview"]
                    .as_str()
                    .is_some_and(|preview| preview.contains(preview_needle))
        })
        .unwrap_or_else(|| {
            panic!("missing anonymous outcome containing {preview_needle}: {outcomes:#?}")
        })
}
