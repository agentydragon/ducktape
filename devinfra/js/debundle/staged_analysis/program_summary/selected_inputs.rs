//! Validates analysis source sites and resolves the author inputs they select.

use super::chunk_allowed;
use super::input_resolution::{
    resolve_argument_inputs, resolve_native_operation_inputs,
    resolve_no_accessor_install_callables, resolve_value_inputs,
};
use analysis_projection::{AnalysisNativeOperationInput, AnalysisProjection, AnalysisSourceSites};
use analysis_vocabulary::inputs::{
    ResolvedArgumentInput, ResolvedNoAccessorInstall, SelectedBindingValueInput,
    SelectedNativeOperationInput,
};
use anyhow::{Context, Result, ensure};
use artifact::ChunkBundle;
use cross_module_purity::module_facts::ModulePurityFacts;
use graph_provenance::GraphProvenance;
use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet};

pub(super) fn validate_analysis_source_sites(
    artifact: &ChunkBundle,
    source_sites: &AnalysisSourceSites,
    native_operation_inputs: &BTreeMap<String, Vec<AnalysisNativeOperationInput>>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<()> {
    let mut site_chunks = BTreeSet::new();
    site_chunks.extend(source_sites.value_inputs.keys());
    site_chunks.extend(source_sites.native_call_identity_inputs.keys());
    site_chunks.extend(source_sites.initializer_call_effect_inputs.keys());
    site_chunks.extend(source_sites.argument_inputs.keys());
    site_chunks.extend(source_sites.no_accessor_install_callables.keys());
    site_chunks.extend(source_sites.imported_primitive_array_spread_inputs.keys());
    site_chunks.extend(native_operation_inputs.keys());
    for input in source_sites
        .imported_primitive_array_spread_inputs
        .values()
        .flatten()
    {
        site_chunks.extend(input.operands.iter().map(|operand| &operand.provider_chunk));
    }
    ensure!(
        site_chunks.iter().copied().collect::<BTreeSet<_>>()
            == source_sites.source_sha256.keys().collect::<BTreeSet<_>>(),
        "analysis projection source-site chunks do not match their source digests"
    );
    if site_chunks.is_empty() {
        return Ok(());
    }
    let provenance =
        graph_provenance.context("analysis source sites require parser-input provenance")?;
    for chunk_name in site_chunks {
        let chunk = artifact.chunk_named(chunk_name).with_context(|| {
            format!("analysis source sites name no loaded provider chunk {chunk_name}")
        })?;
        ensure!(
            chunk_allowed(chunk.chunk_id, chunk_filter),
            "analysis source-site provider {chunk_name} is outside the analyzed import closure"
        );
        let source = provenance
            .chunk_sources
            .iter()
            .find(|source| source.chunk_id == *chunk_name)
            .with_context(|| {
                format!("analysis source-site provider {chunk_name} has no parser-input provenance")
            })?;
        let expected = &source_sites.source_sha256[chunk_name];
        ensure!(
            &source.sha256 == expected,
            "analysis source-site provider {chunk_name} has stale parser input: expected {}, found {}",
            expected,
            source.sha256
        );
    }
    Ok(())
}
/// Source-resolved author inputs that callable summaries consume.
pub(super) struct SelectedInputs {
    pub(super) value_inputs: BTreeMap<String, Vec<SelectedBindingValueInput>>,
    pub(super) native_operation_inputs: BTreeMap<String, Vec<SelectedNativeOperationInput>>,
    pub(super) argument_inputs: BTreeMap<String, Vec<ResolvedArgumentInput>>,
    pub(super) no_accessor_install_callables: BTreeMap<String, Vec<ResolvedNoAccessorInstall>>,
}

pub(super) fn resolve_selected_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    projection: &AnalysisProjection<'_>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<SelectedInputs> {
    let chunk_analysis_inputs = projection.chunk_analysis_inputs.as_ref();
    let value_inputs = resolve_value_inputs(
        artifact,
        modules,
        chunk_analysis_inputs,
        &projection.source_sites.value_inputs,
        graph_provenance,
        chunk_filter,
    )?;
    let native_operation_inputs = resolve_native_operation_inputs(
        artifact,
        modules,
        chunk_analysis_inputs,
        graph_provenance,
        chunk_filter,
        &projection.native_operation_inputs,
        &projection.source_sites,
    )?;
    let argument_inputs = resolve_argument_inputs(
        artifact,
        modules,
        chunk_analysis_inputs,
        &projection.source_sites.argument_inputs,
        graph_provenance,
        chunk_filter,
    )?;
    let no_accessor_install_callables = resolve_no_accessor_install_callables(
        artifact,
        modules,
        chunk_analysis_inputs,
        &projection.source_sites.no_accessor_install_callables,
        chunk_filter,
    )?;
    Ok(SelectedInputs {
        value_inputs,
        native_operation_inputs,
        argument_inputs,
        no_accessor_install_callables,
    })
}
