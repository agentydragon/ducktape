//! `debundle run --dry-run` keep-going: every selector that does not resolve
//! gets an outcome record in `selector_diagnostics.json` and a line in the
//! stderr report, and none of them takes the chunk's other selectors down.
//! With `--fail-fast` the first of them stops the run.

use debundle_e2e_support::{
    BindingGroup, FixtureOpts, Member, assert_fail_fast_stops_at_first_outcome,
    assert_stderr_lists_every_outcome, find_outcome, logical_module, logical_module_with_anon,
    logical_module_with_anon_alpha, logical_module_with_binding_groups,
    mixed_selector_failure_fixture, read_selector_outcomes, run_dry_run_rejection_fixture,
};
use serde_json::{Value, json};

#[test]
fn keep_going_writes_machine_readable_selector_outcomes() {
    let mut opts = mixed_selector_failure_fixture();
    opts.logical_modules.push(logical_module_with_anon(
        "diagnostics/anon",
        &[],
        &["console.warn(\"absent\");"],
    ));

    let rejected = run_dry_run_rejection_fixture(opts);
    let outcomes = read_selector_outcomes(&rejected.report_root);
    assert_eq!(outcomes.len(), 4, "{outcomes:#?}");
    assert_stderr_lists_every_outcome(&rejected.stderr, &outcomes);

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
    assert_eq!(ambiguous["outcome"]["kind"], "ambiguous");
    assert_eq!(
        ambiguous["outcome"]["candidates"],
        json!([
            {"owner": 1, "binding": "decoratePrimary"},
            {"owner": 2, "binding": "decorateSecondary"},
        ])
    );
    assert_eq!(ambiguous["outcome"]["truncated"], false);
    let differentiators = ambiguous["outcome"]["differentiators"].as_array().unwrap();
    assert_eq!(differentiators.len(), 2, "{ambiguous:#}");
    for (differentiator, (owner, statement, token)) in differentiators
        .iter()
        .zip([(1, 0, ".trim"), (2, 1, "\"shared\"")])
    {
        assert_eq!(differentiator["owner"], owner);
        assert_eq!(differentiator["statement"], statement);
        assert!(
            differentiator["anchor"].as_str().unwrap().contains(token),
            "{differentiator:#}"
        );
    }

    let duplicate = outcomes
        .iter()
        .find(|record| record["outcome"]["kind"] == "duplicate_claim")
        .expect("duplicate claim outcome");
    assert_eq!(duplicate["outcome"]["binding"], "renderCard");
    assert_eq!(
        duplicate["outcome"]["declaration"],
        json!({"owner": 0, "kind": "function"}),
        "{duplicate:#}"
    );
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
fn infeasible_pairs_fixture() -> FixtureOpts<'static> {
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

/// Each pair is its own contradiction: both of its selectors are `unsatisfiable`
/// with its ownership witness, and neither pair takes down the other or
/// the independent selector.
#[test]
fn keep_going_reports_every_selector_of_an_infeasible_pair_with_a_witness() {
    let rejected = run_dry_run_rejection_fixture(infeasible_pairs_fixture());
    let outcomes = read_selector_outcomes(&rejected.report_root);
    for export_name in ["AlphaLeft", "AlphaRight", "BetaLeft", "BetaRight"] {
        let record = find_outcome(&outcomes, "unsatisfiable", export_name);
        assert_eq!(
            record["outcome"]["witness"]["selectors"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
        assert_eq!(
            record["outcome"]["witness"]["owners"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert!(
            record["outcome"].get("nearest_unclaimed").is_none(),
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
fn fail_fast_stops_at_the_first_infeasible_selector() {
    assert_fail_fast_stops_at_first_outcome(infeasible_pairs_fixture, "unsatisfiable");
}

#[test]
fn keep_going_unmatched_anonymous_statement_does_not_cascade() {
    let opts = FixtureOpts::new(
        r#"console.log("present");
"#,
        vec![logical_module_with_anon_alpha(
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

    let outcomes = keep_going_outcomes(opts);
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
