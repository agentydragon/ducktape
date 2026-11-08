//! Resolve spec selectors and source-bound assumptions before graph planning.
//!
//! This target deliberately has no dependency on lowering. It returns the
//! exact joint selector result that both analysis projection and materializing
//! callers consume.

use crate::chunk_ast::{ChunkAstAnalysis, analyze_chunk_ast, binding_declaration};
use crate::source_ast::RuntimeImportKind;
use analysis::AnalysisAssumptions;
use analysis::analysis_inputs::{NativeOperationSelectorProvenance, SourceDeclaration};
use analysis::facts::StructuralChunkSourceFacts;
use analysis_projection::{
    self, AnalysisNativeOperationInput, AnalysisProjection, AnalysisSourceSites, AnalysisSpec,
    ChunkGraphInputs, ProjectedChunkOptions,
};
use anyhow::{Context, Result, bail, ensure};
use artifact::{
    ArtifactIndexes, ChunkBundle, chunk_runtime_entry, export_policies, normalized_chunk_ids,
    prune_artifact_to_chunk_ids,
};
use graph_provenance::GraphProvenance;
use rayon::prelude::*;
use selector_outcome::{Entity, EntityRef, Outcome, SelectorOutcome, Severity};
use selector_resolve::{Chunk as SelectorChunk, EntityIndex, MemberSelector};
use source_identity::ChunkId;
use source_identity::StatementId;
use source_match::source_match_claim_member_selectors;
use spec::{
    ChunkExportPurity, ChunkRenames, DependencyGraphOptions, HostBehavior, IntrinsicsNeverWritten,
    LogicalModule, SourceMatchClaim, SourceUnitSelector, UnassignedMode,
};
use spec_resolution::{
    LogicalRequest, ProjectedChunk, ResolvedChunk, logical_requests_for_chunk, project_chunk,
};
use std::collections::{BTreeMap, BTreeSet};
use swc_common::GLOBALS;
use swc_ecma_ast::{Module, ModuleItem};

pub struct FrontendChunkInput<'a> {
    pub artifact: &'a ChunkBundle,
    pub chunk_id: &'a str,
    pub file: Option<&'a str>,
}

pub struct FrontendChunk<'a> {
    pub chunk_id: String,
    pub chunk_id_interned: ChunkId,
    pub target_file: String,
    pub runtime_ast: &'a js_ast::ParsedJsModule,
    pub header_lines: Vec<String>,
    pub source_path: String,
    pub ast_analysis: ChunkAstAnalysis,
    pub requests: Vec<LogicalRequest>,
    pub explicit_requests: Vec<LogicalRequest>,
    pub structural_analysis: StructuralChunkSourceFacts<'a>,
    pub resolution: selector_resolve::Resolution,
    pub resolved_members: Vec<Vec<usize>>,
    pub member_claims: MemberClaimProjection,
    pub declared_assumptions: AnalysisAssumptions,
    pub source_only_initializer_bindings: BTreeSet<String>,
}

#[derive(Debug, Clone)]
pub struct ResolvedMemberSite {
    pub provider_chunk: String,
    pub module: String,
    pub name: String,
    pub declaration: Option<SourceDeclaration>,
}

/// Canonical binding ownership after the joint selector solve. Lowering applies
/// these same decisions when it mutates output plans; analysis projection uses
/// them to resolve layout-relative member premises.
#[derive(Debug, Default)]
pub struct MemberClaimProjection {
    pub owned_bindings: BTreeSet<String>,
    pub claimed_members: BTreeMap<(usize, usize), String>,
    pub claimed_anonymous_statements: BTreeMap<(usize, usize), StatementId>,
}

impl FrontendChunk<'_> {
    pub fn resolved_member_sites(&self) -> Vec<ResolvedMemberSite> {
        let mut sites = Vec::new();
        for (request_index, request) in self.explicit_requests.iter().enumerate() {
            for (member_index, member) in request.members.iter().enumerate() {
                let binding = self
                    .member_claims
                    .claimed_members
                    .get(&(request_index, member_index))
                    .cloned();
                let declaration = binding.and_then(|binding| {
                    let id = analysis::top_level_id(&binding, self.runtime_ast.top_level_mark);
                    if !self.member_claims.owned_bindings.contains(&binding) {
                        return None;
                    }
                    let statements = self.ast_analysis.declaration_by_name.get(&id)?;
                    if statements.len() != 1 {
                        return None;
                    }
                    Some(SourceDeclaration {
                        binding,
                        body_index: statements[0].0,
                    })
                });
                sites.push(ResolvedMemberSite {
                    provider_chunk: self.chunk_id.clone(),
                    module: request.target_path.clone(),
                    name: member.export_name.clone(),
                    declaration,
                });
            }
        }
        sites
    }
}

pub struct FrontendElaboration<'a> {
    pub chunks: Vec<FrontendChunk<'a>>,
    pub projection: AnalysisProjection<'a>,
    pub graph_inputs: BTreeMap<String, ChunkGraphInputs>,
    pub resolved_member_sites: BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
}

pub struct FrontendSpecInputs<'a> {
    pub logical_modules: &'a BTreeMap<String, BTreeMap<String, LogicalModule>>,
    pub chunk_renames: &'a BTreeMap<String, ChunkRenames>,
    pub unassigned_mode: &'a BTreeMap<String, UnassignedMode>,
    pub chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
    pub graph_provenance: Option<&'a GraphProvenance>,
    pub chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
    pub host_behavior: &'a HostBehavior,
    pub intrinsics_never_written: Option<&'a IntrinsicsNeverWritten>,
    pub fail_fast: bool,
    pub report_warnings: bool,
}

/// Build the canonical project-analysis artifact directly from source-level
/// inputs. This path performs selector resolution and premise collection but
/// never constructs output plans or enters lowering.
pub fn project_analysis(
    mut artifact: ChunkBundle,
    spec: &spec::TransformSpec,
    base: &AnalysisSpec,
    graph_provenance: Option<&GraphProvenance>,
) -> Result<AnalysisSpec> {
    if !spec.materialize_logical_modules.target_dir.is_empty() {
        artifact::paths::normalize_module_path(&spec.materialize_logical_modules.target_dir)?;
    }
    let chunk_ids =
        normalized_chunk_ids(&spec.materialize_chunk_ids().into_iter().collect::<Vec<_>>())?;
    ensure!(
        !chunk_ids.is_empty(),
        "project_analysis requires at least one chunk_id"
    );
    let selected: BTreeSet<&str> = chunk_ids.iter().map(String::as_str).collect();
    for (chunk, options) in &spec.chunk_analysis_options {
        ensure!(
            options.commuting_statements.is_empty()
                || selected.contains(artifact::paths::normalize_module_path(chunk)?.as_str()),
            "chunk_analysis_options.{chunk}.commuting_statements names a chunk this run does not materialize"
        );
    }
    let chunk_export_purity =
        export_policies::resolve_source_export_policies(&artifact, &spec.chunk_export_purity)?;
    if spec.materialize_logical_modules.prune_other_chunks {
        prune_artifact_to_chunk_ids(&mut artifact, &chunk_ids);
    }
    let inputs = chunk_ids
        .iter()
        .map(|chunk_id| FrontendChunkInput {
            artifact: &artifact,
            chunk_id,
            file: spec.materialize_logical_modules.file.as_deref(),
        })
        .collect();
    let frontend = elaborate(
        inputs,
        FrontendSpecInputs {
            logical_modules: &spec.logical_modules,
            chunk_renames: &spec.chunk_renames,
            unassigned_mode: &spec.unassigned_mode,
            chunk_analysis_options: &spec.chunk_analysis_options,
            graph_provenance,
            chunk_export_purity: chunk_export_purity.as_ref(),
            host_behavior: &spec.host_behavior,
            intrinsics_never_written: spec.program_premises.intrinsics_never_written.as_ref(),
            fail_fast: true,
            report_warnings: true,
        },
    )?;
    complete_analysis_spec(base, &frontend.projection)
}

pub fn elaborate<'a>(
    inputs: Vec<FrontendChunkInput<'a>>,
    spec: FrontendSpecInputs<'a>,
) -> Result<FrontendElaboration<'a>> {
    let prepared_artifact = inputs
        .first()
        .map(|input| input.artifact)
        .context("frontend elaboration requires at least one chunk")?;
    ensure!(
        inputs
            .iter()
            .all(|input| std::ptr::eq(input.artifact, prepared_artifact)),
        "frontend elaboration inputs must share one prepared artifact"
    );
    let selected_chunk_ids = inputs
        .iter()
        .map(|input| input.chunk_id.to_string())
        .collect::<Vec<_>>();
    let mut prepared = GLOBALS
        .with(|globals| {
            inputs
                .into_par_iter()
                .map(|input| GLOBALS.set(globals, || prepare_chunk(input, &spec)))
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<Vec<_>>>()?;
    let projected = prepared
        .iter_mut()
        .map(|chunk| {
            chunk
                .projected
                .take()
                .expect("prepared projection is present")
        })
        .collect();
    let resolutions = spec_resolution::solve_projected_chunks(projected)?;
    let chunks = prepared
        .into_iter()
        .zip(resolutions)
        .map(|(chunk, resolved)| {
            finish_frontend_chunk(
                chunk,
                resolved,
                spec.chunk_renames,
                spec.fail_fast,
                spec.report_warnings,
            )
        })
        .collect::<Result<Vec<_>>>()?;

    let declared_assumptions = chunks
        .iter()
        .map(|chunk| (chunk.chunk_id.clone(), chunk.declared_assumptions.clone()))
        .collect();
    let mut resolved_member_sites = BTreeMap::new();
    for chunk in &chunks {
        for site in chunk.resolved_member_sites() {
            resolved_member_sites
                .entry((site.module.clone(), site.name.clone()))
                .or_insert_with(Vec::new)
                .push(site);
        }
    }
    let projected_options =
        analysis_projection::project_chunk_options(spec.chunk_analysis_options)?;
    let graph_inputs = projected_options
        .iter()
        .map(|(chunk, options)| (chunk.clone(), options.graph.clone()))
        .collect();
    let (source_sites, native_operation_inputs) = resolve_analysis_source_sites(
        prepared_artifact,
        &selected_chunk_ids,
        &projected_options,
        &resolved_member_sites,
        spec.graph_provenance,
    )?;
    let projection = AnalysisProjection::new(
        &projected_options,
        spec.chunk_export_purity,
        spec.host_behavior,
        spec.intrinsics_never_written,
        declared_assumptions,
        source_sites,
        native_operation_inputs,
    )?;
    Ok(FrontendElaboration {
        chunks,
        projection,
        graph_inputs,
        resolved_member_sites,
    })
}

fn resolve_analysis_source_sites(
    artifact: &ChunkBundle,
    selected_chunk_ids: &[String],
    projected_options: &BTreeMap<String, ProjectedChunkOptions>,
    resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
    graph_provenance: Option<&GraphProvenance>,
) -> Result<(
    AnalysisSourceSites,
    BTreeMap<String, Vec<AnalysisNativeOperationInput>>,
)> {
    let indexes = ArtifactIndexes::build(artifact)?;
    let provider_closure = analysis_projection::selected_static_import_closure(
        artifact,
        &indexes,
        selected_chunk_ids,
    )?;
    let mut source_sites = AnalysisSourceSites::default();
    let mut native_operation_inputs = BTreeMap::new();

    for (chunk_name, options) in projected_options {
        let has_source_premises = !options.native_operation_inputs.is_empty()
            || !options.imported_primitive_array_spread_inputs.is_empty()
            || !options.analysis.value_inputs.is_empty()
            || !options.analysis.initializer_call_effect_inputs.is_empty()
            || !options.analysis.native_call_identity_inputs.is_empty()
            || !options.analysis.argument_inputs.is_empty()
            || !options.analysis.no_accessor_install_callables.is_empty();
        if !has_source_premises {
            continue;
        }

        let prefix = format!("chunk_analysis_options.{chunk_name}");
        let chunk = artifact
            .chunks
            .iter()
            .find(|chunk| artifact.chunk_table.name(chunk.chunk_id) == chunk_name)
            .with_context(|| format!("{prefix} names no loaded provider chunk"))?;
        if !provider_closure.contains(&chunk.chunk_id) {
            if !options.analysis.value_inputs.is_empty() {
                bail!(
                    "chunk_analysis_options.{chunk_name}.value_inputs provider is outside the existing analyzed import closure"
                );
            }
            if !options.analysis.initializer_call_effect_inputs.is_empty() {
                bail!(
                    "chunk_analysis_options.{chunk_name}.initializer_call_effect_inputs importer is outside the existing analyzed import closure"
                );
            }
            if !options.native_operation_inputs.is_empty()
                || !options.imported_primitive_array_spread_inputs.is_empty()
                || !options.analysis.native_call_identity_inputs.is_empty()
            {
                bail!(
                    "chunk_analysis_options.{chunk_name}.native_operation_inputs provider is outside the existing analyzed import closure"
                );
            }
            if !options.analysis.argument_inputs.is_empty() {
                bail!(
                    "chunk_analysis_options.{chunk_name}.argument_inputs cannot target an outside-closure provider"
                );
            }
            bail!(
                "chunk_analysis_options.{chunk_name}.no_accessor_install_callables cannot target an outside-closure provider"
            );
        }
        let source = graph_provenance
            .context("source premise sites require parser-input provenance")?
            .chunk_sources
            .iter()
            .find(|source| source.chunk_id == *chunk_name)
            .with_context(|| format!("{prefix} provider has no parser-input provenance"))?;
        source_sites
            .source_sha256
            .insert(chunk_name.clone(), source.sha256.clone());
        for (index, contract) in options
            .analysis
            .initializer_call_effect_inputs
            .iter()
            .enumerate()
        {
            let request = format!(
                "chunk_analysis_options.{chunk_name}.initializer_call_effect_inputs[{index}]"
            );
            let provider = artifact
                .chunks
                .iter()
                .find(|candidate| {
                    artifact.chunk_table.name(candidate.chunk_id) == contract.provider_chunk
                })
                .with_context(|| format!("{request}.provider_chunk names no loaded chunk"))?;
            ensure!(
                provider_closure.contains(&provider.chunk_id),
                "{request}.provider_chunk is outside the analyzed import closure"
            );
        }
        let entry_file = chunk
            .js
            .get_file(&chunk.analysis.entry_file)
            .with_context(|| {
                format!("{prefix} provider entry is unavailable to whole-program analysis")
            })?;

        let parsed = entry_file.ast().with_context(|| {
            format!("{prefix} provider entry has no retained source-premise AST")
        })?;
        let resolved = resolve_provider_source_sites(
            chunk_name,
            &parsed.module,
            options,
            resolved_member_sites,
        )?;
        let ProviderSourceSites {
            value_inputs,
            native_call_identity_inputs,
            initializer_call_effect_inputs,
            argument_inputs,
            no_accessor_install_callables,
            native_operation_inputs: operations,
        } = resolved;
        let (imported_array_spreads, provider_digests) = resolve_imported_array_spread_inputs(
            artifact,
            &provider_closure,
            chunk_name,
            &parsed.module,
            &options.imported_primitive_array_spread_inputs,
            &source.sha256,
            graph_provenance,
        )?;
        source_sites.source_sha256.extend(provider_digests);
        if !imported_array_spreads.is_empty() {
            source_sites
                .imported_primitive_array_spread_inputs
                .insert(chunk_name.clone(), imported_array_spreads);
        }
        if !value_inputs.is_empty() {
            source_sites
                .value_inputs
                .insert(chunk_name.clone(), value_inputs);
        }
        if !native_call_identity_inputs.is_empty() {
            source_sites
                .native_call_identity_inputs
                .insert(chunk_name.clone(), native_call_identity_inputs);
        }
        if !initializer_call_effect_inputs.is_empty() {
            source_sites
                .initializer_call_effect_inputs
                .insert(chunk_name.clone(), initializer_call_effect_inputs);
        }
        if !argument_inputs.is_empty() {
            source_sites
                .argument_inputs
                .insert(chunk_name.clone(), argument_inputs);
        }
        if !no_accessor_install_callables.is_empty() {
            source_sites
                .no_accessor_install_callables
                .insert(chunk_name.clone(), no_accessor_install_callables);
        }
        if !operations.is_empty() {
            native_operation_inputs.insert(chunk_name.clone(), operations);
        }
    }

    Ok((source_sites, native_operation_inputs))
}

fn resolve_imported_array_spread_inputs(
    artifact: &ChunkBundle,
    provider_closure: &BTreeSet<ChunkId>,
    consumer_chunk: &str,
    consumer_module: &Module,
    contracts: &[spec::ImportedPrimitiveArraySpreadInputContract],
    consumer_source_sha256: &str,
    graph_provenance: Option<&GraphProvenance>,
) -> Result<(
    Vec<analysis_projection::AnalysisImportedPrimitiveArraySpreadInput>,
    BTreeMap<String, String>,
)> {
    if contracts.is_empty() {
        return Ok((Vec::new(), BTreeMap::new()));
    }
    let consumer_request =
        format!("chunk_analysis_options.{consumer_chunk}.imported_primitive_array_spread_inputs");
    let mut sites = Vec::with_capacity(contracts.len());
    let mut known_chunks = BTreeSet::from([consumer_chunk.to_string()]);
    for (input_index, contract) in contracts.iter().enumerate() {
        let request = format!("{consumer_request}[{input_index}]");
        anyhow::ensure!(
            !contract.operands.is_empty(),
            "{request}.operands must contain at least one direct imported operand"
        );
        anyhow::ensure!(
            contract.lifetime.identity_unchanged_through_spread
                && contract
                    .lifetime
                    .dense_own_data_primitive_contents_unchanged_through_spread
                && contract
                    .lifetime
                    .ordinary_array_prototype_chain_unchanged_through_spread
                && contract
                    .lifetime
                    .no_own_iterator_or_accessors_through_spread
                && contract.lifetime.no_rebinding_through_spread,
            "{request}.lifetime must explicitly affirm every exact-spread lifetime premise"
        );
        anyhow::ensure!(
            contract.audited_source_sha256 == consumer_source_sha256,
            "{request} has stale audited_source_sha256"
        );
        let consumer_selector = SelectorChunk::new(
            consumer_chunk,
            consumer_module,
            analysis::facts::collect_chunk_source_facts(consumer_module, None, |_| None),
        );
        let consumer_declaration =
            select_one_source_declaration(&consumer_selector, &request, &contract.source_match)?;
        let selector_list = source_match_claim_member_selectors(&request, &contract.source_match)?;
        let mut matching_free_bindings = Vec::new();
        for selector in &selector_list {
            matching_free_bindings.extend(
                consumer_selector
                    .matcher()
                    .member_candidates(&request, &selector.parsed_selector)?
                    .into_iter()
                    .filter(|candidate| {
                        candidate.body_idx == consumer_declaration.body_index
                            && candidate.binding.binding_name == consumer_declaration.binding
                    })
                    .map(|candidate| candidate.free_bindings),
            );
        }
        let [free_bindings] = matching_free_bindings.as_slice() else {
            anyhow::bail!(
                "{request}.source_match must uniquely capture the selected consumer's direct operands"
            );
        };
        let mut selector_locals = BTreeSet::new();
        let mut operands = Vec::with_capacity(contract.operands.len());
        for operand in &contract.operands {
            anyhow::ensure!(
                selector_locals.insert(operand.selector_local.clone()),
                "{request}.operands repeats selector_local `{}`",
                operand.selector_local
            );
            let consumer_binding = free_bindings
                .get(&operand.selector_local)
                .with_context(|| {
                    format!(
                        "{request}.operands selector_local `{}` is not a free identifier in source_match",
                        operand.selector_local
                    )
                })?
                .clone();
            let provider_id = artifact
                .chunk_table
                .get(&operand.provider_chunk)
                .with_context(|| {
                    format!(
                        "{request} names no loaded provider chunk alias `{}`",
                        operand.provider_chunk
                    )
                })?;
            anyhow::ensure!(
                provider_closure.contains(&provider_id),
                "{request} provider {} is outside the analyzed static import closure",
                operand.provider_chunk
            );
            let provider_chunk = artifact
                .find_chunk(provider_id)
                .with_context(|| format!("{request} provider chunk is unavailable"))?;
            let provider_source = graph_provenance
                .context("imported array source selectors require parser-input provenance")?
                .chunk_sources
                .iter()
                .find(|source| source.chunk_id == operand.provider_chunk)
                .with_context(|| {
                    format!(
                        "{request} provider {} has no parser-input provenance",
                        operand.provider_chunk
                    )
                })?;
            anyhow::ensure!(
                operand.audited_provider_source_sha256 == provider_source.sha256,
                "{request} provider {} has stale audited_provider_source_sha256",
                operand.provider_chunk
            );
            let provider_entry = provider_chunk
                .js
                .get_file(&provider_chunk.analysis.entry_file)
                .and_then(|file| file.ast())
                .with_context(|| {
                    format!(
                        "{request} provider {} has no retained source-premise AST",
                        operand.provider_chunk
                    )
                })?;
            let provider_selector = SelectorChunk::new(
                &operand.provider_chunk,
                &provider_entry.module,
                analysis::facts::collect_chunk_source_facts(&provider_entry.module, None, |_| None),
            );
            let provider_declaration = select_one_source_declaration(
                &provider_selector,
                &format!("{request}.operands.provider_source_match"),
                &operand.provider_source_match,
            )?;
            known_chunks.insert(operand.provider_chunk.clone());
            operands.push(analysis_projection::AnalysisImportedPrimitiveArrayOperand {
                selector_local: operand.selector_local.clone(),
                consumer_binding,
                provider_chunk: operand.provider_chunk.clone(),
                provider_source_match: operand.provider_source_match.clone(),
                provider_declaration,
                audited_provider_source_sha256: operand.audited_provider_source_sha256.clone(),
            });
        }
        sites.push(
            analysis_projection::AnalysisImportedPrimitiveArraySpreadInput {
                consumer_declaration,
                source_match: contract.source_match.clone(),
                audited_source_sha256: contract.audited_source_sha256.clone(),
                operands,
                lifetime: contract.lifetime.clone(),
                note: contract.note.clone(),
            },
        );
    }
    let sources = graph_provenance
        .context("imported array source selectors require parser-input provenance")?;
    let mut provider_digests = BTreeMap::new();
    for chunk in known_chunks {
        let source = sources
            .chunk_sources
            .iter()
            .find(|source| source.chunk_id == chunk)
            .with_context(|| {
                format!("{consumer_request} chunk {chunk} has no parser-input provenance")
            })?;
        if chunk != consumer_chunk {
            provider_digests.insert(chunk, source.sha256.clone());
        }
    }
    Ok((sites, provider_digests))
}

struct ProviderSourceSites {
    value_inputs: Vec<SourceDeclaration>,
    native_call_identity_inputs: Vec<SourceDeclaration>,
    initializer_call_effect_inputs: Vec<SourceDeclaration>,
    argument_inputs: Vec<SourceDeclaration>,
    no_accessor_install_callables: Vec<SourceDeclaration>,
    native_operation_inputs: Vec<AnalysisNativeOperationInput>,
}

fn resolve_provider_source_sites(
    chunk_name: &str,
    module: &Module,
    options: &ProjectedChunkOptions,
    resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
) -> Result<ProviderSourceSites> {
    let structural = analysis::facts::collect_chunk_source_facts(module, None, |_| None);
    let selector_chunk = SelectorChunk::new(chunk_name, module, structural);
    let mut resolved = ProviderSourceSites {
        value_inputs: Vec::with_capacity(options.analysis.value_inputs.len()),
        native_call_identity_inputs: Vec::with_capacity(
            options.analysis.native_call_identity_inputs.len(),
        ),
        initializer_call_effect_inputs: Vec::with_capacity(
            options.analysis.initializer_call_effect_inputs.len(),
        ),
        argument_inputs: Vec::with_capacity(options.analysis.argument_inputs.len()),
        no_accessor_install_callables: Vec::with_capacity(
            options.analysis.no_accessor_install_callables.len(),
        ),
        native_operation_inputs: Vec::with_capacity(options.native_operation_inputs.len()),
    };

    for (index, contract) in options.analysis.value_inputs.iter().enumerate() {
        let request_id = format!("chunk_analysis_options.{chunk_name}.value_inputs[{index}]");
        resolved.value_inputs.push(select_one_source_declaration(
            &selector_chunk,
            &request_id,
            &contract.source_match,
        )?);
    }
    for (index, contract) in options
        .analysis
        .native_call_identity_inputs
        .iter()
        .enumerate()
    {
        let request_id =
            format!("chunk_analysis_options.{chunk_name}.native_call_identity_inputs[{index}]");
        resolved
            .native_call_identity_inputs
            .push(select_one_source_declaration(
                &selector_chunk,
                &request_id,
                &contract.source_match,
            )?);
    }
    for (index, contract) in options
        .analysis
        .initializer_call_effect_inputs
        .iter()
        .enumerate()
    {
        let request_id =
            format!("chunk_analysis_options.{chunk_name}.initializer_call_effect_inputs[{index}]");
        // Reuse the chunk's joint logical-member claim projection rather than
        // resolving a second selector for each initializer input.
        let site = resolve_premise_member_site(
            resolved_member_sites,
            &request_id,
            chunk_name,
            &contract.resolved_member.module,
            &contract.resolved_member.name,
        )?;
        let declaration = site.declaration.clone().with_context(|| {
            format!("{request_id}.resolved_member has no unique original local declaration")
        })?;
        resolved.initializer_call_effect_inputs.push(declaration);
    }
    for (index, contract) in options.analysis.argument_inputs.iter().enumerate() {
        let request_id = format!("chunk_analysis_options.{chunk_name}.argument_inputs[{index}]");
        resolved.argument_inputs.push(select_one_source_declaration(
            &selector_chunk,
            &request_id,
            &contract.source_match,
        )?);
    }
    for (index, contract) in options
        .analysis
        .no_accessor_install_callables
        .iter()
        .enumerate()
    {
        let request_id =
            format!("chunk_analysis_options.{chunk_name}.no_accessor_install_callables[{index}]");
        resolved
            .no_accessor_install_callables
            .push(select_one_source_declaration(
                &selector_chunk,
                &request_id,
                &contract.source_match,
            )?);
    }
    for (index, contract) in options.native_operation_inputs.iter().enumerate() {
        let request_id =
            format!("chunk_analysis_options.{chunk_name}.native_operation_inputs[{index}]");
        let (declaration, selector) = match &contract.selector {
            SourceUnitSelector::SourceMatch(source_match) => {
                let declaration =
                    select_one_source_declaration(&selector_chunk, &request_id, source_match)?;
                (
                    declaration,
                    NativeOperationSelectorProvenance::SourceMatch(source_match.clone()),
                )
            }
            SourceUnitSelector::ResolvedMember(member) => {
                let site = resolve_premise_member_site(
                    resolved_member_sites,
                    &request_id,
                    chunk_name,
                    &member.module,
                    &member.name,
                )?;
                let declaration = site.declaration.clone().with_context(|| {
                    format!("{request_id}.resolved_member has no unique original local declaration")
                })?;
                (
                    declaration.clone(),
                    NativeOperationSelectorProvenance::Declaration(declaration),
                )
            }
        };
        resolved
            .native_operation_inputs
            .push(AnalysisNativeOperationInput {
                declaration,
                selector,
                operation: contract.operation,
                audited_source_sha256: contract.audited_source_sha256.clone(),
            });
    }

    Ok(resolved)
}

fn resolve_premise_member_site<'a>(
    resolved_member_sites: &'a BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
    request_id: &str,
    chunk_name: &str,
    module: &str,
    name: &str,
) -> Result<&'a ResolvedMemberSite> {
    let key = (module.to_owned(), name.to_owned());
    let sites = resolved_member_sites.get(&key).with_context(|| {
        format!(
            "{request_id}.resolved_member names no logical module member in chunk {chunk_name} (module {module}, member {name}); the reference may be stale after an owner rename or move"
        )
    })?;
    let mut chunk_sites = sites
        .iter()
        .filter(|site| site.provider_chunk == chunk_name);
    let site = chunk_sites.next().with_context(|| {
        let providers: BTreeSet<_> = sites
            .iter()
            .map(|site| site.provider_chunk.as_str())
            .collect();
        format!(
            "{request_id}.resolved_member belongs to another chunk(s) {providers:?}, not requested chunk {chunk_name} (module {module}, member {name})"
        )
    })?;
    ensure!(
        chunk_sites.next().is_none(),
        "{request_id}.resolved_member is ambiguous in chunk {chunk_name} (module {module}, member {name})"
    );
    Ok(site)
}

pub fn select_one_declared_binding(
    selector_chunk: &SelectorChunk<'_>,
    request_id: &str,
    claim: &SourceMatchClaim,
) -> Result<(usize, String)> {
    ensure!(
        claim.bindings.len() == 1,
        "{request_id}.source_match must select exactly one declared binding"
    );
    let spec::SourceMatchBinding::Local(local) = &claim.bindings[0] else {
        bail!("{request_id}.source_match does not accept detailed name/rename payloads");
    };
    let mut selectors = source_match_claim_member_selectors(request_id, claim)?;
    ensure!(
        selectors.len() == 1,
        "{request_id}.source_match must produce one selector"
    );
    let selector = selectors.pop().expect("one selector was checked");
    ensure!(
        selector
            .parsed_selector
            .declared_binding_names()
            .iter()
            .any(|name| name == local),
        "{request_id}.source_match binding `{local}` must be declared by its pattern"
    );
    let parsed_selector = selector.parsed_selector.clone();
    let selector_module = spec_resolution::spec_module(
        request_id.to_owned(),
        vec![spec_resolution::member_from_source_match(
            selector.export_name,
            selector.parsed_selector,
        )],
        Vec::new(),
    );
    let resolution = selector_chunk.resolve(&[selector_module])?;
    let outcome = resolution
        .outcome(0, EntityIndex::Member(0))
        .with_context(|| format!("{request_id}.source_match produced no selector outcome"))?;
    let (representative, binding) = match &outcome.outcome {
        Outcome::Resolved {
            body_index,
            binding: Some(binding),
            ..
        } => (*body_index, binding.clone()),
        other => bail!(
            "{request_id}.source_match must select exactly one declared binding; selector result: {other:?}"
        ),
    };
    let mut sites = selector_chunk
        .matcher()
        .member_candidates(request_id, &parsed_selector)?
        .into_iter()
        .filter(|matched| matched.binding.binding_name == binding)
        .map(|matched| matched.body_idx)
        .collect::<BTreeSet<_>>();
    match sites.len() {
        1 => Ok((sites.pop_first().expect("one site"), binding)),
        0 => bail!(
            "{request_id}.source_match resolved `{binding}` to statement #{representative} but its template matches none of the binding's declaration sites"
        ),
        _ => bail!(
            "{request_id}.source_match matches `{binding}` at several declaration sites (statements {}); a source-unit endpoint must pin one",
            sites
                .iter()
                .map(|site| format!("#{site}"))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn select_one_source_declaration(
    selector_chunk: &SelectorChunk<'_>,
    request_id: &str,
    claim: &SourceMatchClaim,
) -> Result<SourceDeclaration> {
    let (body_index, binding) = select_one_declared_binding(selector_chunk, request_id, claim)?;
    Ok(SourceDeclaration {
        binding,
        body_index,
    })
}

struct PreparedChunk<'a> {
    chunk_id: String,
    chunk_id_interned: ChunkId,
    target_file: String,
    runtime_ast: &'a js_ast::ParsedJsModule,
    header_lines: Vec<String>,
    source_path: String,
    ast_analysis: ChunkAstAnalysis,
    requests: Vec<LogicalRequest>,
    explicit_requests: Vec<LogicalRequest>,
    projected: Option<ProjectedChunk<'a>>,
}

fn prepare_chunk<'a>(
    input: FrontendChunkInput<'a>,
    spec: &FrontendSpecInputs<'_>,
) -> Result<PreparedChunk<'a>> {
    let FrontendChunkInput {
        artifact,
        chunk_id,
        file,
    } = input;
    let chunk_unassigned_mode = spec
        .unassigned_mode
        .get(chunk_id)
        .cloned()
        .with_context(|| {
            format!("materialize_logical_modules missing unassigned_mode for chunk: {chunk_id}")
        })?;
    let chunk_id_interned = artifact
        .chunk_table
        .get(chunk_id)
        .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
    let (target_file, runtime_file, runtime_ast) =
        chunk_runtime_entry(artifact, chunk_id, chunk_id_interned, file)?;
    let source_path = runtime_file.metadata.source_path.clone();
    let ast_analysis = analyze_chunk_ast(&runtime_ast.module);
    let requests = logical_requests_for_chunk(
        spec.logical_modules.get(chunk_id),
        &chunk_unassigned_mode,
        chunk_id,
    )?;
    let explicit_requests = requests
        .iter()
        .filter(|request| !request.residual)
        .cloned()
        .collect::<Vec<_>>();
    for request in &explicit_requests {
        spec_resolution::reject_duplicate_member_bindings(
            "logical_module",
            &request.id,
            &request.members,
        )?;
        for member in &request.members {
            if member.selector.is_import_specifier() {
                validate_named_import_member(member, &ast_analysis)?;
            }
        }
    }
    let line_index = runtime_ast.line_index();
    let structural_analysis = analysis::facts::collect_chunk_source_facts(
        &runtime_ast.module,
        Some(&source_path),
        |span| line_index.line_range_and_start_column_for_span(span),
    );
    let selectors =
        selector_resolve::Chunk::new(chunk_id, &runtime_ast.module, structural_analysis);
    let resolved_members = eligible_member_indices(
        chunk_id,
        &explicit_requests,
        &ast_analysis,
        runtime_ast.top_level_mark,
        &runtime_ast.module.body,
        spec.fail_fast,
    )?;
    let projected = project_chunk(selectors, &explicit_requests, resolved_members)?;
    Ok(PreparedChunk {
        chunk_id: chunk_id.to_string(),
        chunk_id_interned,
        target_file,
        runtime_ast,
        header_lines: runtime_file.header_lines.clone(),
        source_path,
        ast_analysis,
        requests,
        explicit_requests,
        projected: Some(projected),
    })
}

fn eligible_member_indices(
    chunk_id: &str,
    requests: &[LogicalRequest],
    ast: &ChunkAstAnalysis,
    top_level_mark: swc_common::Mark,
    body: &[ModuleItem],
    fail_fast: bool,
) -> Result<Vec<Vec<usize>>> {
    let mut existing_binding_claims = BTreeMap::<String, (usize, usize)>::new();
    let mut eligible_by_request = Vec::with_capacity(requests.len());
    for (request_index, request) in requests.iter().enumerate() {
        let mut eligible = Vec::new();
        for (member_index, member) in request.members.iter().enumerate() {
            if !member.binding.is_empty()
                && let Some(&(owner_request_index, owner_member_index)) =
                    existing_binding_claims.get(&member.binding)
            {
                if fail_fast {
                    let declaration = binding_declaration(
                        body,
                        &ast.declaration_by_name,
                        &analysis::top_level_id(&member.binding, top_level_mark),
                    )?;
                    let owner_request = &requests[owner_request_index];
                    let owner_member = &owner_request.members[owner_member_index];
                    let duplicate = selector_resolve::member_outcome(
                        chunk_id,
                        &request.target_path,
                        &member.export_name,
                        &member.selector,
                        Outcome::DuplicateClaim {
                            binding: member.binding.clone(),
                            declaration,
                            claimed_by: EntityRef {
                                logical_module: owner_request.target_path.clone(),
                                entity: Some(Entity::Export(owner_member.export_name.clone())),
                            },
                        },
                    );
                    bail!("{}", duplicate.render_line());
                }
                continue;
            }
            if member.resolves_after_chunk_analysis() {
                let binding_id = analysis::top_level_id(&member.binding, top_level_mark);
                if matches!(member.selector, MemberSelector::Binding(_))
                    && !ast.declaration_by_name.contains_key(&binding_id)
                {
                    continue;
                }
                if !member.binding.is_empty() {
                    existing_binding_claims
                        .insert(member.binding.clone(), (request_index, member_index));
                }
                eligible.push(member_index);
            } else if !member.binding.is_empty() {
                existing_binding_claims
                    .insert(member.binding.clone(), (request_index, member_index));
            }
        }
        eligible_by_request.push(eligible);
    }
    Ok(eligible_by_request)
}

fn validate_named_import_member(
    member: &spec_resolution::MemberRequest,
    ast: &ChunkAstAnalysis,
) -> Result<()> {
    let Some(info) = ast.runtime_import_facts.lookup_by_sym(&member.binding) else {
        bail!(
            "no import specifier found for `{}` in source chunk",
            member.binding
        );
    };
    if !matches!(info.kind, RuntimeImportKind::Named { .. }) {
        bail!(
            "no named import specifier found for `{}` in source chunk",
            member.binding
        );
    }
    Ok(())
}

fn finish_frontend_chunk<'a>(
    prepared: PreparedChunk<'a>,
    resolved: ResolvedChunk<'a>,
    chunk_renames: &BTreeMap<String, ChunkRenames>,
    fail_fast: bool,
    report_warnings: bool,
) -> Result<FrontendChunk<'a>> {
    let ResolvedChunk {
        resolution,
        resolved_members,
        structural_analysis,
    } = resolved;
    let member_claims = project_member_claims(MemberClaimInput {
        chunk_id: &prepared.chunk_id,
        requests: &prepared.explicit_requests,
        resolution: &resolution,
        resolved_members: &resolved_members,
        ast: &prepared.ast_analysis,
        top_level_mark: prepared.runtime_ast.top_level_mark,
        body: &prepared.runtime_ast.module.body,
        fail_fast,
        report_warnings,
    })?;
    let (declared_assumptions, source_only_initializer_bindings) = collect_binding_contracts(
        &prepared.explicit_requests,
        &member_claims,
        chunk_renames.get(&prepared.chunk_id),
        &prepared.runtime_ast.module,
        fail_fast,
    )?;
    Ok(FrontendChunk {
        chunk_id: prepared.chunk_id,
        chunk_id_interned: prepared.chunk_id_interned,
        target_file: prepared.target_file,
        runtime_ast: prepared.runtime_ast,
        header_lines: prepared.header_lines,
        source_path: prepared.source_path,
        ast_analysis: prepared.ast_analysis,
        requests: prepared.requests,
        explicit_requests: prepared.explicit_requests,
        structural_analysis,
        resolution,
        resolved_members,
        member_claims,
        declared_assumptions,
        source_only_initializer_bindings,
    })
}

struct MemberClaimInput<'a> {
    chunk_id: &'a str,
    requests: &'a [LogicalRequest],
    resolution: &'a selector_resolve::Resolution,
    resolved_members: &'a [Vec<usize>],
    ast: &'a ChunkAstAnalysis,
    top_level_mark: swc_common::Mark,
    body: &'a [ModuleItem],
    fail_fast: bool,
    report_warnings: bool,
}

fn project_member_claims(input: MemberClaimInput<'_>) -> Result<MemberClaimProjection> {
    let MemberClaimInput {
        chunk_id,
        requests,
        resolution,
        resolved_members,
        ast,
        top_level_mark,
        body,
        fail_fast,
        report_warnings,
    } = input;
    struct ExistingClaim {
        request_index: usize,
        export_name: String,
        claim_origin: String,
        imported: bool,
    }

    let mut owners = BTreeMap::<String, ExistingClaim>::new();
    let mut anonymous_owners = BTreeMap::<StatementId, (usize, String)>::new();
    let mut claimed_anonymous_statements = BTreeMap::new();
    let mut claimed_members = BTreeMap::new();
    for (request_index, request) in requests.iter().enumerate() {
        for (member_index, member) in request.members.iter().enumerate() {
            if member.selector.is_import_specifier()
                && !member.binding.is_empty()
                && let std::collections::btree_map::Entry::Vacant(entry) =
                    owners.entry(member.binding.clone())
            {
                entry.insert(ExistingClaim {
                    request_index,
                    export_name: member.export_name.clone(),
                    claim_origin: member.claim_origin.clone(),
                    imported: true,
                });
                claimed_members.insert((request_index, member_index), member.binding.clone());
            }
        }
    }
    for outcome in &resolution.outcomes {
        record_frontend_outcome(&outcome.outcome, fail_fast, report_warnings)?;
        let request = requests.get(outcome.module).with_context(|| {
            format!(
                "selector outcome names missing logical module {}",
                outcome.module
            )
        })?;
        if !matches!(outcome.outcome.outcome, Outcome::Resolved { .. }) {
            continue;
        }
        if let selector_resolve::EntityIndex::AnonymousStatement(statement_index) = outcome.entity
            && let Outcome::Resolved { body_index, .. } = outcome.outcome.outcome
        {
            let statement = StatementId(body_index);
            ensure!(
                request.anonymous_statements.get(statement_index).is_some(),
                "selector outcome names missing anonymous statement {} in logical module {}",
                statement_index,
                request.id,
            );
            if let Some((_, existing_module)) = anonymous_owners.get(&statement) {
                bail!(
                    "anonymous_statements[].match in module {} also matches the top-level statement at body index {} already claimed by module {}; each anonymous statement may belong to at most one logical module.",
                    request.id,
                    statement.0,
                    existing_module,
                );
            }
            anonymous_owners.insert(statement, (outcome.module, request.id.clone()));
            claimed_anonymous_statements.insert((outcome.module, statement_index), statement);
            continue;
        }
        let selector_resolve::EntityIndex::Member(projected_index) = outcome.entity else {
            continue;
        };
        let Some(member_index) = resolved_members
            .get(outcome.module)
            .and_then(|indices| indices.get(projected_index))
            .copied()
        else {
            bail!(
                "selector outcome names missing projected member {} in logical module {}",
                projected_index,
                request.id
            );
        };
        let member = request.members.get(member_index).with_context(|| {
            format!(
                "selector outcome names missing member {} in logical module {}",
                member_index, request.id
            )
        })?;
        let Outcome::Resolved { binding, .. } = &outcome.outcome.outcome else {
            unreachable!("non-resolved selector outcomes were handled above")
        };
        let binding = binding.as_ref().with_context(|| {
            format!(
                "logical_module {}: global selector solver resolved member `{}` to a body item without a single declared binding",
                request.id, member.export_name
            )
        })?;
        let binding_id = analysis::top_level_id(binding, top_level_mark);
        if !ast.declaration_by_name.contains_key(&binding_id) {
            continue;
        }
        if let Some(existing) = owners.get(binding) {
            if existing.request_index == outcome.module
                && !existing.imported
                && member.selector.source_match().is_some()
            {
                bail!(
                    "logical_module {} has duplicate source binding claims:\n- source binding `{}` claimed 2 times:\n  - export `{}` ({})\n  - export `{}` ({})",
                    request.id,
                    binding,
                    existing.export_name,
                    existing.claim_origin,
                    member.export_name,
                    member.claim_origin,
                );
            }
            if fail_fast {
                let declaration = binding_declaration(body, &ast.declaration_by_name, &binding_id)?;
                let duplicate = selector_resolve::member_outcome(
                    chunk_id,
                    &request.target_path,
                    &member.export_name,
                    &member.selector,
                    Outcome::DuplicateClaim {
                        binding: binding.clone(),
                        declaration,
                        claimed_by: EntityRef {
                            logical_module: requests[existing.request_index].target_path.clone(),
                            entity: Some(Entity::Export(existing.export_name.clone())),
                        },
                    },
                );
                bail!("{}", duplicate.render_line());
            }
            continue;
        }
        owners.insert(
            binding.clone(),
            ExistingClaim {
                request_index: outcome.module,
                export_name: member.export_name.clone(),
                claim_origin: member.claim_origin.clone(),
                imported: false,
            },
        );
        claimed_members.insert((outcome.module, member_index), binding.clone());
    }
    Ok(MemberClaimProjection {
        owned_bindings: owners.into_keys().collect(),
        claimed_members,
        claimed_anonymous_statements,
    })
}

fn record_frontend_outcome(
    outcome: &SelectorOutcome,
    fail_fast: bool,
    report_warnings: bool,
) -> Result<()> {
    match outcome.severity() {
        Severity::Error if fail_fast => bail!("{}", outcome.render_line()),
        Severity::Warning if report_warnings => eprintln!("{}", outcome.render_line()),
        Severity::Error | Severity::Warning => {}
        Severity::Ok => {}
    }
    Ok(())
}

fn collect_binding_contracts(
    requests: &[LogicalRequest],
    member_claims: &MemberClaimProjection,
    chunk_renames: Option<&ChunkRenames>,
    module: &Module,
    fail_fast: bool,
) -> Result<(AnalysisAssumptions, BTreeSet<String>)> {
    let mut assumptions = AnalysisAssumptions::default();
    let mut source_only_initializer_bindings = BTreeSet::new();
    for (request_index, request) in requests.iter().enumerate() {
        for (member_index, member) in request.members.iter().enumerate() {
            if !member.has_semantic_annotations() {
                continue;
            }
            let binding = if member.binding.is_empty() {
                member_claims
                    .claimed_members
                    .get(&(request_index, member_index))
                    .cloned()
            } else {
                Some(member.binding.clone())
            };
            if let Some(binding) = binding {
                if member.initializer_purity == spec::InitializerPurity::Pure {
                    validate_initializer_target(module, &binding, "initializer_purity", false)?;
                }
                if member.initializer_binding_effects
                    == Some(spec::InitializerBindingEffects::SourceOnly)
                {
                    validate_initializer_target(
                        module,
                        &binding,
                        "initializer_binding_effects",
                        true,
                    )?;
                    source_only_initializer_bindings.insert(binding.clone());
                }
                member.collect_assumptions_for_binding(&mut assumptions, &binding);
            } else if fail_fast && member.has_semantic_annotations() {
                bail!(
                    "logical_module {}: semantic annotations on selector member `{}` could not be applied because selector resolution did not produce a concrete binding",
                    request.id,
                    member.export_name
                );
            }
        }
    }
    for (&(request_index, statement_index), &statement) in
        &member_claims.claimed_anonymous_statements
    {
        if let Some(assertion) = requests
            .get(request_index)
            .and_then(|request| request.anonymous_statements.get(statement_index))
            .and_then(|request| request.no_reentry_call.as_ref())
        {
            collect_no_reentry_contract(module, statement, assertion, &mut assumptions)?;
        }
    }
    collect_chunk_rename_binding_contracts(
        chunk_renames,
        module,
        &mut assumptions,
        &mut source_only_initializer_bindings,
    )?;
    Ok((assumptions, source_only_initializer_bindings))
}

fn collect_no_reentry_contract(
    module: &Module,
    statement: StatementId,
    assertion: &spec::NoReentryCall,
    assumptions: &mut AnalysisAssumptions,
) -> Result<()> {
    let Some(ModuleItem::Stmt(swc_ecma_ast::Stmt::Expr(expression_statement))) =
        module.body.get(statement.0)
    else {
        return Ok(());
    };
    let Some(call) = asserted_registration_call(&expression_statement.expr) else {
        return Ok(());
    };
    let argument_count = call.args.len();
    if let Some(position) = assertion
        .callback_arguments
        .iter()
        .find(|position| **position >= argument_count)
    {
        bail!(
            "no_reentry_call callback argument position {position} is out of range for the matched call with {argument_count} arguments"
        );
    }
    assumptions.no_reentry_statement_calls.insert(
        (call.span.lo.0, call.span.hi.0),
        spec::RetainedCallbackSignature {
            argument_count,
            callback_arguments: assertion.callback_arguments.iter().copied().collect(),
        },
    );
    Ok(())
}

fn asserted_registration_call(expression: &swc_ecma_ast::Expr) -> Option<&swc_ecma_ast::CallExpr> {
    match expression.unwrap_parens() {
        swc_ecma_ast::Expr::Call(call) => Some(call),
        swc_ecma_ast::Expr::Bin(binary) if binary.op == swc_ecma_ast::BinaryOp::LogicalAnd => {
            asserted_registration_call(&binary.right)
        }
        _ => None,
    }
}

fn collect_chunk_rename_binding_contracts(
    chunk_renames: Option<&ChunkRenames>,
    module: &Module,
    assumptions: &mut AnalysisAssumptions,
    source_only_initializer_bindings: &mut BTreeSet<String>,
) -> Result<()> {
    let Some(chunk_renames) = chunk_renames else {
        return Ok(());
    };
    let mut by_export_name = BTreeMap::<String, Vec<String>>::new();
    for member in &chunk_renames.members {
        let binding = &member.selector.binding.name;
        let export_name = member.name.as_deref().unwrap_or(binding);
        by_export_name
            .entry(export_name.to_string())
            .or_default()
            .push(binding.clone());
    }
    for (export_name, annotation) in &chunk_renames.annotations {
        let Some(bindings) = by_export_name.get(export_name) else {
            bail!(
                "chunk_renames.annotations key `{export_name}` does not match any chunk_renames member name"
            );
        };
        if bindings.len() != 1 {
            bail!(
                "chunk_renames.annotations key `{export_name}` is ambiguous because that readable binding name is claimed {} times",
                bindings.len()
            );
        }
        collect_binding_annotation_contracts(
            assumptions,
            source_only_initializer_bindings,
            &bindings[0],
            annotation,
            module,
        )?;
    }
    Ok(())
}

fn collect_binding_annotation_contracts(
    assumptions: &mut AnalysisAssumptions,
    source_only_initializer_bindings: &mut BTreeSet<String>,
    binding: &str,
    annotation: &spec::BindingAnnotation,
    module: &Module,
) -> Result<()> {
    if annotation.retained_record_factory {
        assumptions
            .retained_record_factories
            .insert(binding.to_string());
    }
    if annotation.initializer_purity == spec::InitializerPurity::Pure {
        validate_initializer_target(module, binding, "initializer_purity", false)?;
        assumptions
            .declared_pure_initializers
            .insert(binding.to_string());
    }
    if annotation.initializer_binding_effects == Some(spec::InitializerBindingEffects::SourceOnly) {
        validate_initializer_target(module, binding, "initializer_binding_effects", true)?;
        source_only_initializer_bindings.insert(binding.to_string());
    }
    if annotation.purity == spec::MemberPurity::Pure {
        assumptions.declared_pure.insert(binding.to_string());
    }
    if annotation.purity == spec::MemberPurity::PureNew {
        assumptions.declared_pure_new.insert(binding.to_string());
    }
    if !annotation.pure_members.is_empty() {
        assumptions
            .declared_pure_members
            .entry(binding.to_string())
            .or_default()
            .extend(annotation.pure_members.iter().cloned());
    }
    if !annotation.no_sync_callback_members.is_empty() {
        assumptions
            .no_sync_callback_members
            .entry(binding.to_string())
            .or_default()
            .extend(annotation.no_sync_callback_members.iter().cloned());
    }
    if let Some(effect) = spec_resolution::known_effect_from_member_effect(annotation.effect) {
        assumptions
            .known_effects
            .insert(binding.to_string(), effect);
    }
    Ok(())
}

fn validate_initializer_target(
    module: &Module,
    binding: &str,
    contract: &str,
    require_single_source_unit: bool,
) -> Result<()> {
    let mut initialized_sites = 0usize;
    let mut unsupported_sites = 0usize;
    let mut bare_var_sites = 0usize;
    for view in analysis::facts::top_level_item_views(&module.body) {
        let item = view.as_module_item();
        let var = match item {
            ModuleItem::Stmt(swc_ecma_ast::Stmt::Decl(swc_ecma_ast::Decl::Var(var))) => {
                Some(var.as_ref())
            }
            ModuleItem::ModuleDecl(swc_ecma_ast::ModuleDecl::ExportDecl(export)) => {
                match &export.decl {
                    swc_ecma_ast::Decl::Var(var) => Some(var.as_ref()),
                    _ => None,
                }
            }
            _ => None,
        };
        if let Some(var) = var {
            for declarator in &var.decls {
                if !pattern_binds_name(&declarator.name, binding) {
                    continue;
                }
                match &declarator.name {
                    swc_ecma_ast::Pat::Ident(ident) if ident.id.sym.as_ref() == binding => {
                        if require_single_source_unit && var.decls.len() != 1 {
                            unsupported_sites += 1;
                        } else if declarator.init.is_some() {
                            initialized_sites += 1;
                        } else if var.kind == swc_ecma_ast::VarDeclKind::Var {
                            bare_var_sites += 1;
                        } else {
                            unsupported_sites += 1;
                        }
                    }
                    _ => unsupported_sites += 1,
                }
            }
            continue;
        }
        let is_import = match item {
            ModuleItem::ModuleDecl(swc_ecma_ast::ModuleDecl::Import(import)) => import
                .specifiers
                .iter()
                .any(|specifier| specifier.local().sym.as_ref() == binding),
            _ => false,
        };
        if is_import || item_declares_nonvariable_name(item, binding) {
            unsupported_sites += 1;
        }
    }
    if initialized_sites == 1 && unsupported_sites == 0 {
        return Ok(());
    }
    let detail = if initialized_sites > 1 {
        "multiple initialized declarations"
    } else if unsupported_sites > 0 {
        "a non-simple, imported, or non-variable declaration"
    } else if bare_var_sites > 0 && initialized_sites == 0 {
        "no initialized declaration"
    } else {
        "no simple initialized variable declaration"
    };
    bail!(
        "{contract} for binding `{binding}` requires exactly one direct top-level simple variable initializer; found {detail}"
    )
}

fn item_declares_nonvariable_name(item: &ModuleItem, binding: &str) -> bool {
    let declaration_matches = |declaration: &swc_ecma_ast::Decl| match declaration {
        swc_ecma_ast::Decl::Fn(function) => function.ident.sym.as_ref() == binding,
        swc_ecma_ast::Decl::Class(class) => class.ident.sym.as_ref() == binding,
        _ => false,
    };
    match item {
        ModuleItem::Stmt(swc_ecma_ast::Stmt::Decl(declaration)) => declaration_matches(declaration),
        ModuleItem::ModuleDecl(swc_ecma_ast::ModuleDecl::ExportDecl(export)) => {
            declaration_matches(&export.decl)
        }
        ModuleItem::ModuleDecl(swc_ecma_ast::ModuleDecl::ExportDefaultDecl(export)) => {
            match &export.decl {
                swc_ecma_ast::DefaultDecl::Fn(function) => function
                    .ident
                    .as_ref()
                    .is_some_and(|ident| ident.sym.as_ref() == binding),
                swc_ecma_ast::DefaultDecl::Class(class) => class
                    .ident
                    .as_ref()
                    .is_some_and(|ident| ident.sym.as_ref() == binding),
                _ => false,
            }
        }
        _ => false,
    }
}

fn pattern_binds_name(pattern: &swc_ecma_ast::Pat, binding: &str) -> bool {
    match pattern {
        swc_ecma_ast::Pat::Ident(ident) => ident.id.sym.as_ref() == binding,
        swc_ecma_ast::Pat::Array(array) => array
            .elems
            .iter()
            .flatten()
            .any(|element| pattern_binds_name(element, binding)),
        swc_ecma_ast::Pat::Rest(rest) => pattern_binds_name(&rest.arg, binding),
        swc_ecma_ast::Pat::Object(object) => object.props.iter().any(|property| match property {
            swc_ecma_ast::ObjectPatProp::KeyValue(property) => {
                pattern_binds_name(&property.value, binding)
            }
            swc_ecma_ast::ObjectPatProp::Assign(property) => {
                property.key.id.sym.as_ref() == binding
            }
            swc_ecma_ast::ObjectPatProp::Rest(property) => {
                pattern_binds_name(&property.arg, binding)
            }
        }),
        swc_ecma_ast::Pat::Assign(assign) => pattern_binds_name(&assign.left, binding),
        swc_ecma_ast::Pat::Expr(_) | swc_ecma_ast::Pat::Invalid(_) => false,
    }
}

pub fn complete_analysis_spec(
    base: &AnalysisSpec,
    projection: &AnalysisProjection<'_>,
) -> Result<AnalysisSpec> {
    base.clone().with_premises(
        projection.declared_assumptions.clone(),
        projection.source_sites.clone().into_owned(),
        projection.native_operation_inputs.clone(),
    )
}
