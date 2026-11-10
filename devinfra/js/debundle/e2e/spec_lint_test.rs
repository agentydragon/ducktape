//! `debundle spec lint` over one exact source/spec pair (SPEC.md § Spec lint):
//! every author annotation the engine may already infer, as typed warnings.
//! An `inferable` finding is checked against its claim: withholding the
//! annotations leaves the emitted tree unchanged.

use analysis::purity::RedundantPurityProofBasis;
use analysis::reports::spec_lint::{
    InconclusiveReason, InferableReason, InferenceVerdict, LintFinding, LintRule, LintedAnnotation,
    PurityHintOwner,
};
use debundle_e2e_support::*;
use spec::AdmissionCheck;

const SOURCE: &str = "const trace = [];\nglobalThis.record = (key) => { trace.push(key); };\nconst alpha = (globalThis.record(\"alpha\"), \"A\");\nconst beta = (globalThis.record(\"beta\"), \"B\");\nconst gamma = (globalThis.record(\"gamma\"), \"C\");\nfunction seven() { return 7; }\nconsole.log(alpha, beta, gamma, seven(), trace.slice().sort().join(\",\"));\nexport { alpha, beta, gamma, seven };\n";
const RECORD_INSTALL: &str = "globalThis.record = (key) => { trace.push(key); };";
const OUTPUT: &str = "A B C 7 alpha,beta,gamma\n";

/// `alpha`, `beta` and `gamma` share one module; `seven` lives in another.
/// `annotated` adds the three annotations under test: a `purity: pure` hint on
/// `seven`, an `a1_eval` override on a chunk without `eval`, and an
/// `alpha`/`gamma` commutation no edge needs.
fn fixture(annotated: bool) -> FixtureOpts<'static> {
    let seven = if annotated {
        Member::new("seven").with_purity(MemberPurity::Pure)
    } else {
        Member::new("seven")
    };
    let opts = FixtureOpts::new(
        SOURCE,
        vec![
            logical_module_with_anon(
                "probes/outer",
                &[
                    Member::new("trace"),
                    Member::new("alpha"),
                    Member::new("beta"),
                    Member::new("gamma"),
                ],
                &[RECORD_INSTALL],
            ),
            logical_module("helpers/math", &[seven]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline());
    if !annotated {
        return opts;
    }
    opts.with_admission_overrides(&["a1_eval"])
        .with_commuting_statements(
            "static/app",
            vec![commuting_pair(
                member_source_unit("probes/outer", "alpha"),
                member_source_unit("probes/outer", "gamma"),
            )],
        )
}

fn lint(opts: FixtureOpts<'_>) -> Vec<LintFinding> {
    let fixture = write_validate_fixture_spec(opts);
    let report = spec_lint_json(&fixture.spec_path, &[]);
    assert!(report.get("unlinted_chunks").is_none(), "{report:#}");
    serde_json::from_value(report["findings"].clone())
        .unwrap_or_else(|error| panic!("{error}: {report:#}"))
}

#[test]
fn lint_reports_each_inferable_annotation_kind_by_authored_identity() {
    let findings = lint(fixture(true));
    assert!(
        findings
            .iter()
            .all(|finding| finding.rule == LintRule::InferableAnnotation
                && finding.chunk.as_deref() == Some("static/app")),
        "{findings:#?}"
    );
    let [commuting, admission, purity] = findings.as_slice() else {
        panic!("expected three findings: {findings:#?}");
    };

    let LintedAnnotation::CommutingStatements {
        spec_location,
        a,
        b,
    } = &commuting.annotation
    else {
        panic!("{commuting:#?}");
    };
    assert!(
        spec_location.ends_with("commuting_statements[0]"),
        "{commuting:#?}"
    );
    for (endpoint, name) in [(a, "alpha"), (b, "gamma")] {
        let [member] = endpoint.logical_members.as_slice() else {
            panic!("{commuting:#?}");
        };
        assert_eq!(
            (member.module.as_str(), member.name.as_str()),
            ("probes/outer", name)
        );
        assert!(endpoint.source_location.is_some(), "{commuting:#?}");
    }
    assert_eq!(
        commuting.verdict,
        InferenceVerdict::Inferable {
            reason: InferableReason::NoSequencedEdge
        }
    );

    assert!(
        matches!(
            &admission.annotation,
            LintedAnnotation::AdmissionOverride {
                check: AdmissionCheck::A1Eval,
                ..
            }
        ),
        "{admission:#?}"
    );
    assert_eq!(
        admission.verdict,
        InferenceVerdict::Inferable {
            reason: InferableReason::NoAdmissionViolation
        }
    );

    let LintedAnnotation::PurityHint {
        owner: PurityHintOwner::LogicalMember(member),
    } = &purity.annotation
    else {
        panic!("{purity:#?}");
    };
    assert_eq!(
        (member.module.as_str(), member.name.as_str()),
        ("helpers/math", "seven")
    );
    // The classifier probe alone never establishes an unchanged verdict.
    assert!(
        matches!(
            purity.verdict,
            InferenceVerdict::Inconclusive {
                reason: InconclusiveReason::PurityClassification {
                    proof_basis: RedundantPurityProofBasis::WithoutPurityHints,
                    ..
                }
            }
        ),
        "{purity:#?}"
    );

    // The `inferable` claim: the annotated spec emits exactly what the spec
    // without the annotations emits, under Node parity.
    let annotated = run_fixture(fixture(true));
    assert_preserved_entry_output(&annotated, OUTPUT);
    assert_same_tree(&run_fixture(fixture(false)).out_root, &annotated.out_root);
}

/// Annotations the run consumes are not findings: a commutation that inerts an
/// edge and an override that suppresses a violation.
#[test]
fn lint_reports_nothing_for_consumed_annotations() {
    const EVAL_SOURCE: &str = "const trace = [];\nglobalThis.record = (key) => { trace.push(key); };\nconst alpha = (globalThis.record(\"alpha\"), \"A\");\nconst beta = (globalThis.record(\"beta\"), \"B\");\nconst gamma = (globalThis.record(\"gamma\"), eval(\"'C'\"));\nconsole.log(alpha, beta, gamma, trace.slice().sort().join(\",\"));\nexport { alpha, beta, gamma };\n";
    let opts = FixtureOpts::new(
        EVAL_SOURCE,
        vec![
            logical_module_with_anon(
                "probes/outer",
                &[
                    Member::new("trace"),
                    Member::new("alpha"),
                    Member::new("gamma"),
                ],
                &[RECORD_INSTALL],
            ),
            logical_module("probes/inner", &[Member::new("beta")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline())
    .with_admission_overrides(&["a1_eval"])
    .with_commuting_statements(
        "static/app",
        vec![commuting_pair(
            member_source_unit("probes/inner", "beta"),
            member_source_unit("probes/outer", "gamma"),
        )],
    );
    assert_eq!(lint(opts), Vec::new());
}
