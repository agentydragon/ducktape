//! Output-only views of the complete loader derivation and interpreter policy.
//! These types are never deserialized into staged analysis or used as proof.

use std::collections::BTreeSet;

use analysis::analysis_inputs::{HostBehaviorKey, ProgramPremiseKey};
use analysis::facts::ExportReentryPolicy;
use serde::Serialize;

use super::loader_surface::{
    ExportReentryLoaderDerivation, ExportReentryLoaderFailure, ExportReentryLoaderRequest,
};
use super::{
    ExportReentryClosureBlocker, ExportReentryGlobalBlocker, ExportReentryHostBuiltinLeaf,
    ExportReentryReport,
};

const EXAMPLE_LIMIT: usize = 20;

#[derive(Serialize)]
struct Examples<T> {
    total: usize,
    examples: T,
    truncated: bool,
}

fn slice_examples<T>(values: &[T]) -> Examples<&[T]> {
    Examples {
        total: values.len(),
        examples: &values[..values.len().min(EXAMPLE_LIMIT)],
        truncated: values.len() > EXAMPLE_LIMIT,
    }
}

fn set_examples<T: Ord>(values: &BTreeSet<T>) -> Examples<Vec<&T>> {
    Examples {
        total: values.len(),
        examples: values.iter().take(EXAMPLE_LIMIT).collect(),
        truncated: values.len() > EXAMPLE_LIMIT,
    }
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
enum PolicySummary<'a> {
    Conservative,
    NoStaticImporterReentry,
    /// An explicitly sampled diagnostic, never the actual interpreter set.
    ReentryThrough(Examples<Vec<&'a String>>),
}

#[derive(Serialize)]
#[serde(tag = "rule", rename_all = "snake_case")]
enum LoaderSummary<'a> {
    NoSynchronousLoader,
    NoHostLoaderSites,
    LoaderClosure {
        closed: bool,
        host_keys: &'a BTreeSet<HostBehaviorKey>,
        premise_keys: &'a BTreeSet<ProgramPremiseKey>,
        requests: Examples<&'a [ExportReentryLoaderRequest]>,
        request_site_count: usize,
        loaded_chunks: Examples<&'a [String]>,
        suspended_sites: usize,
        premised_sites: usize,
        outside_window_sites: usize,
        unreachable_sites: usize,
        /// Already one first witness and exact count per finite failure kind.
        failures: &'a [ExportReentryLoaderFailure],
        reentry_through: Option<Examples<Vec<&'a String>>>,
    },
}

impl<'a> From<&'a ExportReentryLoaderDerivation> for LoaderSummary<'a> {
    fn from(loader: &'a ExportReentryLoaderDerivation) -> Self {
        match loader {
            ExportReentryLoaderDerivation::NoSynchronousLoader => Self::NoSynchronousLoader,
            ExportReentryLoaderDerivation::NoHostLoaderSites => Self::NoHostLoaderSites,
            ExportReentryLoaderDerivation::LoaderClosure {
                closed,
                host_keys,
                premise_keys,
                requests,
                loaded_chunks,
                suspended_sites,
                premised_sites,
                outside_window_sites,
                unreachable_sites,
                failures,
                reentry_through,
            } => Self::LoaderClosure {
                closed: *closed,
                host_keys,
                premise_keys,
                requests: slice_examples(requests),
                request_site_count: requests.iter().map(|request| request.request_count).sum(),
                loaded_chunks: slice_examples(loaded_chunks),
                suspended_sites: *suspended_sites,
                premised_sites: *premised_sites,
                outside_window_sites: *outside_window_sites,
                unreachable_sites: *unreachable_sites,
                failures,
                reentry_through: reentry_through.as_ref().map(set_examples),
            },
        }
    }
}

/// Bounded presentation, borrowing the complete semantic report unchanged.
#[derive(Serialize)]
pub(super) struct ExportReentrySummary<'a> {
    host_behavior: &'a spec::HostBehavior,
    program_premises: &'a spec::ProgramPremises,
    policy: PolicySummary<'a>,
    loader: LoaderSummary<'a>,
    global_blockers: Examples<&'a [ExportReentryGlobalBlocker]>,
    closure_blockers: Examples<&'a [ExportReentryClosureBlocker]>,
    host_builtin_leaves: Examples<&'a [ExportReentryHostBuiltinLeaf]>,
    host_builtin_request_count: usize,
    example_limit: usize,
    full_detail_export_available: bool,
    full_detail_export_command: &'static str,
}

impl<'a> From<&'a ExportReentryReport> for ExportReentrySummary<'a> {
    fn from(report: &'a ExportReentryReport) -> Self {
        Self {
            host_behavior: &report.host_behavior,
            program_premises: &report.program_premises,
            policy: match &report.policy {
                ExportReentryPolicy::Conservative => PolicySummary::Conservative,
                ExportReentryPolicy::NoStaticImporterReentry => {
                    PolicySummary::NoStaticImporterReentry
                }
                ExportReentryPolicy::ReentryThrough(names) => {
                    PolicySummary::ReentryThrough(set_examples(names))
                }
            },
            loader: (&report.loader).into(),
            global_blockers: slice_examples(&report.global_blockers),
            closure_blockers: slice_examples(&report.closure_blockers),
            host_builtin_leaves: slice_examples(&report.host_builtin_leaves),
            host_builtin_request_count: report
                .host_builtin_leaves
                .iter()
                .map(|leaf| leaf.request_count)
                .sum(),
            example_limit: EXAMPLE_LIMIT,
            full_detail_export_available: true,
            full_detail_export_command: "debundle run --full-report export-reentry",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn policy_and_blocker_samples_do_not_change_the_complete_report() {
        let names: BTreeSet<_> = (0..32).map(|i| format!("name{i:02}")).collect();
        let report = ExportReentryReport {
            host_behavior: spec::HostBehavior::default(),
            program_premises: spec::ProgramPremises::default(),
            policy: ExportReentryPolicy::ReentryThrough(names.clone()),
            loader: ExportReentryLoaderDerivation::NoHostLoaderSites,
            global_blockers: (0..32)
                .map(|i| ExportReentryGlobalBlocker::NonOrdinaryStaticRequest {
                    source_chunk: format!("peer{i:02}"),
                    specifier: "./value.js".into(),
                    resolved_target: None,
                })
                .collect(),
            closure_blockers: (0..32)
                .map(|i| ExportReentryClosureBlocker::NonEsmChunk {
                    source_chunk: format!("peer{i:02}"),
                })
                .collect(),
            host_builtin_leaves: Vec::new(),
        };
        let original = serde_json::to_value(&report).unwrap();
        let summary = serde_json::to_value(ExportReentrySummary::from(&report)).unwrap();
        for examples in [
            &summary["policy"]["reentry_through"],
            &summary["global_blockers"],
            &summary["closure_blockers"],
        ] {
            assert_eq!(examples["total"], 32);
            assert_eq!(examples["examples"].as_array().unwrap().len(), 20);
            assert_eq!(examples["truncated"], true);
        }
        assert_eq!(serde_json::to_value(&report).unwrap(), original);
        let ExportReentryPolicy::ReentryThrough(complete_names) = &report.policy else {
            panic!("summary changed the semantic policy");
        };
        assert_eq!(complete_names, &names);
        assert!(serde_json::from_value::<ExportReentryReport>(summary).is_err());
    }
}
