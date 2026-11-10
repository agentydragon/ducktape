//! `spec lint` decides `chunk_analysis_options.<chunk>.trusted_dataflow_summaries`
//! from the havoc causes it discharged (`StatementFacts::trusted_summary_discharged`,
//! docs/premise_provenance.md): a chunk where it discharged nothing is
//! `inferable`; a consumed one is `inconclusive` until `--probe-analysis`
//! reruns without it.

use analysis::reports::spec_lint::{
    InconclusiveReason, InferableReason, InferenceVerdict, LintFinding, LintedAnnotation,
};
use debundle_e2e_support::*;

/// Two disjoint global writes: summarizable without any trust.
const DISJOINT: &str = "const tagA = (globalThis.alpha = \"alpha-val\", \"tag-a\");\nconst tagB = (globalThis.beta = \"beta-val\", \"tag-b\");\nexport { tagA, tagB };\n";
/// Two constructor calls the classifier cannot prove pure: opaque at-init
/// calls the trusted summary discharges.
const CONSTRUCTORS: &str = "class Holder1 { constructor() { this.kind = \"h1\"; } }\nclass Holder2 { constructor() { this.kind = \"h2\"; } }\nconst instA = new Holder1();\nconst instB = new Holder2();\nconsole.log(instA.kind, instB.kind);\nexport { Holder1, Holder2, instA, instB };\n";

fn opts(
    source: &'static str,
    modules: Vec<LogicalModuleEntry>,
    trusted: bool,
) -> FixtureOpts<'static> {
    let opts = FixtureOpts::new(source, modules).with_dataflow_aware_s_chain();
    if trusted {
        opts.with_trusted_dataflow_summaries()
    } else {
        opts
    }
}

fn disjoint(trusted: bool) -> FixtureOpts<'static> {
    opts(
        DISJOINT,
        vec![
            logical_module("mod_a", &[Member::new("tagA")]),
            logical_module("mod_b", &[Member::new("tagB")]),
        ],
        trusted,
    )
}

fn constructors() -> FixtureOpts<'static> {
    opts(
        CONSTRUCTORS,
        vec![
            logical_module("mod_a", &[Member::new("Holder1"), Member::new("instA")]),
            logical_module("mod_b", &[Member::new("Holder2"), Member::new("instB")]),
        ],
        true,
    )
}

fn trusted_verdicts(
    opts: FixtureOpts<'_>,
    args: &[&str],
) -> (Vec<InferenceVerdict>, serde_json::Value) {
    let fixture = write_validate_fixture_spec(opts);
    let report = spec_lint_json(&fixture.spec_path, args);
    let findings: Vec<LintFinding> = serde_json::from_value(report["findings"].clone())
        .unwrap_or_else(|error| panic!("{error}: {report:#}"));
    let verdicts = findings
        .into_iter()
        .filter(|finding| {
            matches!(
                finding.annotation,
                LintedAnnotation::TrustedDataflowSummaries { .. }
            )
        })
        .map(|finding| finding.verdict)
        .collect();
    (verdicts, report)
}

#[test]
fn trust_that_discharged_nothing_is_inferable() {
    let (verdicts, report) = trusted_verdicts(disjoint(true), &[]);
    assert_eq!(
        verdicts,
        vec![InferenceVerdict::Inferable {
            reason: InferableReason::NeverConsumed
        }],
        "{report:#}"
    );
    // The `inferable` claim: the same tree without the option.
    assert_same_tree(
        &run_fixture(disjoint(false)).out_root,
        &run_fixture(disjoint(true)).out_root,
    );
}

#[test]
fn trust_that_discharged_an_opaque_call_is_consumed() {
    let (verdicts, report) = trusted_verdicts(constructors(), &[]);
    assert_eq!(
        verdicts,
        vec![InferenceVerdict::Inconclusive {
            reason: InconclusiveReason::Consumed
        }],
        "{report:#}"
    );
}

/// Withheld, the constructor calls become barriers again: the graph
/// changes, so the probe reports nothing for the option.
#[test]
fn probe_drops_trust_the_graph_depends_on() {
    let (verdicts, report) = trusted_verdicts(constructors(), &["--probe-analysis"]);
    assert_eq!(verdicts, vec![], "{report:#}");
    assert_eq!(report["probes"][0]["outcome"], "changed", "{report:#}");
}
