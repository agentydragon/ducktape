//! File-owning result of the single joint final-output assessment.
//!
//! The capability is deliberately separate from the serialized application
//! report: a report can be copied or edited, while these values can only be
//! produced by the final checker that inspected the same immutable output
//! files they retain.

use artifact::ChunkBundle;
use artifact::reports::CandidateSourceCorrespondence;
use js_ast::GlobalUseElisionReceipt;
use source_identity::ChunkId;
use std::collections::BTreeMap;

use crate::assignment_analysis::{
    ApplicationRelocationAssessmentReport, ModuleInitializationRefusal, ProviderTargetRefusal,
};
use crate::final_global_resolution::{
    FinalGlobalResolutionAssessment, FinalGlobalResolutionSourceInputs, GlobalResolutionStatus,
    assess_final_global_resolution,
};
use crate::final_source_correspondence::assess_final_source_correspondence;

/// Seals a candidate report to the exact authored module document map that
/// entered the joint final-output checker. Its constructor is private to this
/// module, so writers cannot turn a report status into candidate identity.
pub(crate) struct FinalCandidateOutputSeal(());

/// Reconcile a report produced by canonical candidate preparation against the
/// exact finalized artifact. This function produces diagnostics only; the
/// file-owning Accepted capability is minted by reconstruction from the
/// private finalized materialization product.
pub fn assess_final_candidate_emission(
    mut assessment: ApplicationRelocationAssessmentReport,
    source_inputs: &FinalGlobalResolutionSourceInputs,
    source_correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    global_use_elisions: &BTreeMap<ChunkId, GlobalUseElisionReceipt>,
) -> ApplicationRelocationAssessmentReport {
    // A public/serialized report is diagnostic input, not preparation
    // authority. Check its private CandidateEvaluation provenance against the
    // exact source snapshot before allowing supplied correspondence or final
    // files to participate in acceptance.
    if !assessment.preparation_matches_source(source_inputs) {
        assessment.mark_preparation_unproven(
            FinalGlobalResolutionAssessment::candidate_preparation_unproven(source_inputs),
        );
        return assessment;
    }

    let route_result = assess_final_source_correspondence(
        source_inputs,
        source_correspondence,
        files,
        &assessment.chunks,
    );
    let mut global = assess_final_global_resolution(source_inputs, files, global_use_elisions);
    global.attach_source_correspondence(&route_result, &source_inputs.source_chunk_names);
    let finalized =
        assessment.finalize_joint_runtime_checks(FinalCandidateOutputSeal(()), global, |reports| {
            for (chunk_name, report) in reports {
                let Some(chunk_id) = source_inputs
                    .source_chunk_names
                    .iter()
                    .find_map(|(id, name)| (name == chunk_name).then_some(*id))
                else {
                    report.apply_joint_runtime_checks(
                        Default::default(),
                        Vec::new(),
                        Some(ModuleInitializationRefusal::PreparedEmissionUnavailable),
                        Vec::new(),
                        Some(ProviderTargetRefusal::PreparedEmissionUnavailable),
                    );
                    continue;
                };
                let required_cell_uses = report.module_initialization.required_cell_uses.clone();
                let preserved_cell_uses = route_result
                    .preserved_cell_uses
                    .iter()
                    .filter_map(|(chunk, unit, cell)| {
                        (*chunk == chunk_id).then_some((*unit, *cell))
                    })
                    .collect::<std::collections::BTreeSet<_>>();
                let cells_complete = required_cell_uses.is_subset(&preserved_cell_uses);
                let required_reads = &report.module_initialization.required_imported_read_facts;
                let preserved_reads = required_reads
                    .iter()
                    .filter(|read| route_result.preserved_imported_reads.contains(read))
                    .cloned()
                    .collect::<Vec<_>>();
                let reads_complete = preserved_reads.len() == required_reads.len();
                let required_providers = &report.provider_targets.required_facts;
                let preserved_providers = required_providers
                    .iter()
                    .filter(|target| route_result.preserved_provider_targets.contains(target))
                    .cloned()
                    .collect::<Vec<_>>();
                let providers_complete = preserved_providers.len() == required_providers.len();
                let route_status = route_result
                    .per_chunk_status
                    .get(&chunk_id)
                    .copied()
                    .unwrap_or(GlobalResolutionStatus::Incomplete);
                let initialization_refusal = (!cells_complete
                    || !reads_complete
                    || route_status != GlobalResolutionStatus::Preserved)
                    .then_some(ModuleInitializationRefusal::ProviderIncompleteAtConsumer);
                let provider_refusal = (!providers_complete
                    || route_status != GlobalResolutionStatus::Preserved)
                    .then_some(ProviderTargetRefusal::ProviderRouteChanged);
                report.apply_joint_runtime_checks(
                    preserved_cell_uses,
                    preserved_reads,
                    initialization_refusal,
                    preserved_providers,
                    provider_refusal,
                );
            }
        });
    if finalized.is_err() {
        // Duplicate finalization or a changed preparation is inspection-only;
        // no old report can authorize files after a failed transaction.
        return assessment;
    }

    if assessment.status
        == crate::assignment_analysis::CandidateRelocationStatus::AcceptedWithinCheckedScope
        && assessment.global_resolution.as_ref().is_some_and(|global| {
            global.status == GlobalResolutionStatus::Preserved
                && global.source_correspondence_status == Some(GlobalResolutionStatus::Preserved)
        })
    {
        assessment.seal_final_output(FinalCandidateOutputSeal(()));
    }
    assessment
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::*;
    use artifact::ChunkBundle;
    use source_identity::ChunkTable;

    #[test]
    fn reconstructed_accepted_report_cannot_authorize_the_same_final_files() {
        // The files are a valid immutable emission object. The report is a
        // public diagnostic reconstructed without CandidateEvaluation's
        // private preparation provenance, then edited to claim acceptance.
        // Neither the public status nor a caller-supplied document identity
        // can turn that pair into an accepted file-owning capability.
        let files = ChunkBundle {
            chunks: Vec::new(),
            chunk_table: ChunkTable::default(),
        };
        let mut report =
            ApplicationRelocationAssessmentReport::from_chunks(&BTreeSet::new(), Vec::new(), None)
                .unwrap();
        report.status =
            crate::assignment_analysis::CandidateRelocationStatus::AcceptedWithinCheckedScope;

        let assessment = assess_final_candidate_emission(
            report,
            &FinalGlobalResolutionSourceInputs::default(),
            &BTreeMap::new(),
            &files,
            &BTreeMap::new(),
        );

        assert_eq!(
            assessment.status,
            crate::assignment_analysis::CandidateRelocationStatus::Incomplete,
        );
    }
}
