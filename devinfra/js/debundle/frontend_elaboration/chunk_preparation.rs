//! Per-chunk preparation: selector solving, eligible members and the finished
//! frontend chunk.

use super::binding_contracts::collect_binding_contracts;
use super::member_claims::{MemberClaimInput, project_member_claims};
use super::{FrontendChunk, FrontendChunkInput, MemberClaimProjection};
use crate::chunk_ast::{ChunkAstAnalysis, analyze_chunk_ast, binding_declaration};
use crate::source_ast::RuntimeImportKind;
use analysis_vocabulary::execution_evidence::SourceLocator;
use anyhow::{Result, bail};
use artifact::{JsFile, chunk_runtime_entry, chunk_unassigned_mode};
use rayon::prelude::*;
use selector_outcome::{Entity, EntityRef, Outcome};
use selector_resolve::Chunk as SelectorChunk;
use selector_resolve::MemberSelector;
use source_identity::ChunkId;
use spec::member_selectors::MemberSelectorSpec;
use spec::{ChunkRenames, LogicalModule, UnassignedMode};
use spec_resolution::{
    LogicalRequest, ProjectedChunk, ResolvedChunk, logical_requests_for_chunk, project_chunk,
};
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::GLOBALS;
use swc_ecma_ast::ModuleItem;

pub(super) struct PreparedChunk<'a> {
    pub(super) chunk_id: String,
    pub(super) chunk_id_interned: ChunkId,
    pub(super) runtime_file: &'a JsFile,
    pub(super) runtime_ast: &'a js_ast::ParsedJsModule,
    pub(super) ast_analysis: ChunkAstAnalysis,
    pub(super) requests: Vec<LogicalRequest>,
    pub(super) explicit_requests: Vec<LogicalRequest>,
    pub(super) projected: Option<ProjectedChunk<'a>>,
}

pub(super) fn solve_selectors<'a>(
    inputs: Vec<FrontendChunkInput<'a>>,
    logical_modules: &BTreeMap<String, BTreeMap<String, LogicalModule>>,
    unassigned_mode: &BTreeMap<String, UnassignedMode>,
    placement_chunks: &BTreeSet<String>,
    fail_fast: bool,
) -> Result<Vec<(PreparedChunk<'a>, ResolvedChunk<'a>)>> {
    let mut prepared = GLOBALS
        .with(|globals| {
            inputs
                .into_par_iter()
                .map(|input| {
                    GLOBALS.set(globals, || {
                        prepare_chunk(
                            input,
                            logical_modules,
                            unassigned_mode,
                            placement_chunks,
                            fail_fast,
                        )
                    })
                })
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
    Ok(prepared.into_iter().zip(resolutions).collect())
}

fn prepare_chunk<'a>(
    input: FrontendChunkInput<'a>,
    logical_modules: &BTreeMap<String, BTreeMap<String, LogicalModule>>,
    unassigned_mode: &BTreeMap<String, UnassignedMode>,
    placement_chunks: &BTreeSet<String>,
    fail_fast: bool,
) -> Result<PreparedChunk<'a>> {
    let FrontendChunkInput {
        artifact,
        chunk_id,
        file,
    } = input;
    let chunk_unassigned_mode = chunk_unassigned_mode(unassigned_mode, chunk_id)?;
    let (chunk_id_interned, runtime_file, runtime_ast) =
        chunk_runtime_entry(artifact, chunk_id, file)?;
    let ast_analysis = analyze_chunk_ast(&runtime_ast.module);
    let requests = logical_requests_for_chunk(
        logical_modules.get(chunk_id),
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
            selector_outcome::ClaimOperation::LogicalModule,
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
    let structural_analysis = analysis::facts::analyze::collect_chunk_source_facts(
        &runtime_ast.module,
        Some(SourceLocator {
            source_path: &runtime_file.metadata.source_path,
            lines: &line_index,
        }),
    );
    let selectors = SelectorChunk::new(
        chunk_id,
        &runtime_ast.module,
        Cow::Owned(structural_analysis),
    );
    let resolved_members = eligible_member_indices(
        chunk_id,
        &explicit_requests,
        &ast_analysis,
        runtime_ast.top_level_mark,
        &runtime_ast.module.body,
        fail_fast,
    )?;
    let projected = project_chunk(
        selectors,
        &explicit_requests,
        resolved_members,
        placement_chunks.contains(chunk_id),
    )?;
    Ok(PreparedChunk {
        chunk_id: chunk_id.to_string(),
        chunk_id_interned,
        runtime_file,
        runtime_ast,
        ast_analysis,
        requests,
        explicit_requests,
        projected: Some(projected),
    })
}

pub fn eligible_member_indices(
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
                    bail!(selector_outcome::SelectorFailure::Single(Box::new(
                        duplicate
                    )));
                }
                continue;
            }
            if member.resolves_after_chunk_analysis() {
                let binding_id = analysis::top_level_id(&member.binding, top_level_mark);
                if matches!(
                    member.selector,
                    MemberSelector::Authored(MemberSelectorSpec::Binding(_))
                ) && !ast.declaration_by_name.contains_key(&binding_id)
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

impl PreparedChunk<'_> {
    pub(super) fn claims(
        &self,
        resolution: &selector_resolve::Resolution,
        resolved_members: &[Vec<usize>],
        fail_fast: bool,
        diagnostics: &analysis::diagnostics::Diagnostics,
    ) -> Result<MemberClaimProjection> {
        project_member_claims(MemberClaimInput {
            chunk_id: &self.chunk_id,
            requests: &self.explicit_requests,
            resolution,
            resolved_members,
            ast: &self.ast_analysis,
            top_level_mark: self.runtime_ast.top_level_mark,
            body: &self.runtime_ast.module.body,
            fail_fast,
            diagnostics,
        })
    }
}

pub(super) fn finish_frontend_chunk<'a>(
    prepared: PreparedChunk<'a>,
    resolved: ResolvedChunk<'a>,
    chunk_renames: &BTreeMap<String, ChunkRenames>,
    fail_fast: bool,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<FrontendChunk<'a>> {
    let ResolvedChunk {
        resolution,
        resolved_members,
        structural_analysis,
        placement_anchors,
    } = resolved;
    let member_claims = prepared.claims(&resolution, &resolved_members, fail_fast, diagnostics)?;
    diagnostics.metrics().frontend_chunk();
    let (declared_assumptions, source_only_initializer_bindings, external_initializer_contracts) =
        collect_binding_contracts(
            &prepared.explicit_requests,
            &member_claims,
            chunk_renames.get(&prepared.chunk_id),
            &prepared.runtime_ast.module,
            fail_fast,
            diagnostics.metrics(),
        )?;
    Ok(FrontendChunk {
        chunk_id: prepared.chunk_id,
        chunk_id_interned: prepared.chunk_id_interned,
        runtime_file: prepared.runtime_file,
        runtime_ast: prepared.runtime_ast,
        ast_analysis: prepared.ast_analysis,
        requests: prepared.requests,
        explicit_requests: prepared.explicit_requests,
        structural_analysis,
        resolution,
        placement_anchors,
        resolved_members,
        member_claims,
        declared_assumptions,
        source_only_initializer_bindings,
        external_initializer_contracts,
    })
}
