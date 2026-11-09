//! Reconcile typed source/lowering routes with the exact final emitted files.
//!
//! This check is intentionally stricter than report projection: every final
//! static request must have one typed prepared route, and every route must
//! still identify one final request. The check proves neither provider values
//! nor host behavior; it establishes only request/binding identity and the
//! module graph on which the separate initialization obligations rely.

use std::collections::{BTreeMap, BTreeSet};

use artifact::reports::{
    CandidateImportTarget, CandidateOutputRequestKind, CandidatePreparedRequestBinding,
    CandidatePreparedRequestRoute, CandidateRewriteDisposition, CandidateRewriteElision,
    CandidateRewriteStage, CandidateSourceBindingRoute, CandidateSourceCorrespondence,
    CandidateVendorBindingReplacement, CandidateVendorDestination, CandidateVendorPlanAction,
    CandidateVendorSourceUse, CandidateVendorUseResult, ImportSpecifierKind, OutputFileAddress,
    ParsedSourceImports, SourceCorrespondenceStatus,
};
use artifact::{ArtifactIndexes, ChunkBundle, FileRole, JsFile, JsFileBody};
use source_identity::{ChunkId, ModuleCellId, SourceUnitOrdinal};
use swc_common::{Span, Spanned};
use swc_ecma_ast::{
    ExportSpecifier, ImportDecl, ImportSpecifier, ModuleDecl, ModuleItem, NamedExport,
};
use swc_ecma_visit::{Visit, VisitWith};

use crate::assignment_analysis::CandidateRelocationAssessmentReport;
use crate::final_global_resolution::{
    FinalGlobalResolutionSourceInputs, GlobalResolutionFinding, GlobalResolutionReason,
    GlobalResolutionStatus, RawSourceIdentityProof,
};

#[derive(Debug, Default)]
pub(crate) struct FinalSourceCorrespondenceResult {
    pub status: GlobalResolutionStatus,
    pub per_chunk_status: BTreeMap<ChunkId, GlobalResolutionStatus>,
    pub findings: BTreeMap<ChunkId, Vec<GlobalResolutionFinding>>,
    pub graph: BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
    pub preserved_cells: BTreeSet<ModuleCellId>,
    pub preserved_cell_uses: BTreeSet<(ChunkId, SourceUnitOrdinal, ModuleCellId)>,
    pub preserved_imported_reads: Vec<analysis::facts::RequiredImportedPrimitiveRead>,
    pub preserved_provider_targets: Vec<analysis::facts::ConditionalProviderTargetRequirement>,
}

impl Default for GlobalResolutionStatus {
    fn default() -> Self {
        Self::Preserved
    }
}

#[derive(Debug, Clone)]
struct ActualRequest {
    output: OutputFileAddress,
    item_index: usize,
    item_span: Span,
    kind: CandidateOutputRequestKind,
    /// `None` retains a static request whose WTF-8 specifier cannot be
    /// represented by the correspondence ABI. It remains in the census and
    /// is refused as unmatched rather than disappearing as a non-request.
    request: Option<String>,
    bindings: Vec<ActualRequestBinding>,
}

#[derive(Debug, Clone)]
struct ActualRequestBinding {
    local: Option<swc_ecma_ast::Id>,
    specifier_index: usize,
    specifier_span: Span,
    imported_name: Option<String>,
    exported_name: Option<String>,
    kind: ImportSpecifierKind,
}

/// Check the final request inventory and request targets against typed
/// correspondence rows. Unsupported or missing identities are incomplete;
/// an observed target/binding disagreement is a changed candidate.
pub(crate) fn assess_final_source_correspondence(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    reports: &BTreeMap<String, CandidateRelocationAssessmentReport>,
) -> FinalSourceCorrespondenceResult {
    let mut result = FinalSourceCorrespondenceResult::default();
    let actual = collect_actual_requests(files);
    let indexes = ArtifactIndexes::build(files).ok();
    let route_view = VerifiedFinalCandidateRoutes::build(
        source_inputs,
        correspondence,
        files,
        indexes.as_ref(),
        &actual,
    );
    let correspondence = route_view.correspondence();
    let mut matched_actual = BTreeSet::<(ChunkId, String, usize, u32, u32)>::new();
    let mut final_graph = BTreeMap::<OutputFileAddress, BTreeSet<OutputFileAddress>>::new();
    for chunk in &files.chunks {
        for file in &chunk.js.files {
            final_graph
                .entry(OutputFileAddress {
                    chunk: chunk.chunk_id,
                    file: file.path.clone(),
                })
                .or_default();
        }
    }

    for source_chunk in &source_inputs.required_source_chunks {
        result
            .per_chunk_status
            .entry(*source_chunk)
            .or_insert(GlobalResolutionStatus::Preserved);
        let mut status = GlobalResolutionStatus::Preserved;
        let Some(source_name) = source_inputs.source_chunk_names.get(source_chunk) else {
            record(
                &mut result,
                *source_chunk,
                GlobalResolutionStatus::Incomplete,
                finding(GlobalResolutionReason::SourceIdentityMismatch, None),
            );
            continue;
        };
        let Some(row) = correspondence.get(source_chunk) else {
            record(
                &mut result,
                *source_chunk,
                GlobalResolutionStatus::Incomplete,
                finding(GlobalResolutionReason::SourceCorrespondenceMissing, None),
            );
            continue;
        };
        if row.source_file.is_empty()
            || !source_inputs
                .source_uses
                .get(source_chunk)
                .is_some_and(|manifest| manifest.source_file == row.source_file)
        {
            record(
                &mut result,
                *source_chunk,
                GlobalResolutionStatus::Incomplete,
                finding(GlobalResolutionReason::SourceIdentityMismatch, None),
            );
            status = combine(status, GlobalResolutionStatus::Incomplete);
        }

        let canonical_source_imports = source_inputs
            .source_imports
            .get(source_chunk)
            .and_then(Option::as_deref);
        let correspondence_source_imports = ParsedSourceImports {
            source_file: row.source_file.clone(),
            requests: row
                .source_requests
                .iter()
                .map(|request| request.source.clone())
                .collect(),
            bindings: row
                .imports
                .iter()
                .map(|binding| binding.source.clone())
                .collect(),
            reexports: row
                .reexports
                .iter()
                .map(|reexport| reexport.source.clone())
                .collect(),
            exports: row
                .exports
                .iter()
                .map(|export| export.source.clone())
                .collect(),
        };
        if canonical_source_imports != Some(&correspondence_source_imports) {
            record(
                &mut result,
                *source_chunk,
                GlobalResolutionStatus::Incomplete,
                finding(GlobalResolutionReason::SourceCorrespondenceMissing, None),
            );
            status = combine(status, GlobalResolutionStatus::Incomplete);
        }

        for request in &row.prepared_requests {
            if request.status != SourceCorrespondenceStatus::Complete {
                record(
                    &mut result,
                    *source_chunk,
                    GlobalResolutionStatus::Incomplete,
                    finding(
                        GlobalResolutionReason::SourceCorrespondenceIncomplete,
                        Some(format!("{}/{}", source_name, request.output.file)),
                    ),
                );
                status = combine(status, GlobalResolutionStatus::Incomplete);
            }
            let matches = actual
                .iter()
                .filter(|candidate| {
                    candidate.output == request.output
                        && candidate.item_span == request.item_span
                        && candidate.kind == request.request_kind
                })
                .collect::<Vec<_>>();
            let raw_exact = matches.is_empty()
                && raw_request_is_exact(source_inputs, *source_chunk, files, &request.output)
                && row.source_requests.iter().any(|source_request| {
                    source_request.routes.iter().any(|route| {
                        route.output == request.output
                            && route.item_span == request.item_span
                            && route.emitted_request == request.emitted_request
                    })
                });
            if matches.len() != 1 && !raw_exact {
                record(
                    &mut result,
                    *source_chunk,
                    GlobalResolutionStatus::Incomplete,
                    finding(
                        GlobalResolutionReason::SourceRequestUnmatched,
                        Some(format!("{}/{}", source_name, request.output.file)),
                    ),
                );
                status = combine(status, GlobalResolutionStatus::Incomplete);
                continue;
            }
            let (candidate_request, candidate_output) = if raw_exact {
                (&request.emitted_request, &request.output)
            } else {
                let candidate = matches[0];
                let Some(candidate_request) = candidate.request.as_deref() else {
                    record(
                        &mut result,
                        *source_chunk,
                        GlobalResolutionStatus::Incomplete,
                        finding(
                            GlobalResolutionReason::SourceRequestUnmatched,
                            Some(format!("{}/{}", source_name, request.output.file)),
                        ),
                    );
                    status = combine(status, GlobalResolutionStatus::Incomplete);
                    continue;
                };
                matched_actual.insert((
                    candidate.output.chunk,
                    candidate.output.file.clone(),
                    candidate.item_index,
                    candidate.item_span.lo.0,
                    candidate.item_span.hi.0,
                ));
                if candidate.item_index != request.item_index
                    || candidate_request != request.emitted_request
                    || !candidate_bindings_match(request, candidate)
                {
                    record(
                        &mut result,
                        *source_chunk,
                        GlobalResolutionStatus::Changed,
                        finding(
                            GlobalResolutionReason::SourceRequestChanged,
                            Some(format!("{}/{}", source_name, request.output.file)),
                        ),
                    );
                    status = combine(status, GlobalResolutionStatus::Changed);
                    continue;
                }
                (candidate_request, &candidate.output)
            };
            if let Some(indexes) = &indexes {
                let Some(resolved) = indexes.resolve_runtime_import_reference(
                    candidate_request,
                    candidate_output.chunk,
                    &candidate_output.file,
                    &files.chunk_table,
                ) else {
                    if !request_and_binding_targets_match(
                        source_inputs,
                        *source_chunk,
                        row,
                        &request.targets,
                        &request.bindings,
                        candidate_request,
                        None,
                        raw_request_is_exact(source_inputs, *source_chunk, files, candidate_output),
                    ) {
                        record(
                            &mut result,
                            *source_chunk,
                            GlobalResolutionStatus::Incomplete,
                            finding(
                                GlobalResolutionReason::SourceRequestTargetUnproved,
                                Some(format!("{}/{}", source_name, request.output.file)),
                            ),
                        );
                        status = combine(status, GlobalResolutionStatus::Incomplete);
                    }
                    continue;
                };
                let actual_target = OutputFileAddress {
                    chunk: resolved.target_chunk_id,
                    file: resolved.target_file,
                };
                final_graph
                    .entry(candidate_output.clone())
                    .or_default()
                    .insert(actual_target.clone());
                if !request_and_binding_targets_match(
                    source_inputs,
                    *source_chunk,
                    row,
                    &request.targets,
                    &request.bindings,
                    candidate_request,
                    Some(&actual_target),
                    raw_request_is_exact(source_inputs, *source_chunk, files, candidate_output),
                ) {
                    record(
                        &mut result,
                        *source_chunk,
                        GlobalResolutionStatus::Incomplete,
                        finding(
                            GlobalResolutionReason::SourceRequestTargetUnproved,
                            Some(format!("{}/{}", source_name, request.output.file)),
                        ),
                    );
                    status = combine(status, GlobalResolutionStatus::Incomplete);
                }
            } else if !raw_exact {
                record(
                    &mut result,
                    *source_chunk,
                    GlobalResolutionStatus::Incomplete,
                    finding(
                        GlobalResolutionReason::SourceRequestTargetUnproved,
                        Some(format!("{}/{}", source_name, request.output.file)),
                    ),
                );
                status = combine(status, GlobalResolutionStatus::Incomplete);
            }
        }

        // Source requests are independent obligations from prepared requests:
        // a dropped request needs an exact receipt to discharge it. An empty
        // route list alone never counts as proof.
        for source_request in &row.source_requests {
            if source_request.status != SourceCorrespondenceStatus::Complete {
                record(
                    &mut result,
                    *source_chunk,
                    GlobalResolutionStatus::Incomplete,
                    finding(
                        GlobalResolutionReason::SourceCorrespondenceIncomplete,
                        Some(format!("{}/source-request", source_name)),
                    ),
                );
                status = combine(status, GlobalResolutionStatus::Incomplete);
                continue;
            }
            if !source_request_specifiers_are_accounted(row, source_request) {
                record(
                    &mut result,
                    *source_chunk,
                    GlobalResolutionStatus::Incomplete,
                    finding(
                        GlobalResolutionReason::SourceCorrespondenceIncomplete,
                        Some(format!("{}/source-request", source_name)),
                    ),
                );
                status = combine(status, GlobalResolutionStatus::Incomplete);
                continue;
            }
            if source_request.routes.is_empty() {
                continue;
            }
            for route in &source_request.routes {
                if route.status != SourceCorrespondenceStatus::Complete
                    || route.source_statement != Some(source_request.source.statement)
                {
                    record(
                        &mut result,
                        *source_chunk,
                        GlobalResolutionStatus::Incomplete,
                        finding(
                            GlobalResolutionReason::SourceCorrespondenceIncomplete,
                            Some(format!("{}/{}", source_name, route.output.file)),
                        ),
                    );
                    status = combine(status, GlobalResolutionStatus::Incomplete);
                    continue;
                }
                let candidate_kind = source_request_output_kind(source_request.source.kind);
                let matches = actual
                    .iter()
                    .filter(|candidate| {
                        candidate.output == route.output
                            && candidate.item_span == route.item_span
                            && candidate.kind == candidate_kind
                    })
                    .collect::<Vec<_>>();
                let raw_exact = matches.is_empty()
                    && raw_request_is_exact(source_inputs, *source_chunk, files, &route.output);
                if matches.len() != 1 && !raw_exact {
                    record(
                        &mut result,
                        *source_chunk,
                        GlobalResolutionStatus::Incomplete,
                        finding(
                            GlobalResolutionReason::SourceRequestUnmatched,
                            Some(format!("{}/{}", source_name, route.output.file)),
                        ),
                    );
                    status = combine(status, GlobalResolutionStatus::Incomplete);
                    continue;
                }
                let (candidate_request, candidate_output) = if raw_exact {
                    (&route.emitted_request, &route.output)
                } else {
                    let candidate = matches[0];
                    let Some(candidate_request) = candidate.request.as_deref() else {
                        record(
                            &mut result,
                            *source_chunk,
                            GlobalResolutionStatus::Incomplete,
                            finding(
                                GlobalResolutionReason::SourceRequestUnmatched,
                                Some(format!("{}/{}", source_name, route.output.file)),
                            ),
                        );
                        status = combine(status, GlobalResolutionStatus::Incomplete);
                        continue;
                    };
                    matched_actual.insert((
                        candidate.output.chunk,
                        candidate.output.file.clone(),
                        candidate.item_index,
                        candidate.item_span.lo.0,
                        candidate.item_span.hi.0,
                    ));
                    if candidate.item_index != route.item_index
                        || candidate_request != route.emitted_request
                        || !request_bindings_match(&route.bindings, &candidate.bindings)
                    {
                        record(
                            &mut result,
                            *source_chunk,
                            GlobalResolutionStatus::Changed,
                            finding(
                                GlobalResolutionReason::SourceRequestChanged,
                                Some(format!("{}/{}", source_name, route.output.file)),
                            ),
                        );
                        status = combine(status, GlobalResolutionStatus::Changed);
                        continue;
                    }
                    (candidate_request, &candidate.output)
                };
                if let Some(indexes) = &indexes {
                    if let Some(resolved) = indexes.resolve_runtime_import_reference(
                        candidate_request,
                        candidate_output.chunk,
                        &candidate_output.file,
                        &files.chunk_table,
                    ) {
                        let actual_target = OutputFileAddress {
                            chunk: resolved.target_chunk_id,
                            file: resolved.target_file,
                        };
                        final_graph
                            .entry(candidate_output.clone())
                            .or_default()
                            .insert(actual_target.clone());
                        if !request_and_binding_targets_match(
                            source_inputs,
                            *source_chunk,
                            row,
                            &route.targets,
                            &route.bindings,
                            candidate_request,
                            Some(&actual_target),
                            raw_request_is_exact(
                                source_inputs,
                                *source_chunk,
                                files,
                                candidate_output,
                            ),
                        ) {
                            record(
                                &mut result,
                                *source_chunk,
                                GlobalResolutionStatus::Incomplete,
                                finding(
                                    GlobalResolutionReason::SourceRequestTargetUnproved,
                                    Some(format!("{}/{}", source_name, route.output.file)),
                                ),
                            );
                            status = combine(status, GlobalResolutionStatus::Incomplete);
                        }
                    } else if !request_and_binding_targets_match(
                        source_inputs,
                        *source_chunk,
                        row,
                        &route.targets,
                        &route.bindings,
                        candidate_request,
                        None,
                        raw_request_is_exact(source_inputs, *source_chunk, files, candidate_output),
                    ) {
                        record(
                            &mut result,
                            *source_chunk,
                            GlobalResolutionStatus::Incomplete,
                            finding(
                                GlobalResolutionReason::SourceRequestTargetUnproved,
                                Some(format!("{}/{}", source_name, route.output.file)),
                            ),
                        );
                        status = combine(status, GlobalResolutionStatus::Incomplete);
                    }
                }
            }
        }

        assess_source_imports(
            *source_chunk,
            source_inputs,
            &route_view,
            row,
            files,
            indexes.as_ref(),
            &actual,
            &mut result,
        );
        assess_source_exports(*source_chunk, source_inputs, row, files, &mut result);

        if row.status == SourceCorrespondenceStatus::MissingSourceFacts {
            record(
                &mut result,
                *source_chunk,
                GlobalResolutionStatus::Incomplete,
                finding(GlobalResolutionReason::SourceCorrespondenceMissing, None),
            );
            status = combine(status, GlobalResolutionStatus::Incomplete);
        }
        result
            .per_chunk_status
            .entry(*source_chunk)
            .and_modify(|current| *current = combine(*current, status))
            .or_insert(status);
    }

    for candidate in &actual {
        let key = (
            candidate.output.chunk,
            candidate.output.file.clone(),
            candidate.item_index,
            candidate.item_span.lo.0,
            candidate.item_span.hi.0,
        );
        if !matched_actual.contains(&key) {
            record(
                &mut result,
                candidate.output.chunk,
                GlobalResolutionStatus::Incomplete,
                finding(
                    GlobalResolutionReason::UnplannedSourceRequest,
                    Some(format!(
                        "{}/{}",
                        files.chunk_table.name(candidate.output.chunk),
                        candidate.output.file
                    )),
                ),
            );
        }
    }

    assess_runtime_obligations(
        source_inputs,
        &route_view,
        files,
        reports,
        &final_graph,
        &mut result,
    );

    result.status = aggregate(result.per_chunk_status.values().copied());
    result.graph = final_graph;
    result
}

fn collect_actual_requests(bundle: &ChunkBundle) -> Vec<ActualRequest> {
    let mut requests = Vec::new();
    for chunk in &bundle.chunks {
        for file in &chunk.js.files {
            let Some(ast) = file.ast() else {
                continue;
            };
            let output = OutputFileAddress {
                chunk: chunk.chunk_id,
                file: file.path.clone(),
            };
            for (item_index, item) in ast.module.body.iter().enumerate() {
                if let Some(request) = actual_request(&output, item_index, item) {
                    requests.push(request);
                }
            }
        }
    }
    requests
}

fn actual_request(
    output: &OutputFileAddress,
    item_index: usize,
    item: &ModuleItem,
) -> Option<ActualRequest> {
    let (kind, request, bindings) = match item {
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => (
            CandidateOutputRequestKind::Import,
            import.src.value.as_str().map(str::to_owned),
            import_bindings(import),
        ),
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) if export.src.is_some() => (
            CandidateOutputRequestKind::NamedReExport,
            export
                .src
                .as_ref()
                .and_then(|src| src.value.as_str())
                .map(str::to_owned),
            reexport_bindings(export),
        ),
        ModuleItem::ModuleDecl(ModuleDecl::ExportAll(export)) => (
            CandidateOutputRequestKind::ExportAll,
            export.src.value.as_str().map(str::to_owned),
            Vec::new(),
        ),
        _ => return None,
    };
    Some(ActualRequest {
        output: output.clone(),
        item_index,
        item_span: item.span(),
        kind,
        request,
        bindings,
    })
}

fn import_bindings(import: &ImportDecl) -> Vec<ActualRequestBinding> {
    import
        .specifiers
        .iter()
        .enumerate()
        .map(|(specifier_index, specifier)| match specifier {
            ImportSpecifier::Named(named) => ActualRequestBinding {
                local: Some(named.local.to_id()),
                specifier_index,
                specifier_span: named.span,
                imported_name: Some(
                    named
                        .imported
                        .as_ref()
                        .map(binding_targets::module_export_name)
                        .unwrap_or_else(|| named.local.sym.to_string()),
                ),
                exported_name: None,
                kind: ImportSpecifierKind::Named,
            },
            ImportSpecifier::Default(default) => ActualRequestBinding {
                local: Some(default.local.to_id()),
                specifier_index,
                specifier_span: default.span,
                imported_name: Some("default".to_string()),
                exported_name: None,
                kind: ImportSpecifierKind::Default,
            },
            ImportSpecifier::Namespace(namespace) => ActualRequestBinding {
                local: Some(namespace.local.to_id()),
                specifier_index,
                specifier_span: namespace.span,
                imported_name: Some("*".to_string()),
                exported_name: None,
                kind: ImportSpecifierKind::Namespace,
            },
        })
        .collect()
}

fn reexport_bindings(export: &NamedExport) -> Vec<ActualRequestBinding> {
    export
        .specifiers
        .iter()
        .enumerate()
        .filter_map(|(specifier_index, specifier)| match specifier {
            ExportSpecifier::Named(named) if !named.is_type_only => Some(ActualRequestBinding {
                local: None,
                specifier_index,
                specifier_span: named.span,
                imported_name: Some(binding_targets::module_export_name(&named.orig)),
                exported_name: Some(
                    named
                        .exported
                        .as_ref()
                        .map(binding_targets::module_export_name)
                        .unwrap_or_else(|| binding_targets::module_export_name(&named.orig)),
                ),
                kind: ImportSpecifierKind::Named,
            }),
            ExportSpecifier::Namespace(namespace) => Some(ActualRequestBinding {
                local: None,
                specifier_index,
                specifier_span: namespace.span,
                imported_name: None,
                exported_name: Some(binding_targets::module_export_name(&namespace.name)),
                kind: ImportSpecifierKind::Namespace,
            }),
            _ => None,
        })
        .collect()
}

fn candidate_bindings_match(route: &CandidatePreparedRequestRoute, actual: &ActualRequest) -> bool {
    request_bindings_match(&route.bindings, &actual.bindings)
}

fn request_bindings_match(
    expected_bindings: &[CandidatePreparedRequestBinding],
    actual_bindings: &[ActualRequestBinding],
) -> bool {
    if expected_bindings.len() != actual_bindings.len() {
        return false;
    }
    expected_bindings
        .iter()
        .zip(actual_bindings)
        .all(|(expected, actual)| {
            expected.specifier_index == actual.specifier_index
                && expected.specifier_span == actual.specifier_span
                && expected.imported_name == actual.imported_name
                && expected.exported_name == actual.exported_name
                && expected.kind == actual.kind
                && actual.local.as_ref() == expected.candidate_local.as_ref()
        })
}

fn request_and_binding_targets_match(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    source_chunk: ChunkId,
    correspondence: &CandidateSourceCorrespondence,
    request_targets: &[CandidateImportTarget],
    bindings: &[CandidatePreparedRequestBinding],
    emitted_request: &str,
    actual: Option<&OutputFileAddress>,
    raw_identity: bool,
) -> bool {
    request_targets_match_targets(
        source_inputs,
        source_chunk,
        correspondence,
        request_targets,
        emitted_request,
        actual,
        raw_identity,
    ) && bindings.iter().all(|binding| {
        request_targets_match_targets(
            source_inputs,
            source_chunk,
            correspondence,
            std::slice::from_ref(&binding.target),
            emitted_request,
            actual,
            raw_identity,
        )
    })
}

fn source_request_output_kind(
    kind: artifact::reports::SourceStaticRequestKind,
) -> CandidateOutputRequestKind {
    match kind {
        artifact::reports::SourceStaticRequestKind::Import => CandidateOutputRequestKind::Import,
        artifact::reports::SourceStaticRequestKind::NamedReExport => {
            CandidateOutputRequestKind::NamedReExport
        }
        artifact::reports::SourceStaticRequestKind::ExportAll => {
            CandidateOutputRequestKind::ExportAll
        }
    }
}

fn source_request_specifiers_are_accounted(
    row: &CandidateSourceCorrespondence,
    request: &artifact::reports::CandidateSourceStaticRequestRoutes,
) -> bool {
    use artifact::reports::SourceStaticRequestKind;

    match request.source.kind {
        SourceStaticRequestKind::Import => {
            if request.source.specifier_spans.is_empty() {
                return !request.routes.is_empty()
                    && request.routes.iter().all(|route| route.bindings.is_empty());
            }
            request
                .source
                .specifier_spans
                .iter()
                .enumerate()
                .all(|(index, specifier_span)| {
                    let bindings = row
                        .imports
                        .iter()
                        .filter(|import| {
                            import.source.statement == request.source.statement
                                && import.source.specifier_index == index
                        })
                        .collect::<Vec<_>>();
                    let [binding] = bindings.as_slice() else {
                        return false;
                    };
                    let active = request.routes.iter().any(|route| {
                        route.bindings.iter().any(|candidate| {
                            candidate.source_binding.as_ref() == Some(&binding.source.local)
                                && candidate.specifier_span == *specifier_span
                                && candidate.imported_name.as_deref()
                                    == Some(binding.source.imported_name.as_str())
                                && candidate.kind == binding.source.kind
                        })
                    });
                    let elided = exact_dead_import_elision(
                        row,
                        &request.source,
                        &binding.source,
                        *specifier_span,
                        &request.routes,
                    );
                    let replaced = vendor_replacement_is_present(
                        row,
                        &request.source,
                        &binding.source,
                        *specifier_span,
                    );
                    let naturalized = naturalized_import_route_is_present(
                        row,
                        request,
                        &binding.source,
                        *specifier_span,
                    );
                    usize::from(active)
                        + usize::from(elided)
                        + usize::from(replaced)
                        + usize::from(naturalized)
                        == 1
                })
        }
        SourceStaticRequestKind::NamedReExport => {
            if request.source.specifier_spans.is_empty() {
                return false;
            }
            request
                .source
                .specifier_spans
                .iter()
                .enumerate()
                .all(|(index, specifier_span)| {
                    let reexports = row
                        .reexports
                        .iter()
                        .filter(|reexport| {
                            reexport.source.statement == request.source.statement
                                && reexport.source.request == request.source.request
                                && reexport.source.specifier == Some(index)
                                && reexport.source.specifier_span == Some(*specifier_span)
                        })
                        .collect::<Vec<_>>();
                    let [reexport] = reexports.as_slice() else {
                        return false;
                    };
                    reexport.status == SourceCorrespondenceStatus::Complete
                        && request.routes.iter().any(|route| {
                            route.bindings.iter().any(|candidate| {
                                candidate.specifier_span == *specifier_span
                                    && candidate.imported_name == reexport.source.imported_name
                                    && candidate.exported_name == reexport.source.exported_name
                                    && candidate.kind == reexport_kind(reexport.source.kind)
                                    && candidate.source_binding.is_none()
                                    && candidate.candidate_local.is_none()
                            })
                        })
                })
        }
        SourceStaticRequestKind::ExportAll => {
            request.source.specifier_spans.is_empty() && !request.routes.is_empty()
        }
    }
}

fn naturalized_import_route_is_present(
    row: &CandidateSourceCorrespondence,
    request: &artifact::reports::CandidateSourceStaticRequestRoutes,
    source_binding: &artifact::reports::ParsedSourceImportBinding,
    source_specifier_span: Span,
) -> bool {
    if source_binding.kind != ImportSpecifierKind::Named {
        return false;
    }
    let imports = row
        .imports
        .iter()
        .filter(|import| {
            import.source.statement == request.source.statement
                && import.source.declaration_span == request.source.declaration_span
                && import.source.specifier_index == source_binding.specifier_index
                && import.source.local == source_binding.local
                && import.source.imported_name == source_binding.imported_name
                && import.source.request == request.source.request
                && import.status == SourceCorrespondenceStatus::Complete
        })
        .collect::<Vec<_>>();
    let [import] = imports.as_slice() else {
        return false;
    };
    let mut matched_transitions = 0;
    for receipt in &row.rewrite_receipts {
        if receipt.source_chunk != row.source_chunk
            || receipt.stage != CandidateRewriteStage::CrossChunkNaturalization
            || receipt.issue.is_some()
            || receipt.disposition != CandidateRewriteDisposition::Rewritten
        {
            continue;
        }
        let (Some(before), Some(after)) = (receipt.before.as_ref(), receipt.after.as_ref()) else {
            continue;
        };
        if before.kind != CandidateOutputRequestKind::Import
            || after.kind != CandidateOutputRequestKind::Import
            || before.source_statement != Some(request.source.statement)
            || after.source_statement != Some(request.source.statement)
            || before.request.as_deref() != Some(request.source.request.as_str())
            || after.request != before.request
            || before.item_span != after.item_span
            || before.targets != after.targets
            || before.mode != Some(request.source.mode)
            || after.mode != before.mode
        {
            continue;
        }
        let before_bindings = before
            .bindings
            .iter()
            .filter(|candidate| {
                candidate.specifier_index == source_binding.specifier_index
                    && candidate.specifier_span == source_specifier_span
                    && candidate.source_binding.as_ref() == Some(&source_binding.local)
            })
            .collect::<Vec<_>>();
        let after_bindings = after
            .bindings
            .iter()
            .filter(|candidate| {
                candidate.specifier_index == source_binding.specifier_index
                    && candidate.specifier_span == source_specifier_span
                    && candidate.source_binding.as_ref() == Some(&source_binding.local)
            })
            .collect::<Vec<_>>();
        let ([before_binding], [after_binding]) =
            (before_bindings.as_slice(), after_bindings.as_slice())
        else {
            continue;
        };
        let (
            Some(cell),
            Some(before_target),
            Some(after_target),
            Some(after_local),
            Some(after_name),
        ) = (
            before_binding.source_cell,
            before_binding.target.as_ref(),
            after_binding.target.as_ref(),
            after_binding.local.as_ref(),
            after_binding.imported_name.as_deref(),
        )
        else {
            continue;
        };
        if before_binding.imported_name.as_deref() != Some(source_binding.imported_name.as_str())
            || before_binding.kind != Some(source_binding.kind)
            || after_name == source_binding.imported_name
            || after_binding.kind != Some(source_binding.kind)
            || after_binding.source_cell != Some(cell)
            || before_target != after_target
        {
            continue;
        }
        let candidate_routes = request
            .routes
            .iter()
            .filter(|route| {
                route.output == receipt.output
                    && route.status == SourceCorrespondenceStatus::Complete
                    && route.item_span == after.item_span
                    && route.source_statement == Some(request.source.statement)
                    && route.emitted_request == request.source.request
                    && route.mode == request.source.mode
                    && route.targets == after.targets
                    && route.bindings.iter().any(|candidate| {
                        candidate.specifier_index == source_binding.specifier_index
                            && candidate.specifier_span == source_specifier_span
                            && candidate.source_binding.as_ref() == Some(&source_binding.local)
                            && candidate.candidate_local.as_ref() == Some(after_local)
                            && candidate.imported_name.as_deref() == Some(after_name)
                            && candidate.kind == after_binding.kind
                            && candidate.source_cell == after_binding.source_cell
                            && candidate.target.as_ref() == Some(after_target)
                    })
            })
            .count();
        let source_routes = import
            .routes
            .iter()
            .filter(|route| {
                route.output == receipt.output
                    && route.item_span == after.item_span
                    && route.specifier_index == source_binding.specifier_index
                    && route.specifier_span == source_specifier_span
                    && &route.candidate_local == after_local
                    && route.imported_name == after_name
                    && route.kind == source_binding.kind
                    && route.source_cell == Some(cell)
                    && &route.target == after_target
            })
            .count();
        if candidate_routes == 1 && source_routes == 1 {
            matched_transitions += 1;
        }
    }
    matched_transitions == 1
}

fn vendor_replacement_is_present(
    row: &CandidateSourceCorrespondence,
    source_request: &artifact::reports::ParsedSourceRequest,
    source_binding: &artifact::reports::ParsedSourceImportBinding,
    source_specifier_span: Span,
) -> bool {
    row.rewrite_receipts.iter().any(|receipt| {
        if receipt.source_chunk != row.source_chunk
            || receipt.stage != CandidateRewriteStage::EmissionVendorRewrite
            || receipt.issue.is_some()
        {
            return false;
        }
        let CandidateRewriteDisposition::Replaced(replacement) = &receipt.disposition else {
            return false;
        };
        vendor_source_request_matches(row, source_request, &replacement.source_request)
            && replacement.bindings.iter().any(|binding| {
                vendor_source_binding_matches(source_binding, source_specifier_span, binding)
            })
    })
}

fn vendor_source_request_matches(
    row: &CandidateSourceCorrespondence,
    source: &artifact::reports::ParsedSourceRequest,
    replacement: &artifact::reports::CandidateVendorSourceRequestIdentity,
) -> bool {
    replacement.source_chunk == row.source_chunk
        && replacement.source_file == row.source_file
        && replacement.statement == source.statement
        && replacement.declaration_span == source.declaration_span
        && replacement.request == source.request
        && replacement.mode == source.mode
        && replacement.kind == source.kind
}

fn vendor_source_binding_matches(
    source: &artifact::reports::ParsedSourceImportBinding,
    source_specifier_span: Span,
    replacement: &artifact::reports::CandidateVendorBindingReplacement,
) -> bool {
    replacement.source_specifier_index == source.specifier_index
        && replacement.source_specifier_span == source_specifier_span
        && replacement.source_binding.as_ref() == Some(&source.local)
        && replacement.source_export == source.imported_name
        && replacement.source_exported_name.is_none()
        && replacement.source_kind == source.kind
}

fn reexport_kind(kind: artifact::reports::SourceReExportKind) -> ImportSpecifierKind {
    use artifact::reports::SourceReExportKind;
    match kind {
        SourceReExportKind::Named => ImportSpecifierKind::Named,
        SourceReExportKind::Namespace => ImportSpecifierKind::Namespace,
        SourceReExportKind::Default => ImportSpecifierKind::Default,
        SourceReExportKind::All => ImportSpecifierKind::Namespace,
    }
}

fn exact_dead_import_elision(
    row: &CandidateSourceCorrespondence,
    source_request: &artifact::reports::ParsedSourceRequest,
    source_binding: &artifact::reports::ParsedSourceImportBinding,
    source_specifier_span: Span,
    candidate_routes: &[artifact::reports::CandidateSourceRequestRoute],
) -> bool {
    let matching = row
        .rewrite_receipts
        .iter()
        .filter(|receipt| {
            let Some(before) = receipt.before.as_ref() else {
                return false;
            };
            let exact_before = receipt.source_chunk == row.source_chunk
                && receipt.stage == CandidateRewriteStage::DeadImportPruning
                && matches!(
                    &receipt.disposition,
                    CandidateRewriteDisposition::Elided | CandidateRewriteDisposition::Rewritten
                )
                && receipt.issue.is_none()
                && before.kind == CandidateOutputRequestKind::Import
                && before.item_span == source_request.declaration_span
                && before.source_statement == Some(source_request.statement)
                && before.request.as_deref() == Some(source_request.request.as_str())
                && before.mode == Some(source_request.mode)
                && (candidate_routes.iter().any(|route| {
                    route.output == receipt.output
                        && route.item_span == before.item_span
                        && route.source_statement == Some(source_request.statement)
                        && route.emitted_request == source_request.request
                        && route.mode == source_request.mode
                        && route.targets == before.targets
                }) || (candidate_routes.is_empty()
                    && receipt.after.is_none()
                    && receipt.elisions.iter().any(|elision| {
                        matches!(
                            elision,
                            CandidateRewriteElision::DeadNamedImport {
                                duplicate_request_dropped_after_earlier_load: true,
                                ..
                            }
                        )
                    })))
                && before
                    .bindings
                    .iter()
                    .filter(|binding| {
                        binding.specifier_index == source_binding.specifier_index
                            && binding.specifier_span == source_specifier_span
                            && binding.source_binding.as_ref() == Some(&source_binding.local)
                    })
                    .count()
                    == 1;
            let exact_elision = receipt
                .elisions
                .iter()
                .filter(|elision| {
                    matches!(elision, CandidateRewriteElision::DeadNamedImport { local, .. }
                    if before.bindings.iter().any(|binding| {
                        binding.specifier_index == source_binding.specifier_index
                            && binding.specifier_span == source_specifier_span
                            && binding.source_binding.as_ref() == Some(&source_binding.local)
                            && binding.local.as_ref() == Some(local)
                    }))
                })
                .count()
                == 1;
            exact_before && exact_elision
        })
        .collect::<Vec<_>>();
    let [receipt] = matching.as_slice() else {
        return false;
    };
    let Some(before) = receipt.before.as_ref() else {
        return false;
    };
    let elision = receipt.elisions.iter().find(|elision| {
        matches!(elision, CandidateRewriteElision::DeadNamedImport { local, .. }
        if before.bindings.iter().any(|binding| {
            binding.specifier_index == source_binding.specifier_index
                && binding.specifier_span == source_specifier_span
                && binding.source_binding.as_ref() == Some(&source_binding.local)
                && binding.local.as_ref() == Some(local)
        }))
    });
    let Some(CandidateRewriteElision::DeadNamedImport {
        local: _,
        request_retained_as_bare,
        duplicate_request_dropped_after_earlier_load,
    }) = elision
    else {
        return false;
    };
    let prior_emission = row
        .rewrite_receipts
        .iter()
        .filter(|prior| {
            prior.source_chunk == row.source_chunk
                && prior.output == receipt.output
                && prior.stage == CandidateRewriteStage::EmissionVendorRewrite
                && prior.issue.is_none()
                && matches!(
                    &prior.disposition,
                    CandidateRewriteDisposition::Preserved | CandidateRewriteDisposition::Rewritten
                )
                && prior.after.as_ref() == Some(before)
        })
        .count();
    if prior_emission != 1 {
        return false;
    }
    match &receipt.after {
        Some(after) => {
            let after_routes = candidate_routes
                .iter()
                .filter(|route| request_route_matches_identity(route, &receipt.output, after))
                .count();
            after_routes == 1
                && after.kind == CandidateOutputRequestKind::Import
                && after.item_span == before.item_span
                && after.source_statement == before.source_statement
                && after.request == before.request
                && after.mode == before.mode
                && after.targets == before.targets
                && !after
                    .bindings
                    .iter()
                    .any(|binding| binding.source_binding.as_ref() == Some(&source_binding.local))
                && after.bindings.iter().enumerate().all(|(index, binding)| {
                    binding.specifier_index == index
                        && before.bindings.iter().any(|prior| {
                            prior.specifier_span == binding.specifier_span
                                && prior.source_binding == binding.source_binding
                                && prior.local == binding.local
                                && prior.imported_name == binding.imported_name
                                && prior.kind == binding.kind
                                && prior.target == binding.target
                        })
                })
                && if after.bindings.is_empty() {
                    *request_retained_as_bare && !*duplicate_request_dropped_after_earlier_load
                } else {
                    !*request_retained_as_bare && !*duplicate_request_dropped_after_earlier_load
                }
        }
        None => {
            receipt.disposition == CandidateRewriteDisposition::Elided
                && !*request_retained_as_bare
                && *duplicate_request_dropped_after_earlier_load
                && candidate_routes.is_empty()
        }
    }
}

fn request_route_matches_identity(
    route: &artifact::reports::CandidateSourceRequestRoute,
    output: &OutputFileAddress,
    identity: &artifact::reports::CandidateStaticRequestIdentity,
) -> bool {
    route.status == SourceCorrespondenceStatus::Complete
        && route.output == *output
        && route.item_index == identity.item_index
        && route.item_span == identity.item_span
        && route.source_statement == identity.source_statement
        && route.emitted_request == identity.request.as_deref().unwrap_or_default()
        && Some(route.mode) == identity.mode
        && route.targets == identity.targets
        && route.bindings.len() == identity.bindings.len()
        && route
            .bindings
            .iter()
            .zip(&identity.bindings)
            .all(|(route, identity)| {
                route.specifier_index == identity.specifier_index
                    && route.specifier_span == identity.specifier_span
                    && route.candidate_local == identity.local
                    && route.imported_name == identity.imported_name
                    && route.exported_name == identity.exported_name
                    && Some(route.kind) == identity.kind
                    && route.source_binding == identity.source_binding
                    && route.source_cell == identity.source_cell
                    && Some(&route.target) == identity.target.as_ref()
            })
}

fn assess_source_imports(
    source_chunk: ChunkId,
    source_inputs: &FinalGlobalResolutionSourceInputs,
    route_view: &VerifiedFinalCandidateRoutes,
    row: &CandidateSourceCorrespondence,
    files: &ChunkBundle,
    indexes: Option<&ArtifactIndexes>,
    actual: &[ActualRequest],
    result: &mut FinalSourceCorrespondenceResult,
) {
    for import in &row.imports {
        let exact_elision = import.status == SourceCorrespondenceStatus::Complete
            && import.routes.is_empty()
            && import.source.use_spans.is_empty()
            && import.source.use_sites.is_empty()
            && row.source_requests.iter().any(|request| {
                request.source.statement == import.source.statement
                    && request.source.kind == artifact::reports::SourceStaticRequestKind::Import
                    && exact_dead_import_elision(
                        row,
                        &request.source,
                        &import.source,
                        request
                            .source
                            .specifier_spans
                            .get(import.source.specifier_index)
                            .copied()
                            .unwrap_or(swc_common::DUMMY_SP),
                        &request.routes,
                    )
            });
        let mut complete = if exact_elision {
            true
        } else {
            import.status == SourceCorrespondenceStatus::Complete && !import.routes.is_empty()
        };

        let import_routes = &import.routes;
        for route in import_routes {
            let matches = actual
                .iter()
                .filter(|candidate| {
                    candidate.output == route.output
                        && candidate.item_span == route.item_span
                        && candidate.kind == CandidateOutputRequestKind::Import
                        && candidate.request.as_deref() == Some(route.emitted_request.as_str())
                        && candidate.bindings.iter().any(|binding| {
                            binding.local.as_ref() == Some(&route.candidate_local)
                                && binding.specifier_index == route.specifier_index
                                && binding.specifier_span == route.specifier_span
                                && binding.imported_name.as_deref()
                                    == Some(route.imported_name.as_str())
                                && binding.kind == route.kind
                        })
                })
                .collect::<Vec<_>>();
            if matches.len() != 1 {
                complete = false;
                continue;
            }
            let resolved = indexes.and_then(|indexes| {
                indexes.resolve_runtime_import_reference(
                    &route.emitted_request,
                    route.output.chunk,
                    &route.output.file,
                    &files.chunk_table,
                )
            });
            let actual_target = resolved.as_ref().map(|resolved| OutputFileAddress {
                chunk: resolved.target_chunk_id,
                file: resolved.target_file.clone(),
            });
            let raw_exact = raw_request_is_exact(source_inputs, source_chunk, files, &route.output);
            if !request_targets_match_targets(
                source_inputs,
                source_chunk,
                row,
                std::slice::from_ref(&route.target),
                &route.emitted_request,
                actual_target.as_ref(),
                raw_exact,
            ) {
                complete = false;
            }
        }

        let expected_uses = &import.source.use_spans;
        let observed_sites = import
            .source
            .use_sites
            .iter()
            .map(|site| site.span)
            .collect::<Vec<_>>();
        if observed_sites != *expected_uses || import.uses.len() != expected_uses.len() {
            complete = false;
        }
        for source_span in expected_uses {
            let use_routes = import
                .uses
                .iter()
                .filter(|use_route| use_route.source_span == *source_span)
                .collect::<Vec<_>>();
            let [use_route] = use_routes.as_slice() else {
                complete = false;
                continue;
            };
            if use_route.status != SourceCorrespondenceStatus::Complete
                || use_route.routes.is_empty()
            {
                complete = false;
            }
            for location in &use_route.routes {
                let matching_imports = import
                    .routes
                    .iter()
                    .filter(|route| {
                        route.output == location.output
                            && route.candidate_local == location.candidate_local
                    })
                    .count();
                if matching_imports != 1
                    || !candidate_use_exists(files, source_inputs, source_chunk, location)
                {
                    complete = false;
                }
            }
        }

        for route in import_routes {
            if route.kind != ImportSpecifierKind::Namespace
                && !candidate_import_export_is_mapped(
                    source_inputs,
                    route_view.correspondence(),
                    files,
                    indexes,
                    actual,
                    route,
                )
            {
                complete = false;
            }
            if !route_view.allows_candidate_import_name(
                source_chunk,
                import.source.statement,
                &import.source.local,
                &import.source.imported_name,
                &route.imported_name,
            ) {
                complete = false;
            }
        }

        if !complete {
            record(
                result,
                source_chunk,
                GlobalResolutionStatus::Incomplete,
                finding(GlobalResolutionReason::SourceCorrespondenceIncomplete, None),
            );
        }
    }
}

/// The final checker consumes one private route view. It starts with the
/// materializer's typed source correspondence and replaces only BoundaryRename
/// rows whose receipt, every source use, exact output import, and final export
/// of the same module cell all match. Later source-request, provider-read, and
/// initialization checks use this same view rather than interpreting vendor
/// receipts independently.
struct VerifiedFinalCandidateRoutes {
    correspondence: BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    approved_import_names:
        BTreeMap<(ChunkId, source_identity::StatementId, swc_ecma_ast::Id), BTreeSet<String>>,
    boundary_cell_uses: Vec<(
        ChunkId,
        ModuleCellId,
        artifact::reports::CandidateSourceUseLocation,
    )>,
}

impl VerifiedFinalCandidateRoutes {
    fn build(
        source_inputs: &FinalGlobalResolutionSourceInputs,
        correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
        files: &ChunkBundle,
        indexes: Option<&ArtifactIndexes>,
        actual: &[ActualRequest],
    ) -> Self {
        let mut verified = correspondence.clone();
        let mut replacements = Vec::new();
        let mut approved_import_names = BTreeMap::new();
        let mut boundary_cell_uses = Vec::new();
        for (source_chunk, row) in correspondence {
            for (index, import) in row.imports.iter().enumerate() {
                let mut names =
                    naturalized_import_names(source_inputs, correspondence, row, import, files);
                if import.status != SourceCorrespondenceStatus::Complete
                    && let Some((routes, uses)) = boundary_rename_import_routes(
                        source_inputs,
                        correspondence,
                        row,
                        import,
                        files,
                        indexes,
                        actual,
                    )
                {
                    names.extend(routes.iter().map(|route| route.imported_name.clone()));
                    for use_route in &uses {
                        for location in &use_route.routes {
                            let matching_routes = routes
                                .iter()
                                .filter(|route| {
                                    route.output == location.output
                                        && route.candidate_local == location.candidate_local
                                })
                                .collect::<Vec<_>>();
                            if let [route] = matching_routes.as_slice()
                                && let Some(cell) = route.source_cell
                            {
                                boundary_cell_uses.push((*source_chunk, cell, location.clone()));
                            }
                        }
                    }
                    replacements.push((*source_chunk, index, routes, uses));
                }
                if !names.is_empty() {
                    approved_import_names.insert(
                        (
                            *source_chunk,
                            import.source.statement,
                            import.source.local.clone(),
                        ),
                        names,
                    );
                }
            }
        }
        for (source_chunk, index, routes, uses) in replacements {
            if let Some(import) = verified
                .get_mut(&source_chunk)
                .and_then(|row| row.imports.get_mut(index))
            {
                import.routes = routes;
                import.uses = uses;
                import.status = SourceCorrespondenceStatus::Complete;
            }
        }
        for row in verified.values_mut() {
            for request in &mut row.source_requests {
                if request.status == SourceCorrespondenceStatus::Complete
                    || !request
                        .routes
                        .iter()
                        .all(|route| route.status == SourceCorrespondenceStatus::Complete)
                {
                    continue;
                }
                let imports_complete = row
                    .imports
                    .iter()
                    .filter(|import| import.source.statement == request.source.statement)
                    .all(|import| import.status == SourceCorrespondenceStatus::Complete);
                let reexports_complete = row
                    .reexports
                    .iter()
                    .filter(|reexport| reexport.source.statement == request.source.statement)
                    .all(|reexport| reexport.status == SourceCorrespondenceStatus::Complete);
                if imports_complete
                    && reexports_complete
                    && source_request_specifiers_are_accounted(row, request)
                {
                    request.status = SourceCorrespondenceStatus::Complete;
                }
            }
        }
        Self {
            correspondence: verified,
            approved_import_names,
            boundary_cell_uses,
        }
    }

    fn correspondence(&self) -> &BTreeMap<ChunkId, CandidateSourceCorrespondence> {
        &self.correspondence
    }

    fn allows_candidate_import_name(
        &self,
        source_chunk: ChunkId,
        statement: source_identity::StatementId,
        source_local: &swc_ecma_ast::Id,
        source_name: &str,
        candidate_name: &str,
    ) -> bool {
        source_name == candidate_name
            || self
                .approved_import_names
                .get(&(source_chunk, statement, source_local.clone()))
                .is_some_and(|names| names.contains(candidate_name))
    }
}

/// Cross-chunk naturalization may select a new public alias for a source
/// binding. Accept that name change only when the exact recorded transition
/// preserves source binding, target and cell identity, and the final named
/// export still resolves to that same cell. This is route identity evidence;
/// it carries no value or effect facts.
fn naturalized_import_names(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    row: &CandidateSourceCorrespondence,
    import: &artifact::reports::CandidateSourceImportRoutes,
    files: &ChunkBundle,
) -> BTreeSet<String> {
    let mut names = BTreeSet::new();
    for route in &import.routes {
        if route.imported_name == import.source.imported_name
            || !candidate_export_alias_maps_cell(source_inputs, correspondence, files, route)
        {
            continue;
        }
        let transitions = row
            .rewrite_receipts
            .iter()
            .filter(|receipt| {
                receipt.source_chunk == row.source_chunk
                    && receipt.output == route.output
                    && receipt.stage == CandidateRewriteStage::CrossChunkNaturalization
                    && receipt.issue.is_none()
                    && receipt.disposition == CandidateRewriteDisposition::Rewritten
            })
            .filter_map(|receipt| {
                let (before, after) = (receipt.before.as_ref()?, receipt.after.as_ref()?);
                (before.kind == CandidateOutputRequestKind::Import
                    && after.kind == CandidateOutputRequestKind::Import
                    && before.item_span == route.item_span
                    && after.item_span == route.item_span
                    && before.source_statement == Some(import.source.statement)
                    && after.source_statement == Some(import.source.statement)
                    && before.request == Some(route.emitted_request.clone())
                    && after.request == Some(route.emitted_request.clone())
                    && before.mode == after.mode
                    && before.targets == after.targets
                    && before.targets.as_slice() == [route.target.clone()])
                .then_some((before, after))
            })
            .collect::<Vec<_>>();
        let [(_before, after)] = transitions.as_slice() else {
            continue;
        };
        let after_bindings = after
            .bindings
            .iter()
            .filter(|binding| {
                binding.specifier_index == route.specifier_index
                    && binding.specifier_span == route.specifier_span
                    && binding.source_binding.as_ref() == Some(&import.source.local)
                    && binding.local.as_ref() == Some(&route.candidate_local)
                    && binding.imported_name.as_deref() == Some(&route.imported_name)
                    && binding.kind == Some(route.kind)
                    && binding.source_cell == route.source_cell
                    && binding.target.as_ref() == Some(&route.target)
            })
            .collect::<Vec<_>>();
        let before_bindings = _before
            .bindings
            .iter()
            .filter(|binding| {
                binding.specifier_index == route.specifier_index
                    && binding.specifier_span == route.specifier_span
                    && binding.source_binding.as_ref() == Some(&import.source.local)
                    && binding.imported_name.as_deref() == Some(&import.source.imported_name)
                    && binding.kind == Some(route.kind)
                    && binding.source_cell == route.source_cell
                    && binding.target.as_ref() == Some(&route.target)
            })
            .collect::<Vec<_>>();
        if after_bindings.len() == 1 && before_bindings.len() == 1 && route.source_cell.is_some() {
            names.insert(route.imported_name.clone());
        }
    }
    names
}

fn candidate_export_alias_maps_cell(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    import: &artifact::reports::CandidateImportRoute,
) -> bool {
    let (CandidateImportTarget::CandidateOutput { chunk, file }, Some(cell)) =
        (&import.target, import.source_cell)
    else {
        return false;
    };
    if *chunk != cell.chunk {
        return false;
    }
    let output = OutputFileAddress {
        chunk: *chunk,
        file: file.clone(),
    };
    let Some(row) = correspondence.get(chunk) else {
        return false;
    };
    let routes = row
        .module_cell_declarations
        .iter()
        .filter(|declaration| {
            declaration.source_cell == cell
                && declaration.status == SourceCorrespondenceStatus::Complete
        })
        .flat_map(|declaration| {
            declaration
                .routes
                .iter()
                .filter(|route| route.output == output)
        })
        .collect::<Vec<_>>();
    let [source_route] = routes.as_slice() else {
        return false;
    };
    let mut alias_route = (*source_route).clone();
    alias_route.exported_name = Some(import.imported_name.clone());
    alias_route.exported_specifier_span = None;
    candidate_binding_exists(files, source_inputs, cell.chunk, source_route)
        && candidate_binding_export_exists(
            files,
            source_inputs,
            cell.chunk,
            &alias_route,
            &import.imported_name,
        )
}

/// Boundary-renamed imports remain ordinary Artifact edges. The action may
/// change the imported public name, but the final route is complete only if
/// its exact emitted request, all moved use sites, and the canonical source
/// binding's final export are still present. Tracked module cells also carry
/// an independent initialization-order obligation.
fn boundary_rename_import_routes(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    row: &CandidateSourceCorrespondence,
    import: &artifact::reports::CandidateSourceImportRoutes,
    files: &ChunkBundle,
    indexes: Option<&ArtifactIndexes>,
    actual: &[ActualRequest],
) -> Option<(
    Vec<artifact::reports::CandidateImportRoute>,
    Vec<artifact::reports::CandidateSourceUseRoute>,
)> {
    let source_request = row
        .source_requests
        .iter()
        .filter(|request| {
            request.source.kind == artifact::reports::SourceStaticRequestKind::Import
                && request.source.statement == import.source.statement
                && request.source.declaration_span == import.source.declaration_span
                && request.source.request == import.source.request
                && request.source.mode == artifact::reports::StaticModuleRequestMode::OrdinaryEsm
        })
        .collect::<Vec<_>>();
    let [source_request] = source_request.as_slice() else {
        return None;
    };
    let source_use_census = import
        .source
        .use_sites
        .iter()
        .map(|site| CandidateVendorSourceUse {
            statement: site.statement,
            span: site.span,
        })
        .collect::<Vec<_>>();
    if import.source.use_spans
        != source_use_census
            .iter()
            .map(|site| site.span)
            .collect::<Vec<_>>()
    {
        return None;
    }

    let matching_bindings = row
        .rewrite_receipts
        .iter()
        .filter_map(|receipt| {
            if receipt.source_chunk != row.source_chunk
                || receipt.stage != CandidateRewriteStage::EmissionVendorRewrite
                || receipt.issue.is_some()
            {
                return None;
            }
            let CandidateRewriteDisposition::Replaced(replacement) = &receipt.disposition else {
                return None;
            };
            if !vendor_source_request_matches(
                row,
                &source_request.source,
                &replacement.source_request,
            ) {
                return None;
            }
            replacement.bindings.iter().find_map(|binding| {
                (vendor_source_binding_matches(
                    &import.source,
                    source_request
                        .source
                        .specifier_spans
                        .get(import.source.specifier_index)
                        .copied()?,
                    binding,
                ) && matches!(
                    binding.action,
                    CandidateVendorPlanAction::BoundaryRename { .. }
                ))
                .then_some(binding)
            })
        })
        .collect::<Vec<_>>();
    if matching_bindings.is_empty() {
        return None;
    }

    let mut routes = Vec::new();
    let mut mapped_before = BTreeMap::<
        (source_identity::StatementId, Span),
        Vec<artifact::reports::CandidateSourceUseLocation>,
    >::new();
    let mut mapped_uses = BTreeMap::<
        (source_identity::StatementId, Span),
        Vec<artifact::reports::CandidateSourceUseLocation>,
    >::new();
    for binding in &matching_bindings {
        if binding.source_binding.as_ref() != Some(&import.source.local)
            || binding.source_export != import.source.imported_name
            || binding.source_exported_name.is_some()
            || binding.source_kind != import.source.kind
            || !same_vendor_source_use_census(&source_use_census, &binding.source_uses)
        {
            return None;
        }
        let (provider_chunk, source_export, public_export, destination_output, destination_export) =
            match (&binding.action, &binding.destination) {
                (
                    CandidateVendorPlanAction::BoundaryRename {
                        provider_chunk,
                        source_export,
                        public_export,
                    },
                    CandidateVendorDestination::BoundaryOutput { output, export },
                ) => (
                    *provider_chunk,
                    source_export,
                    public_export,
                    output,
                    export,
                ),
                _ => return None,
            };
        if source_export != &import.source.imported_name
            || public_export != destination_export
            || destination_output.chunk != provider_chunk
        {
            return None;
        }
        let expected_cell = canonical_boundary_provider_cell(
            source_inputs,
            correspondence,
            provider_chunk,
            source_export,
            public_export,
        )?;
        let existing_consumer_cell = row
            .source_module_cells
            .as_ref()?
            .get(&import.source.local)
            .copied();
        if binding
            .source_cell
            .is_some_and(|cell| Some(cell) != expected_cell)
            || existing_consumer_cell.is_some_and(|cell| Some(cell) != expected_cell)
        {
            return None;
        }

        for emitted in &binding.emitted_requests {
            let (Some(request), Some(mode)) =
                (emitted.identity.request.as_ref(), emitted.identity.mode)
            else {
                return None;
            };
            if emitted.identity.kind != CandidateOutputRequestKind::Import
                || emitted.identity.source_statement != Some(source_request.source.statement)
                || mode != artifact::reports::StaticModuleRequestMode::OrdinaryEsm
            {
                return None;
            }
            let targets = emitted
                .identity
                .targets
                .iter()
                .filter(|target| {
                    matches!(target, CandidateImportTarget::CandidateOutput { chunk, file }
                        if *chunk == destination_output.chunk && file == &destination_output.file)
                })
                .collect::<Vec<_>>();
            if targets.len() != 1 {
                return None;
            }
            let matching_bindings = emitted
                .identity
                .bindings
                .iter()
                .filter(|candidate| {
                    candidate.local.is_some()
                        && candidate.imported_name.as_deref() == Some(public_export)
                        && candidate.kind == Some(ImportSpecifierKind::Named)
                        && candidate.target.as_ref() == Some(targets[0])
                })
                .collect::<Vec<_>>();
            let [candidate_binding] = matching_bindings.as_slice() else {
                return None;
            };
            let local = candidate_binding.local.as_ref()?;
            let prepared = row
                .prepared_requests
                .iter()
                .filter(|prepared| {
                    prepared.output == emitted.output
                        && artifact::reports::prepared_request_identity(prepared)
                            == emitted.identity
                        && prepared.status == SourceCorrespondenceStatus::Complete
                })
                .collect::<Vec<_>>();
            if prepared.len() != 1 {
                return None;
            }
            let ast_matches = actual
                .iter()
                .filter(|candidate| {
                    candidate.output == emitted.output
                        && candidate.item_span == emitted.identity.item_span
                        && candidate.kind == CandidateOutputRequestKind::Import
                        && candidate.request.as_deref() == Some(request.as_str())
                        && candidate.bindings.iter().any(|binding| {
                            binding.local.as_ref() == Some(local)
                                && binding.specifier_index == candidate_binding.specifier_index
                                && binding.specifier_span == candidate_binding.specifier_span
                                && binding.imported_name.as_deref() == Some(public_export)
                                && binding.kind == ImportSpecifierKind::Named
                        })
                })
                .count();
            let resolved = indexes.and_then(|indexes| {
                indexes.resolve_runtime_import_reference(
                    request,
                    emitted.output.chunk,
                    &emitted.output.file,
                    &files.chunk_table,
                )
            });
            let Some(resolved) = resolved else {
                return None;
            };
            let actual_target = OutputFileAddress {
                chunk: resolved.target_chunk_id,
                file: resolved.target_file,
            };
            let expected_target = CandidateImportTarget::CandidateOutput {
                chunk: destination_output.chunk,
                file: destination_output.file.clone(),
            };
            if ast_matches != 1
                || actual_target != *destination_output
                || !request_targets_match_targets(
                    source_inputs,
                    row.source_chunk,
                    row,
                    std::slice::from_ref(&expected_target),
                    request,
                    Some(&actual_target),
                    false,
                )
                || !candidate_export_is_mapped(
                    source_inputs,
                    correspondence,
                    files,
                    indexes,
                    actual,
                    destination_output,
                    destination_export,
                    expected_cell,
                    &mut BTreeSet::new(),
                )
                || expected_cell.is_some_and(|cell| {
                    !candidate_export_cell_exists(
                        source_inputs,
                        correspondence,
                        files,
                        destination_output,
                        destination_export,
                        cell,
                    )
                })
            {
                return None;
            }
            routes.push(artifact::reports::CandidateImportRoute {
                output: emitted.output.clone(),
                item_span: emitted.identity.item_span,
                candidate_local: local.clone(),
                specifier_index: candidate_binding.specifier_index,
                specifier_span: candidate_binding.specifier_span,
                emitted_request: request.clone(),
                imported_name: public_export.clone(),
                kind: ImportSpecifierKind::Named,
                target: expected_target,
                source_cell: expected_cell,
            });
        }

        for use_ in &binding.uses {
            if !source_use_census.iter().any(|site| {
                site.statement == use_.source_statement && site.span == use_.source_span
            }) || !matches!(
                use_.result,
                CandidateVendorUseResult::LocalIdentifier { .. }
            ) {
                return None;
            }
            let CandidateVendorUseResult::LocalIdentifier { local } = &use_.result else {
                return None;
            };
            let before_matches = import
                .uses
                .iter()
                .filter(|source_use| {
                    source_use.source_span == use_.source_span
                        && source_use.status == SourceCorrespondenceStatus::Complete
                })
                .flat_map(|source_use| &source_use.routes)
                .filter(|location| {
                    location.output == use_.before_output
                        && location.candidate_span == use_.candidate_before_span
                        && location.candidate_local == use_.candidate_before_local
                })
                .collect::<Vec<_>>();
            let [before_location] = before_matches.as_slice() else {
                return None;
            };
            let Some(item_index) = candidate_use_item_index(
                files,
                &use_.after_output,
                local,
                use_.candidate_after_span,
            ) else {
                return None;
            };
            if !routes
                .iter()
                .any(|route| route.output == use_.after_output && route.candidate_local == *local)
            {
                return None;
            }
            let source_statement = use_.source_statement;
            mapped_before
                .entry((source_statement, use_.source_span))
                .or_default()
                .push((*before_location).clone());
            mapped_uses
                .entry((use_.source_statement, use_.source_span))
                .or_default()
                .push(artifact::reports::CandidateSourceUseLocation {
                    output: use_.after_output.clone(),
                    item_index,
                    candidate_span: use_.candidate_after_span,
                    candidate_local: local.clone(),
                });
        }
    }

    if routes.is_empty() || !candidate_import_routes_are_unique(&routes) {
        return None;
    }
    let mut uses = Vec::new();
    for site in &source_use_census {
        let locations = mapped_uses.get(&(site.statement, site.span))?;
        if locations.len() != 1
            || !candidate_use_locations_are_unique(locations)
            || !candidate_use_locations_equal(
                &import
                    .uses
                    .iter()
                    .filter(|use_route| use_route.source_span == site.span)
                    .flat_map(|use_route| &use_route.routes)
                    .cloned()
                    .collect::<Vec<_>>(),
                mapped_before.get(&(site.statement, site.span))?,
            )
        {
            return None;
        }
        uses.push(artifact::reports::CandidateSourceUseRoute {
            source_span: site.span,
            routes: locations.clone(),
            status: SourceCorrespondenceStatus::Complete,
        });
    }
    if mapped_uses.keys().any(|site| {
        !source_use_census
            .iter()
            .any(|expected| (expected.statement, expected.span) == *site)
    }) {
        return None;
    }
    Some((routes, uses))
}

fn candidate_import_routes_are_unique(routes: &[artifact::reports::CandidateImportRoute]) -> bool {
    routes.iter().enumerate().all(|(index, route)| {
        routes[..index].iter().all(|previous| {
            previous.output != route.output
                || previous.item_span != route.item_span
                || previous.candidate_local != route.candidate_local
                || previous.specifier_index != route.specifier_index
                || previous.specifier_span != route.specifier_span
                || previous.emitted_request != route.emitted_request
        })
    })
}

fn candidate_use_locations_are_unique(
    locations: &[artifact::reports::CandidateSourceUseLocation],
) -> bool {
    locations.iter().enumerate().all(|(index, location)| {
        locations[..index].iter().all(|previous| {
            previous.output != location.output
                || previous.item_index != location.item_index
                || previous.candidate_span != location.candidate_span
                || previous.candidate_local != location.candidate_local
        })
    })
}

fn candidate_use_locations_equal(
    left: &[artifact::reports::CandidateSourceUseLocation],
    right: &[artifact::reports::CandidateSourceUseLocation],
) -> bool {
    left.len() == right.len()
        && left.iter().all(|location| {
            right
                .iter()
                .filter(|candidate| {
                    candidate.output == location.output
                        && candidate.item_index == location.item_index
                        && candidate.candidate_span == location.candidate_span
                        && candidate.candidate_local == location.candidate_local
                })
                .count()
                == 1
        })
}

fn same_vendor_source_use_census(
    expected: &[CandidateVendorSourceUse],
    actual: &[CandidateVendorSourceUse],
) -> bool {
    expected.len() == actual.len()
        && expected
            .iter()
            .all(|site| actual.iter().filter(|candidate| *candidate == site).count() == 1)
}

fn candidate_use_item_index(
    files: &ChunkBundle,
    output: &OutputFileAddress,
    local: &swc_ecma_ast::Id,
    span: Span,
) -> Option<usize> {
    let file = files.find_chunk(output.chunk)?.js.get_file(&output.file)?;
    let ast = file.ast()?;
    let matches = ast
        .module
        .body
        .iter()
        .enumerate()
        .filter_map(|(index, item)| {
            let mut identifiers = Vec::new();
            item.visit_with(&mut IdentSpanCollector(&mut identifiers));
            (identifiers
                .iter()
                .filter(|(candidate, candidate_span)| candidate == local && *candidate_span == span)
                .count()
                == 1)
                .then_some(index)
        })
        .collect::<Vec<_>>();
    match matches.as_slice() {
        [index] => Some(*index),
        _ => None,
    }
}

fn candidate_export_cell_exists(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    output: &OutputFileAddress,
    export_name: &str,
    expected_cell: ModuleCellId,
) -> bool {
    let Some(row) = correspondence.get(&output.chunk) else {
        return false;
    };
    let direct_routes = row
        .module_cell_declarations
        .iter()
        .filter(|declaration| {
            declaration.source_cell == expected_cell
                && declaration.status == SourceCorrespondenceStatus::Complete
        })
        .flat_map(|declaration| &declaration.routes)
        .filter(|route| {
            route.output == *output && route.exported_name.as_deref() == Some(export_name)
        })
        .collect::<Vec<_>>();
    direct_routes.len() == 1
        && candidate_binding_exists(files, source_inputs, output.chunk, direct_routes[0])
        && candidate_binding_export_exists(
            files,
            source_inputs,
            output.chunk,
            direct_routes[0],
            export_name,
        )
}

fn candidate_import_export_is_mapped(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    indexes: Option<&ArtifactIndexes>,
    actual: &[ActualRequest],
    import: &artifact::reports::CandidateImportRoute,
) -> bool {
    let CandidateImportTarget::CandidateOutput { chunk, file } = &import.target else {
        // External targets have a separate authored/host proof. This helper
        // only checks exports whose candidate body is present in the output.
        return true;
    };
    let output = OutputFileAddress {
        chunk: *chunk,
        file: file.clone(),
    };
    candidate_export_is_mapped(
        source_inputs,
        correspondence,
        files,
        indexes,
        actual,
        &output,
        &import.imported_name,
        import.source_cell,
        &mut BTreeSet::new(),
    )
}

/// Resolve the vendor-local boundary mapping to the provider's canonical
/// declaration cell. The importing chunk's local binding may not have a cell
/// because the source specifier names the vendor-local spelling while the
/// provider's ESM surface publishes the boundary alias.
fn canonical_boundary_provider_cell(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    provider_chunk: ChunkId,
    source_export: &str,
    public_export: &str,
) -> Option<Option<ModuleCellId>> {
    let row = correspondence.get(&provider_chunk)?;
    let manifest = source_inputs
        .source_imports
        .get(&provider_chunk)?
        .as_deref()?;
    let global_uses = source_inputs.source_uses.get(&provider_chunk)?;
    if row.source_file != manifest.source_file || global_uses.source_file != row.source_file {
        return None;
    }
    let row_manifest = ParsedSourceImports {
        source_file: row.source_file.clone(),
        requests: row
            .source_requests
            .iter()
            .map(|request| request.source.clone())
            .collect(),
        bindings: row
            .imports
            .iter()
            .map(|binding| binding.source.clone())
            .collect(),
        reexports: row
            .reexports
            .iter()
            .map(|reexport| reexport.source.clone())
            .collect(),
        exports: row
            .exports
            .iter()
            .map(|export| export.source.clone())
            .collect(),
    };
    if manifest != &row_manifest {
        return None;
    }

    let source_exports = row
        .exports
        .iter()
        .filter(|export| {
            export.status == SourceCorrespondenceStatus::Complete
                && !export.source.type_only
                && export.source.public_name == public_export
                && export
                    .source
                    .local
                    .as_ref()
                    .is_some_and(|local| local.0.as_ref() == source_export)
        })
        .collect::<Vec<_>>();
    let [source_export] = source_exports.as_slice() else {
        return None;
    };
    let source_local = source_export.source.local.as_ref()?;
    let source_module_cells = row.source_module_cells.as_ref()?;
    let expected_cell = source_module_cells.get(source_local).copied();
    let declarations = row
        .module_cell_declarations
        .iter()
        .filter(|declaration| declaration.source_binding.as_ref() == Some(source_local))
        .collect::<Vec<_>>();
    match expected_cell {
        Some(cell) => {
            let [declaration] = declarations.as_slice() else {
                return None;
            };
            if declaration.source_cell != cell
                || cell.chunk != provider_chunk
                || declaration.status != SourceCorrespondenceStatus::Complete
                || declaration.source_owner.is_none()
                || declaration.declaration_span.is_none()
                || declaration.routes.len() != 1
            {
                return None;
            }
            Some(Some(cell))
        }
        None if declarations.is_empty() => Some(None),
        None => None,
    }
}

fn candidate_export_is_mapped(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    indexes: Option<&ArtifactIndexes>,
    actual: &[ActualRequest],
    output: &OutputFileAddress,
    export_name: &str,
    required_cell: Option<ModuleCellId>,
    visiting: &mut BTreeSet<(OutputFileAddress, String)>,
) -> bool {
    if !visiting.insert((output.clone(), export_name.to_owned())) {
        return false;
    }
    let Some(row) = correspondence.get(&output.chunk) else {
        return false;
    };

    for export in row.exports.iter().filter(|export| {
        export.source.public_name == export_name
            && export.status == SourceCorrespondenceStatus::Complete
    }) {
        for route in export.routes.iter().filter(|route| {
            route.output == *output && route.exported_name.as_deref() == Some(export_name)
        }) {
            let cell_matches = required_cell.is_none_or(|cell| {
                row.module_cell_declarations.iter().any(|declaration| {
                    declaration.source_cell == cell
                        && declaration.status == SourceCorrespondenceStatus::Complete
                        && declaration.routes.iter().any(|cell_route| {
                            cell_route.output == *output
                                && cell_route.candidate_local == route.candidate_local
                                && cell_route.exported_name.as_deref() == Some(export_name)
                        })
                }) || export.source.local.as_ref().is_some_and(|source_local| {
                    row.imports.iter().any(|import| {
                        import.source.local == *source_local
                            && import.status == SourceCorrespondenceStatus::Complete
                            && import.routes.iter().any(|import_route| {
                                import_route.output == *output
                                    && import_route.candidate_local == source_local.clone()
                                    && import_route.source_cell == Some(cell)
                            })
                    })
                })
            });
            if cell_matches
                && candidate_binding_exists(files, source_inputs, output.chunk, route)
                && candidate_binding_export_exists(
                    files,
                    source_inputs,
                    output.chunk,
                    route,
                    export_name,
                )
            {
                return true;
            }
        }
    }

    let Some(indexes) = indexes else {
        return false;
    };
    for reexport in row.reexports.iter().filter(|reexport| {
        reexport.status == SourceCorrespondenceStatus::Complete
            && (reexport.source.exported_name.as_deref() == Some(export_name)
                || (reexport.source.kind == artifact::reports::SourceReExportKind::All
                    && reexport.source.exported_name.is_none()))
    }) {
        for route in reexport
            .routes
            .iter()
            .filter(|route| route.output == *output)
        {
            let request_kind = match reexport.source.kind {
                artifact::reports::SourceReExportKind::All => CandidateOutputRequestKind::ExportAll,
                _ => CandidateOutputRequestKind::NamedReExport,
            };
            let candidates = actual
                .iter()
                .filter(|candidate| {
                    candidate.output == route.output
                        && candidate.item_index == route.item_index
                        && candidate.item_span == route.item_span
                        && candidate.kind == request_kind
                        && candidate.request.as_deref() == Some(route.emitted_request.as_str())
                        && request_bindings_match(&route.bindings, &candidate.bindings)
                })
                .collect::<Vec<_>>();
            let [candidate] = candidates.as_slice() else {
                continue;
            };
            let Some(candidate_request) = candidate.request.as_deref() else {
                continue;
            };
            let resolved = indexes.resolve_runtime_import_reference(
                candidate_request,
                candidate.output.chunk,
                &candidate.output.file,
                &files.chunk_table,
            );
            let Some(resolved) = resolved else {
                continue;
            };
            let actual_target = OutputFileAddress {
                chunk: resolved.target_chunk_id,
                file: resolved.target_file,
            };
            if !request_targets_match_targets(
                source_inputs,
                output.chunk,
                row,
                &route.targets,
                &route.emitted_request,
                Some(&actual_target),
                raw_request_is_exact(source_inputs, output.chunk, files, output),
            ) {
                continue;
            }
            let forwarded_name =
                if reexport.source.kind == artifact::reports::SourceReExportKind::All {
                    export_name.to_owned()
                } else if reexport.source.kind == artifact::reports::SourceReExportKind::Namespace {
                    continue;
                } else if let Some(imported) = reexport.source.imported_name.as_ref() {
                    imported.clone()
                } else {
                    continue;
                };
            if candidate_export_is_mapped(
                source_inputs,
                correspondence,
                files,
                Some(indexes),
                actual,
                &actual_target,
                &forwarded_name,
                required_cell,
                visiting,
            ) {
                return true;
            }
        }
    }
    false
}

fn assess_source_exports(
    source_chunk: ChunkId,
    source_inputs: &FinalGlobalResolutionSourceInputs,
    row: &CandidateSourceCorrespondence,
    files: &ChunkBundle,
    result: &mut FinalSourceCorrespondenceResult,
) {
    for export in &row.exports {
        if export.routes.is_empty() {
            if exact_dead_local_export_elision(row, export) {
                continue;
            }
            record(
                result,
                source_chunk,
                GlobalResolutionStatus::Incomplete,
                finding(GlobalResolutionReason::SourceCorrespondenceIncomplete, None),
            );
            continue;
        }
        if export.status != SourceCorrespondenceStatus::Complete {
            record(
                result,
                source_chunk,
                GlobalResolutionStatus::Incomplete,
                finding(GlobalResolutionReason::SourceCorrespondenceIncomplete, None),
            );
            continue;
        }
        for route in &export.routes {
            let valid = match export.source.local.as_ref() {
                Some(_) => {
                    let raw_exact = raw_source_proofs_match_file(
                        source_inputs,
                        source_chunk,
                        files,
                        &route.output,
                    );
                    candidate_binding_exists(files, source_inputs, source_chunk, route)
                        && (raw_exact
                            || candidate_binding_export_exists(
                                files,
                                source_inputs,
                                source_chunk,
                                route,
                                &export.source.public_name,
                            ))
                }
                None => {
                    raw_source_proofs_match_file(source_inputs, source_chunk, files, &route.output)
                }
            };
            if !valid {
                record(
                    result,
                    source_chunk,
                    GlobalResolutionStatus::Incomplete,
                    finding(GlobalResolutionReason::SourceCorrespondenceIncomplete, None),
                );
            }
        }
    }
}

fn exact_dead_local_export_elision(
    row: &CandidateSourceCorrespondence,
    export: &artifact::reports::CandidateSourceExportRoutes,
) -> bool {
    let Some(source_local) = export.source.local.as_ref() else {
        return false;
    };
    let Some(source_specifier_span) = export.source.specifier_span else {
        return false;
    };
    let matching = row
        .rewrite_receipts
        .iter()
        .filter(|receipt| {
            receipt.source_chunk == row.source_chunk
                && receipt.stage == CandidateRewriteStage::DeadExportPruning
                && receipt.disposition == CandidateRewriteDisposition::Elided
                && receipt.issue.is_none()
                && receipt.after.is_none()
                && receipt.before.as_ref().is_some_and(|before| {
                    before.kind == CandidateOutputRequestKind::LocalExport
                        && before.source_statement == Some(export.source.statement)
                        && before.bindings.iter().any(|binding| {
                            binding.specifier_span == source_specifier_span
                                && binding.source_binding.as_ref() == Some(source_local)
                                && binding.exported_name.as_deref()
                                    == Some(export.source.public_name.as_str())
                        })
                })
        })
        .collect::<Vec<_>>();
    let [receipt] = matching.as_slice() else {
        return false;
    };
    receipt.elisions.iter().any(|elision| match elision {
        CandidateRewriteElision::DeadLocalExport {
            local,
            exported_name,
        } => {
            exported_name == &export.source.public_name
                && receipt.before.as_ref().is_some_and(|before| {
                    before.bindings.iter().any(|binding| {
                        binding.local.as_ref() == Some(local)
                            && binding.source_binding.as_ref() == Some(source_local)
                            && binding.exported_name.as_deref() == Some(exported_name.as_str())
                    })
                })
        }
        CandidateRewriteElision::DeadNamedImport { .. } => false,
    })
}

fn request_targets_match_targets(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    source_chunk: ChunkId,
    correspondence: &CandidateSourceCorrespondence,
    targets: &[CandidateImportTarget],
    emitted_request: &str,
    actual: Option<&OutputFileAddress>,
    raw_identity: bool,
) -> bool {
    if targets.is_empty() {
        return false;
    }
    targets.iter().all(|target| match target {
        CandidateImportTarget::CandidateOutput { chunk, file } => {
            actual.is_some_and(|actual| *chunk == actual.chunk && file == &actual.file)
        }
        CandidateImportTarget::OriginalSource { request } => {
            let Some(source_indexes) = source_inputs.source_indexes.as_ref() else {
                return raw_identity && request == emitted_request;
            };
            let Some(source_chunk_table) = source_inputs.source_chunk_table.as_ref() else {
                return raw_identity && request == emitted_request;
            };
            let Some(source_caller_file) = source_inputs.source_entry_files.get(&source_chunk)
            else {
                return raw_identity && request == emitted_request;
            };
            let original = source_indexes.resolve_runtime_import_reference(
                request,
                source_chunk,
                source_caller_file,
                source_chunk_table,
            );
            let Some(original) = original else {
                return raw_identity && request == emitted_request;
            };
            actual.is_some_and(|actual| {
                original.target_chunk_id == actual.chunk && original.target_file == actual.file
            })
        }
        CandidateImportTarget::OriginalExternal { request } => {
            request == emitted_request
                && (actual.is_none() && spec::HostModule::from_static_specifier(request).is_some()
                    || raw_identity)
        }
        CandidateImportTarget::VendorPackage { request, package } => {
            actual.is_none()
                && request == emitted_request
                && vendor_destination_is_authored(
                    correspondence,
                    request,
                    &CandidateImportTarget::VendorPackage {
                        request: request.clone(),
                        package: package.clone(),
                    },
                )
        }
        CandidateImportTarget::VendorFacade { request, app_path } => {
            request == emitted_request
                && actual.is_none_or(|actual| actual.file == *app_path)
                && vendor_destination_is_authored(
                    correspondence,
                    request,
                    &CandidateImportTarget::VendorFacade {
                        request: request.clone(),
                        app_path: app_path.clone(),
                    },
                )
        }
        CandidateImportTarget::OriginalSourceUnknownExports { .. }
        | CandidateImportTarget::Unsupported => false,
    })
}

fn vendor_destination_is_authored(
    correspondence: &CandidateSourceCorrespondence,
    emitted_request: &str,
    target: &CandidateImportTarget,
) -> bool {
    correspondence.rewrite_receipts.iter().any(|receipt| {
        if receipt.source_chunk != correspondence.source_chunk
            || receipt.stage != CandidateRewriteStage::EmissionVendorRewrite
            || receipt.issue.is_some()
        {
            return false;
        }
        let CandidateRewriteDisposition::Replaced(replacement) = &receipt.disposition else {
            return false;
        };
        replacement.bindings.iter().any(|binding| {
            let destination_matches = match (&binding.action, &binding.destination, target) {
                (
                    artifact::reports::CandidateVendorPlanAction::PartialPackageMember {
                        source_export,
                        package,
                        version,
                        subpath,
                        upstream_export,
                        ..
                    },
                    artifact::reports::CandidateVendorDestination::Package {
                        request,
                        package: destination_package,
                    },
                    CandidateImportTarget::VendorPackage {
                        request: target_request,
                        package: target_package,
                    },
                ) => {
                    source_export == &binding.source_export
                        && package == destination_package
                        && package == target_package
                        && request == target_request
                        && request == emitted_request
                        && version.len() > 0
                        && subpath.len() > 0
                        && binding.emitted_requests.iter().any(|emitted| {
                            emitted.identity.bindings.iter().any(|item| {
                                item.imported_name.as_deref() == Some("*")
                                    && item.kind == Some(ImportSpecifierKind::Namespace)
                            })
                        })
                        || (source_export == &binding.source_export
                            && package == destination_package
                            && package == target_package
                            && request == target_request
                            && request == emitted_request
                            && !version.is_empty()
                            && !subpath.is_empty()
                            && binding.emitted_requests.iter().any(|emitted| {
                                emitted.identity.bindings.iter().any(|item| {
                                    item.imported_name.as_deref() == Some(upstream_export)
                                        && item.kind == Some(ImportSpecifierKind::Named)
                                })
                            }))
                }
                (
                    artifact::reports::CandidateVendorPlanAction::PartialPackageNamespace {
                        source_export,
                        package,
                        version,
                        subpath,
                        ..
                    }
                    | artifact::reports::CandidateVendorPlanAction::PartialPackageDefault {
                        source_export,
                        package,
                        version,
                        subpath,
                        ..
                    }
                    | artifact::reports::CandidateVendorPlanAction::PartialPackageNamed {
                        source_export,
                        package,
                        version,
                        subpath,
                        ..
                    },
                    artifact::reports::CandidateVendorDestination::Package {
                        request,
                        package: destination_package,
                    },
                    CandidateImportTarget::VendorPackage {
                        request: target_request,
                        package: target_package,
                    },
                ) => {
                    source_export == &binding.source_export
                        && package == destination_package
                        && package == target_package
                        && request == target_request
                        && request == emitted_request
                        && !version.is_empty()
                        && !subpath.is_empty()
                }
                (
                    artifact::reports::CandidateVendorPlanAction::BundledFacadeMember {
                        source_export,
                        facade_app_path,
                        ..
                    }
                    | artifact::reports::CandidateVendorPlanAction::BundledFacadeDefault {
                        source_export,
                        facade_app_path,
                        ..
                    },
                    artifact::reports::CandidateVendorDestination::Facade {
                        app_path,
                        emitted_request: destination_request,
                    },
                    CandidateImportTarget::VendorFacade {
                        request: target_request,
                        app_path: target_path,
                    },
                ) => {
                    source_export == &binding.source_export
                        && facade_app_path == app_path
                        && app_path == target_path
                        && destination_request == target_request
                        && destination_request == emitted_request
                }
                _ => false,
            };
            destination_matches
                && binding.emitted_requests.iter().any(|emitted| {
                    emitted.identity.request.as_deref() == Some(emitted_request)
                        && emitted
                            .identity
                            .targets
                            .iter()
                            .any(|emitted_target| emitted_target == target)
                })
        })
    })
}

fn raw_request_is_exact(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    source_chunk: ChunkId,
    bundle: &ChunkBundle,
    output: &OutputFileAddress,
) -> bool {
    let Some(proof) = source_inputs.raw_source_proofs.get(&source_chunk) else {
        return false;
    };
    let Some(chunk) = bundle.find_chunk(output.chunk) else {
        return false;
    };
    let Some(file) = chunk.js.get_file(&output.file) else {
        return false;
    };
    output.chunk == source_chunk
        && source_inputs
            .source_entry_files
            .get(&source_chunk)
            .is_some_and(|entry| entry == &output.file)
        && raw_file_matches(proof, output.chunk, file)
}

fn raw_file_matches(proof: &RawSourceIdentityProof, chunk_id: ChunkId, file: &JsFile) -> bool {
    proof.chunk_id() == chunk_id
        && proof.source_path().to_string_lossy().as_ref() == file.metadata.source_path.as_str()
        && matches!(&file.body, JsFileBody::Source(source) if proof.matches_bytes(source.as_bytes()))
        && file.metadata.role == FileRole::Entry
}

fn finding(reason: GlobalResolutionReason, output: Option<String>) -> GlobalResolutionFinding {
    GlobalResolutionFinding {
        source_file: None,
        symbol: None,
        span: None,
        reason,
        candidate_files: output.into_iter().collect(),
    }
}

fn record(
    result: &mut FinalSourceCorrespondenceResult,
    chunk: ChunkId,
    status: GlobalResolutionStatus,
    finding: GlobalResolutionFinding,
) {
    result.status = combine(result.status, status);
    result
        .per_chunk_status
        .entry(chunk)
        .and_modify(|current| *current = combine(*current, status))
        .or_insert(status);
    result.findings.entry(chunk).or_default().push(finding);
}

fn combine(left: GlobalResolutionStatus, right: GlobalResolutionStatus) -> GlobalResolutionStatus {
    match (left, right) {
        (GlobalResolutionStatus::Changed, _) | (_, GlobalResolutionStatus::Changed) => {
            GlobalResolutionStatus::Changed
        }
        (GlobalResolutionStatus::Incomplete, _) | (_, GlobalResolutionStatus::Incomplete) => {
            GlobalResolutionStatus::Incomplete
        }
        _ => GlobalResolutionStatus::Preserved,
    }
}

fn aggregate(statuses: impl IntoIterator<Item = GlobalResolutionStatus>) -> GlobalResolutionStatus {
    statuses
        .into_iter()
        .fold(GlobalResolutionStatus::Preserved, combine)
}

fn assess_runtime_obligations(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    route_view: &VerifiedFinalCandidateRoutes,
    files: &ChunkBundle,
    reports: &BTreeMap<String, CandidateRelocationAssessmentReport>,
    graph: &BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
    result: &mut FinalSourceCorrespondenceResult,
) {
    let correspondence = route_view.correspondence();
    for (chunk_name, report) in reports {
        let Some(consumer_chunk) = source_inputs
            .source_chunk_names
            .iter()
            .find_map(|(id, name)| (name == chunk_name).then_some(*id))
        else {
            continue;
        };
        let Some(consumer_row) = correspondence.get(&consumer_chunk) else {
            continue;
        };

        for (consumer_unit, cell) in &report.module_initialization.required_cell_uses {
            if preserve_cell_use(
                source_inputs,
                correspondence,
                files,
                graph,
                consumer_row,
                *consumer_unit,
                *cell,
            ) {
                result.preserved_cells.insert(*cell);
                result
                    .preserved_cell_uses
                    .insert((consumer_chunk, *consumer_unit, *cell));
            } else {
                record(
                    result,
                    consumer_chunk,
                    GlobalResolutionStatus::Incomplete,
                    finding(GlobalResolutionReason::InitializationOrderUnproved, None),
                );
            }
        }

        for read in &report.module_initialization.required_imported_read_facts {
            if preserve_imported_read(
                source_inputs,
                route_view,
                files,
                graph,
                consumer_chunk,
                consumer_row,
                read,
            ) {
                result.preserved_imported_reads.push(read.clone());
                for source_cell in &read.source_cells {
                    if let Some(provider_chunk) = source_inputs
                        .source_chunk_table
                        .as_ref()
                        .and_then(|table| table.get(&source_cell.module))
                        && let Some(provider_row) = correspondence.get(&provider_chunk)
                    {
                        let declarations = provider_row
                            .module_cell_declarations
                            .iter()
                            .filter(|declaration| {
                                declaration.source_cell.chunk == provider_chunk
                                    && declaration.routes.iter().any(|route| {
                                        route.exported_name.as_deref()
                                            == Some(source_cell.export.as_str())
                                    })
                            })
                            .collect::<Vec<_>>();
                        if let [declaration] = declarations.as_slice() {
                            result.preserved_cells.insert(declaration.source_cell);
                        }
                    }
                }
            } else {
                record(
                    result,
                    consumer_chunk,
                    GlobalResolutionStatus::Incomplete,
                    finding(GlobalResolutionReason::ProviderRouteUnproved, None),
                );
            }
        }

        for target in &report.provider_targets.required_facts {
            if preserve_provider_target(
                source_inputs,
                correspondence,
                files,
                graph,
                consumer_chunk,
                consumer_row,
                target,
            ) {
                result.preserved_provider_targets.push(target.clone());
            } else {
                record(
                    result,
                    consumer_chunk,
                    GlobalResolutionStatus::Incomplete,
                    finding(GlobalResolutionReason::ProviderRouteUnproved, None),
                );
            }
        }
    }

    // Boundary-rename imports can have a source-local Id that the source
    // module-cell analysis could not join through the vendor-local name.
    // Once the final route has independently recovered the provider cell, do
    // not let that missing source import-cell edge erase initialization
    // ordering for its actual canonical use sites.
    for (consumer_chunk, cell, location) in &route_view.boundary_cell_uses {
        let Some(consumer_row) = correspondence.get(consumer_chunk) else {
            record(
                result,
                *consumer_chunk,
                GlobalResolutionStatus::Incomplete,
                finding(GlobalResolutionReason::InitializationOrderUnproved, None),
            );
            continue;
        };
        if preserve_boundary_cell_use(
            source_inputs,
            correspondence,
            files,
            graph,
            *consumer_chunk,
            consumer_row,
            location,
            *cell,
        ) {
            result.preserved_cells.insert(*cell);
        } else {
            record(
                result,
                *consumer_chunk,
                GlobalResolutionStatus::Incomplete,
                finding(GlobalResolutionReason::InitializationOrderUnproved, None),
            );
        }
    }
}

fn preserve_cell_use(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    graph: &BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
    consumer_row: &CandidateSourceCorrespondence,
    consumer_unit: SourceUnitOrdinal,
    cell: ModuleCellId,
) -> bool {
    if !consumer_row
        .source_initialized_module_cells
        .as_ref()
        .is_some_and(|initialized| initialized.contains(&cell))
    {
        return false;
    }
    let Some((provider_chunk, declaration, provider_route)) =
        unique_cell_route(correspondence, cell)
    else {
        return false;
    };
    if provider_chunk != cell.chunk
        || declaration.status != SourceCorrespondenceStatus::Complete
        || declaration.source_owner.is_none()
        || declaration.source_binding.is_none()
        || declaration.declaration_span.is_none()
        || provider_route.candidate_local.is_none()
        || provider_route.binding_span.is_none()
        || !candidate_binding_exists(files, source_inputs, cell.chunk, provider_route)
    {
        return false;
    }
    let Some((consumer_output, consumer_index)) = source_unit_position(consumer_row, consumer_unit)
    else {
        return false;
    };
    let Some(provider_row) = correspondence.get(&provider_chunk) else {
        return false;
    };
    let Some((provider_output, provider_index)) = declaration
        .source_owner
        .and_then(|owner| source_unit_position(provider_row, owner))
    else {
        return false;
    };
    if provider_output == consumer_output {
        return provider_index < consumer_index;
    }
    provider_precedes_consumer(
        graph,
        provider_row,
        &provider_output,
        &consumer_output,
        consumer_index,
    )
}

fn preserve_boundary_cell_use(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    graph: &BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
    consumer_chunk: ChunkId,
    consumer_row: &CandidateSourceCorrespondence,
    location: &artifact::reports::CandidateSourceUseLocation,
    cell: ModuleCellId,
) -> bool {
    if !consumer_row
        .source_initialized_module_cells
        .as_ref()
        .is_some_and(|initialized| initialized.contains(&cell))
    {
        return false;
    }
    let Some((provider_chunk, declaration, provider_route)) =
        unique_cell_route(correspondence, cell)
    else {
        return false;
    };
    let (Some(local), Some(binding_span), Some(export_name)) = (
        provider_route.candidate_local.as_ref(),
        provider_route.binding_span,
        provider_route.exported_name.as_deref(),
    ) else {
        return false;
    };
    if provider_chunk != cell.chunk
        || declaration.status != SourceCorrespondenceStatus::Complete
        || declaration.source_owner.is_none()
        || declaration.source_binding.is_none()
        || declaration.declaration_span.is_none()
        || !candidate_binding_exists(files, source_inputs, cell.chunk, provider_route)
        || !candidate_binding_export_exists(
            files,
            source_inputs,
            cell.chunk,
            provider_route,
            export_name,
        )
        || !candidate_use_exists(files, source_inputs, consumer_chunk, location)
    {
        return false;
    }
    let Some(provider_index) =
        candidate_use_item_index(files, &provider_route.output, local, binding_span)
    else {
        return false;
    };
    if provider_route.output == location.output {
        return provider_index < location.item_index;
    }
    correspondence
        .get(&provider_chunk)
        .is_some_and(|provider_row| {
            provider_precedes_consumer(
                graph,
                provider_row,
                &provider_route.output,
                &location.output,
                location.item_index,
            )
        })
}

fn preserve_imported_read(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    route_view: &VerifiedFinalCandidateRoutes,
    files: &ChunkBundle,
    graph: &BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
    consumer_chunk: ChunkId,
    consumer_row: &CandidateSourceCorrespondence,
    read: &analysis::facts::RequiredImportedPrimitiveRead,
) -> bool {
    let correspondence = route_view.correspondence();
    let (Some(site), Some(consumer_root), Some(closure)) = (
        read.source_site.as_ref(),
        read.consumer_root,
        read.provider_closure.as_ref(),
    ) else {
        return false;
    };
    let Some(source_name) = source_inputs.source_chunk_names.get(&consumer_chunk) else {
        return false;
    };
    if site.owner() != read.source_owner
        || !closure.is_well_formed()
        || closure.modules != read.dependency_closure
        || !closure.modules.contains(&read.provider_module)
    {
        return false;
    }
    let imports = consumer_row
        .imports
        .iter()
        .filter(|import| {
            import.source.local == read.source_binding
                && import.source.request == read.source_specifier
                && import.source.imported_name == read.imported_name
                && import.status == SourceCorrespondenceStatus::Complete
        })
        .collect::<Vec<_>>();
    let [import] = imports.as_slice() else {
        return false;
    };
    let source_use = import
        .uses
        .iter()
        .filter(|use_route| use_route.source_span == site.span())
        .collect::<Vec<_>>();
    let [source_use] = source_use.as_slice() else {
        return false;
    };
    if source_use.status != SourceCorrespondenceStatus::Complete {
        return false;
    }
    let [use_location] = source_use.routes.as_slice() else {
        return false;
    };
    if !candidate_use_exists(files, source_inputs, consumer_chunk, use_location)
        || import
            .routes
            .iter()
            .filter(|route| {
                route.output == use_location.output
                    && route.candidate_local == use_location.candidate_local
                    && route_view.allows_candidate_import_name(
                        consumer_chunk,
                        import.source.statement,
                        &import.source.local,
                        &read.imported_name,
                        &route.imported_name,
                    )
                    && route.emitted_request == read.source_specifier
            })
            .count()
            != 1
    {
        return false;
    }

    let Some((consumer_output, consumer_index)) = source_unit_position(consumer_row, consumer_root)
    else {
        return false;
    };
    if !provider_closure_reaches_consumer(
        source_inputs,
        correspondence,
        files,
        graph,
        source_name,
        &consumer_output,
        consumer_index,
        closure,
    ) {
        return false;
    }
    let Some(provider_id) = source_inputs
        .source_chunk_table
        .as_ref()
        .and_then(|table| table.get(&read.provider_module))
    else {
        return false;
    };
    if !provider_export_exists(
        source_inputs,
        correspondence,
        files,
        provider_id,
        &read.provider_export,
    ) {
        return false;
    }
    for source_cell in &read.source_cells {
        let Some(provider_chunk) = source_inputs
            .source_chunk_table
            .as_ref()
            .and_then(|table| table.get(&source_cell.module))
        else {
            return false;
        };
        let Some(provider_row) = correspondence.get(&provider_chunk) else {
            return false;
        };
        if !consumer_row
            .source_initialized_module_cells
            .as_ref()
            .is_some_and(|initialized| {
                provider_row
                    .module_cell_declarations
                    .iter()
                    .any(|declaration| {
                        declaration.source_cell.chunk == provider_chunk
                            && initialized.contains(&declaration.source_cell)
                            && declaration.status == SourceCorrespondenceStatus::Complete
                            && declaration.routes.iter().any(|route| {
                                route.exported_name.as_deref() == Some(source_cell.export.as_str())
                                    && candidate_binding_exists(
                                        files,
                                        source_inputs,
                                        provider_chunk,
                                        route,
                                    )
                                    && candidate_binding_export_exists(
                                        files,
                                        source_inputs,
                                        provider_chunk,
                                        route,
                                        &source_cell.export,
                                    )
                            })
                    })
            })
        {
            return false;
        }
    }
    closure
        .admitted_host_modules
        .iter()
        .all(|dependency| host_dependency_route_exists(source_inputs, correspondence, dependency))
}

fn preserve_provider_target(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    graph: &BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
    consumer_chunk: ChunkId,
    consumer_row: &CandidateSourceCorrespondence,
    target: &analysis::facts::ConditionalProviderTargetRequirement,
) -> bool {
    let (Some(closure), Some(source_host_dependencies)) = (
        target.provider_closure.as_ref(),
        target.source_initialized_host_dependencies.as_ref(),
    ) else {
        return false;
    };
    if !closure.is_well_formed()
        || !closure.modules.contains(&target.defining_module)
        || *source_host_dependencies != closure.admitted_host_modules
    {
        return false;
    }
    let imports = consumer_row
        .imports
        .iter()
        .filter(|import| {
            import.source.local == target.source_binding
                && import.status == SourceCorrespondenceStatus::Complete
        })
        .collect::<Vec<_>>();
    let [import] = imports.as_slice() else {
        return false;
    };
    let callsite_uses = import
        .uses
        .iter()
        .filter(|use_route| {
            use_route.source_span.lo >= target.call_span.lo
                && use_route.source_span.hi <= target.call_span.hi
        })
        .collect::<Vec<_>>();
    let [use_route] = callsite_uses.as_slice() else {
        return false;
    };
    let [location] = use_route.routes.as_slice() else {
        return false;
    };
    if !candidate_use_exists(files, source_inputs, consumer_chunk, location) {
        return false;
    }
    let Some((consumer_output, consumer_index)) = source_unit_position(consumer_row, target.owner)
    else {
        return false;
    };
    let Some(defining_chunk) = source_inputs
        .source_chunk_table
        .as_ref()
        .and_then(|table| table.get(&target.defining_module))
    else {
        return false;
    };
    let Some(consumer_name) = source_inputs.source_chunk_names.get(&consumer_chunk) else {
        return false;
    };
    if !provider_export_exists(
        source_inputs,
        correspondence,
        files,
        defining_chunk,
        &target.export,
    ) || !provider_closure_reaches_consumer(
        source_inputs,
        correspondence,
        files,
        graph,
        consumer_name,
        &consumer_output,
        consumer_index,
        closure,
    ) {
        return false;
    }
    closure.admitted_host_modules.iter().all(|dependency| {
        source_host_dependencies.contains(dependency)
            && host_dependency_route_exists(source_inputs, correspondence, dependency)
    })
}

fn source_unit_position(
    row: &CandidateSourceCorrespondence,
    ordinal: SourceUnitOrdinal,
) -> Option<(OutputFileAddress, usize)> {
    let route = row.source_units.get(&ordinal)?;
    if route.status != SourceCorrespondenceStatus::Complete || route.positions.len() != 1 {
        return None;
    }
    let position = route.positions.first()?;
    Some((position.output.clone(), position.item_index))
}

fn unique_cell_route<'a>(
    correspondence: &'a BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    expected_cell: ModuleCellId,
) -> Option<(
    ChunkId,
    &'a artifact::reports::CandidateModuleCellDeclaration,
    &'a CandidateSourceBindingRoute,
)> {
    let routes = correspondence
        .iter()
        .flat_map(|(chunk, row)| {
            row.module_cell_declarations
                .iter()
                .filter(move |declaration| declaration.source_cell == expected_cell)
                .flat_map(move |declaration| {
                    declaration
                        .routes
                        .iter()
                        .map(move |route| (*chunk, declaration, route))
                })
        })
        .collect::<Vec<_>>();
    match routes.as_slice() {
        [(chunk, declaration, route)] => Some((*chunk, *declaration, *route)),
        _ => None,
    }
}

fn candidate_binding_exists(
    files: &ChunkBundle,
    source_inputs: &FinalGlobalResolutionSourceInputs,
    source_chunk: ChunkId,
    route: &CandidateSourceBindingRoute,
) -> bool {
    let (Some(local), Some(binding_span)) = (route.candidate_local.as_ref(), route.binding_span)
    else {
        return false;
    };
    let Some(chunk) = files.find_chunk(route.output.chunk) else {
        return false;
    };
    let Some(file) = chunk.js.get_file(&route.output.file) else {
        return false;
    };
    if let Some(ast) = file.ast() {
        let mut idents = Vec::new();
        ast.module.visit_with(&mut IdentSpanCollector(&mut idents));
        return idents
            .iter()
            .filter(|(candidate, span)| candidate == local && *span == binding_span)
            .count()
            == 1;
    }
    raw_source_proofs_match_file(source_inputs, source_chunk, files, &route.output)
}

fn candidate_use_exists(
    files: &ChunkBundle,
    source_inputs: &FinalGlobalResolutionSourceInputs,
    source_chunk: ChunkId,
    route: &artifact::reports::CandidateSourceUseLocation,
) -> bool {
    let Some(chunk) = files.find_chunk(route.output.chunk) else {
        return false;
    };
    let Some(file) = chunk.js.get_file(&route.output.file) else {
        return false;
    };
    if let Some(ast) = file.ast() {
        let Some(item) = ast.module.body.get(route.item_index) else {
            return false;
        };
        let mut idents = Vec::new();
        item.visit_with(&mut IdentSpanCollector(&mut idents));
        return idents
            .iter()
            .filter(|(candidate, span)| {
                candidate == &route.candidate_local && *span == route.candidate_span
            })
            .count()
            == 1;
    }
    raw_source_proofs_match_file(source_inputs, source_chunk, files, &route.output)
}

struct IdentSpanCollector<'a>(&'a mut Vec<(swc_ecma_ast::Id, Span)>);

impl Visit for IdentSpanCollector<'_> {
    fn visit_ident(&mut self, ident: &swc_ecma_ast::Ident) {
        self.0.push((ident.to_id(), ident.span));
    }
}

fn raw_source_proofs_match_file(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    source_chunk: ChunkId,
    bundle: &ChunkBundle,
    output: &OutputFileAddress,
) -> bool {
    let Some(proof) = source_inputs.raw_source_proofs.get(&source_chunk) else {
        return false;
    };
    let Some(chunk) = bundle.find_chunk(output.chunk) else {
        return false;
    };
    let Some(file) = chunk.js.get_file(&output.file) else {
        return false;
    };
    raw_file_matches(proof, output.chunk, file)
}

fn provider_export_exists(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    provider_chunk: ChunkId,
    export_name: &str,
) -> bool {
    let Some(row) = correspondence.get(&provider_chunk) else {
        return false;
    };
    let routes = row
        .exports
        .iter()
        .filter(|route| {
            route.source.public_name == export_name
                && route.status == SourceCorrespondenceStatus::Complete
        })
        .flat_map(|route| &route.routes)
        .collect::<Vec<_>>();
    let [route] = routes.as_slice() else {
        return false;
    };
    route.exported_name.as_deref() == Some(export_name)
        && candidate_binding_exists(files, source_inputs, provider_chunk, route)
        && candidate_binding_export_exists(files, source_inputs, provider_chunk, route, export_name)
}

fn candidate_binding_export_exists(
    files: &ChunkBundle,
    source_inputs: &FinalGlobalResolutionSourceInputs,
    source_chunk: ChunkId,
    route: &CandidateSourceBindingRoute,
    export_name: &str,
) -> bool {
    let Some(local) = route.candidate_local.as_ref() else {
        return false;
    };
    let Some(file) = files
        .find_chunk(route.output.chunk)
        .and_then(|chunk| chunk.js.get_file(&route.output.file))
    else {
        return false;
    };
    let Some(ast) = file.ast() else {
        return raw_source_proofs_match_file(source_inputs, source_chunk, files, &route.output);
    };
    ast.module.body.iter().any(|item| match item {
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) if export.src.is_none() => {
            export.specifiers.iter().any(|specifier| {
                let ExportSpecifier::Named(named) = specifier else {
                    return false;
                };
                let swc_ecma_ast::ModuleExportName::Ident(orig) = &named.orig else {
                    return false;
                };
                let exported = named
                    .exported
                    .as_ref()
                    .map(binding_targets::module_export_name)
                    .unwrap_or_else(|| binding_targets::module_export_name(&named.orig));
                orig.to_id() == *local
                    && exported == export_name
                    && route
                        .exported_specifier_span
                        .is_none_or(|span| named.span == span)
            })
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
            export_name == local.0.as_ref()
                && route.binding_span.is_some_and(|binding_span| {
                    binding_targets::declaration_ids(&export.decl).contains(local)
                        && item.span().lo.0 <= binding_span.lo.0
                        && item.span().hi.0 >= binding_span.hi.0
                        && route
                            .declaration_span
                            .is_none_or(|span| export.decl.span() == span)
                })
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(export))
            if export_name == "default" =>
        {
            let id = match &export.decl {
                swc_ecma_ast::DefaultDecl::Fn(function) => {
                    function.ident.as_ref().map(swc_ecma_ast::Ident::to_id)
                }
                swc_ecma_ast::DefaultDecl::Class(class) => {
                    class.ident.as_ref().map(swc_ecma_ast::Ident::to_id)
                }
                swc_ecma_ast::DefaultDecl::TsInterfaceDecl(_) => None,
            };
            id.as_ref() == Some(local)
                && route
                    .declaration_span
                    .is_none_or(|span| export.span == span)
        }
        _ => false,
    })
}

fn provider_closure_reaches_consumer(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    graph: &BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
    consumer_name: &str,
    consumer_output: &OutputFileAddress,
    consumer_index: usize,
    closure: &analysis::analysis_inputs::ResolvedProviderDependencyClosure,
) -> bool {
    let Some(table) = source_inputs.source_chunk_table.as_ref() else {
        return false;
    };
    for provider_name in &closure.modules {
        if provider_name == consumer_name {
            return false;
        }
        let Some(provider_chunk) = table.get(provider_name) else {
            return false;
        };
        let Some(provider_outputs) =
            source_chunk_outputs(source_inputs, correspondence, files, provider_chunk)
        else {
            return false;
        };
        let Some(provider_row) = correspondence.get(&provider_chunk) else {
            return false;
        };
        if !provider_outputs_precede_consumer(
            graph,
            provider_row,
            &provider_outputs,
            consumer_output,
            consumer_index,
        ) {
            return false;
        }
    }
    provider_closure_acyclic(graph, consumer_output)
        && closure.admitted_host_modules.iter().all(|dependency| {
            host_dependency_route_exists(source_inputs, correspondence, dependency)
        })
}

fn provider_precedes_consumer(
    graph: &BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
    provider_row: &CandidateSourceCorrespondence,
    provider_output: &OutputFileAddress,
    consumer_output: &OutputFileAddress,
    consumer_index: usize,
) -> bool {
    if provider_output != consumer_output {
        return graph_reaches(graph, consumer_output, provider_output)
            && provider_closure_acyclic(graph, consumer_output);
    }
    if provider_row.source_unit_status != SourceCorrespondenceStatus::Complete {
        return false;
    }
    let provider_positions = provider_row
        .source_units
        .values()
        .flat_map(|route| {
            if route.status == SourceCorrespondenceStatus::Complete {
                route.positions.iter().collect::<Vec<_>>()
            } else {
                Vec::new()
            }
        })
        .filter(|position| &position.output == provider_output)
        .map(|position| position.item_index)
        .collect::<Vec<_>>();
    !provider_positions.is_empty()
        && provider_positions
            .into_iter()
            .all(|provider_index| provider_index < consumer_index)
}

fn provider_outputs_precede_consumer(
    graph: &BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
    provider_row: &CandidateSourceCorrespondence,
    provider_outputs: &BTreeSet<OutputFileAddress>,
    consumer_output: &OutputFileAddress,
    consumer_index: usize,
) -> bool {
    !provider_outputs.is_empty()
        && provider_outputs.iter().all(|provider_output| {
            provider_precedes_consumer(
                graph,
                provider_row,
                provider_output,
                consumer_output,
                consumer_index,
            )
        })
}

fn source_chunk_outputs(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    files: &ChunkBundle,
    source_chunk: ChunkId,
) -> Option<BTreeSet<OutputFileAddress>> {
    let row = correspondence.get(&source_chunk)?;
    if row.source_unit_status == SourceCorrespondenceStatus::Complete {
        let mut outputs = BTreeSet::new();
        for route in row.source_units.values() {
            if route.status != SourceCorrespondenceStatus::Complete || route.positions.len() != 1 {
                return None;
            }
            outputs.insert(route.positions.first()?.output.clone());
        }
        if !outputs.is_empty() {
            return Some(outputs);
        }
    }
    let chunk = files.find_chunk(source_chunk)?;
    let entry = chunk
        .js
        .files
        .iter()
        .filter(|file| file.metadata.role == FileRole::Entry)
        .collect::<Vec<_>>();
    let [entry] = entry.as_slice() else {
        return None;
    };
    if raw_source_proofs_match_file(
        source_inputs,
        source_chunk,
        files,
        &OutputFileAddress {
            chunk: source_chunk,
            file: entry.path.clone(),
        },
    ) {
        Some(BTreeSet::from([OutputFileAddress {
            chunk: source_chunk,
            file: entry.path.clone(),
        }]))
    } else {
        None
    }
}

fn host_dependency_route_exists(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    correspondence: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    dependency: &analysis::analysis_inputs::AdmittedHostModuleDependency,
) -> bool {
    if !dependency.is_well_formed() {
        return false;
    }
    let Some(source_chunk) = source_inputs
        .source_chunk_table
        .as_ref()
        .and_then(|table| table.get(&dependency.source_module))
    else {
        return false;
    };
    let Some(row) = correspondence.get(&source_chunk) else {
        return false;
    };
    row.source_requests.iter().any(|request| {
        request.source.kind == artifact::reports::SourceStaticRequestKind::Import
            && request.source.request == dependency.specifier
            && request.source.mode == artifact::reports::StaticModuleRequestMode::OrdinaryEsm
            && request.status == SourceCorrespondenceStatus::Complete
            && request.routes.iter().any(|route| {
                route.status == SourceCorrespondenceStatus::Complete
                    && route.emitted_request == dependency.specifier
                    && route.targets.iter().any(|target| {
                        matches!(target, CandidateImportTarget::OriginalExternal { request }
                            if request == &dependency.specifier)
                    })
                    && row.prepared_requests.iter().any(|prepared| {
                        prepared.output == route.output
                            && prepared.item_span == route.item_span
                            && prepared.source_statement == route.source_statement
                            && prepared.emitted_request == dependency.specifier
                            && prepared.targets.iter().any(|target| {
                                matches!(target, CandidateImportTarget::OriginalExternal { request }
                                    if request == &dependency.specifier)
                            })
                    })
            })
    })
}

fn graph_reaches(
    graph: &BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
    from: &OutputFileAddress,
    target: &OutputFileAddress,
) -> bool {
    if from == target {
        return true;
    }
    let mut pending = vec![from.clone()];
    let mut seen = BTreeSet::new();
    while let Some(current) = pending.pop() {
        if !seen.insert(current.clone()) {
            continue;
        }
        for next in graph.get(&current).into_iter().flatten() {
            if next == target {
                return true;
            }
            pending.push(next.clone());
        }
    }
    false
}

fn provider_closure_acyclic(
    graph: &BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
    root: &OutputFileAddress,
) -> bool {
    fn visit(
        graph: &BTreeMap<OutputFileAddress, BTreeSet<OutputFileAddress>>,
        node: &OutputFileAddress,
        active: &mut BTreeSet<OutputFileAddress>,
        complete: &mut BTreeSet<OutputFileAddress>,
    ) -> bool {
        if complete.contains(node) {
            return true;
        }
        if !active.insert(node.clone()) {
            return false;
        }
        for target in graph.get(node).into_iter().flatten() {
            if !visit(graph, target, active, complete) {
                return false;
            }
        }
        active.remove(node);
        complete.insert(node.clone());
        true
    }
    visit(graph, root, &mut BTreeSet::new(), &mut BTreeSet::new())
}

#[cfg(test)]
mod tests {
    use super::*;
    use artifact::reports::{
        CandidatePreparedRequestBinding, CandidateRewriteBindingIdentity, CandidateRewriteReceipt,
        CandidateSourceImportRoutes, CandidateSourceRequestRoute,
        CandidateSourceStaticRequestRoutes, ParsedSourceImportBinding, ParsedSourceRequest,
        SourceStaticRequestKind,
    };
    use source_identity::StatementId;
    use swc_atoms::Atom;
    use swc_common::{BytePos, SyntaxContext};

    fn span(lo: u32, hi: u32) -> Span {
        Span::new(BytePos(lo), BytePos(hi))
    }

    fn id(name: &str) -> swc_ecma_ast::Id {
        (Atom::from(name), SyntaxContext::empty())
    }

    fn source_import_correspondence(
        receipt: CandidateRewriteReceipt,
    ) -> (
        CandidateSourceCorrespondence,
        CandidateSourceStaticRequestRoutes,
    ) {
        let source_chunk = ChunkId(0);
        let statement = StatementId(3);
        let declaration_span = span(10, 30);
        let specifier_span = span(17, 24);
        let source_local = id("source_value");
        let source = ParsedSourceRequest {
            statement,
            declaration_span,
            request: "./provider.js".to_owned(),
            mode: artifact::reports::StaticModuleRequestMode::OrdinaryEsm,
            kind: SourceStaticRequestKind::Import,
            specifier_spans: vec![specifier_span],
            type_only: false,
        };
        let binding = ParsedSourceImportBinding {
            statement,
            specifier_index: 0,
            local: source_local,
            local_span: span(20, 22),
            declaration_span,
            request: "./provider.js".to_owned(),
            imported_name: "value".to_owned(),
            kind: ImportSpecifierKind::Named,
            type_only: false,
            use_spans: Vec::new(),
            use_sites: Vec::new(),
        };
        let output = receipt.output.clone();
        let mut routes = Vec::new();
        if let Some(after) = receipt.after.as_ref() {
            routes.push(CandidateSourceRequestRoute {
                output: output.clone(),
                item_index: after.item_index,
                item_span: after.item_span,
                source_statement: after.source_statement,
                emitted_request: after.request.clone().unwrap_or_default(),
                mode: after.mode.unwrap(),
                targets: after.targets.clone(),
                bindings: after
                    .bindings
                    .iter()
                    .map(|binding| CandidatePreparedRequestBinding {
                        source_binding: binding.source_binding.clone(),
                        candidate_local: binding.local.clone(),
                        specifier_index: binding.specifier_index,
                        specifier_span: binding.specifier_span,
                        imported_name: binding.imported_name.clone(),
                        kind: binding.kind.unwrap(),
                        exported_name: binding.exported_name.clone(),
                        source_cell: binding.source_cell,
                        target: binding.target.clone().unwrap(),
                    })
                    .collect(),
                status: SourceCorrespondenceStatus::Complete,
            });
        }
        let request = CandidateSourceStaticRequestRoutes {
            source,
            routes: routes.clone(),
            status: SourceCorrespondenceStatus::Complete,
        };
        let before = receipt.before.clone().unwrap();
        let emission = CandidateRewriteReceipt {
            source_chunk,
            output,
            stage: CandidateRewriteStage::EmissionVendorRewrite,
            before: Some(before.clone()),
            after: Some(before),
            disposition: CandidateRewriteDisposition::Preserved,
            elisions: Vec::new(),
            issue: None,
        };
        let correspondence = CandidateSourceCorrespondence {
            source_chunk,
            source_file: "source/entry.js".to_owned(),
            source_units: BTreeMap::new(),
            source_unit_status: SourceCorrespondenceStatus::MissingCandidateRoute,
            source_requests: vec![request.clone()],
            imports: vec![CandidateSourceImportRoutes {
                source: binding,
                routes: Vec::new(),
                uses: Vec::new(),
                status: SourceCorrespondenceStatus::Complete,
            }],
            reexports: Vec::new(),
            exports: Vec::new(),
            prepared_requests: Vec::new(),
            module_cell_declarations: Vec::new(),
            source_module_cells: Some(BTreeMap::new()),
            source_initialized_module_cells: Some(BTreeSet::new()),
            rewrite_receipts: vec![emission, receipt],
            status: SourceCorrespondenceStatus::Complete,
        };
        (correspondence, request)
    }

    fn dead_import_receipt() -> CandidateRewriteReceipt {
        let source_chunk = ChunkId(0);
        let source_statement = StatementId(3);
        let declaration_span = span(10, 30);
        let specifier_span = span(17, 24);
        let source_binding = id("source_value");
        let candidate_local = id("candidate_value");
        let request = "./provider.js".to_owned();
        let target = CandidateImportTarget::OriginalSource {
            request: request.clone(),
        };
        let before = artifact::reports::CandidateStaticRequestIdentity {
            item_index: 0,
            item_span: declaration_span,
            kind: CandidateOutputRequestKind::Import,
            source_statement: Some(source_statement),
            request: Some(request),
            mode: Some(artifact::reports::StaticModuleRequestMode::OrdinaryEsm),
            targets: vec![target.clone()],
            bindings: vec![CandidateRewriteBindingIdentity {
                specifier_index: 0,
                specifier_span,
                local: Some(candidate_local.clone()),
                imported_name: Some("value".to_owned()),
                exported_name: None,
                kind: Some(ImportSpecifierKind::Named),
                source_binding: Some(source_binding),
                source_cell: None,
                target: Some(target),
            }],
        };
        let after = artifact::reports::CandidateStaticRequestIdentity {
            item_index: 0,
            item_span: declaration_span,
            kind: CandidateOutputRequestKind::Import,
            source_statement: Some(source_statement),
            request: Some("./provider.js".to_owned()),
            mode: Some(artifact::reports::StaticModuleRequestMode::OrdinaryEsm),
            targets: before.targets.clone(),
            bindings: Vec::new(),
        };
        CandidateRewriteReceipt {
            source_chunk,
            output: OutputFileAddress {
                chunk: ChunkId(1),
                file: "entry.js".to_owned(),
            },
            stage: CandidateRewriteStage::DeadImportPruning,
            before: Some(before),
            after: Some(after),
            disposition: CandidateRewriteDisposition::Rewritten,
            elisions: vec![CandidateRewriteElision::DeadNamedImport {
                local: candidate_local,
                request_retained_as_bare: true,
                duplicate_request_dropped_after_earlier_load: false,
            }],
            issue: None,
        }
    }

    #[test]
    fn partial_dead_import_receipt_discharges_only_its_exact_source_binding() {
        let (mut row, request) = source_import_correspondence(dead_import_receipt());
        assert!(source_request_specifiers_are_accounted(&row, &request));

        let receipt = &mut row.rewrite_receipts[1];
        receipt.before.as_mut().unwrap().source_statement = Some(StatementId(4));
        assert!(!source_request_specifiers_are_accounted(&row, &request));

        let (mut row, request) = source_import_correspondence(dead_import_receipt());
        let CandidateRewriteElision::DeadNamedImport { local, .. } =
            &mut row.rewrite_receipts[1].elisions[0]
        else {
            unreachable!();
        };
        *local = id("unrelated_local");
        assert!(!source_request_specifiers_are_accounted(&row, &request));

        let (mut row, request) = source_import_correspondence(dead_import_receipt());
        let receipt = &mut row.rewrite_receipts[1];
        let mut retained = receipt.before.as_ref().unwrap().bindings[0].clone();
        retained.specifier_index = 0;
        receipt.after.as_mut().unwrap().bindings.push(retained);
        assert!(!source_request_specifiers_are_accounted(&row, &request));
    }

    fn source_unit_row(
        chunk: ChunkId,
        rows: &[(SourceUnitOrdinal, OutputFileAddress, usize)],
    ) -> CandidateSourceCorrespondence {
        let source_units = rows
            .iter()
            .map(|(ordinal, output, item_index)| {
                (
                    *ordinal,
                    artifact::reports::CandidateSourceUnitRoute {
                        source_unit: *ordinal,
                        source_statement: StatementId(ordinal.0),
                        source_span: span(10 + ordinal.0 as u32, 11 + ordinal.0 as u32),
                        source_declarator_span: None,
                        outputs: vec![output.clone()],
                        positions: vec![artifact::reports::CandidateSourcePosition {
                            output: output.clone(),
                            item_index: *item_index,
                            item_span: span(100 + *item_index as u32, 101 + *item_index as u32),
                            declarator_span: None,
                            declarator_offset: None,
                        }],
                        status: SourceCorrespondenceStatus::Complete,
                    },
                )
            })
            .collect();
        CandidateSourceCorrespondence {
            source_chunk: chunk,
            source_file: format!("chunk-{}/entry.js", chunk.0),
            source_units,
            source_unit_status: SourceCorrespondenceStatus::Complete,
            source_requests: Vec::new(),
            imports: Vec::new(),
            reexports: Vec::new(),
            exports: Vec::new(),
            prepared_requests: Vec::new(),
            module_cell_declarations: Vec::new(),
            source_module_cells: Some(BTreeMap::new()),
            source_initialized_module_cells: Some(BTreeSet::new()),
            rewrite_receipts: Vec::new(),
            status: SourceCorrespondenceStatus::Complete,
        }
    }

    fn output(chunk: ChunkId, file: &str) -> OutputFileAddress {
        OutputFileAddress {
            chunk,
            file: file.to_owned(),
        }
    }

    #[test]
    fn same_file_provider_must_precede_consumer() {
        let same_file = output(ChunkId(0), "entry.js");
        let before = source_unit_row(ChunkId(0), &[(SourceUnitOrdinal(0), same_file.clone(), 1)]);
        let after = source_unit_row(ChunkId(0), &[(SourceUnitOrdinal(0), same_file.clone(), 3)]);
        let graph = BTreeMap::new();
        assert!(provider_precedes_consumer(
            &graph, &before, &same_file, &same_file, 2,
        ));
        assert!(!provider_precedes_consumer(
            &graph, &after, &same_file, &same_file, 2,
        ));
    }

    #[test]
    fn every_provider_output_must_be_reachable_and_acyclic_before_consumer() {
        let consumer = output(ChunkId(2), "consumer.js");
        let provider_a = output(ChunkId(0), "a.js");
        let provider_b = output(ChunkId(1), "b.js");
        let provider_row = source_unit_row(
            ChunkId(0),
            &[
                (SourceUnitOrdinal(0), provider_a.clone(), 0),
                (SourceUnitOrdinal(1), provider_b.clone(), 0),
            ],
        );
        let provider_outputs = BTreeSet::from([provider_a.clone(), provider_b.clone()]);
        let graph = BTreeMap::from([
            (
                consumer.clone(),
                BTreeSet::from([provider_a.clone(), provider_b.clone()]),
            ),
            (provider_a.clone(), BTreeSet::new()),
            (provider_b.clone(), BTreeSet::new()),
        ]);
        assert!(provider_outputs_precede_consumer(
            &graph,
            &provider_row,
            &provider_outputs,
            &consumer,
            3,
        ));

        let missing_second_provider = BTreeMap::from([
            (consumer.clone(), BTreeSet::from([provider_a.clone()])),
            (provider_a.clone(), BTreeSet::new()),
            (provider_b.clone(), BTreeSet::new()),
        ]);
        assert!(!provider_outputs_precede_consumer(
            &missing_second_provider,
            &provider_row,
            &provider_outputs,
            &consumer,
            3,
        ));

        let cyclic = BTreeMap::from([
            (
                consumer.clone(),
                BTreeSet::from([provider_a.clone(), provider_b.clone()]),
            ),
            (provider_a.clone(), BTreeSet::from([consumer.clone()])),
            (provider_b.clone(), BTreeSet::new()),
        ]);
        assert!(!provider_outputs_precede_consumer(
            &cyclic,
            &provider_row,
            &provider_outputs,
            &consumer,
            3,
        ));
    }
}
