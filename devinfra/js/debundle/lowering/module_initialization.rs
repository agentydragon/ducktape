//! Check bounded provider identity and module-cell initialization obligations
//! against the exact prepared file layout and requests that emission consumes.
//! Source completion is a prerequisite, never the candidate proof.

use std::collections::{BTreeMap, BTreeSet};

use analysis::facts::StatementFacts;
use artifact::reports::{ImportSpecifierKind, StaticModuleRequest, StaticModuleRequestMode};
use artifact::{
    ArtifactIndexes, CandidateArtifactImportResolver, ChunkBundle, ImportReferenceKind, JsFileBody,
};
use frontend_elaboration::source_ast::{RuntimeImportFacts, RuntimeImportKind};
use gate::{
    AssignmentAnalysis, CandidateModuleInitializationEvidence, CandidateProviderTargetEvidence,
    ModuleInitializationRefusal, ProviderTargetRefusal,
};
use source_identity::{ChunkId, ModuleCellId};

use crate::body_facts::{PreparedImportTarget, PreparedStaticRequest};
use crate::lower::PreparedCandidateEmissionPlan;

type ModuleFile = (ChunkId, String);
type ModuleGraph = BTreeMap<ModuleFile, Vec<ModuleFile>>;

#[derive(Clone, Copy)]
pub(super) struct ModuleInitializationContext<'a, 'output> {
    pub(super) artifact: &'a ChunkBundle,
    pub(super) indexes: &'a ArtifactIndexes,
    pub(super) assignment_analysis: &'a AssignmentAnalysis,
    pub(super) consumer: ChunkId,
    pub(super) materialized_chunks: &'a BTreeSet<ChunkId>,
    pub(super) vendor_unchanged: bool,
    pub(super) prepared: Option<&'a PreparedCandidateEmissionPlan<'output>>,
    pub(super) source_initialized_cells: Option<&'a BTreeSet<ModuleCellId>>,
    pub(super) runtime_import_facts: &'a RuntimeImportFacts,
}

pub(super) fn assess_module_initialization(
    context: ModuleInitializationContext<'_, '_>,
    facts: &[StatementFacts],
) -> CandidateModuleInitializationEvidence {
    let required_cells: BTreeSet<_> = facts
        .iter()
        .flat_map(StatementFacts::required_initialized_module_cells)
        .collect();
    let mut required_imported_primitive_reads = Vec::new();
    for requirement in facts
        .iter()
        .flat_map(StatementFacts::required_imported_primitive_reads)
    {
        if !required_imported_primitive_reads.contains(requirement) {
            required_imported_primitive_reads.push(requirement.clone());
        }
    }
    if required_cells.is_empty() && required_imported_primitive_reads.is_empty() {
        return CandidateModuleInitializationEvidence::default();
    }
    match preserve_initialization(
        &context,
        &required_cells,
        &required_imported_primitive_reads,
    ) {
        Ok(()) => CandidateModuleInitializationEvidence {
            preserved_cells: required_cells,
            preserved_imported_primitive_reads: required_imported_primitive_reads,
            refusal: None,
        },
        Err(refusal) => CandidateModuleInitializationEvidence {
            preserved_cells: BTreeSet::new(),
            preserved_imported_primitive_reads: Vec::new(),
            refusal: Some(refusal),
        },
    }
}

pub(super) fn assess_conditional_provider_targets(
    context: &ModuleInitializationContext<'_, '_>,
    facts: &[StatementFacts],
) -> CandidateProviderTargetEvidence {
    let required = facts
        .iter()
        .flat_map(StatementFacts::required_conditional_provider_targets)
        .collect::<Vec<_>>();
    if required.is_empty() {
        return CandidateProviderTargetEvidence::default();
    }
    match preserve_conditional_provider_targets(context, &required) {
        Ok(()) => CandidateProviderTargetEvidence::preserved(required.iter().copied()),
        Err(refusal) => CandidateProviderTargetEvidence::refused(refusal),
    }
}

fn preserve_conditional_provider_targets(
    context: &ModuleInitializationContext<'_, '_>,
    required: &[&analysis::facts::ConditionalProviderTargetRequirement],
) -> Result<(), ProviderTargetRefusal> {
    use ProviderTargetRefusal::{
        BindingRouteChanged, ExportIdentityChanged, PreparedEmissionUnavailable,
        ProviderRouteChanged, UnknownProvider, UnsupportedCandidateScope, UnsupportedRequest,
    };

    let prepared = context.prepared.ok_or(PreparedEmissionUnavailable)?;
    if context.materialized_chunks != &BTreeSet::from([context.consumer])
        || !context.vendor_unchanged
    {
        return Err(UnsupportedCandidateScope);
    }
    let resolver = project_prepared_layout(context, prepared)?;
    let consumer = context
        .artifact
        .find_chunk(context.consumer)
        .ok_or(UnknownProvider)?;
    let source_entry = &consumer.analysis.entry_file;
    let assignment = context.assignment_analysis.assignment();
    let entry_destination = assignment.entry_destination().module_id();
    for requirement in required {
        let import = context
            .runtime_import_facts
            .imports
            .get(&requirement.source_binding)
            .ok_or(BindingRouteChanged)?;
        let imported_name = match &import.kind {
            RuntimeImportKind::Named { imported } => imported,
            RuntimeImportKind::Default | RuntimeImportKind::Namespace => {
                return Err(ExportIdentityChanged);
            }
        };
        let source_first_hop = context
            .indexes
            .resolve_runtime_import_reference(
                &import.src,
                context.consumer,
                source_entry,
                &context.artifact.chunk_table,
            )
            .ok_or(UnknownProvider)?;
        let defining_chunk = context
            .artifact
            .chunk_table
            .get(&requirement.defining_module)
            .filter(|chunk| context.artifact.find_chunk(*chunk).is_some())
            .ok_or(UnknownProvider)?;
        let owner = assignment
            .iter()
            .find(|(source_unit, _)| source_unit.0 == requirement.owner.0)
            .map(|(_, destination)| destination)
            .ok_or(BindingRouteChanged)?;
        let (emitted_file, _, requests, _) = prepared
            .prepared_static_request_inventory_for_destination(owner, entry_destination)
            .ok_or(BindingRouteChanged)?;
        let mut matching_bindings = Vec::new();
        for request in requests {
            for binding in request
                .bindings
                .iter()
                .filter(|binding| binding.source_binding == requirement.source_binding)
            {
                matching_bindings.push((request, binding));
            }
        }
        let [(request, binding)] = matching_bindings.as_slice() else {
            return Err(BindingRouteChanged);
        };
        // This request row comes from the exact named-import constructor in
        // the body selected by `requirement.owner`. The obligation itself was
        // recorded from this source Id as the callee at `call_span`; the
        // assignment projection places that source unit in this prepared
        // body, and canonical body-use planning emits the matching import
        // binding. Therefore this is a live named local, not an unrelated or
        // side-effect-only graph edge that dead-import pruning may discard.
        if request.request.mode != StaticModuleRequestMode::OrdinaryEsm {
            return Err(UnsupportedRequest);
        }
        if binding.imported_name != *imported_name
            || !matches!(
                &binding.target,
                PreparedImportTarget::OriginalSource { specifier }
                    if specifier == &import.src
            )
        {
            return Err(ExportIdentityChanged);
        }
        if request.targets.iter().any(|target| {
            matches!(
                target,
                PreparedImportTarget::Unsupported
                    | PreparedImportTarget::UnsupportedOriginalSource { .. }
            )
        }) {
            return Err(UnsupportedRequest);
        }
        let candidate_target = resolver
            .resolve_runtime_import_reference(
                &request.request.specifier,
                context.consumer,
                emitted_file,
            )
            .filter(|target| {
                owner == entry_destination || target.kind == ImportReferenceKind::ArtifactPath
            })
            .ok_or(BindingRouteChanged)?;
        if candidate_target.target_chunk_id != source_first_hop.target_chunk_id
            || candidate_target.target_file != source_first_hop.target_file
        {
            return Err(BindingRouteChanged);
        }
        preserve_provider_export_route(
            context,
            &resolver,
            (
                source_first_hop.target_chunk_id,
                source_first_hop.target_file.clone(),
            ),
            imported_name,
            defining_chunk,
            &requirement.export,
            &requirement.local,
        )?;
        let provider_closure = preserve_provider_route_closure(
            context,
            &resolver,
            (
                source_first_hop.target_chunk_id,
                source_first_hop.target_file,
            ),
            defining_chunk,
        )
        .map_err(|_| ProviderRouteChanged)?;
        for (consumer_file, consumer_source_file, consumer_requests, consumer_entry) in
            prepared.prepared_static_request_inventories()
        {
            preserve_consumer_request_targets(
                context,
                &resolver,
                consumer_file,
                consumer_source_file,
                consumer_requests,
                consumer_entry,
                &provider_closure,
            )?;
        }
    }
    Ok(())
}

/// Follow the retained provider's canonical export/import records from the
/// source import name to the exact analyzed callable export. These records
/// are produced by source analysis; each forwarding hop is then resolved with
/// the shared source and candidate layout resolvers. Export-star and any
/// forwarding shape without a named route remain unproved.
fn preserve_provider_export_route(
    context: &ModuleInitializationContext<'_, '_>,
    resolver: &CandidateArtifactImportResolver<'_>,
    first_hop: ModuleFile,
    imported_name: &str,
    defining_chunk: ChunkId,
    defining_export: &str,
    defining_local: &str,
) -> Result<(), ProviderTargetRefusal> {
    let mut current = first_hop;
    let mut exported_name = imported_name.to_owned();
    let mut seen = BTreeSet::new();
    loop {
        if current.0 == defining_chunk {
            if exported_name == defining_export {
                return Ok(());
            }
            let chunk = context
                .artifact
                .find_chunk(current.0)
                .ok_or(ProviderTargetRefusal::UnknownProvider)?;
            let aliases = chunk
                .analysis
                .export_aliases
                .iter()
                .filter(|alias| alias.exported == exported_name && alias.source.is_none())
                .collect::<Vec<_>>();
            if matches!(aliases.as_slice(), [alias] if alias.local.as_deref() == Some(defining_local))
            {
                return Ok(());
            }
        }
        if !seen.insert((current.0, current.1.clone(), exported_name.clone())) {
            return Err(ProviderTargetRefusal::ExportIdentityChanged);
        }
        let chunk = context
            .artifact
            .find_chunk(current.0)
            .filter(|chunk| chunk.analysis.entry_file == current.1)
            .ok_or(ProviderTargetRefusal::UnknownProvider)?;
        let aliases = chunk
            .analysis
            .export_aliases
            .iter()
            .filter(|alias| alias.exported == exported_name)
            .collect::<Vec<_>>();
        let [alias] = aliases.as_slice() else {
            return Err(ProviderTargetRefusal::ExportIdentityChanged);
        };

        let (specifier, next_export) = if let Some(source) = &alias.source {
            (
                source.as_str(),
                alias
                    .local
                    .as_deref()
                    .ok_or(ProviderTargetRefusal::ExportIdentityChanged)?,
            )
        } else {
            let local = alias
                .local
                .as_deref()
                .ok_or(ProviderTargetRefusal::ExportIdentityChanged)?;
            let matching_imports = chunk
                .analysis
                .imports
                .iter()
                .filter_map(|import| {
                    let specifier = import.specifiers.iter().find(|specifier| {
                        specifier.kind == ImportSpecifierKind::Named && specifier.local == local
                    })?;
                    Some((import, specifier))
                })
                .collect::<Vec<_>>();
            let [(import, specifier)] = matching_imports.as_slice() else {
                return Err(ProviderTargetRefusal::ExportIdentityChanged);
            };
            (
                import.source.as_str(),
                specifier.imported.as_deref().unwrap_or(local),
            )
        };

        let source_target = context
            .indexes
            .resolve_runtime_import_reference(
                specifier,
                current.0,
                &current.1,
                &context.artifact.chunk_table,
            )
            .ok_or(ProviderTargetRefusal::UnknownProvider)?;
        let candidate_target = resolver
            .resolve_runtime_import_reference(specifier, current.0, &current.1)
            .ok_or(ProviderTargetRefusal::ProviderRouteChanged)?;
        if source_target.target_chunk_id != candidate_target.target_chunk_id
            || source_target.target_file != candidate_target.target_file
        {
            return Err(ProviderTargetRefusal::ProviderRouteChanged);
        }
        current = (source_target.target_chunk_id, source_target.target_file);
        exported_name = next_export.to_owned();
    }
}

/// Recheck every prepared request in the body that contains the applied call.
/// Only paths entering the proved provider route are relevant to this target
/// obligation; other imports remain under their existing checks. The original
/// target comes from constructor-carried intent and the source resolver, while
/// the candidate target is resolved from the actual prepared output path.
fn preserve_consumer_request_targets(
    context: &ModuleInitializationContext<'_, '_>,
    resolver: &CandidateArtifactImportResolver<'_>,
    emitted_file: &str,
    source_file: &str,
    requests: &[PreparedStaticRequest],
    entry_body: bool,
    provider_closure: &BTreeSet<ModuleFile>,
) -> Result<(), ProviderTargetRefusal> {
    use ProviderTargetRefusal::{ProviderRouteChanged, UnsupportedRequest};

    let check = |request: &PreparedStaticRequest,
                 intended: Option<ModuleFile>,
                 unknown_intent: bool|
     -> Result<(), ProviderTargetRefusal> {
        let candidate = resolver.resolve_runtime_import_reference(
            &request.request.specifier,
            context.consumer,
            emitted_file,
        );
        let candidate_module = candidate
            .as_ref()
            .map(|target| (target.target_chunk_id, target.target_file.clone()));
        let intended_is_provider = intended
            .as_ref()
            .is_some_and(|target| provider_closure.contains(target));
        let candidate_is_provider = candidate_module
            .as_ref()
            .is_some_and(|target| provider_closure.contains(target));
        if !intended_is_provider && !candidate_is_provider {
            return Ok(());
        }
        if request.request.mode != StaticModuleRequestMode::OrdinaryEsm {
            return Err(UnsupportedRequest);
        }
        if unknown_intent || intended != candidate_module {
            return Err(if unknown_intent {
                UnsupportedRequest
            } else {
                ProviderRouteChanged
            });
        }
        let candidate = candidate.ok_or(ProviderRouteChanged)?;
        if !entry_body && candidate.kind != ImportReferenceKind::ArtifactPath {
            return Err(ProviderRouteChanged);
        }
        Ok(())
    };

    for request in requests {
        if request.targets.is_empty() {
            let candidate = resolver.resolve_runtime_import_reference(
                &request.request.specifier,
                context.consumer,
                emitted_file,
            );
            let candidate_is_provider = candidate.as_ref().is_some_and(|target| {
                provider_closure.contains(&(target.target_chunk_id, target.target_file.clone()))
            });
            if candidate_is_provider {
                return Err(UnsupportedRequest);
            }
            continue;
        }
        for target in &request.targets {
            match target {
                PreparedImportTarget::OriginalSource { specifier } => {
                    let intended = context
                        .indexes
                        .resolve_runtime_import_reference(
                            specifier,
                            context.consumer,
                            source_file,
                            &context.artifact.chunk_table,
                        )
                        .map(|resolved| (resolved.target_chunk_id, resolved.target_file));
                    check(request, intended, false)?;
                }
                PreparedImportTarget::UnsupportedOriginalSource { specifier } => {
                    let intended = context
                        .indexes
                        .resolve_runtime_import_reference(
                            specifier,
                            context.consumer,
                            source_file,
                            &context.artifact.chunk_table,
                        )
                        .map(|resolved| (resolved.target_chunk_id, resolved.target_file));
                    check(request, intended, true)?;
                }
                PreparedImportTarget::CandidateOutput { chunk, file } => {
                    check(request, Some((*chunk, file.clone())), false)?;
                }
                PreparedImportTarget::Unsupported => check(request, None, true)?,
            }
        }
    }
    Ok(())
}

fn preserve_provider_route_closure(
    context: &ModuleInitializationContext<'_, '_>,
    resolver: &CandidateArtifactImportResolver<'_>,
    first_hop: ModuleFile,
    defining_chunk: ChunkId,
) -> Result<BTreeSet<ModuleFile>, ProviderTargetRefusal> {
    let mut pending = vec![first_hop];
    let mut seen = BTreeSet::new();
    while let Some((chunk_id, file_path)) = pending.pop() {
        if chunk_id == context.consumer {
            return Err(ProviderTargetRefusal::ProviderRouteChanged);
        }
        if !seen.insert((chunk_id, file_path.clone())) {
            continue;
        }
        let chunk = context
            .artifact
            .find_chunk(chunk_id)
            .ok_or(ProviderTargetRefusal::UnknownProvider)?;
        if chunk.js.files.len() != 1 || chunk.analysis.entry_file != file_path {
            return Err(ProviderTargetRefusal::UnsupportedCandidateScope);
        }
        let file = chunk
            .js
            .get_file(&file_path)
            .ok_or(ProviderTargetRefusal::UnknownProvider)?;
        let mut source_targets = Vec::new();
        for request in &chunk.analysis.runtime_facts.static_requests {
            if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                return Err(ProviderTargetRefusal::UnsupportedRequest);
            }
            let source_target = context.indexes.resolve_runtime_import_reference(
                &request.specifier,
                chunk_id,
                &file_path,
                &context.artifact.chunk_table,
            );
            let candidate_target =
                resolver.resolve_runtime_import_reference(&request.specifier, chunk_id, &file_path);
            if source_target
                .as_ref()
                .map(|target| (target.target_chunk_id, target.target_file.as_str()))
                != candidate_target
                    .as_ref()
                    .map(|target| (target.target_chunk_id, target.target_file.as_str()))
            {
                return Err(ProviderTargetRefusal::ProviderRouteChanged);
            }
            let Some(candidate_target) = candidate_target else {
                continue;
            };
            if candidate_target.kind != ImportReferenceKind::ArtifactPath && file.ast().is_none() {
                return Err(ProviderTargetRefusal::ProviderRouteChanged);
            }
            source_targets.push((
                candidate_target.target_chunk_id,
                candidate_target.target_file,
            ));
        }
        pending.extend(source_targets);
    }
    if !seen.iter().any(|(chunk, _)| *chunk == defining_chunk) {
        return Err(ProviderTargetRefusal::ExportIdentityChanged);
    }
    Ok(seen)
}

fn project_prepared_layout<'a>(
    context: &ModuleInitializationContext<'a, '_>,
    prepared: &PreparedCandidateEmissionPlan<'_>,
) -> Result<CandidateArtifactImportResolver<'a>, ProviderTargetRefusal> {
    let inventories: Vec<_> = prepared.prepared_static_request_inventories().collect();
    let Some((entry_file, _, _, _)) = inventories.first() else {
        return Err(ProviderTargetRefusal::PreparedEmissionUnavailable);
    };
    let layout: Vec<_> = prepared.output_file_layout().collect();
    if inventories.len() != layout.len() {
        return Err(ProviderTargetRefusal::UnsupportedCandidateScope);
    }
    context
        .indexes
        .project_replaced_chunk_layout(
            context.consumer,
            entry_file,
            layout,
            &context.artifact.chunk_table,
        )
        .map_err(|_| ProviderTargetRefusal::UnsupportedCandidateScope)
}

/// Match each reached source read to the exact prepared import binding that
/// owns its body, then resolve that emitted request through the complete
/// output-first layout. This proves route identity; the graph check below
/// separately proves provider-before-consumer completion.
///
/// The surrounding guard limits this proof to one materialized consumer and
/// unchanged vendor planning. That makes the remaining whole-artifact passes
/// auditable: cross-chunk naturalization only rewrites processed importers and
/// obtains alias rows from materialized providers, so it cannot rename an
/// import into this retained provider; source canonicalization changes path
/// spelling only and the output-first resolver already checked its target.
/// Dead-import pruning sees the live owner body and keeps its referenced local;
/// provider closure entries are retained `Entry` files, which export pruning
/// skips. Outside that scope this pre-rewrite inventory is not final authority.
fn preserve_imported_read_routes(
    context: &ModuleInitializationContext<'_, '_>,
    prepared: &PreparedCandidateEmissionPlan<'_>,
    resolver: &CandidateArtifactImportResolver<'_>,
    required: &[analysis::facts::RequiredImportedPrimitiveRead],
) -> Result<BTreeSet<ChunkId>, ModuleInitializationRefusal> {
    use ModuleInitializationRefusal::{
        ImportedReadBindingChanged, ImportedReadClosureChanged, ImportedReadSiteUnavailable,
        ImportedReadTargetChanged, UnsupportedCandidateGraph,
    };

    let consumer_chunk = context
        .artifact
        .find_chunk(context.consumer)
        .ok_or(UnsupportedCandidateGraph)?;
    let source_entry = &consumer_chunk.analysis.entry_file;
    let assignment = context.assignment_analysis.assignment();
    let entry_destination = assignment.entry_destination().module_id();
    let mut provider_chunks = BTreeSet::new();

    for requirement in required {
        let Some(source_site) = requirement.source_site.as_ref() else {
            return Err(ImportedReadSiteUnavailable);
        };
        if source_site.owner() != requirement.source_owner {
            return Err(ImportedReadSiteUnavailable);
        }
        let consumer_root = requirement
            .consumer_root
            .ok_or(ImportedReadSiteUnavailable)?;
        if !assignment
            .iter()
            .any(|(source_unit, _)| source_unit.0 == consumer_root.0)
            || !assignment
                .iter()
                .any(|(source_unit, _)| source_unit.0 == requirement.source_owner.0)
        {
            return Err(ImportedReadBindingChanged);
        }

        let source_import = context
            .runtime_import_facts
            .imports
            .get(&requirement.source_binding)
            .ok_or(ImportedReadBindingChanged)?;
        let source_imported_name = match &source_import.kind {
            RuntimeImportKind::Named { imported } => imported.as_str(),
            RuntimeImportKind::Default => "default",
            RuntimeImportKind::Namespace => return Err(ImportedReadBindingChanged),
        };
        let Some(source_provider_closure) = requirement.provider_closure.as_ref() else {
            return Err(ImportedReadBindingChanged);
        };
        if !source_provider_closure.is_well_formed()
            || source_provider_closure.modules != requirement.dependency_closure
            || source_import.src != requirement.source_specifier
            || source_imported_name != requirement.imported_name
            || requirement.provider_export != requirement.imported_name
            || requirement.dependency_closure.is_empty()
            || !requirement
                .dependency_closure
                .contains(&requirement.provider_module)
            || requirement.source_cells.is_empty()
            || requirement
                .source_cells
                .iter()
                .any(|cell| !requirement.dependency_closure.contains(&cell.module))
        {
            return Err(ImportedReadBindingChanged);
        }

        let source_target = context
            .indexes
            .resolve_runtime_import_reference(
                &requirement.source_specifier,
                context.consumer,
                source_entry,
                &context.artifact.chunk_table,
            )
            .ok_or(ImportedReadTargetChanged)?;
        let provider_chunk = context
            .artifact
            .chunk_table
            .get(&requirement.provider_module)
            .ok_or(ImportedReadTargetChanged)?;
        let provider = context
            .artifact
            .find_chunk(provider_chunk)
            .ok_or(ImportedReadTargetChanged)?;
        if source_target.target_chunk_id != provider_chunk
            || source_target.target_file != provider.analysis.entry_file
        {
            return Err(ImportedReadTargetChanged);
        }

        let destination = assignment
            .iter()
            .find(|(source_unit, _)| source_unit.0 == requirement.source_owner.0)
            .map(|(_, destination)| destination)
            .ok_or(ImportedReadBindingChanged)?;
        let (emitted_file, _, requests, entry_body) = prepared
            .prepared_static_request_inventory_for_destination(destination, entry_destination)
            .ok_or(ImportedReadBindingChanged)?;
        let matching_bindings = requests
            .iter()
            .flat_map(|request| {
                request
                    .bindings
                    .iter()
                    .filter(move |binding| binding.source_binding == requirement.source_binding)
                    .map(move |binding| (request, binding))
            })
            .collect::<Vec<_>>();
        let [(request, binding)] = matching_bindings.as_slice() else {
            return Err(ImportedReadBindingChanged);
        };
        if request.request.mode != StaticModuleRequestMode::OrdinaryEsm
            || binding.imported_name != requirement.imported_name
            || !matches!(
                &binding.target,
                PreparedImportTarget::OriginalSource { specifier }
                    if specifier == &requirement.source_specifier
            )
        {
            return Err(ImportedReadBindingChanged);
        }

        let candidate_target = resolver
            .resolve_runtime_import_reference(
                &request.request.specifier,
                context.consumer,
                emitted_file,
            )
            .ok_or(ImportedReadTargetChanged)?;
        if (!entry_body && candidate_target.kind != ImportReferenceKind::ArtifactPath)
            || candidate_target.target_chunk_id != source_target.target_chunk_id
            || candidate_target.target_file != source_target.target_file
        {
            return Err(ImportedReadTargetChanged);
        }

        let provider_closure = preserve_provider_route_closure(
            context,
            resolver,
            (
                source_target.target_chunk_id,
                source_target.target_file.clone(),
            ),
            source_target.target_chunk_id,
        )
        .map_err(|_| ImportedReadClosureChanged)?;
        let candidate_closure_names = provider_closure
            .iter()
            .map(|(chunk, _)| context.artifact.chunk_table.name(*chunk).to_owned())
            .collect::<BTreeSet<_>>();
        if candidate_closure_names != requirement.dependency_closure {
            return Err(ImportedReadClosureChanged);
        }
        provider_chunks.insert(provider_chunk);
    }
    Ok(provider_chunks)
}

fn preserve_initialization(
    context: &ModuleInitializationContext<'_, '_>,
    required_cells: &BTreeSet<ModuleCellId>,
    required_imported_reads: &[analysis::facts::RequiredImportedPrimitiveRead],
) -> Result<(), ModuleInitializationRefusal> {
    use ModuleInitializationRefusal::{
        ForeignInitializationChanged, PreparedEmissionUnavailable, ProviderClosureChanged,
        ProviderIncompleteAtConsumer, UnsupportedCandidateGraph,
    };

    let prepared = context.prepared.ok_or(PreparedEmissionUnavailable)?;
    // This first stage proves one replaced chunk against retained provider
    // files. Multiple prepared chunks and vendor transforms need a joint plan.
    if context.materialized_chunks != &BTreeSet::from([context.consumer])
        || !context.vendor_unchanged
    {
        return Err(UnsupportedCandidateGraph);
    }
    if !required_cells.is_empty()
        && !context
            .source_initialized_cells
            .is_some_and(|cells| required_cells.is_subset(cells))
    {
        return Err(ProviderClosureChanged);
    }

    let inventories: Vec<_> = prepared.prepared_static_request_inventories().collect();
    let Some((entry_file, _, _, _)) = inventories.first() else {
        return Err(PreparedEmissionUnavailable);
    };
    let resolver =
        project_prepared_layout(context, prepared).map_err(|_| UnsupportedCandidateGraph)?;
    let read_provider_chunks =
        preserve_imported_read_routes(context, prepared, &resolver, required_imported_reads)?;

    let mut source = ModuleGraph::new();
    for chunk in &context.artifact.chunks {
        let runtime = &chunk.analysis.runtime_facts;
        if chunk.js.files.len() != 1
            || !runtime.module_kind.supports_static_esm_closure()
            || runtime.has_top_level_await
            || runtime.has_cjs_interop
        {
            return Err(UnsupportedCandidateGraph);
        }
        let file = &chunk.analysis.entry_file;
        let targets = resolve_requests(&runtime.static_requests, |specifier| {
            context.indexes.resolve_runtime_import_reference(
                specifier,
                chunk.chunk_id,
                file,
                &context.artifact.chunk_table,
            )
        })?;
        source.insert((chunk.chunk_id, file.clone()), targets);
    }
    if source
        .values()
        .flatten()
        .any(|target| !source.contains_key(target))
    {
        return Err(UnsupportedCandidateGraph);
    }
    let consumer_entry = context
        .artifact
        .find_chunk(context.consumer)
        .ok_or(UnsupportedCandidateGraph)?
        .analysis
        .entry_file
        .clone();
    let original_entry = (context.consumer, consumer_entry);
    let source_order = evaluation_order(&source, &original_entry, None)?;

    // A new output can shadow a source-path fallback without changing provider
    // completion. Retained imports must still select the defining source module
    // whose body receipt the analysis used.
    let consumer = context
        .artifact
        .find_chunk(context.consumer)
        .ok_or(UnsupportedCandidateGraph)?;
    let retained_targets = resolve_requests(
        &consumer.analysis.runtime_facts.static_requests,
        |specifier| {
            resolver.resolve_runtime_import_reference(specifier, context.consumer, entry_file)
        },
    )?;
    if source.get(&original_entry) != Some(&retained_targets) {
        return Err(ProviderClosureChanged);
    }
    let mut candidate = ModuleGraph::new();
    for chunk in &context.artifact.chunks {
        if chunk.chunk_id == context.consumer {
            continue;
        }
        let file = &chunk.analysis.entry_file;
        let original = chunk.js.get_file(file).ok_or(UnsupportedCandidateGraph)?;
        // Only retained AST entries take the late SourceCanonicalizer pass.
        // A raw pass-through source must already name actual output files.
        let canonicalized = matches!(&original.body, JsFileBody::Ast(_));
        let targets =
            resolve_requests(&chunk.analysis.runtime_facts.static_requests, |specifier| {
                resolver
                    .resolve_runtime_import_reference(specifier, chunk.chunk_id, file)
                    .filter(|target| {
                        canonicalized || target.kind == ImportReferenceKind::ArtifactPath
                    })
            })?;
        if source.get(&(chunk.chunk_id, file.clone())) != Some(&targets) {
            return Err(ForeignInitializationChanged);
        }
        candidate.insert((chunk.chunk_id, file.clone()), targets);
    }
    for (file, _, requests, entry_body) in &inventories {
        let targets = resolve_prepared_requests(requests, |specifier| {
            resolver
                .resolve_runtime_import_reference(specifier, context.consumer, file)
                // Materialized modules skip SourceCanonicalizer. In particular,
                // an unre-based named/star reexport is not a runtime edge merely
                // because original source-path fallback finds its provider.
                .filter(|target| *entry_body || target.kind == ImportReferenceKind::ArtifactPath)
        })?;
        if candidate
            .insert((context.consumer, (*file).to_owned()), targets)
            .is_some()
        {
            return Err(UnsupportedCandidateGraph);
        }
    }
    if candidate
        .values()
        .flatten()
        .any(|target| !candidate.contains_key(target))
    {
        return Err(UnsupportedCandidateGraph);
    }

    let mut providers = BTreeSet::new();
    let mut closure = BTreeSet::new();
    let mut provider_chunks = required_cells
        .iter()
        .map(|cell| cell.chunk)
        .collect::<BTreeSet<_>>();
    provider_chunks.extend(read_provider_chunks);
    for provider_chunk_id in provider_chunks {
        let provider = context
            .artifact
            .find_chunk(provider_chunk_id)
            .ok_or(ProviderClosureChanged)?;
        let entry = (provider_chunk_id, provider.analysis.entry_file.clone());
        if provider_chunk_id == context.consumer || !source_order.contains(&entry) {
            return Err(ProviderClosureChanged);
        }
        providers.insert(entry.clone());
        let mut pending = vec![entry];
        while let Some(module) = pending.pop() {
            if !closure.insert(module.clone()) {
                continue;
            }
            if context.materialized_chunks.contains(&module.0)
                || source.get(&module) != candidate.get(&module)
            {
                return Err(ProviderClosureChanged);
            }
            pending.extend(
                source
                    .get(&module)
                    .ok_or(ProviderClosureChanged)?
                    .iter()
                    .cloned(),
            );
        }
    }

    // A second foreign consumer may mutate the shared provider between calls.
    // Until linked events model that chronology, reject every such context in
    // the complete source graph, including callers through the entry chunk.
    let mut reverse = BTreeMap::<&ModuleFile, Vec<&ModuleFile>>::new();
    for (module, targets) in &source {
        for target in targets {
            reverse.entry(target).or_default().push(module);
        }
    }
    let mut reaching = closure.clone();
    let mut pending: Vec<_> = closure.iter().collect();
    while let Some(module) = pending.pop() {
        for parent in reverse.get(module).into_iter().flatten() {
            if reaching.insert((**parent).clone()) {
                pending.push(*parent);
            }
        }
    }
    if reaching
        .iter()
        .any(|module| module.0 != context.consumer && !closure.contains(module))
    {
        return Err(ForeignInitializationChanged);
    }

    let candidate_entry = (context.consumer, (*entry_file).to_owned());
    let candidate_order = evaluation_order(
        &candidate,
        &candidate_entry,
        Some((context.consumer, &providers)),
    )?;
    if source_order
        .iter()
        .filter(|module| module.0 != context.consumer)
        .ne(candidate_order
            .iter()
            .filter(|module| module.0 != context.consumer))
    {
        return Err(ForeignInitializationChanged);
    }
    if !providers
        .iter()
        .all(|provider| candidate_order.contains(provider))
    {
        return Err(ProviderIncompleteAtConsumer);
    }
    Ok(())
}

fn resolve_prepared_requests(
    requests: &[PreparedStaticRequest],
    mut resolve: impl FnMut(&str) -> Option<artifact::ResolvedImportReference>,
) -> Result<Vec<ModuleFile>, ModuleInitializationRefusal> {
    requests
        .iter()
        .map(|prepared| {
            let request = &prepared.request;
            if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                return Err(ModuleInitializationRefusal::UnsupportedCandidateGraph);
            }
            let target = resolve(&request.specifier)
                .ok_or(ModuleInitializationRefusal::UnsupportedCandidateGraph)?;
            Ok((target.target_chunk_id, target.target_file))
        })
        .collect()
}

fn resolve_requests(
    requests: &[StaticModuleRequest],
    mut resolve: impl FnMut(&str) -> Option<artifact::ResolvedImportReference>,
) -> Result<Vec<ModuleFile>, ModuleInitializationRefusal> {
    requests
        .iter()
        .map(|request| {
            if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                return Err(ModuleInitializationRefusal::UnsupportedCandidateGraph);
            }
            let target = resolve(&request.specifier)
                .ok_or(ModuleInitializationRefusal::UnsupportedCandidateGraph)?;
            Ok((target.target_chunk_id, target.target_file))
        })
        .collect()
}

/// DFS postorder is the synchronous ESM body order for an acyclic graph. Check
/// provider completion before every consumer file body, even one whose own
/// source units did not consume a cell summary. Duplicate requests keep their
/// first evaluation position.
fn evaluation_order(
    graph: &ModuleGraph,
    entry: &ModuleFile,
    consumer: Option<(ChunkId, &BTreeSet<ModuleFile>)>,
) -> Result<Vec<ModuleFile>, ModuleInitializationRefusal> {
    use ModuleInitializationRefusal::{ProviderIncompleteAtConsumer, UnsupportedCandidateGraph};

    let mut stack = vec![(entry.clone(), 0)];
    let mut active = BTreeSet::from([entry.clone()]);
    let mut complete = BTreeSet::new();
    let mut order = Vec::new();
    while let Some((module, next)) = stack.last_mut() {
        let requests = graph.get(module).ok_or(UnsupportedCandidateGraph)?;
        if let Some(target) = requests.get(*next) {
            *next += 1;
            if complete.contains(target) {
                continue;
            }
            if !active.insert(target.clone()) {
                return Err(UnsupportedCandidateGraph);
            }
            stack.push((target.clone(), 0));
        } else {
            if let Some((consumer_chunk, providers)) = consumer
                && module.0 == consumer_chunk
                && !providers.is_subset(&complete)
            {
                return Err(ProviderIncompleteAtConsumer);
            }
            let module = module.clone();
            stack.pop();
            active.remove(&module);
            complete.insert(module.clone());
            order.push(module);
        }
    }
    Ok(order)
}
