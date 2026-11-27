//! Resolve spec selectors and source-bound assumptions before graph planning.
//!
//! This target deliberately has no dependency on lowering. It returns the
//! exact joint selector result that both analysis projection and materializing
//! callers consume.

use crate::chunk_ast::ChunkAstAnalysis;
use analysis::facts::StructuralChunkSourceFacts;
use analysis_projection::{self, AnalysisProjection, AnalysisSpec, ChunkGraphInputs};
use analysis_vocabulary::inputs::{AnalysisAssumptions, SourceDeclaration};
use anyhow::{Context, Result, ensure};
use artifact::{
    ChunkBundle, JsFile, export_policies, normalized_chunk_ids, prune_artifact_to_chunk_ids,
};
use chunk_preparation::{finish_frontend_chunk, solve_selectors};
use graph_provenance::GraphProvenance;
use source_identity::{ChunkId, StatementId};
use source_sites::resolve_analysis_source_sites;
use spec::{
    ChunkExportPurity, ChunkRenames, DependencyGraphOptions, HostBehavior, IntrinsicsNeverWritten,
    LogicalModule, UnassignedMode,
};
use spec_resolution::LogicalRequest;
use std::collections::{BTreeMap, BTreeSet};

mod binding_contracts;
pub(crate) mod chunk_preparation;
pub(crate) mod member_claims;
pub(crate) mod source_sites;

pub struct FrontendChunkInput<'a> {
    pub artifact: &'a ChunkBundle,
    pub chunk_id: &'a str,
    pub file: Option<&'a str>,
}

pub struct FrontendChunk<'a> {
    pub chunk_id: String,
    pub chunk_id_interned: ChunkId,
    pub runtime_file: &'a JsFile,
    pub runtime_ast: &'a js_ast::ParsedJsModule,
    pub ast_analysis: ChunkAstAnalysis,
    pub requests: Vec<LogicalRequest>,
    pub explicit_requests: Vec<LogicalRequest>,
    pub structural_analysis: StructuralChunkSourceFacts<'a>,
    pub resolution: selector_resolve::Resolution,
    pub placement_anchors: Option<selector_resolve::PlacementAnchors>,
    pub resolved_members: Vec<Vec<usize>>,
    pub member_claims: MemberClaimProjection,
    pub declared_assumptions: AnalysisAssumptions,
    pub source_only_initializer_bindings: BTreeSet<String>,
    pub external_initializer_contracts: BTreeMap<String, spec::InitializerExternalEffects>,
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
    /// Freeze full-base selector identities only for placement-view consumers.
    pub placement_chunks: BTreeSet<String>,
    pub chunk_renames: &'a BTreeMap<String, ChunkRenames>,
    pub unassigned_mode: &'a BTreeMap<String, UnassignedMode>,
    pub chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
    pub graph_provenance: Option<&'a GraphProvenance>,
    pub chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
    pub host_behavior: &'a HostBehavior,
    pub intrinsics_never_written: Option<&'a IntrinsicsNeverWritten>,
    pub fail_fast: bool,
    pub diagnostics: &'a analysis::diagnostics::Diagnostics<'a>,
}

/// Build the canonical project-analysis artifact directly from source-level
/// inputs. This path performs selector resolution and premise collection but
/// never constructs output plans or enters lowering.
pub fn project_analysis(
    mut artifact: ChunkBundle,
    spec: &spec::TransformSpec,
    base: &AnalysisSpec,
    graph_provenance: Option<&GraphProvenance>,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<AnalysisSpec> {
    if !spec.materialize_logical_modules.target_dir.is_empty() {
        artifact::paths::normalize_module_path(&spec.materialize_logical_modules.target_dir)?;
    }
    let chunk_ids = normalized_chunk_ids(spec.materialize_chunk_ids())?;
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
            placement_chunks: BTreeSet::new(),
            chunk_renames: &spec.chunk_renames,
            unassigned_mode: &spec.unassigned_mode,
            chunk_analysis_options: &spec.chunk_analysis_options,
            graph_provenance,
            chunk_export_purity: chunk_export_purity.as_ref(),
            host_behavior: &spec.host_behavior,
            intrinsics_never_written: spec.program_premises.intrinsics_never_written.as_ref(),
            fail_fast: true,
            diagnostics,
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
    let solved = solve_selectors(
        inputs,
        spec.logical_modules,
        spec.unassigned_mode,
        &spec.placement_chunks,
        spec.fail_fast,
    )?;
    let chunks = solved
        .into_iter()
        .map(|(chunk, resolved)| {
            finish_frontend_chunk(
                chunk,
                resolved,
                spec.chunk_renames,
                spec.fail_fast,
                spec.diagnostics,
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
    let projected_options = analysis_projection::project_chunk_options(spec.chunk_analysis_options);
    let graph_inputs = projected_options
        .iter()
        .map(|(chunk, options)| (chunk.clone(), options.graph.clone()))
        .collect();
    let (source_sites, native_operation_inputs) = resolve_analysis_source_sites(
        prepared_artifact,
        &chunks,
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

#[path = "resolved_ownership.rs"]
pub(crate) mod resolved_ownership;
