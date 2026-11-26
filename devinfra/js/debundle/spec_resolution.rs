//! Compile spec claims into request types and resolve those requests against source chunks.

use analysis::facts::StructuralChunkSourceFacts;
use analysis_vocabulary::inputs::KnownEffect;
use anyhow::{Result, bail};
use selector_outcome::{
    BindingClaimant, ClaimOperation, ClaimValidationError, DuplicateBinding, SelectorKind,
};
use selector_resolve::{
    AnonymousStatement, Chunk, FrozenImportAnchor, Member, MemberSelector, PlacementAnchors,
    Projection, Resolution, SpecModule,
};
use source_match::ParsedSourceMatchSelector;
use spec::{BindingAnnotation, LogicalModule, MemberEffect, UnassignedMode};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone)]
pub struct LogicalRequest {
    pub id: String,
    pub target_path: String,
    /// True only for a synthesized memberless catchall-file request; an
    /// explicit request at the catchall target remains false.
    pub residual: bool,
    pub members: Vec<MemberRequest>,
    /// Anonymous-statement members the spec asked to co-move into
    /// this module. Lowering claims their resolved source ordinals into
    /// its output plan.
    pub anonymous_statements: Vec<AnonymousStatementRequest>,
    /// Module-level human-readable comment from the spec. Emitted
    /// at the top of the generated module file, before any imports.
    /// See [`spec::LogicalModule::comment`].
    pub comment: Option<String>,
}

#[derive(Debug, Clone)]
pub struct AnonymousStatementRequest {
    pub parsed_selector: source_match::ParsedSourceMatchSelector,
    /// Optional timing premise for this exact selected anonymous call.
    pub no_reentry_call: Option<spec::NoReentryCall>,
    /// Optional `comment:` text from the anonymous statement spec
    /// entry. `note:` is not emitted; it remains YAML scratch
    /// metadata.
    pub comment: Option<String>,
}

#[derive(Debug, Clone)]
pub struct MemberRequest {
    pub binding: String,
    pub export_name: String,
    /// Whether the spec explicitly assigned this public name (as opposed to
    /// the implicit binding-name default).
    pub explicit_export_name: bool,
    /// How the member names its declaration. Every selector but an import
    /// specifier name pin resolves through the global selector solver.
    pub selector: MemberSelector,
    /// Module annotation selected by this claim's readable export name.
    pub annotation: BindingAnnotation,
    /// Short spec-facing description of how this member claim was authored.
    /// Used only in diagnostics after source-match selectors have resolved to
    /// concrete source bindings.
    pub claim_origin: String,
}

impl MemberRequest {
    /// Whether this member's ownership is intentionally unknown until chunk
    /// analysis facts are available and the global selector solver runs.
    ///
    /// Plain binding selectors keep their binding spelling for assumptions and
    /// duplicate diagnostics, but ownership is read back from the solver.
    pub fn resolves_after_chunk_analysis(&self) -> bool {
        !self.selector.is_import_specifier()
    }
}

pub fn logical_requests_for_chunk(
    chunk_logical_modules: Option<&BTreeMap<String, LogicalModule>>,
    chunk_unassigned_mode: &UnassignedMode,
    chunk_id: &str,
) -> Result<Vec<LogicalRequest>> {
    let mut requests = Vec::new();
    let catchall_target = chunk_unassigned_mode
        .catchall_file_target()
        .map(str::to_string);
    let mut explicit_module_at_catchall = false;
    if let Some(by_target_path) = chunk_logical_modules {
        for (target_path, module) in by_target_path {
            let id = format!("{chunk_id}::{target_path}");
            let members = build_members(
                &module.members,
                &module.source_matches,
                &module.annotations,
                &id,
            )?;
            reject_duplicate_export_names(ClaimOperation::LogicalModule, &id, &members)?;
            reject_duplicate_member_bindings(ClaimOperation::LogicalModule, &id, &members)?;
            let anonymous_statements = module
                .anonymous_statements
                .iter()
                .map(|stmt| {
                    let selector = stmt.selector()?;
                    let parsed_selector = parse_anonymous_selector(
                        &id,
                        format!("<anonymous source_match in {id}>"),
                        &selector,
                    )?;
                    Ok(AnonymousStatementRequest {
                        parsed_selector,
                        no_reentry_call: stmt.no_reentry_call.clone(),
                        comment: stmt.comment.clone(),
                    })
                })
                .collect::<Result<Vec<_>>>()?;
            if catchall_target.as_deref() == Some(target_path.as_str()) {
                explicit_module_at_catchall = true;
            }
            requests.push(LogicalRequest {
                id,
                target_path: target_path.clone(),
                residual: false,
                members,
                anonymous_statements,
                comment: module.comment.clone(),
            });
        }
    }
    // Synthesize a memberless catchall-file request when the chunk's
    // `unassigned_mode` is `CatchallFile` and no explicit logical
    // module already claims the catchall target. When an explicit
    // module *is* at the catchall target, the residual sweep in
    // `materialize_logical_chunk` will append unclaimed bindings to
    // that explicit plan instead.
    if let Some(target_path) = catchall_target
        && !explicit_module_at_catchall
    {
        requests.push(LogicalRequest {
            id: format!("{chunk_id}::residual"),
            target_path,
            residual: true,
            members: Vec::new(),
            anonymous_statements: Vec::new(),
            comment: None,
        });
    }
    Ok(requests)
}

pub fn build_members(
    members: &[spec::Member],
    source_matches: &[spec::SourceMatchClaim],
    annotations: &BTreeMap<String, spec::BindingAnnotation>,
    request_id: &str,
) -> Result<Vec<MemberRequest>> {
    let mut requests = members
        .iter()
        .map(|m| {
            let selected = m.selector.validate()?;
            let origin = SelectorKind::from(&selected).origin(None);
            let binding = match &selected {
                spec::MemberSelectorSpec::Binding(binding) => binding.name.clone(),
                _ => String::new(),
            };
            let export_name = match &selected {
                spec::MemberSelectorSpec::Binding(binding) => {
                    m.name.clone().unwrap_or_else(|| binding.name.clone())
                }
                // A solver-resolved member's public export name can't default to a
                // binding name — the binding isn't known until the selector resolves
                // — so `name:` is required.
                _ => m
                    .name
                    .clone()
                    .ok_or_else(|| ClaimValidationError::MissingExportName {
                        logical_module: request_id.to_string(),
                        selector_kind: SelectorKind::from(&selected),
                    })?,
            };
            let claim_origin = format!("{origin} as `{export_name}`");
            let selector = MemberSelector::Authored(selected);
            Ok(MemberRequest {
                binding,
                export_name,
                explicit_export_name: m.name.is_some(),
                selector,
                annotation: BindingAnnotation::default(),
                claim_origin,
            })
        })
        .collect::<Result<Vec<_>>>()?;

    for claim in source_matches {
        for expanded in source_match::source_match_claim_member_selectors(request_id, claim)? {
            let source_match::BindingGroupMemberSelector {
                export_name,
                parsed_selector,
            } = expanded;
            let claim_origin = match &parsed_selector.selector().target_binding {
                Some(target) => format!("source_matches[].bindings[`{target}`]"),
                None => "source_matches[]".to_string(),
            };
            requests.push(MemberRequest {
                binding: String::new(),
                export_name,
                explicit_export_name: true,
                selector: MemberSelector::SourceMatch(parsed_selector),
                annotation: BindingAnnotation::default(),
                claim_origin,
            });
        }
    }

    apply_binding_annotations(request_id, &mut requests, annotations)?;
    Ok(requests)
}

fn apply_binding_annotations(
    request_id: &str,
    requests: &mut [MemberRequest],
    annotations: &BTreeMap<String, spec::BindingAnnotation>,
) -> Result<()> {
    let mut by_export_name = BTreeMap::<String, Vec<usize>>::new();
    for (idx, request) in requests.iter().enumerate() {
        by_export_name
            .entry(request.export_name.clone())
            .or_default()
            .push(idx);
    }

    for (export_name, annotation) in annotations {
        let Some(indices) = by_export_name.get(export_name) else {
            return Err(ClaimValidationError::UnknownAnnotation {
                logical_module: request_id.to_string(),
                export_name: export_name.clone(),
            }
            .into());
        };
        if indices.len() != 1 {
            return Err(ClaimValidationError::AmbiguousAnnotation {
                logical_module: request_id.to_string(),
                export_name: export_name.clone(),
                count: indices.len(),
            }
            .into());
        }
        requests[indices[0]].annotation = annotation.clone();
    }
    Ok(())
}

pub fn known_effect_from_member_effect(effect: MemberEffect) -> Option<KnownEffect> {
    match effect {
        MemberEffect::Default => None,
        MemberEffect::TypescriptDecorateHelper => Some(KnownEffect::TypescriptDecorateHelper),
        MemberEffect::TypescriptMetadataDecorateHelper => {
            Some(KnownEffect::TypescriptMetadataDecorateHelper)
        }
        MemberEffect::TypescriptMetadataDecorateHelperWithMemberDecorators => {
            Some(KnownEffect::TypescriptMetadataDecorateHelperWithMemberDecorators)
        }
        MemberEffect::TypescriptMetadataDecorateHelperThreeArgs => {
            Some(KnownEffect::TypescriptMetadataDecorateHelperThreeArgs)
        }
    }
}

/// Convert one parsed selector into the selector engine's module member.
pub fn member_from_spec(export_name: String, selector: spec::MemberSelectorSpec) -> Member {
    member_from_selector(export_name, MemberSelector::Authored(selector))
}

/// Convert one compiled selector into the selector engine's module member.
pub fn member_from_selector(export_name: String, selector: MemberSelector) -> Member {
    Member {
        export_name,
        selector,
    }
}

/// Convert one expanded source-match binding into the selector engine's module
/// member.
pub fn member_from_source_match(
    export_name: String,
    selector: ParsedSourceMatchSelector,
) -> Member {
    member_from_selector(export_name, MemberSelector::SourceMatch(selector))
}

/// Parse an anonymous source selector with the shared selector conventions.
pub fn parse_anonymous_selector(
    request_id: &str,
    description: impl Into<String>,
    selector: &spec::AnonymousStatementSelector,
) -> Result<ParsedSourceMatchSelector> {
    Ok(ParsedSourceMatchSelector::parse(
        request_id,
        description.into(),
        selector,
        source_match::SelectorField::SourceMatch,
    )?)
}

/// Build a selector-engine anonymous statement from its authored index.
pub fn anonymous_statement(
    index: usize,
    selector: ParsedSourceMatchSelector,
) -> AnonymousStatement {
    AnonymousStatement { index, selector }
}

/// Construct one selector-engine module from compiled members and anonymous
/// statements.
pub fn spec_module(
    path: String,
    members: Vec<Member>,
    anonymous_statements: Vec<AnonymousStatement>,
) -> SpecModule {
    SpecModule {
        path,
        members,
        anonymous_statements,
    }
}

/// The projected selectors and original request indices for one source chunk.
pub struct ProjectedChunk<'a> {
    chunk: Chunk<'a>,
    modules: Vec<SpecModule>,
    projection: Projection,
    resolved_members: Vec<Vec<usize>>,
    placement_import_anchors: Option<Vec<FrozenImportAnchor>>,
}

/// The selector result, source structure, and request indices lowering needs
/// to apply the resolved ownership claims.
pub struct ResolvedChunk<'a> {
    pub resolution: Resolution,
    pub resolved_members: Vec<Vec<usize>>,
    pub structural_analysis: StructuralChunkSourceFacts<'a>,
    pub placement_anchors: Option<PlacementAnchors>,
}

/// Compile the exact eligible request-member layout used by the canonical
/// resolver. Ownership layers share this mapping instead of rebuilding member
/// or grouped-selector identities in their callers.
pub fn selector_modules_for_requests(
    requests: &[LogicalRequest],
    resolved_members: &[Vec<usize>],
) -> Result<Vec<SpecModule>> {
    if requests.len() != resolved_members.len() {
        bail!(
            "selector request/module count mismatch: {} requests, {} member-index lists",
            requests.len(),
            resolved_members.len()
        );
    }
    requests
        .iter()
        .zip(resolved_members)
        .map(|(request, member_indices)| {
            let members = member_indices
                .iter()
                .map(|index| {
                    let member = request.members.get(*index).ok_or_else(|| {
                        anyhow::anyhow!(
                            "logical_module {}: selector member index {} is out of range",
                            request.id,
                            index
                        )
                    })?;
                    Ok(member_from_selector(
                        member.export_name.clone(),
                        member.selector.clone(),
                    ))
                })
                .collect::<Result<Vec<_>>>()?;
            let anonymous_statements = request
                .anonymous_statements
                .iter()
                .enumerate()
                .map(|(index, statement)| {
                    anonymous_statement(index, statement.parsed_selector.clone())
                })
                .collect();
            Ok(spec_module(
                request.target_path.clone(),
                members,
                anonymous_statements,
            ))
        })
        .collect::<Result<Vec<_>>>()
}

/// Compile requests into selector-engine modules and project them against one
/// source chunk. `resolved_members` identifies which request members are
/// eligible after lowering has accounted for duplicate and unmatched pins.
pub fn project_chunk<'a>(
    chunk: Chunk<'a>,
    requests: &[LogicalRequest],
    resolved_members: Vec<Vec<usize>>,
    freeze_for_placement: bool,
) -> Result<ProjectedChunk<'a>> {
    let modules = selector_modules_for_requests(requests, &resolved_members)?;
    let projection = chunk.project(&modules)?;
    let placement_import_anchors = freeze_for_placement.then(|| {
        requests
            .iter()
            .flat_map(|request| {
                request
                    .members
                    .iter()
                    .filter(|member| member.selector.is_import_specifier())
                    .map(|member| FrozenImportAnchor {
                        logical_module: request.target_path.clone(),
                        export_name: member.export_name.clone(),
                        imported_binding: member.binding.clone(),
                    })
            })
            .collect()
    });
    Ok(ProjectedChunk {
        chunk,
        modules,
        projection,
        resolved_members,
        placement_import_anchors,
    })
}

/// Solve projections for all chunks together and return per-chunk results in
/// the same order as the input.
pub fn solve_projected_chunks<'a>(
    chunks: Vec<ProjectedChunk<'a>>,
) -> Result<Vec<ResolvedChunk<'a>>> {
    let mut source_chunks = Vec::with_capacity(chunks.len());
    let mut modules = Vec::with_capacity(chunks.len());
    let mut projections = Vec::with_capacity(chunks.len());
    let mut resolved_members = Vec::with_capacity(chunks.len());
    let mut placement_import_anchors = Vec::with_capacity(chunks.len());
    for ProjectedChunk {
        chunk,
        modules: chunk_modules,
        projection,
        resolved_members: member_indices,
        placement_import_anchors: import_anchors,
    } in chunks
    {
        source_chunks.push(chunk);
        modules.push(chunk_modules);
        projections.push(projection);
        resolved_members.push(member_indices);
        placement_import_anchors.push(import_anchors);
    }
    let resolutions = selector_resolve::solve(
        source_chunks
            .iter()
            .zip(&modules)
            .zip(projections)
            .map(|((chunk, modules), projection)| (chunk, modules.as_slice(), projection))
            .collect(),
    )?;
    resolutions
        .into_iter()
        .zip(resolved_members)
        .zip(source_chunks)
        .zip(modules)
        .zip(placement_import_anchors)
        .map(
            |((((resolution, resolved_members), chunk), modules), import_anchors)| {
                let placement_anchors = import_anchors
                    .map(|imports| -> Result<PlacementAnchors> {
                        let mut anchors = chunk.freeze_placement_anchors(&modules, &resolution)?;
                        chunk.add_frozen_import_anchors(&mut anchors, &imports)?;
                        Ok(anchors)
                    })
                    .transpose()?;
                Ok(ResolvedChunk {
                    resolution,
                    resolved_members,
                    structural_analysis: chunk.into_structural(),
                    placement_anchors,
                })
            },
        )
        .collect::<Result<Vec<_>>>()
}

/// Resolve a single set of compiled modules through the shared exact solver.
pub fn resolve_chunk(chunk: &Chunk<'_>, modules: &[SpecModule]) -> Result<Resolution> {
    let mut resolutions = resolve_chunks(&[(chunk, modules)])?;
    Ok(resolutions.remove(0))
}

/// Resolve the same compiled modules against multiple source chunks together.
pub fn resolve_chunks(chunks: &[(&Chunk<'_>, &[SpecModule])]) -> Result<Vec<Resolution>> {
    selector_resolve::resolve(chunks)
}

/// Validate request construction and duplicate claims for one authored module.
pub fn validate_logical_module_claims(
    id: &str,
    members: &[spec::Member],
    source_matches: &[spec::SourceMatchClaim],
    annotations: &BTreeMap<String, spec::BindingAnnotation>,
) -> Result<()> {
    let members = build_members(members, source_matches, annotations, id)?;
    reject_duplicate_export_names(ClaimOperation::LogicalModule, id, &members)?;
    reject_duplicate_member_bindings(ClaimOperation::LogicalModule, id, &members)?;
    Ok(())
}

/// Reject repeated exported names in one logical-module request.
pub fn reject_duplicate_export_names(
    operation: ClaimOperation,
    id: &str,
    members: &[MemberRequest],
) -> Result<()> {
    let mut seen = BTreeSet::new();
    let mut names = BTreeSet::new();
    for member in members {
        if !seen.insert(&member.export_name) {
            names.insert(member.export_name.clone());
        }
    }
    if names.is_empty() {
        return Ok(());
    }
    Err(ClaimValidationError::DuplicateExports {
        operation,
        id: id.to_string(),
        names: names.into_iter().collect(),
    }
    .into())
}

/// Reject duplicate source-binding claims whose binding spelling is known
/// before selector resolution.
pub fn reject_duplicate_member_bindings(
    operation: ClaimOperation,
    id: &str,
    members: &[MemberRequest],
) -> Result<()> {
    let mut by_binding = BTreeMap::<String, Vec<&MemberRequest>>::new();
    for member in members {
        if member.binding.is_empty() {
            continue;
        }
        by_binding
            .entry(member.binding.clone())
            .or_default()
            .push(member);
    }
    let duplicates = by_binding
        .into_iter()
        .filter(|(_, members)| members.len() > 1)
        .collect::<Vec<_>>();
    if duplicates.is_empty() {
        return Ok(());
    }
    Err(ClaimValidationError::DuplicateBindings {
        operation,
        id: id.to_string(),
        claims: duplicates
            .into_iter()
            .map(|(binding, members)| DuplicateBinding {
                binding,
                claimants: members
                    .iter()
                    .map(|member| BindingClaimant {
                        export_name: member.export_name.clone(),
                        claim_origin: member.claim_origin.clone(),
                    })
                    .collect(),
            })
            .collect(),
    }
    .into())
}
