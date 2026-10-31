//! The spec as the analysis stages read it. The cross-module pass and each
//! chunk's fact analysis take their spec-derived inputs only from an
//! [`AnalysisProjection`], built once the selector solve has run. Its
//! assignment-derived fields hold what the solve resolved premises to
//! (binding names, declarations); module destinations and the logical-module
//! layout do not reach analysis through it.

use std::collections::{BTreeMap, BTreeSet};

use analysis::AnalysisAssumptions;
use spec::{ChunkExportPurity, DependencyGraphOptions, HostBehavior, SourceUnitSelector};

use super::materialize::ResolvedMemberSite;

pub(super) struct AnalysisProjection<'a> {
    /// Analysis reads every field but `commuting_statements` and
    /// `dataflow_aware_s_chain`, which only the chunk graph build reads.
    pub(super) chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
    /// With source-marker export policies already resolved.
    pub(super) chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
    pub(super) host_behavior: &'a HostBehavior,
    /// Per analysed chunk: member and chunk-rename annotations and
    /// `no_reentry_call` premises, keyed by the bindings the selector solve
    /// resolved them to.
    pub(super) declared_assumptions: BTreeMap<String, AnalysisAssumptions>,
    /// The logical-module members that `native_operation_inputs` name
    /// through `resolved_member`, keyed by `(module, name)`, with the
    /// declaration each resolved to. No other member reaches analysis.
    pub(super) premise_members: BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
}

impl<'a> AnalysisProjection<'a> {
    pub(super) fn new(
        chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
        chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
        host_behavior: &'a HostBehavior,
        declared_assumptions: BTreeMap<String, AnalysisAssumptions>,
        resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
    ) -> Self {
        let premise_members = chunk_analysis_options
            .values()
            .flat_map(|options| &options.native_operation_inputs)
            .filter_map(|input| match &input.selector {
                SourceUnitSelector::ResolvedMember(member) => {
                    let key = (member.module.clone(), member.name.clone());
                    let sites = resolved_member_sites.get(&key)?.clone();
                    Some((key, sites))
                }
                SourceUnitSelector::SourceMatch(_) => None,
            })
            .collect();
        Self {
            chunk_analysis_options,
            chunk_export_purity,
            host_behavior,
            declared_assumptions,
            premise_members,
        }
    }

    /// `purity: pure_new` annotations, the only declared assumptions the
    /// cross-module pass reads.
    pub(super) fn declared_pure_new(&self) -> BTreeMap<String, BTreeSet<String>> {
        self.declared_assumptions
            .iter()
            .map(|(chunk, assumptions)| (chunk.clone(), assumptions.declared_pure_new.clone()))
            .collect()
    }
}
