use analysis::reports::spec_lint::LintFinding;
use debundle_e2e_support::{FixtureOpts, spec_lint_json, write_validate_fixture_spec};
use serde_json::Value;

mod annotation_findings;
mod host_provenance;
mod probe_analysis;
mod trusted_summary;

fn run_spec_lint(opts: FixtureOpts<'_>, args: &[&str]) -> (Vec<LintFinding>, Value) {
    let fixture = write_validate_fixture_spec(opts);
    let report = spec_lint_json(&fixture.spec_path, args);
    let findings = serde_json::from_value(report["findings"].clone())
        .unwrap_or_else(|error| panic!("{error}: {report:#}"));
    (findings, report)
}
