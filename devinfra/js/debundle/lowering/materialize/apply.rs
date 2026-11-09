use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet, HashMap};

use crate::staged_analysis::program_summary::ProgramSummary;
use anyhow::{Result, bail};
use artifact::reports::{
    CandidateImportRoute, CandidateImportTarget, CandidateModuleCellDeclaration,
    CandidateOutputRequestKind, CandidatePreparedRequestBinding, CandidatePreparedRequestRoute,
    CandidateSourceBindingRoute, CandidateSourceCorrespondence, CandidateSourceExportRoutes,
    CandidateSourceImportRoutes, CandidateSourceReExportRoutes, CandidateSourceRequestRoute,
    CandidateSourceStaticRequestRoutes, CandidateSourceUseLocation, CandidateSourceUseRoute,
    ChunkAnalysisReport, ChunkDecompositionOutput, ChunkFileRecord, ChunkLogicalModulesSummary,
    ImportSpecifierKind, OutputFileAddress, ParsedSourceImports, ParsedSourceReExport,
    ParsedSourceRequest, SourceCorrespondenceStatus, SourceReExportKind, SourceStaticRequestKind,
    StaticModuleRequestMode,
};
use artifact::{ArtifactIndexes, ChunkArtifact, ChunkBundle, ChunkMetadata, JsChunk};
use swc_common::{Span, Spanned};

use super::MaterializedLogicalChunk;
#[cfg(test)]
use crate::{ChunkModulesCounts, ChunkModulesReport};
#[cfg(test)]
use artifact::reports::ChunkValidationSummary;
#[cfg(test)]
use source_identity::ChunkTable;

/// Files finalized by lowering, together with decomposition metadata for
/// lowered chunks. Pass-through chunks retain their original position.
pub struct EmissionChunkOutputs {
    pub files: artifact::EmissionFiles,
    pub decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput>,
    /// Runtime-only source-to-candidate correspondence. It is not serialized
    /// into artifact reports; downstream gates consume the exact typed proof.
    pub source_correspondence_by_chunk: BTreeMap<ChunkId, CandidateSourceCorrespondence>,
}

pub(crate) fn collect_materialized_logical_chunks(
    artifact: ChunkBundle,
    target_dir: &str,
    chunks: Vec<MaterializedLogicalChunk>,
    program_summary: Option<&ProgramSummary>,
) -> Result<EmissionChunkOutputs> {
    let mut replacements = BTreeMap::<ChunkId, MaterializedLogicalChunk>::new();
    let known_chunks: BTreeSet<ChunkId> =
        artifact.chunks.iter().map(|chunk| chunk.chunk_id).collect();
    for chunk in chunks {
        let chunk_id = chunk.chunk_id;
        if !known_chunks.contains(&chunk_id) {
            bail!(
                "materialize_logical_modules produced unknown chunk index: {}",
                chunk_id.0
            );
        }
        if replacements.insert(chunk_id, chunk).is_some() {
            bail!(
                "materialize_logical_modules produced duplicate chunk_id: {}",
                artifact.chunk_table.name(chunk_id)
            );
        }
    }
    let indexes = ArtifactIndexes::build(&artifact)?;
    let chunk_table = artifact.chunk_table.clone();
    let mut decomposition_by_chunk = HashMap::new();
    let mut source_correspondence_by_chunk = BTreeMap::new();
    let chunks = artifact
        .chunks
        .into_iter()
        .map(|chunk| {
            if let Some(replacement) = replacements.remove(&chunk.chunk_id) {
                source_correspondence_by_chunk
                    .insert(chunk.chunk_id, replacement.source_correspondence.clone());
                let (output, decomposition) =
                    materialized_chunk_artifact(target_dir, chunk.analysis, replacement);
                decomposition_by_chunk.insert(chunk.chunk_id, decomposition);
                output
            } else {
                source_correspondence_by_chunk.insert(
                    chunk.chunk_id,
                    raw_source_correspondence(&chunk, &chunk_table, &indexes, program_summary),
                );
                chunk
            }
        })
        .collect();
    Ok(EmissionChunkOutputs {
        files: artifact::EmissionFiles::new(ChunkBundle {
            chunks,
            chunk_table: artifact.chunk_table,
        })?,
        decomposition_by_chunk,
        source_correspondence_by_chunk,
    })
}

/// Exact identity rows for an unchanged source chunk. The source-unit layout
/// is intentionally unavailable here: source statement spans and import Ids
/// survive the raw pass-through, but this collector has no split-aware
/// SourceUnitLayout and does not manufacture ordinal placement.
pub(super) fn raw_source_correspondence(
    chunk: &ChunkArtifact,
    chunk_table: &source_identity::ChunkTable,
    indexes: &ArtifactIndexes,
    summary: Option<&ProgramSummary>,
) -> CandidateSourceCorrespondence {
    let parsed = chunk.analysis.runtime_facts.parsed_source_imports.as_ref();
    let Some(parsed) = parsed else {
        return CandidateSourceCorrespondence::unavailable(
            chunk.chunk_id,
            chunk
                .analysis
                .runtime_facts
                .global_uses
                .as_ref()
                .map(|uses| uses.source_file.clone())
                .unwrap_or_else(|| chunk.analysis.source_path.clone()),
            None,
        );
    };
    let output = OutputFileAddress {
        chunk: chunk.chunk_id,
        file: chunk.js.entry_file.clone(),
    };
    let source_cells =
        summary.and_then(|summary| summary.module_cells.get(chunk_table.name(chunk.chunk_id)));
    let source_initialized = summary.and_then(|summary| {
        summary
            .source_initialized_module_cells
            .get(chunk_table.name(chunk.chunk_id))
    });
    let module_cells = source_cells.cloned();
    let initialized_cells = source_initialized.cloned();

    let target_for = |request: &ParsedSourceRequest| {
        if let Some(resolved) = indexes.resolve_runtime_import_reference(
            &request.request,
            chunk.chunk_id,
            &chunk.js.entry_file,
            chunk_table,
        ) {
            CandidateImportTarget::CandidateOutput {
                chunk: resolved.target_chunk_id,
                file: resolved.target_file,
            }
        } else if request.request.starts_with('.') || request.request.starts_with('/') {
            CandidateImportTarget::OriginalSourceUnknownExports {
                request: request.request.clone(),
            }
        } else if request.kind == SourceStaticRequestKind::Import {
            CandidateImportTarget::OriginalSource {
                request: request.request.clone(),
            }
        } else {
            CandidateImportTarget::OriginalExternal {
                request: request.request.clone(),
            }
        }
    };

    let prepared_requests = parsed
        .requests
        .iter()
        .map(|request| {
            let target = target_for(request);
            let bindings = raw_request_bindings(parsed, request, &target, source_cells);
            let status = raw_request_status(request, &target, &bindings);
            CandidatePreparedRequestRoute {
                output: output.clone(),
                item_index: request.statement.0,
                item_span: request.declaration_span,
                request_kind: raw_request_kind(request.kind),
                source_statement: Some(request.statement),
                emitted_request: request.request.clone(),
                mode: request.mode,
                targets: vec![target],
                bindings,
                status,
            }
        })
        .collect::<Vec<_>>();

    let source_requests = parsed
        .requests
        .iter()
        .cloned()
        .zip(prepared_requests.iter())
        .map(|(source, prepared)| {
            let route = CandidateSourceRequestRoute {
                output: prepared.output.clone(),
                item_index: prepared.item_index,
                item_span: prepared.item_span,
                source_statement: prepared.source_statement,
                emitted_request: prepared.emitted_request.clone(),
                mode: prepared.mode,
                targets: prepared.targets.clone(),
                bindings: prepared.bindings.clone(),
                status: prepared.status,
            };
            CandidateSourceStaticRequestRoutes {
                source,
                routes: vec![route],
                status: prepared.status,
            }
        })
        .collect::<Vec<_>>();

    let imports = parsed
        .bindings
        .iter()
        .cloned()
        .map(|source| {
            let request = parsed.requests.iter().find(|request| {
                request.statement == source.statement
                    && request.kind == SourceStaticRequestKind::Import
            });
            let prepared = request.and_then(|request| {
                prepared_requests.iter().find(|prepared| {
                    prepared.item_index == request.statement.0
                        && prepared.item_span == request.declaration_span
                        && prepared.request_kind == CandidateOutputRequestKind::Import
                })
            });
            let binding = prepared.and_then(|prepared| {
                prepared.bindings.iter().find(|binding| {
                    binding.specifier_index == source.specifier_index
                        && binding.source_binding.as_ref() == Some(&source.local)
                        && binding.candidate_local.as_ref() == Some(&source.local)
                })
            });
            let route = binding.and_then(|binding| {
                let prepared = prepared?;
                Some(CandidateImportRoute {
                    output: prepared.output.clone(),
                    item_span: prepared.item_span,
                    candidate_local: source.local.clone(),
                    specifier_index: source.specifier_index,
                    specifier_span: binding.specifier_span,
                    emitted_request: prepared.emitted_request.clone(),
                    imported_name: source.imported_name.clone(),
                    kind: source.kind,
                    target: binding.target.clone(),
                    source_cell: binding.source_cell,
                })
            });
            let uses = source
                .use_sites
                .iter()
                .copied()
                .map(|site| CandidateSourceUseRoute {
                    source_span: site.span,
                    routes: vec![CandidateSourceUseLocation {
                        output: output.clone(),
                        item_index: site.statement.0,
                        candidate_span: site.span,
                        candidate_local: source.local.clone(),
                    }],
                    status: SourceCorrespondenceStatus::Complete,
                })
                .collect::<Vec<_>>();
            let status = match (&route, prepared, source.type_only) {
                (_, _, true) => SourceCorrespondenceStatus::UnsupportedRewrite,
                (None, _, false) => SourceCorrespondenceStatus::MissingCandidateRoute,
                (Some(route), Some(prepared), false)
                    if prepared.status == SourceCorrespondenceStatus::Complete
                        && !matches!(
                            route.target,
                            CandidateImportTarget::Unsupported
                                | CandidateImportTarget::OriginalSourceUnknownExports { .. }
                        ) =>
                {
                    SourceCorrespondenceStatus::Complete
                }
                _ => SourceCorrespondenceStatus::UnsupportedRewrite,
            };
            CandidateSourceImportRoutes {
                source,
                routes: route.into_iter().collect(),
                uses,
                status,
            }
        })
        .collect::<Vec<_>>();

    let reexports = parsed
        .reexports
        .iter()
        .cloned()
        .map(|source| {
            let request = parsed.requests.iter().find(|request| {
                request.statement == source.statement
                    && request.kind != SourceStaticRequestKind::Import
                    && request.request == source.request
            });
            let routes = request
                .and_then(|request| {
                    prepared_requests.iter().find(|prepared| {
                        prepared.item_index == request.statement.0
                            && prepared.item_span == request.declaration_span
                    })
                })
                .map(|prepared| CandidateSourceRequestRoute {
                    output: prepared.output.clone(),
                    item_index: prepared.item_index,
                    item_span: prepared.item_span,
                    source_statement: prepared.source_statement,
                    emitted_request: prepared.emitted_request.clone(),
                    mode: prepared.mode,
                    targets: prepared.targets.clone(),
                    bindings: prepared.bindings.clone(),
                    status: prepared.status,
                })
                .into_iter()
                .collect::<Vec<_>>();
            let status = if source.type_only {
                SourceCorrespondenceStatus::UnsupportedRewrite
            } else if routes.is_empty() {
                SourceCorrespondenceStatus::MissingCandidateRoute
            } else {
                routes[0].status
            };
            CandidateSourceReExportRoutes {
                source,
                routes,
                status,
            }
        })
        .collect::<Vec<_>>();

    let exports = parsed
        .exports
        .iter()
        .cloned()
        .map(|source| {
            let location = source.local.as_ref().and_then(|local| {
                parsed
                    .bindings
                    .iter()
                    .find(|binding| &binding.local == local)
                    .map(|binding| (binding.declaration_span, binding.local_span))
                    .or_else(|| {
                        let cell = source_cells?.get(local)?;
                        let binding_span = summary?
                            .module_cell_invariants
                            .get(cell)?
                            .declaration
                            .binding_span;
                        Some((
                            raw_declaration_span(chunk, local, binding_span)?,
                            binding_span,
                        ))
                    })
            });
            let status = if source.type_only {
                SourceCorrespondenceStatus::UnsupportedRewrite
            } else if source.local.is_some() && location.is_none() {
                SourceCorrespondenceStatus::MissingCandidateRoute
            } else {
                SourceCorrespondenceStatus::Complete
            };
            CandidateSourceExportRoutes {
                source: source.clone(),
                routes: vec![CandidateSourceBindingRoute {
                    output: output.clone(),
                    candidate_local: source.local.clone(),
                    declaration_span: location.map(|location| location.0),
                    binding_span: location.map(|location| location.1),
                    exported_specifier_span: source.specifier_span,
                    exported_name: Some(source.public_name.clone()),
                }],
                status,
            }
        })
        .collect::<Vec<_>>();

    let module_cell_declarations = source_cells
        .into_iter()
        .flat_map(|cells| cells.iter())
        .filter(|(_, cell)| cell.chunk == chunk.chunk_id)
        .map(|(binding, source_cell)| {
            let invariant =
                summary.and_then(|summary| summary.module_cell_invariants.get(source_cell));
            let export = parsed
                .exports
                .iter()
                .find(|export| export.local.as_ref() == Some(binding));
            let route = invariant.and_then(|invariant| {
                Some(CandidateSourceBindingRoute {
                    output: output.clone(),
                    candidate_local: Some(binding.clone()),
                    declaration_span: raw_declaration_span(
                        chunk,
                        binding,
                        invariant.declaration.binding_span,
                    ),
                    binding_span: Some(invariant.declaration.binding_span),
                    exported_specifier_span: export.and_then(|export| export.specifier_span),
                    exported_name: export.map(|export| export.public_name.clone()),
                })
            });
            CandidateModuleCellDeclaration {
                source_cell: *source_cell,
                source_binding: Some(binding.clone()),
                source_owner: invariant.map(|invariant| invariant.declaration.owner),
                declaration_span: route.as_ref().and_then(|route| route.declaration_span),
                routes: route.clone().into_iter().collect(),
                status: if route
                    .as_ref()
                    .is_some_and(|route| route.declaration_span.is_some())
                {
                    SourceCorrespondenceStatus::Complete
                } else {
                    SourceCorrespondenceStatus::MissingCandidateRoute
                },
            }
        })
        .collect::<Vec<_>>();

    let has_uninventoried_runtime_file = chunk.js.files.iter().any(|file| {
        file.path != chunk.js.entry_file && file.metadata.role == artifact::FileRole::Runtime
    });
    let request_rows_complete = prepared_requests
        .iter()
        .all(|request| request.status == SourceCorrespondenceStatus::Complete)
        && imports
            .iter()
            .all(|import| import.status == SourceCorrespondenceStatus::Complete)
        && reexports
            .iter()
            .all(|reexport| reexport.status == SourceCorrespondenceStatus::Complete)
        && exports
            .iter()
            .all(|export| export.status == SourceCorrespondenceStatus::Complete);
    let cells_complete = module_cells.is_some()
        && initialized_cells.is_some()
        && module_cell_declarations
            .iter()
            .all(|declaration| declaration.status == SourceCorrespondenceStatus::Complete);
    let parsed_identity = !parsed.source_file.is_empty()
        && chunk
            .analysis
            .runtime_facts
            .global_uses
            .as_ref()
            .is_some_and(|uses| uses.source_file == parsed.source_file);
    let status = if request_rows_complete
        && cells_complete
        && parsed_identity
        && !has_uninventoried_runtime_file
    {
        SourceCorrespondenceStatus::Complete
    } else if parsed_identity {
        SourceCorrespondenceStatus::MissingCandidateRoute
    } else {
        SourceCorrespondenceStatus::MissingSourceFacts
    };

    CandidateSourceCorrespondence {
        source_chunk: chunk.chunk_id,
        source_file: parsed.source_file.clone(),
        source_units: BTreeMap::new(),
        source_unit_status: SourceCorrespondenceStatus::MissingCandidateRoute,
        source_requests,
        imports,
        reexports,
        exports,
        prepared_requests,
        module_cell_declarations,
        source_module_cells: module_cells,
        source_initialized_module_cells: initialized_cells,
        rewrite_receipts: Vec::new(),
        status,
    }
}

fn raw_request_kind(kind: SourceStaticRequestKind) -> CandidateOutputRequestKind {
    match kind {
        SourceStaticRequestKind::Import => CandidateOutputRequestKind::Import,
        SourceStaticRequestKind::NamedReExport => CandidateOutputRequestKind::NamedReExport,
        SourceStaticRequestKind::ExportAll => CandidateOutputRequestKind::ExportAll,
    }
}

fn raw_declaration_span(
    chunk: &ChunkArtifact,
    binding: &swc_ecma_ast::Id,
    binding_span: Span,
) -> Option<Span> {
    let ast = chunk.js.get_file(&chunk.js.entry_file)?.ast()?;
    let matches = ast
        .module
        .body
        .iter()
        .filter(|item| {
            let span = item.span();
            span.lo.0 <= binding_span.lo.0 && span.hi.0 >= binding_span.hi.0
        })
        .filter(|item| match item {
            swc_ecma_ast::ModuleItem::Stmt(swc_ecma_ast::Stmt::Decl(declaration)) => {
                binding_targets::declaration_ids(declaration).contains(binding)
            }
            swc_ecma_ast::ModuleItem::ModuleDecl(swc_ecma_ast::ModuleDecl::ExportDecl(export)) => {
                binding_targets::declaration_ids(&export.decl).contains(binding)
            }
            _ => false,
        })
        .map(Spanned::span)
        .collect::<Vec<_>>();
    match matches.as_slice() {
        [span] => Some(*span),
        _ => None,
    }
}

fn raw_request_bindings(
    parsed: &ParsedSourceImports,
    request: &ParsedSourceRequest,
    target: &CandidateImportTarget,
    source_cells: Option<&BTreeMap<swc_ecma_ast::Id, source_identity::ModuleCellId>>,
) -> Vec<CandidatePreparedRequestBinding> {
    match request.kind {
        SourceStaticRequestKind::Import => parsed
            .bindings
            .iter()
            .filter(|binding| binding.statement == request.statement)
            .filter_map(|binding| {
                Some(CandidatePreparedRequestBinding {
                    source_binding: Some(binding.local.clone()),
                    candidate_local: Some(binding.local.clone()),
                    specifier_index: binding.specifier_index,
                    specifier_span: *request.specifier_spans.get(binding.specifier_index)?,
                    imported_name: Some(binding.imported_name.clone()),
                    kind: binding.kind,
                    exported_name: None,
                    source_cell: source_cells
                        .and_then(|cells| cells.get(&binding.local))
                        .copied(),
                    target: target.clone(),
                })
            })
            .collect(),
        SourceStaticRequestKind::NamedReExport => parsed
            .reexports
            .iter()
            .filter(|reexport| reexport.statement == request.statement)
            .filter_map(|reexport| {
                let specifier_index = reexport.specifier?;
                Some(CandidatePreparedRequestBinding {
                    source_binding: None,
                    candidate_local: None,
                    specifier_index,
                    specifier_span: reexport.specifier_span?,
                    imported_name: reexport.imported_name.clone(),
                    kind: match reexport.kind {
                        SourceReExportKind::Named => ImportSpecifierKind::Named,
                        SourceReExportKind::Namespace => ImportSpecifierKind::Namespace,
                        SourceReExportKind::Default => ImportSpecifierKind::Default,
                        SourceReExportKind::All => return None,
                    },
                    exported_name: reexport.exported_name.clone(),
                    source_cell: None,
                    target: target.clone(),
                })
            })
            .collect(),
        SourceStaticRequestKind::ExportAll => Vec::new(),
    }
}

fn raw_request_status(
    request: &ParsedSourceRequest,
    target: &CandidateImportTarget,
    bindings: &[CandidatePreparedRequestBinding],
) -> SourceCorrespondenceStatus {
    let binding_inventory_is_exact = match request.kind {
        SourceStaticRequestKind::Import | SourceStaticRequestKind::NamedReExport => {
            request.specifier_spans.len() == bindings.len()
                && bindings.iter().enumerate().all(|(index, binding)| {
                    binding.specifier_index == index
                        && request.specifier_spans.get(index) == Some(&binding.specifier_span)
                })
        }
        SourceStaticRequestKind::ExportAll => {
            request.specifier_spans.is_empty() && bindings.is_empty()
        }
    };
    if request.type_only || request.mode != StaticModuleRequestMode::OrdinaryEsm {
        return SourceCorrespondenceStatus::UnsupportedRewrite;
    }
    if !binding_inventory_is_exact
        || matches!(
            target,
            CandidateImportTarget::Unsupported
                | CandidateImportTarget::OriginalSourceUnknownExports { .. }
        )
        || bindings
            .iter()
            .any(|binding| binding.specifier_span == swc_common::DUMMY_SP)
    {
        return SourceCorrespondenceStatus::UnsupportedRewrite;
    }
    SourceCorrespondenceStatus::Complete
}

pub(super) fn materialized_chunk_artifact(
    target_dir: &str,
    base_analysis: ChunkAnalysisReport,
    chunk: MaterializedLogicalChunk,
) -> (ChunkArtifact, ChunkDecompositionOutput) {
    let MaterializedLogicalChunk {
        chunk_id,
        target_file,
        source_path,
        files,
        file_records,
        applied,
        directory_dependency_facts,
        validation,
        report,
        source_correspondence: _,
        // `unmatched_spec_claims`, `vendor_reference_rewrites` and
        // `gate_rejection` are rolled up by `materialize_logical_modules`
        // before this point; downstream artifact construction doesn't carry
        // them.
        unmatched_spec_claims: _,
        vendor_reference_rewrites: _,
        gate_rejection: _,
    } = chunk;
    let manifest_files = file_records
        .iter()
        .map(|(file, role)| ChunkFileRecord {
            file: file.clone(),
            role: *role,
        })
        .collect();
    let logical_modules = ChunkLogicalModulesSummary {
        module_paths: report
            .final_module_contents
            .iter()
            .map(|module| module.path.clone())
            .collect(),
        target_dir: target_dir.to_string(),
    };
    let js = JsChunk {
        entry_file: target_file.clone(),
        files,
        metadata: ChunkMetadata { source_path },
    };
    let analysis = ChunkAnalysisReport {
        entry_file: target_file,
        files: manifest_files,
        ..base_analysis
    };

    let decomposition = ChunkDecompositionOutput {
        logical_modules,
        selected_module_lowerings: applied,
        directory_dependency_facts,
        validation,
    };
    (
        ChunkArtifact {
            chunk_id,
            js,
            analysis,
        },
        decomposition,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source_chunk(chunk_id: ChunkId, name: &str) -> ChunkArtifact {
        let entry_file = "entry.js".to_string();
        let source_path = format!("{name}.js");
        ChunkArtifact {
            chunk_id,
            js: JsChunk {
                entry_file: entry_file.clone(),
                files: Vec::new(),
                metadata: ChunkMetadata {
                    source_path: source_path.clone(),
                },
            },
            analysis: ChunkAnalysisReport {
                chunk_id: name.to_string(),
                source_path,
                entry_file,
                counts: Default::default(),
                files: Vec::new(),
                imports: Vec::new(),
                export_aliases: Vec::new(),
                unresolved_exports: Vec::new(),
                kept_top_level_declarations: Vec::new(),
                runtime_facts: Default::default(),
            },
        }
    }

    fn lowered_chunk(chunk_id: ChunkId) -> MaterializedLogicalChunk {
        MaterializedLogicalChunk {
            chunk_id,
            target_file: "lowered.js".to_string(),
            source_path: "first.js".to_string(),
            files: Vec::new(),
            file_records: Vec::new(),
            applied: Vec::new(),
            directory_dependency_facts: Vec::new(),
            validation: ChunkValidationSummary {
                linker_order: Vec::new(),
            },
            report: ChunkModulesReport {
                chunk_id: "first".to_string(),
                ownership_subset: None,
                counts: ChunkModulesCounts {
                    applied: 0,
                    selected_bindings: 0,
                },
                final_module_contents: Vec::new(),
                atomic_group_owner_completions: Vec::new(),
                requested_logical_modules: Vec::new(),
                imported_callables: BTreeMap::new(),
                redundant_purity_hints: Vec::new(),
            },
            source_correspondence: CandidateSourceCorrespondence::unavailable(
                chunk_id,
                "first.js".to_string(),
                None,
            ),
            vendor_reference_rewrites: BTreeMap::new(),
            unmatched_spec_claims: Vec::new(),
            gate_rejection: None,
        }
    }

    #[test]
    fn lowered_chunk_replaces_only_its_source_at_the_original_position() {
        let mut chunk_table = ChunkTable::default();
        let first = chunk_table.intern("first".to_string());
        let second = chunk_table.intern("second".to_string());
        let source = ChunkBundle {
            chunks: vec![source_chunk(second, "second"), source_chunk(first, "first")],
            chunk_table,
        };
        let output = collect_materialized_logical_chunks(
            source,
            "modules",
            vec![lowered_chunk(first)],
            None,
        )
        .unwrap();
        assert_eq!(output.files.files().chunks[0].chunk_id, second);
        assert_eq!(
            output.files.files().chunks[0].analysis.entry_file,
            "entry.js"
        );
        assert_eq!(output.files.files().chunks[1].chunk_id, first);
        assert_eq!(
            output.files.files().chunks[1].analysis.entry_file,
            "lowered.js"
        );
        assert_eq!(
            output.files.files().chunks[1].analysis.source_path,
            "first.js"
        );
        assert_eq!(output.decomposition_by_chunk.len(), 1);
        assert!(output.decomposition_by_chunk.contains_key(&first));
    }

    #[test]
    fn unknown_lowered_chunk_is_rejected_instead_of_silently_dropped() {
        let mut chunk_table = ChunkTable::default();
        let first = chunk_table.intern("first".to_string());
        let source = ChunkBundle {
            chunks: vec![source_chunk(first, "first")],
            chunk_table,
        };
        let result =
            collect_materialized_logical_chunks(source, "", vec![lowered_chunk(ChunkId(5))], None);
        assert!(result.is_err());
    }
}
