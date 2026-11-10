//! `debundle spec lint --probe-analysis` for `purity: pure` hints (SPEC.md
//! § Spec lint): each hint is withheld alone and the whole program rerun on the
//! same prepared input. A hint is `inferable` only when that run is accepted
//! with reports identical to the baseline's.

use analysis::reports::spec_lint::{
    InconclusiveReason, InferableReason, InferenceVerdict, LintFinding, LintedAnnotation,
    PurityHintOwner,
};
use debundle_e2e_support::*;

/// `seven` is provably pure without its hint. `leaf` reads a property of the
/// global object after a global write: without its hint, the call orders
/// after that write.
const SOURCE: &str = "function seven() { return 7; }\nfunction leaf(value) { return value.key; }\nglobalThis.flag = 1;\nconst a = leaf(globalThis);\nconsole.log(seven(), a);\nexport { seven, leaf, a };\n";

fn probe(opts: FixtureOpts<'_>) -> (Vec<LintFinding>, serde_json::Value) {
    let fixture = write_validate_fixture_spec(opts);
    let report = spec_lint_json(&fixture.spec_path, &["--probe-analysis"]);
    let findings = serde_json::from_value(report["findings"].clone())
        .unwrap_or_else(|error| panic!("{error}: {report:#}"));
    (findings, report)
}

fn purity_findings(findings: &[LintFinding]) -> Vec<(&str, &InferenceVerdict)> {
    findings
        .iter()
        .filter_map(|finding| match &finding.annotation {
            LintedAnnotation::PurityHint {
                owner: PurityHintOwner::LogicalMember(member),
            } => Some((member.name.as_str(), &finding.verdict)),
            _ => None,
        })
        .collect()
}

#[test]
fn redundant_hint_is_inferable_and_a_needed_hint_is_not_reported() {
    let (findings, report) = probe(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module(
                    "helpers/functions",
                    &[
                        Member::new("seven").with_purity(MemberPurity::Pure),
                        Member::new("leaf").with_purity(MemberPurity::Pure),
                    ],
                ),
                logical_module_with_anon(
                    "values/main",
                    &[Member::new("a")],
                    &["globalThis.flag = 1;"],
                ),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_eq!(
        purity_findings(&findings),
        vec![(
            "seven",
            &InferenceVerdict::Inferable {
                reason: InferableReason::WithheldRunUnchanged
            }
        )],
        "{report:#}"
    );
    // Both hints were probed: one run each.
    assert_eq!(
        report["probes"].as_array().map(Vec::len),
        Some(2),
        "{report:#}"
    );
}

/// A baseline the gate rejects cannot be compared: the hint is inconclusive.
#[test]
fn rejected_baseline_leaves_every_probed_hint_inconclusive() {
    const CYCLE: &str = "const trace = [];\nglobalThis.record = (key) => { trace.push(key); };\nfunction seven() { return 7; }\nconst alpha = (globalThis.record(\"alpha\"), \"A\");\nconst beta = (globalThis.record(\"beta\"), \"B\");\nconst gamma = (globalThis.record(\"gamma\"), seven());\nconsole.log(alpha, beta, gamma, trace.slice().sort().join(\",\"));\nexport { alpha, beta, gamma, seven };\n";
    let (findings, report) = probe(
        FixtureOpts::new(
            CYCLE,
            vec![
                logical_module_with_anon(
                    "probes/outer",
                    &[
                        Member::new("trace"),
                        Member::new("alpha"),
                        Member::new("gamma"),
                        Member::new("seven").with_purity(MemberPurity::Pure),
                    ],
                    &["globalThis.record = (key) => { trace.push(key); };"],
                ),
                logical_module("probes/inner", &[Member::new("beta")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let purity = purity_findings(&findings);
    let [("seven", verdict)] = purity.as_slice() else {
        panic!("{report:#}");
    };
    assert!(
        matches!(
            verdict,
            InferenceVerdict::Inconclusive {
                reason: InconclusiveReason::BaselineNotAccepted { .. }
            }
        ),
        "{report:#}"
    );
}

/// A probe budget never drops a hint: the unprobed one is inconclusive.
#[test]
fn hints_beyond_the_probe_budget_are_not_probed() {
    let fixture = write_validate_fixture_spec(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "helpers/functions",
                &[
                    Member::new("seven").with_purity(MemberPurity::Pure),
                    Member::new("leaf").with_purity(MemberPurity::Pure),
                    Member::new("a"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let report = spec_lint_json(
        &fixture.spec_path,
        &["--probe-analysis", "--max-probes", "1"],
    );
    let findings: Vec<LintFinding> = serde_json::from_value(report["findings"].clone()).unwrap();
    assert!(
        purity_findings(&findings)
            .iter()
            .any(|(_, verdict)| matches!(
                verdict,
                InferenceVerdict::Inconclusive {
                    reason: InconclusiveReason::NotProbed
                }
            )),
        "{report:#}"
    );
    assert_eq!(
        report["probes"].as_array().map(Vec::len),
        Some(1),
        "{report:#}"
    );
}
