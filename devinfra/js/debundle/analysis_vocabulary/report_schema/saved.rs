//! Explicit saved projections for report records with artifact-only spans.

use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use std::borrow::Cow;
use swc_common::Span;

use crate::execution_evidence::ExecutionPrecisionLoss;

use crate::report_schema::{
    LocalRecordFactoryRejection, LocalRecordFactoryUse, NativeMutationWitness,
    NativeOperationInputUse, NativePrototypeMutation, NativeUnreachableBranch,
    ReentryFreeOperationUse,
};

#[derive(Serialize, Deserialize)]
pub(crate) struct SavedNativeMutationWitness<'a> {
    report: Cow<'a, NativeMutationWitness>,
    span: Span,
    /// Required field, nullable value: missing must differ from explicit None.
    #[serde(deserialize_with = "Deserialize::deserialize")]
    unreachable_branch_spans: Option<SavedNativeUnreachableBranchSpans>,
}

impl<'a> SavedNativeMutationWitness<'a> {
    pub fn snapshot(report: &'a NativeMutationWitness) -> Self {
        Self {
            report: Cow::Borrowed(report),
            span: report.span,
            unreachable_branch_spans: report
                .unreachable_branch
                .as_ref()
                .map(SavedNativeUnreachableBranchSpans::snapshot),
        }
    }

    pub fn restore(self) -> Result<NativeMutationWitness> {
        let mut report = self.report.into_owned();
        report.span = self.span;
        match (
            report.unreachable_branch.as_mut(),
            self.unreachable_branch_spans,
        ) {
            (Some(branch), Some(spans)) => spans.restore(branch),
            (None, None) => {}
            _ => bail!("saved unreachable-branch spans do not match their witness"),
        }
        Ok(report)
    }
}

#[derive(Serialize, Deserialize)]
struct SavedNativeUnreachableBranchSpans {
    guard_span: Span,
    wrapper_call_span: Span,
    callback_span: Span,
}

impl SavedNativeUnreachableBranchSpans {
    fn snapshot(branch: &NativeUnreachableBranch) -> Self {
        Self {
            guard_span: branch.guard_span,
            wrapper_call_span: branch.wrapper_call_span,
            callback_span: branch.callback_span,
        }
    }

    fn restore(self, branch: &mut NativeUnreachableBranch) {
        branch.guard_span = self.guard_span;
        branch.wrapper_call_span = self.wrapper_call_span;
        branch.callback_span = self.callback_span;
    }
}

// A report's public Serde projection omits its SWC span. Persist that span
// beside the borrowed report, then reattach it after decoding.
macro_rules! saved_report_with_span {
    ($saved:ident, $report:ty) => {
        #[derive(Serialize, Deserialize)]
        pub struct $saved<'a> {
            report: Cow<'a, $report>,
            span: Span,
        }

        impl<'a> $saved<'a> {
            pub fn snapshot(report: &'a $report) -> Self {
                Self {
                    report: Cow::Borrowed(report),
                    span: report.span,
                }
            }

            pub fn restore(self) -> $report {
                let mut report = self.report.into_owned();
                report.span = self.span;
                report
            }
        }
    };
}

saved_report_with_span!(SavedNativePrototypeMutation, NativePrototypeMutation);
saved_report_with_span!(SavedNativeOperationInputUse, NativeOperationInputUse);
saved_report_with_span!(SavedLocalRecordFactoryUse, LocalRecordFactoryUse);
saved_report_with_span!(
    SavedLocalRecordFactoryRejection,
    LocalRecordFactoryRejection
);
saved_report_with_span!(SavedReentryFreeOperationUse, ReentryFreeOperationUse);
saved_report_with_span!(SavedExecutionPrecisionLoss, ExecutionPrecisionLoss);

#[cfg(test)]
mod tests {
    use super::*;
    use crate::inputs::{CallbackFactoryEvidence, CallbackObjectArguments};
    use crate::report_schema::NativeMutationOperation;
    use source_identity::SourceUnitOrdinal;
    use std::collections::BTreeSet;
    use swc_common::{BytePos, Span};

    #[test]
    fn witness_and_nested_unreachable_spans_round_trip_in_ordinary_serde() {
        let span = Span::new(BytePos(7), BytePos(12));
        let witness = NativeMutationWitness {
            chunk_id: "app".to_string(),
            statement_ordinal: SourceUnitOrdinal(3),
            source_location: None,
            operation: NativeMutationOperation::BindingAssignment,
            receiver_roots: BTreeSet::new(),
            through_local_alias: false,
            receiver_global: None,
            affected_roots: BTreeSet::new(),
            target_slots: BTreeSet::new(),
            key_dependencies: BTreeSet::new(),
            invalidated_key_dependencies: BTreeSet::new(),
            value_input_refs: BTreeSet::new(),
            value_input_evidence: Vec::new(),
            unreachable_branch: Some(NativeUnreachableBranch {
                arguments: CallbackObjectArguments {
                    wrapper: CallbackFactoryEvidence::AuthorAssertion {
                        defining_module: "provider".to_string(),
                        export: "wrap".to_string(),
                    },
                    object_arguments: BTreeSet::new(),
                },
                imported_local: "wrap".to_string(),
                wrapper_call_location: None,
                callback_location: None,
                guard_location: None,
                guard_span: Span::new(BytePos(13), BytePos(14)),
                wrapper_call_span: Span::new(BytePos(15), BytePos(16)),
                callback_span: Span::new(BytePos(17), BytePos(18)),
            }),
            span,
        };

        let json = serde_json::to_value(&witness).expect("report witness serializes");
        assert!(json.get("span").is_none());
        assert!(json["unreachable_branch"].get("guard_span").is_none());

        let bytes = rmp_serde::to_vec_named(&SavedNativeMutationWitness::snapshot(&witness))
            .expect("ordinary saved encode");
        let saved: SavedNativeMutationWitness<'_> =
            rmp_serde::from_slice(&bytes).expect("ordinary saved decode");
        let restored = saved.restore().expect("valid saved witness");
        assert_eq!(restored.span, span);
        let branch = restored.unreachable_branch.expect("branch retained");
        assert_eq!(branch.guard_span, Span::new(BytePos(13), BytePos(14)));
        assert_eq!(
            branch.wrapper_call_span,
            Span::new(BytePos(15), BytePos(16))
        );
        assert_eq!(branch.callback_span, Span::new(BytePos(17), BytePos(18)));

        let mut saved_json = serde_json::to_value(SavedNativeMutationWitness::snapshot(&witness))
            .expect("saved witness serializes as ordinary Serde");
        saved_json
            .as_object_mut()
            .expect("saved witness is a map")
            .remove("unreachable_branch_spans");
        assert!(serde_json::from_value::<SavedNativeMutationWitness<'static>>(saved_json).is_err());

        let mut mismatched_json =
            serde_json::to_value(SavedNativeMutationWitness::snapshot(&witness)).unwrap();
        mismatched_json
            .as_object_mut()
            .expect("saved witness is a map")
            .insert(
                "unreachable_branch_spans".to_string(),
                serde_json::Value::Null,
            );
        let decoded: SavedNativeMutationWitness<'static> =
            serde_json::from_value(mismatched_json).expect("explicit None spans decode");
        assert!(decoded.restore().is_err());
    }
}
