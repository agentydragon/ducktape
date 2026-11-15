//! `spec lint` decides `host_behavior.reentry_free_operations` entries from
//! the analysis's recorded uses (docs/premise_provenance.md): an entry no chunk
//! consumed is `inferable` at no extra cost; a consumed one is `inconclusive`
//! until `--probe-analysis` reruns the program without it.

use analysis::reports::spec_lint::{
    InconclusiveReason, InferableReason, InferenceVerdict, LintFinding, LintedAnnotation,
};
use debundle_e2e_support::*;

const SOURCE: &str = "globalThis.saved = () => later;\nconst gate = Promise.resolve();\nconst later = 42;\nconsole.log(Boolean(gate), later);\n";
const SAVED: &str = "globalThis.saved = () => later;";

fn policy(operations: &[ReentryFreeOperation]) -> HostBehavior {
    HostBehavior {
        reentry_free_operations: operations.iter().copied().collect(),
        ..HostBehavior::default()
    }
}

/// The legacy reentry proof needs the Promise.resolve premise, but it does
/// not prove that moving `later` before the host publication preserves its
/// captured-cell phase. The final schedule guard retains that limitation.
fn split(operations: &[ReentryFreeOperation]) -> FixtureOpts<'static> {
    FixtureOpts::new(
        SOURCE,
        vec![
            logical_module_with_anon("host/value", &[Member::new("gate")], &[SAVED]),
            logical_module("host/later", &[Member::new("later")]),
        ],
    )
    .with_host_behavior(policy(operations))
}

/// Everything in one module: the call is still analyzed with the entry, but
/// no split needs it.
fn one_module(operations: &[ReentryFreeOperation]) -> FixtureOpts<'static> {
    FixtureOpts::new(
        SOURCE,
        vec![logical_module_with_anon(
            "host/value",
            &[Member::new("gate"), Member::new("later")],
            &[SAVED],
        )],
    )
    .with_host_behavior(policy(operations))
}

fn lint(opts: FixtureOpts<'_>, args: &[&str]) -> (Vec<LintFinding>, serde_json::Value) {
    let fixture = write_validate_fixture_spec(opts);
    let report = spec_lint_json(&fixture.spec_path, args);
    let findings = serde_json::from_value(report["findings"].clone())
        .unwrap_or_else(|error| panic!("{error}: {report:#}"));
    (findings, report)
}

fn operation_verdicts(findings: &[LintFinding]) -> Vec<(ReentryFreeOperation, &InferenceVerdict)> {
    findings
        .iter()
        .filter_map(|finding| match &finding.annotation {
            LintedAnnotation::ReentryFreeOperation { operation } => {
                assert_eq!(finding.chunk, None, "{finding:?}");
                Some((*operation, &finding.verdict))
            }
            _ => None,
        })
        .collect()
}

const CONSUMED: InferenceVerdict = InferenceVerdict::Inconclusive {
    reason: InconclusiveReason::Consumed,
};

#[test]
fn unconsumed_entry_is_inferable_and_consumed_entry_is_not() {
    let both = [
        ReentryFreeOperation::AsyncLocalStorageConstruct,
        ReentryFreeOperation::PromiseResolve,
    ];
    let (findings, report) = lint(one_module(&both), &[]);
    assert_eq!(
        operation_verdicts(&findings),
        vec![
            (
                ReentryFreeOperation::AsyncLocalStorageConstruct,
                &InferenceVerdict::Inferable {
                    reason: InferableReason::NeverConsumed
                }
            ),
            (ReentryFreeOperation::PromiseResolve, &CONSUMED),
        ],
        "{report:#}"
    );
    // The `inferable` claim: without the unconsumed entry the run emits the
    // same tree, under Node parity.
    let with_both = run_fixture(one_module(&both));
    let without = run_fixture(one_module(&[ReentryFreeOperation::PromiseResolve]));
    assert_same_tree(&without.out_root, &with_both.out_root);
    assert_preserved_entry_output(&with_both, "true 42\n");
    assert_preserved_entry_output(&without, "true 42\n");
}

/// An entry required by the legacy gate cannot authorize a schedule change.
/// Its rejected baseline makes the independent-removal probe inconclusive.
#[test]
fn a_needed_legacy_entry_does_not_override_an_unproved_schedule() {
    expect_atomic_conflict_rejection(split(&[]), &["host/value", "host/later"], &[]);
    expect_rejection_containing_all(
        split(&[ReentryFreeOperation::PromiseResolve]),
        &["evaluation_schedule_unknown_reordered"],
    );
    let (findings, report) = lint(
        split(&[ReentryFreeOperation::PromiseResolve]),
        &["--probe-analysis"],
    );
    let verdicts = operation_verdicts(&findings);
    assert!(
        matches!(
            verdicts.as_slice(),
            [(
                ReentryFreeOperation::PromiseResolve,
                InferenceVerdict::Inconclusive {
                    reason: InconclusiveReason::BaselineNotAccepted { error }
                }
            )] if error.contains("evaluation_schedule_unknown_reordered")
        ),
        "{report:#}"
    );
    assert_eq!(
        report["probes"].as_array().map_or(0, Vec::len),
        0,
        "{report:#}"
    );
}

/// A consumed entry no split needs stays `inconclusive` without the probe;
/// the probe runs for it and replaces that lead.
#[test]
fn consumed_entry_without_a_needing_split_goes_to_the_probe() {
    let operations = [ReentryFreeOperation::PromiseResolve];
    let (findings, report) = lint(one_module(&operations), &[]);
    assert_eq!(
        operation_verdicts(&findings),
        vec![(ReentryFreeOperation::PromiseResolve, &CONSUMED)],
        "{report:#}"
    );
    let (findings, report) = lint(one_module(&operations), &["--probe-analysis"]);
    assert!(
        operation_verdicts(&findings)
            .iter()
            .all(|(_, verdict)| **verdict != CONSUMED),
        "{report:#}"
    );
    assert_eq!(
        report["probes"][0]["annotation"]["kind"], "reentry_free_operation",
        "{report:#}"
    );
}
