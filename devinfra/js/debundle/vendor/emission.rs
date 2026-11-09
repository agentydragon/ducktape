//! Emission-time vendor application.
//!
//! One artifact pass with two disjoint file sets:
//!
//! * **Pass-through wave** — files emitted without lowering in
//!   non-vendor chunks get the unified directive rewriter
//!   (`passthrough::rewrite_passthrough_module`: canonicalization,
//!   boundary-rename mapping, partial-swap consumer surgery).
//!   Suppress-marked chunks are skipped (hands-off); full-swap chunks
//!   are excluded from the emission set and never rewritten;
//!   partially-swapped chunks are handled by the residual composition
//!   below.
//! * **Vendor residual emission** — each partially / bundled-partially
//!   swapped chunk's residual is computed by [`emit_vendor_residual`]:
//!   the canonicalize → self-rewrite → strip composition as one
//!   function body, so the former stage ordering is structural rather
//!   than a pipeline concern.
//!
//! Wrappers, facade bundles, and the bundle copy are emission outputs
//! written by [`write_planned_vendor_outputs`] (write-gated behind
//! `swap_vendor_chunks.write`).

use source_identity::{ChunkId, ChunkTable};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use anyhow::{Context, Result, bail};
use rayon::prelude::*;
use swc_common::{GLOBALS, Spanned};
use swc_ecma_ast::{Id, ModuleDecl, ModuleItem};
use swc_ecma_visit::VisitMutWith;

use artifact::pruning::request_identity;
use artifact::reports::{
    CandidateImportTarget, CandidateOutputRequestKind, CandidatePreparedRequestRoute,
    CandidateRewriteBindingIdentity, CandidateRewriteDisposition, CandidateRewriteIssue,
    CandidateRewriteReceipt, CandidateRewriteStage, CandidateSourceCorrespondence,
    CandidateStaticRequestIdentity, CandidateVendorBindingReplacement, CandidateVendorDestination,
    CandidateVendorEmittedRequest, CandidateVendorPlanAction, CandidateVendorReplacement,
    CandidateVendorSourceRequestIdentity, CandidateVendorSourceUse, CandidateVendorUseResult,
    CandidateVendorUseRewrite, ImportSpecifierKind, OutputFileAddress, SourceCorrespondenceStatus,
    SourceStaticRequestKind, StaticModuleRequestMode, prepared_request_identity,
};
use artifact::{
    ArtifactIndexes, ChunkBundle, FileRole, JsFile, JsFileAstParts, list_chunk_file_paths,
};
use js_ast::{GlobalUseElisionReceipt, ParsedGlobalUses, ParsedJsModule};
use spec::PartialSwapSymbol;

use crate::passthrough::{
    PassthroughContext, PassthroughRewriteResult, PassthroughVendorBindingRewrite,
    rewrite_passthrough_module,
};
use crate::plan::{ChunkBundledPartialSwapPlan, VendorResolutionPlan};
use crate::strip::strip_one_chunk_with_replacement_imports;
use crate::wrappers::{write_planned_bundled_assets, write_planned_wrapper};
use crate::{
    DeferredImport, IdentRewriteTarget, MaterializedOutputChunkIndex, PartialSwapIdentRewriter,
    SelfRewriteOutputs, seed_bundled_partial_swap_self_rewrites,
};

/// Counts and exact source-use receipts produced by the checked emission
/// rewrites. The ChunkId key identifies the source chunk; each receipt's
/// source file owns its spans.
#[derive(Debug, Default)]
pub struct EmissionRewriteResult {
    pub references_by_symbol: BTreeMap<(ChunkId, String), usize>,
    pub global_use_elisions: BTreeMap<ChunkId, GlobalUseElisionReceipt>,
    /// Exact ordered transitions for prepared static requests. The caller
    /// applies these to the source-correspondence rows before pruning.
    pub correspondence_receipts: BTreeMap<ChunkId, Vec<CandidateRewriteReceipt>>,
}

/// Body-only emit pass: ASTs are taken out for parallel rewrites and put back
/// at the same file paths. The caller may retain its output-path indexes.
pub fn apply_emission_rewrites_in_place(
    artifact: &mut ChunkBundle,
    plan: &VendorResolutionPlan,
    references: &ArtifactIndexes,
    source_correspondence_by_chunk: &BTreeMap<ChunkId, CandidateSourceCorrespondence>,
) -> Result<EmissionRewriteResult> {
    let chunk_table = artifact.chunk_table.clone();
    let materialized_index = MaterializedOutputChunkIndex::build(&chunk_table);
    let context = PassthroughContext {
        plan,
        references,
        chunk_table: &chunk_table,
        materialized_index: &materialized_index,
    };
    let full_swap_chunks = plan.full_swap_chunk_ids();

    // Pass-through wave over non-vendor chunks.
    let mut jobs = Vec::new();
    for (chunk_index, chunk_artifact) in artifact.chunks.iter_mut().enumerate() {
        let chunk_id = chunk_artifact.chunk_id;
        if plan.is_suppressed(chunk_id)
            || full_swap_chunks.contains(&chunk_id)
            || plan.partial_swaps.contains_key(&chunk_id)
            || plan.bundled_partial_swaps.contains_key(&chunk_id)
        {
            continue;
        }
        let chunk_name = chunk_table.name(chunk_id).to_string();
        for file_path in list_chunk_file_paths(&chunk_artifact.js) {
            let Some(file) = chunk_artifact.js.get_file(&file_path) else {
                continue;
            };
            // Materialized module files' specifiers are constructed by
            // lowering from the same plan; only pass-through files
            // (entries, runtime files) are rewritten here.
            if file.metadata.role == FileRole::Module || file.ast().is_none() {
                continue;
            }
            let (parts, ast) = chunk_artifact
                .js
                .remove_file(&file_path)
                .and_then(|file| file.into_ast_parts())
                .with_context(|| format!("missing AST for {chunk_name}/{file_path}"))?;
            jobs.push(PassthroughFileJob {
                chunk_index,
                chunk_id,
                file_path,
                parts,
                ast,
            });
        }
    }

    // Rayon workers don't inherit `GLOBALS`; re-set per worker so any
    // `Mark::new()` / `Id` use stays in the caller's arena.
    let context_ref = &context;
    let results: Vec<PassthroughFileResult> = GLOBALS.with(|globals| {
        jobs.into_par_iter()
            .map(|mut job| {
                GLOBALS.set(globals, || {
                    let mut references_by_symbol = BTreeMap::new();
                    let vendor_rewrites = rewrite_passthrough_module(
                        &mut job.ast.module,
                        job.chunk_id,
                        &job.file_path,
                        context_ref,
                        &mut references_by_symbol,
                    );
                    PassthroughFileResult {
                        chunk_index: job.chunk_index,
                        chunk_id: job.chunk_id,
                        file_path: job.file_path,
                        parts: job.parts,
                        ast: job.ast,
                        references_by_symbol,
                        vendor_rewrites,
                    }
                })
            })
            .collect()
    });

    let mut references_by_symbol = BTreeMap::new();
    let mut correspondence_receipts = BTreeMap::<ChunkId, Vec<CandidateRewriteReceipt>>::new();
    for result in results {
        let output = OutputFileAddress {
            chunk: result.chunk_id,
            file: result.file_path.clone(),
        };
        let vendor_receipts = passthrough_vendor_rewrite_receipts(
            result.chunk_id,
            &result.file_path,
            source_correspondence_by_chunk.get(&result.chunk_id),
            &result.ast.module,
            references,
            &chunk_table,
            result.vendor_rewrites,
        );
        correspondence_receipts
            .entry(result.chunk_id)
            .or_default()
            .extend(vendor_receipts.iter().cloned());
        correspondence_receipts
            .entry(result.chunk_id)
            .or_default()
            .extend(emission_request_receipts(
                result.chunk_id,
                &result.file_path,
                source_correspondence_by_chunk.get(&result.chunk_id),
                &result.ast.module,
                references,
                &chunk_table,
                &vendor_receipts,
            ));
        for ((chunk_id, chunk_export), count) in result.references_by_symbol {
            if !plan.partial_swaps.contains_key(&chunk_id)
                && !plan.bundled_partial_swaps.contains_key(&chunk_id)
            {
                bail!(
                    "pass-through rewrite reported references to `{chunk_export}` on chunk {}, which has no partial-swap plan",
                    chunk_table.name(chunk_id)
                );
            }
            *references_by_symbol
                .entry((chunk_id, chunk_export))
                .or_insert(0) += count;
        }
        artifact
            .chunks
            .get_mut(result.chunk_index)
            .with_context(|| format!("missing chunk index {}", result.chunk_index))?
            .js
            .insert_file(JsFile::from_ast_parts(result.parts, result.ast));
    }

    // Vendor residual emission: one composition per swapped chunk.
    let mut residual_jobs = Vec::new();
    for (&chunk_id, partial) in &plan.partial_swaps {
        residual_jobs.push(extract_residual_job(
            artifact,
            &chunk_table,
            chunk_id,
            &partial.chunk_path,
            &partial.entry_file,
            &partial.symbols,
            None,
        )?);
    }
    for (&chunk_id, bundled) in &plan.bundled_partial_swaps {
        residual_jobs.push(extract_residual_job(
            artifact,
            &chunk_table,
            chunk_id,
            &bundled.chunk_path,
            &bundled.entry_file,
            &bundled.symbols,
            Some(bundled),
        )?);
    }

    let outputs: Vec<ResidualOutput> = GLOBALS.with(|globals| -> Result<Vec<ResidualOutput>> {
        residual_jobs
            .into_par_iter()
            .map(|job| GLOBALS.set(globals, || emit_vendor_residual(job, context_ref)))
            .collect()
    })?;

    let mut global_use_elisions = BTreeMap::new();
    for output in outputs {
        let js_chunk = artifact.js_chunk_mut(output.chunk_id)?;
        let mut vendor_rewrites = output.vendor_rewrites;
        for (file_path, parts, ast) in output.files {
            let vendor_receipts = passthrough_vendor_rewrite_receipts(
                output.chunk_id,
                &file_path,
                source_correspondence_by_chunk.get(&output.chunk_id),
                &ast.module,
                references,
                &chunk_table,
                vendor_rewrites.remove(&file_path).unwrap_or_default(),
            );
            correspondence_receipts
                .entry(output.chunk_id)
                .or_default()
                .extend(vendor_receipts.iter().cloned());
            correspondence_receipts
                .entry(output.chunk_id)
                .or_default()
                .extend(emission_request_receipts(
                    output.chunk_id,
                    &file_path,
                    source_correspondence_by_chunk.get(&output.chunk_id),
                    &ast.module,
                    references,
                    &chunk_table,
                    &vendor_receipts,
                ));
            js_chunk.insert_file(JsFile::from_ast_parts(parts, ast));
        }
        for (key, count) in output.references_by_symbol {
            *references_by_symbol.entry(key).or_insert(0) += count;
        }
        for (chunk_id, receipt) in output.global_use_elisions {
            if global_use_elisions.insert(chunk_id, receipt).is_some() {
                bail!(
                    "vendor emission produced more than one global-use elision receipt for source chunk {}",
                    chunk_table.name(chunk_id),
                );
            }
        }
    }

    for (&chunk_id, correspondence) in source_correspondence_by_chunk {
        let receipts = correspondence_receipts.entry(chunk_id).or_default();
        for route in &correspondence.prepared_requests {
            if receipts.iter().any(|receipt| {
                receipt.output == route.output
                    && receipt.before.as_ref().is_some_and(|before| {
                        before.item_span == route.item_span && before.kind == route.request_kind
                    })
            }) {
                continue;
            }
            let before = prepared_request_identity(route);
            let full_swap = full_swap_chunks.contains(&chunk_id);
            let supported = !full_swap
                && route.status == artifact::reports::SourceCorrespondenceStatus::Complete;
            receipts.push(CandidateRewriteReceipt {
                source_chunk: chunk_id,
                output: route.output.clone(),
                stage: CandidateRewriteStage::EmissionVendorRewrite,
                before: Some(before.clone()),
                after: Some(before),
                disposition: if supported {
                    CandidateRewriteDisposition::Preserved
                } else {
                    CandidateRewriteDisposition::Unsupported
                },
                elisions: Vec::new(),
                issue: (!supported).then_some(if full_swap {
                    CandidateRewriteIssue::MissingExactOutputIdentity
                } else {
                    CandidateRewriteIssue::UnprovedTarget
                }),
            });
        }
    }

    Ok(EmissionRewriteResult {
        references_by_symbol,
        global_use_elisions,
        correspondence_receipts,
    })
}

/// Record the static requests that this emission pass actually left in one
/// output file. Source spans identify preserved requests; generated vendor
/// imports use synthetic spans and remain unsupported unless the prepared
/// inventory can match their exact position and request.
fn emission_request_receipts(
    chunk: ChunkId,
    file: &str,
    correspondence: Option<&CandidateSourceCorrespondence>,
    module: &Module,
    references: &ArtifactIndexes,
    chunk_table: &ChunkTable,
    vendor_receipts: &[CandidateRewriteReceipt],
) -> Vec<CandidateRewriteReceipt> {
    let output = OutputFileAddress {
        chunk,
        file: file.to_owned(),
    };
    let prepared = correspondence
        .map(|correspondence| {
            correspondence
                .prepared_requests
                .iter()
                .enumerate()
                .filter(|(_, route)| route.output == output)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let mut matched = BTreeSet::new();
    let mut receipts = Vec::new();

    for (item_index, item) in module.body.iter().enumerate() {
        let Some(kind) = emitted_request_kind(item) else {
            continue;
        };
        let Some((mut after, observation_issue)) =
            request_identity(None, &output, item_index, item)
        else {
            continue;
        };
        if vendor_receipts.iter().any(|receipt| {
            receipt.output == output
                && receipt.after.as_ref().is_some_and(|vendor_after| {
                    vendor_after.item_span == after.item_span
                        && vendor_after.request == after.request
                        && vendor_after.bindings.iter().any(|vendor_binding| {
                            after.bindings.iter().any(|binding| {
                                binding.local == vendor_binding.local
                                    && binding.specifier_span == vendor_binding.specifier_span
                            })
                        })
                })
                || replacement_emits_observed_request(receipt, &output, &after)
        }) {
            continue;
        }
        let candidates = prepared
            .iter()
            .filter(|(_, route)| {
                route.request_kind == kind
                    && route.item_span == after.item_span
                    && (route.item_span != swc_common::DUMMY_SP
                        || (route.item_index == item_index
                            && route.emitted_request == after.request.as_deref().unwrap_or("")))
            })
            .collect::<Vec<_>>();
        if candidates.len() != 1 {
            receipts.push(CandidateRewriteReceipt {
                source_chunk: chunk,
                output: output.clone(),
                stage: CandidateRewriteStage::EmissionVendorRewrite,
                before: None,
                after: Some(after),
                disposition: CandidateRewriteDisposition::Unsupported,
                elisions: Vec::new(),
                issue: Some(match candidates.as_slice() {
                    [] if after.item_span == swc_common::DUMMY_SP => {
                        CandidateRewriteIssue::UnmappedGeneratedRequest
                    }
                    [] => CandidateRewriteIssue::MissingExactInputIdentity,
                    _ => CandidateRewriteIssue::AmbiguousTransition,
                }),
            });
            continue;
        }
        let (prepared_index, route) = *candidates[0];
        matched.insert(prepared_index);
        let before = prepared_request_identity(route);
        if vendor_receipts
            .iter()
            .any(|receipt| receipt.output == output && receipt.before.as_ref() == Some(&before))
        {
            matched.insert(prepared_index);
            continue;
        }
        after.source_statement = route.source_statement;
        after.targets = route.targets.clone();
        let mut issue = observation_issue
            .filter(|issue| *issue != CandidateRewriteIssue::MissingExactInputIdentity);

        if route.status != SourceCorrespondenceStatus::Complete
            || after.kind != route.request_kind
            || after.mode != Some(route.mode)
        {
            issue.get_or_insert(CandidateRewriteIssue::UnprovedTarget);
        }
        if let Some(request) = after.request.as_deref() {
            if request != route.emitted_request
                && !rewritten_request_keeps_target(
                    &route.targets,
                    request,
                    &output,
                    references,
                    chunk_table,
                )
            {
                issue.get_or_insert(CandidateRewriteIssue::UnprovedTarget);
            }
        } else {
            issue.get_or_insert(CandidateRewriteIssue::MissingExactOutputIdentity);
        }

        let mut bindings = Vec::with_capacity(after.bindings.len());
        for observed in &after.bindings {
            let matches = route
                .bindings
                .iter()
                .filter(|binding| {
                    binding.specifier_span == observed.specifier_span
                        && (route.item_span != swc_common::DUMMY_SP
                            || binding.specifier_index == observed.specifier_index)
                })
                .collect::<Vec<_>>();
            let [binding] = matches.as_slice() else {
                issue.get_or_insert(if matches.is_empty() {
                    CandidateRewriteIssue::MissingExactInputIdentity
                } else {
                    CandidateRewriteIssue::AmbiguousTransition
                });
                continue;
            };
            if binding.candidate_local != observed.local
                || binding.imported_name != observed.imported_name
                || binding.exported_name != observed.exported_name
                || observed.kind != Some(binding.kind)
            {
                issue.get_or_insert(CandidateRewriteIssue::UnprovedUseRewrite);
            }
            bindings.push(CandidateRewriteBindingIdentity {
                specifier_index: observed.specifier_index,
                specifier_span: observed.specifier_span,
                local: observed.local.clone(),
                imported_name: observed.imported_name.clone(),
                exported_name: observed.exported_name.clone(),
                kind: observed.kind,
                source_binding: binding.source_binding.clone(),
                source_cell: binding.source_cell,
                target: Some(binding.target.clone()),
            });
        }
        if bindings.len() != route.bindings.len() {
            issue.get_or_insert(CandidateRewriteIssue::UnprovedUseRewrite);
        }
        after.bindings = bindings;

        let disposition = if issue.is_some() {
            CandidateRewriteDisposition::Unsupported
        } else if after == before {
            CandidateRewriteDisposition::Preserved
        } else {
            CandidateRewriteDisposition::Rewritten
        };
        receipts.push(CandidateRewriteReceipt {
            source_chunk: chunk,
            output: output.clone(),
            stage: CandidateRewriteStage::EmissionVendorRewrite,
            before: Some(before),
            after: Some(after),
            disposition,
            elisions: Vec::new(),
            issue,
        });
    }

    for (prepared_index, route) in prepared {
        if matched.contains(&prepared_index) {
            continue;
        }
        let before = prepared_request_identity(route);
        if vendor_receipts
            .iter()
            .any(|receipt| receipt.output == output && receipt.before.as_ref() == Some(&before))
        {
            continue;
        }
        receipts.push(CandidateRewriteReceipt {
            source_chunk: chunk,
            output: output.clone(),
            stage: CandidateRewriteStage::EmissionVendorRewrite,
            before: Some(before),
            after: None,
            disposition: CandidateRewriteDisposition::Unsupported,
            elisions: Vec::new(),
            issue: Some(CandidateRewriteIssue::MissingExactOutputIdentity),
        });
    }
    receipts
}

fn emitted_request_kind(item: &ModuleItem) -> Option<CandidateOutputRequestKind> {
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::Import(_)) => Some(CandidateOutputRequestKind::Import),
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) if export.src.is_some() => {
            Some(CandidateOutputRequestKind::NamedReExport)
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_)) => {
            Some(CandidateOutputRequestKind::ExportAll)
        }
        _ => None,
    }
}

fn rewritten_request_keeps_target(
    targets: &[CandidateImportTarget],
    request: &str,
    output: &OutputFileAddress,
    references: &ArtifactIndexes,
    chunk_table: &ChunkTable,
) -> bool {
    if targets.len() != 1 {
        return false;
    }
    if let Some(resolved) = references.resolve_runtime_import_reference(
        request,
        output.chunk,
        &output.file,
        chunk_table,
    ) {
        return targets.iter().any(|target| {
            matches!(target, CandidateImportTarget::CandidateOutput { chunk, file }
                if *chunk == resolved.target_chunk_id && *file == resolved.target_file)
        });
    }
    targets.iter().any(|target| {
        matches!(target, CandidateImportTarget::OriginalExternal { request: original }
            if original == request)
    })
}

struct PassthroughFileJob {
    chunk_index: usize,
    chunk_id: ChunkId,
    file_path: String,
    parts: JsFileAstParts,
    ast: ParsedJsModule,
}

struct PassthroughFileResult {
    chunk_index: usize,
    chunk_id: ChunkId,
    file_path: String,
    parts: JsFileAstParts,
    ast: ParsedJsModule,
    references_by_symbol: BTreeMap<(ChunkId, String), usize>,
    vendor_rewrites: PassthroughRewriteResult,
}

/// One partially-swapped chunk's residual inputs: every pass-through
/// AST file of the chunk (owned), pulled out of the artifact so the
/// residual computation can run as a parallel job over owned data.
struct ResidualJob<'a> {
    chunk_id: ChunkId,
    chunk_path: &'a str,
    entry_file: &'a str,
    symbols: &'a BTreeMap<String, PartialSwapSymbol>,
    bundled: Option<&'a ChunkBundledPartialSwapPlan>,
    global_uses: Option<Arc<ParsedGlobalUses>>,
    files: Vec<(String, JsFileAstParts, ParsedJsModule)>,
}

struct ResidualOutput {
    chunk_id: ChunkId,
    files: Vec<(String, JsFileAstParts, ParsedJsModule)>,
    vendor_rewrites: BTreeMap<String, PassthroughRewriteResult>,
    references_by_symbol: BTreeMap<(ChunkId, String), usize>,
    global_use_elisions: BTreeMap<ChunkId, GlobalUseElisionReceipt>,
}

fn passthrough_vendor_rewrite_receipts(
    chunk: ChunkId,
    file: &str,
    correspondence: Option<&CandidateSourceCorrespondence>,
    module: &swc_ecma_ast::Module,
    references: &ArtifactIndexes,
    chunk_table: &ChunkTable,
    rewrites: PassthroughRewriteResult,
) -> Vec<CandidateRewriteReceipt> {
    let output = OutputFileAddress {
        chunk,
        file: file.to_owned(),
    };
    let mut groups = Vec::<(
        swc_common::Span,
        String,
        SourceStaticRequestKind,
        Vec<crate::passthrough::PassthroughVendorBindingRewrite>,
    )>::new();
    for rewrite in rewrites.vendor_bindings {
        if let Some((_, _, _, bindings)) = groups.iter_mut().find(|(span, request, kind, _)| {
            *span == rewrite.declaration_span
                && *request == rewrite.source_request
                && *kind == rewrite.source_request_kind
        }) {
            bindings.push(rewrite);
        } else {
            groups.push((
                rewrite.declaration_span,
                rewrite.source_request.clone(),
                rewrite.source_request_kind,
                vec![rewrite],
            ));
        }
    }
    groups
        .into_iter()
        .map(|(declaration_span, request, source_kind, bindings)| {
            let Some(correspondence) = correspondence else {
                return unsupported_vendor_receipt(
                    chunk,
                    output.clone(),
                    None,
                    None,
                    CandidateRewriteIssue::MissingExactInputIdentity,
                );
            };
            let candidate_kind = match source_kind {
                SourceStaticRequestKind::Import => CandidateOutputRequestKind::Import,
                SourceStaticRequestKind::NamedReExport => {
                    CandidateOutputRequestKind::NamedReExport
                }
                SourceStaticRequestKind::ExportAll => CandidateOutputRequestKind::ExportAll,
            };
            let prepared_matches = correspondence
                .prepared_requests
                .iter()
                .filter(|route| {
                    route.output == output
                        && route.item_span == declaration_span
                        && route.request_kind == candidate_kind
                        && route.emitted_request == request
                        && route.source_statement.is_some()
                })
                .collect::<Vec<_>>();
            let [prepared] = prepared_matches.as_slice() else {
                return unsupported_vendor_receipt(
                    chunk,
                    output.clone(),
                    None,
                    None,
                    if prepared_matches.is_empty() {
                        CandidateRewriteIssue::MissingExactInputIdentity
                    } else {
                        CandidateRewriteIssue::AmbiguousTransition
                    },
                );
            };
            let before = prepared_request_identity(prepared);
            let statement = prepared.source_statement.expect("filtered above");
            let source_requests = correspondence
                .source_requests
                .iter()
                .filter(|route| {
                    route.source.statement == statement
                        && route.source.declaration_span == declaration_span
                        && route.source.request == request
                        && route.source.kind == source_kind
                })
                .collect::<Vec<_>>();
            let [source_request] = source_requests.as_slice() else {
                return unsupported_vendor_receipt(
                    chunk,
                    output.clone(),
                    Some(before),
                    None,
                    if source_requests.is_empty() {
                        CandidateRewriteIssue::MissingExactInputIdentity
                    } else {
                        CandidateRewriteIssue::AmbiguousTransition
                    },
                );
            };
            let source_identity = CandidateVendorSourceRequestIdentity {
                source_chunk: chunk,
                source_file: correspondence.source_file.clone(),
                statement,
                declaration_span,
                request: request.clone(),
                mode: source_request.source.mode,
                kind: source_request.source.kind,
            };
            let mut issue = None;
            if prepared.status != SourceCorrespondenceStatus::Complete
                || source_request.status != SourceCorrespondenceStatus::Complete
                || source_request.source.type_only
                || source_request.source.mode != StaticModuleRequestMode::OrdinaryEsm
            {
                issue = Some(CandidateRewriteIssue::UnprovedTarget);
            }
            let after = retained_request_identity(
                &output,
                module,
                declaration_span,
                prepared,
                &bindings,
                references,
                chunk_table,
            );
            if after.is_none()
                && bindings.iter().any(|binding| {
                    binding.checked_action.as_ref().is_some_and(|(_, destination)| {
                        matches!(destination, CandidateVendorDestination::BoundaryOutput { .. })
                    })
                })
            {
                issue.get_or_insert(CandidateRewriteIssue::MissingExactOutputIdentity);
            }
            let mut replacement_bindings = Vec::new();
            for rewrite in &bindings {
                let mut source_uses = Vec::new();
                let mut source_cell = None;
                let mut uses = Vec::new();
                match source_kind {
                    SourceStaticRequestKind::Import => {
                        let Some(source_id) = rewrite.source_binding.as_ref() else {
                            issue.get_or_insert(CandidateRewriteIssue::MissingExactInputIdentity);
                            continue;
                        };
                        let matching_sources = correspondence
                            .imports
                            .iter()
                            .filter(|source| {
                                source.source.statement == statement
                                    && source.source.specifier_index
                                        == rewrite.source_specifier_index
                                    && source.source.local == *source_id
                                    && source.source.declaration_span == declaration_span
                                    && source.source.request == request
                                    && source.source.imported_name == rewrite.source_export
                                    && source.source.kind == rewrite.source_kind
                            })
                            .collect::<Vec<_>>();
                        let [source_binding] = matching_sources.as_slice() else {
                            issue.get_or_insert(if matching_sources.is_empty() {
                                CandidateRewriteIssue::MissingExactInputIdentity
                            } else {
                                CandidateRewriteIssue::AmbiguousTransition
                            });
                            continue;
                        };
                        let candidate_routes = source_binding
                            .routes
                            .iter()
                            .filter(|route| {
                                route.output == output
                                    && route.item_span == declaration_span
                                    && route.specifier_index == rewrite.source_specifier_index
                                    && route.specifier_span == rewrite.source_specifier_span
                                    && route.candidate_local == *source_id
                            })
                            .collect::<Vec<_>>();
                        let target_is_exact = matches!(
                            candidate_routes.as_slice(),
                            [route]
                                if matches!(
                                    &route.target,
                                    CandidateImportTarget::CandidateOutput { chunk, file }
                                        if *chunk == rewrite.target_chunk
                                            && rewrite.source_target_file.as_deref() == Some(file.as_str())
                                )
                        );
                        if source_binding.status != SourceCorrespondenceStatus::Complete
                            || !target_is_exact
                        {
                            issue.get_or_insert(CandidateRewriteIssue::UnprovedTarget);
                        }
                        source_cell = correspondence
                            .source_module_cells
                            .as_ref()
                            .and_then(|cells| cells.get(source_id).copied());
                        if correspondence.source_module_cells.is_none() {
                            issue.get_or_insert(CandidateRewriteIssue::UnprovedTarget);
                        }
                        source_uses = source_binding
                            .source
                            .use_sites
                            .iter()
                            .map(|site| CandidateVendorSourceUse {
                                statement: site.statement,
                                span: site.span,
                            })
                            .collect();
                        uses = passthrough_vendor_use_rewrites(
                            &output,
                            source_binding,
                            rewrite,
                            &rewrites.use_rewrites,
                            &mut issue,
                        );
                    }
                    SourceStaticRequestKind::NamedReExport => {
                        let expected_export = rewrite.source_exported_name.as_deref();
                        let matches = correspondence
                            .reexports
                            .iter()
                            .filter(|source| {
                                source.source.statement == statement
                                    && source.source.specifier == Some(rewrite.source_specifier_index)
                                    && source.source.specifier_span
                                        == Some(rewrite.source_specifier_span)
                                    && source.source.declaration_span == declaration_span
                                    && source.source.request == request
                                    && source.source.imported_name.as_deref()
                                        == Some(rewrite.source_export.as_str())
                                    && source.source.exported_name.as_deref() == expected_export
                            })
                            .collect::<Vec<_>>();
                        let [source_binding] = matches.as_slice() else {
                            issue.get_or_insert(if matches.is_empty() {
                                CandidateRewriteIssue::MissingExactInputIdentity
                            } else {
                                CandidateRewriteIssue::AmbiguousTransition
                            });
                            continue;
                        };
                        let candidate_routes = source_binding
                            .routes
                            .iter()
                            .filter(|route| {
                                route.output == output
                                    && route.item_span == declaration_span
                                    && route.source_statement == Some(statement)
                            })
                            .collect::<Vec<_>>();
                        let route_matches = matches!(candidate_routes.as_slice(), [route] if {
                            let matching_bindings = route
                                .bindings
                                .iter()
                                .filter(|binding| {
                                    binding.specifier_index == rewrite.source_specifier_index
                                        && binding.specifier_span == rewrite.source_specifier_span
                                        && binding.candidate_local.is_none()
                                        && binding.imported_name.as_deref()
                                            == Some(rewrite.source_export.as_str())
                                        && binding.exported_name.as_deref() == expected_export
                                        && matches!(
                                            &binding.target,
                                            CandidateImportTarget::CandidateOutput { chunk, file }
                                                if *chunk == rewrite.target_chunk
                                                    && rewrite.source_target_file.as_deref() == Some(file.as_str())
                                        )
                                })
                                .count();
                            matching_bindings == 1
                        });
                        if source_binding.status != SourceCorrespondenceStatus::Complete
                            || source_binding.source.type_only
                            || !route_matches
                        {
                            issue.get_or_insert(CandidateRewriteIssue::UnprovedTarget);
                        }
                    }
                    SourceStaticRequestKind::ExportAll => {
                        issue.get_or_insert(CandidateRewriteIssue::UnprovedTarget);
                        continue;
                    }
                }
                if source_request
                    .source
                    .specifier_spans
                    .get(rewrite.source_specifier_index)
                    .copied()
                    != Some(rewrite.source_specifier_span)
                {
                    issue.get_or_insert(CandidateRewriteIssue::MissingExactInputIdentity);
                }
                let Some((action, destination)) = rewrite.checked_action.clone() else {
                    issue.get_or_insert(CandidateRewriteIssue::UnprovedTarget);
                    continue;
                };
                let emitted_requests = if matches!(
                    &destination,
                    CandidateVendorDestination::BoundaryOutput { .. }
                ) {
                    after
                        .as_ref()
                        .map(|identity| {
                            vec![CandidateVendorEmittedRequest {
                                output: output.clone(),
                                identity: identity.clone(),
                            }]
                        })
                        .unwrap_or_default()
                } else {
                    let target = destination.import_target();
                    let mut emitted = rewrite
                        .emitted_requests
                        .iter()
                        .filter_map(|marker| {
                            let identities = emitted_import_identities(
                                module,
                                marker,
                                target.as_ref(),
                            );
                            match identities.as_slice() {
                                [identity] => Some(CandidateVendorEmittedRequest {
                                    output: output.clone(),
                                    identity: identity.clone(),
                                }),
                                _ => {
                                    issue.get_or_insert(if identities.is_empty() {
                                        CandidateRewriteIssue::MissingExactOutputIdentity
                                    } else {
                                        CandidateRewriteIssue::AmbiguousTransition
                                    });
                                    None
                                }
                            }
                        })
                        .collect::<Vec<_>>();
                    emitted.extend(rewrite.emitted_exports.iter().filter_map(|marker| {
                        let identities = emitted_export_identities(
                            module,
                            &output,
                            marker,
                            target.as_ref(),
                        );
                        match identities.as_slice() {
                            [identity] => Some(CandidateVendorEmittedRequest {
                                output: output.clone(),
                                identity: identity.clone(),
                            }),
                            _ => {
                                issue.get_or_insert(if identities.is_empty() {
                                    CandidateRewriteIssue::MissingExactOutputIdentity
                                } else {
                                    CandidateRewriteIssue::AmbiguousTransition
                                });
                                None
                            }
                        }
                    }));
                    emitted
                };
                if emitted_requests.is_empty() {
                    issue.get_or_insert(CandidateRewriteIssue::MissingExactOutputIdentity);
                }
                replacement_bindings.push(CandidateVendorBindingReplacement {
                    source_specifier_index: rewrite.source_specifier_index,
                    source_specifier_span: rewrite.source_specifier_span,
                    source_binding: rewrite.source_binding.clone(),
                    source_cell,
                    source_export: rewrite.source_export.clone(),
                    source_exported_name: rewrite.source_exported_name.clone(),
                    source_kind: rewrite.source_kind,
                    source_uses,
                    action,
                    destination,
                    emitted_requests,
                    uses,
                });
            }
            if replacement_bindings.len() != bindings.len() {
                issue.get_or_insert(CandidateRewriteIssue::UnprovedUseRewrite);
            }
            let disposition = if issue.is_some() {
                CandidateRewriteDisposition::Unsupported
            } else {
                CandidateRewriteDisposition::Replaced(CandidateVendorReplacement {
                    source_request: source_identity,
                    bindings: replacement_bindings,
                })
            };
            CandidateRewriteReceipt {
                source_chunk: chunk,
                output: output.clone(),
                stage: CandidateRewriteStage::EmissionVendorRewrite,
                before: Some(before),
                after,
                disposition,
                elisions: Vec::new(),
                issue,
            }
        })
        .collect()
}

fn unsupported_vendor_receipt(
    chunk: ChunkId,
    output: OutputFileAddress,
    before: Option<CandidateStaticRequestIdentity>,
    after: Option<CandidateStaticRequestIdentity>,
    issue: CandidateRewriteIssue,
) -> CandidateRewriteReceipt {
    CandidateRewriteReceipt {
        source_chunk: chunk,
        output,
        stage: CandidateRewriteStage::EmissionVendorRewrite,
        before,
        after,
        disposition: CandidateRewriteDisposition::Unsupported,
        elisions: Vec::new(),
        issue: Some(issue),
    }
}

fn retained_request_identity(
    output: &OutputFileAddress,
    module: &swc_ecma_ast::Module,
    declaration_span: swc_common::Span,
    prepared: &CandidatePreparedRequestRoute,
    vendor_bindings: &[PassthroughVendorBindingRewrite],
    references: &ArtifactIndexes,
    chunk_table: &ChunkTable,
) -> Option<CandidateStaticRequestIdentity> {
    for (item_index, item) in module.body.iter().enumerate() {
        if item.span() != declaration_span
            || emitted_request_kind(item) != Some(prepared.request_kind)
        {
            continue;
        }
        let Some((observed, _)) = request_identity(None, output, item_index, item) else {
            continue;
        };
        let Some(request) = observed.request.as_deref() else {
            continue;
        };
        if observed.mode != Some(StaticModuleRequestMode::OrdinaryEsm)
            || (request != prepared.emitted_request
                && !rewritten_request_keeps_target(
                    &prepared.targets,
                    request,
                    output,
                    references,
                    chunk_table,
                ))
        {
            continue;
        }
        let mut identity = prepared_request_identity(prepared);
        identity.item_index = item_index;
        identity.request = observed.request;
        let mut targets = Vec::new();
        identity.bindings = observed
            .bindings
            .into_iter()
            .map(|binding| {
                // A sibling specifier may have been replaced, shifting this
                // retained specifier's index. Its exact span and hygienic
                // binding identity are the stable correspondence keys.
                let matches = prepared
                    .bindings
                    .iter()
                    .filter(|planned| {
                        planned.specifier_span == binding.specifier_span
                            && planned.candidate_local == binding.local
                    })
                    .collect::<Vec<_>>();
                let [planned] = matches.as_slice() else {
                    return None;
                };
                let boundary_rewrites = vendor_bindings
                    .iter()
                    .filter(|rewrite| {
                        rewrite.source_specifier_span == binding.specifier_span
                            && rewrite.source_binding == planned.source_binding
                            && rewrite.checked_action.as_ref().is_some_and(|(action, _)| {
                                matches!(action, CandidateVendorPlanAction::BoundaryRename { .. })
                            })
                    })
                    .collect::<Vec<_>>();
                let (source_cell, target) = match boundary_rewrites.as_slice() {
                    [] => (planned.source_cell, planned.target.clone()),
                    [rewrite] => {
                        let Some((
                            CandidateVendorPlanAction::BoundaryRename {
                                source_export,
                                public_export,
                                ..
                            },
                            CandidateVendorDestination::BoundaryOutput { output, export },
                        )) = rewrite.checked_action.as_ref()
                        else {
                            return None;
                        };
                        if source_export != &rewrite.source_export
                            || public_export != export
                            || binding.imported_name.as_deref() != Some(public_export.as_str())
                            || rewrite.target_chunk != output.chunk
                            || rewrite.source_target_file.as_deref() != Some(output.file.as_str())
                        {
                            return None;
                        }
                        (
                            None,
                            CandidateImportTarget::CandidateOutput {
                                chunk: output.chunk,
                                file: output.file.clone(),
                            },
                        )
                    }
                    _ => return None,
                };
                if !targets.contains(&target) {
                    targets.push(target.clone());
                }
                Some(CandidateRewriteBindingIdentity {
                    specifier_index: binding.specifier_index,
                    specifier_span: binding.specifier_span,
                    local: binding.local,
                    imported_name: binding.imported_name,
                    exported_name: binding.exported_name,
                    kind: binding.kind,
                    source_binding: planned.source_binding.clone(),
                    source_cell,
                    target: Some(target),
                })
            })
            .collect::<Option<Vec<_>>>()?;
        if identity.bindings.is_empty() {
            targets = prepared.targets.clone();
        }
        identity.targets = targets;
        return Some(identity);
    }
    None
}

fn emitted_import_identities(
    module: &swc_ecma_ast::Module,
    marker: &crate::DeferredImportMarker,
    target: Option<&CandidateImportTarget>,
) -> Vec<CandidateStaticRequestIdentity> {
    let Some(target) = target else {
        return Vec::new();
    };
    module
        .body
        .iter()
        .enumerate()
        .filter_map(|(item_index, item)| {
            let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
                return None;
            };
            if import.span != marker.source_span || js_ast::str_value(&import.src) != marker.request
            {
                return None;
            }
            let bindings = import
                .specifiers
                .iter()
                .enumerate()
                .filter_map(|(specifier_index, specifier)| {
                    let local = import_specifier_local(specifier);
                    if local != marker.local || specifier.span() != marker.source_span {
                        return None;
                    }
                    let (imported_name, kind) = match specifier {
                        swc_ecma_ast::ImportSpecifier::Named(named) => (
                            Some(
                                named
                                    .imported
                                    .as_ref()
                                    .map(binding_targets::module_export_name)
                                    .unwrap_or_else(|| named.local.sym.to_string()),
                            ),
                            ImportSpecifierKind::Named,
                        ),
                        swc_ecma_ast::ImportSpecifier::Default(_) => {
                            (Some("default".to_owned()), ImportSpecifierKind::Default)
                        }
                        swc_ecma_ast::ImportSpecifier::Namespace(_) => {
                            (Some("*".to_owned()), ImportSpecifierKind::Namespace)
                        }
                    };
                    Some(CandidateRewriteBindingIdentity {
                        specifier_index,
                        specifier_span: specifier.span(),
                        local: Some(local),
                        imported_name,
                        exported_name: None,
                        kind: Some(kind),
                        source_binding: None,
                        source_cell: None,
                        target: Some(target.clone()),
                    })
                })
                .collect::<Vec<_>>();
            (!bindings.is_empty()).then_some(CandidateStaticRequestIdentity {
                item_index,
                item_span: import.span,
                kind: CandidateOutputRequestKind::Import,
                source_statement: None,
                request: Some(marker.request.clone()),
                mode: Some(StaticModuleRequestMode::OrdinaryEsm),
                targets: vec![target.clone()],
                bindings,
            })
        })
        .collect()
}

fn emitted_export_identities(
    module: &swc_ecma_ast::Module,
    output: &OutputFileAddress,
    marker: &crate::passthrough::PassthroughVendorExportMarker,
    target: Option<&CandidateImportTarget>,
) -> Vec<CandidateStaticRequestIdentity> {
    let Some(target) = target else {
        return Vec::new();
    };
    module
        .body
        .iter()
        .enumerate()
        .filter_map(|(item_index, item)| {
            if item.span() != marker.declaration_span
                || emitted_request_kind(item) != Some(CandidateOutputRequestKind::NamedReExport)
            {
                return None;
            }
            let (mut identity, _) = request_identity(None, output, item_index, item)?;
            if identity.request.as_deref() != Some(marker.request.as_str()) {
                return None;
            }
            let matches = identity
                .bindings
                .iter()
                .filter(|binding| {
                    binding.specifier_span == marker.specifier_span
                        && binding.imported_name == marker.imported_name
                        && binding.exported_name == marker.exported_name
                        && binding.kind == Some(marker.kind)
                })
                .count();
            if matches != 1 || identity.bindings.len() != 1 {
                return None;
            }
            identity.targets = vec![target.clone()];
            identity.source_statement = None;
            for binding in &mut identity.bindings {
                binding.source_binding = None;
                binding.source_cell = None;
                binding.target = Some(target.clone());
            }
            Some(identity)
        })
        .collect()
}

fn import_specifier_local(specifier: &swc_ecma_ast::ImportSpecifier) -> Id {
    match specifier {
        swc_ecma_ast::ImportSpecifier::Named(named) => named.local.to_id(),
        swc_ecma_ast::ImportSpecifier::Default(default) => default.local.to_id(),
        swc_ecma_ast::ImportSpecifier::Namespace(namespace) => namespace.local.to_id(),
    }
}

fn passthrough_vendor_use_rewrites(
    output: &OutputFileAddress,
    source: &artifact::reports::CandidateSourceImportRoutes,
    rewrite: &crate::passthrough::PassthroughVendorBindingRewrite,
    observations: &[crate::VendorUseRewriteObservation],
    issue: &mut Option<CandidateRewriteIssue>,
) -> Vec<CandidateVendorUseRewrite> {
    let mut uses = Vec::new();
    for source_use in &source.uses {
        if source_use.status != SourceCorrespondenceStatus::Complete || source_use.routes.len() != 1
        {
            issue.get_or_insert(CandidateRewriteIssue::AmbiguousTransition);
            continue;
        }
        let locations = source_use
            .routes
            .iter()
            .filter(|location| location.output == *output)
            .collect::<Vec<_>>();
        if locations.len() > 1 {
            issue.get_or_insert(CandidateRewriteIssue::AmbiguousTransition);
            continue;
        }
        if locations.is_empty() {
            continue;
        }
        let location = locations[0];
        let matching_observations = observations
            .iter()
            .filter(|observation| {
                observation.source_span == location.candidate_span
                    && observation.before_local == location.candidate_local
                    && rewrite
                        .source_binding
                        .as_ref()
                        .is_some_and(|source_binding| observation.before_local == *source_binding)
            })
            .collect::<Vec<_>>();
        if matching_observations.len() > 1 {
            issue.get_or_insert(CandidateRewriteIssue::AmbiguousTransition);
            continue;
        }
        let observation = matching_observations.first().copied();
        let (candidate_after_span, result) = if let Some(observation) = observation {
            let result = match &observation.result {
                crate::VendorUseRewriteTarget::LocalIdentifier { local } => {
                    CandidateVendorUseResult::LocalIdentifier {
                        local: local.clone(),
                    }
                }
                crate::VendorUseRewriteTarget::MemberPath { namespace, path } => {
                    CandidateVendorUseResult::MemberPath {
                        namespace: namespace.clone(),
                        path: path.clone(),
                    }
                }
            };
            (observation.after_span, result)
        } else if rewrite
            .emitted_requests
            .iter()
            .any(|request| rewrite.source_binding.as_ref() == Some(&request.local))
        {
            (
                location.candidate_span,
                CandidateVendorUseResult::LocalIdentifier {
                    local: location.candidate_local.clone(),
                },
            )
        } else {
            issue.get_or_insert(CandidateRewriteIssue::UnprovedUseRewrite);
            continue;
        };
        let source_sites = source
            .source
            .use_sites
            .iter()
            .filter(|site| site.span == source_use.source_span)
            .collect::<Vec<_>>();
        let [source_site] = source_sites.as_slice() else {
            issue.get_or_insert(if source_sites.is_empty() {
                CandidateRewriteIssue::MissingExactInputIdentity
            } else {
                CandidateRewriteIssue::AmbiguousTransition
            });
            continue;
        };
        uses.push(CandidateVendorUseRewrite {
            source_statement: source_site.statement,
            source_span: source_use.source_span,
            before_output: output.clone(),
            candidate_before_span: location.candidate_span,
            candidate_before_local: location.candidate_local.clone(),
            after_output: output.clone(),
            candidate_after_span,
            result,
        });
    }
    if observations.iter().any(|observation| {
        rewrite
            .source_binding
            .as_ref()
            .is_some_and(|source_binding| observation.before_local == *source_binding)
            && !uses
                .iter()
                .any(|use_rewrite| use_rewrite.candidate_before_span == observation.source_span)
    }) {
        issue.get_or_insert(CandidateRewriteIssue::UnprovedUseRewrite);
    }
    uses
}

fn replacement_emits_observed_request(
    receipt: &CandidateRewriteReceipt,
    output: &OutputFileAddress,
    observed: &CandidateStaticRequestIdentity,
) -> bool {
    let CandidateRewriteDisposition::Replaced(replacement) = &receipt.disposition else {
        return false;
    };
    replacement.bindings.iter().any(|binding| {
        binding.emitted_requests.iter().any(|emitted| {
            emitted.output == *output
                && emitted.identity.item_index == observed.item_index
                && emitted.identity.item_span == observed.item_span
                && emitted.identity.kind == observed.kind
                && emitted.identity.request == observed.request
                && emitted.identity.bindings.iter().any(|emitted_binding| {
                    observed.bindings.iter().any(|observed_binding| {
                        emitted_binding.local == observed_binding.local
                            && emitted_binding.specifier_span == observed_binding.specifier_span
                    })
                })
        })
    })
}

fn unsupported_vendor_receipt(
    chunk: ChunkId,
    output: OutputFileAddress,
    before: Option<CandidateStaticRequestIdentity>,
    after: Option<CandidateStaticRequestIdentity>,
    issue: CandidateRewriteIssue,
) -> CandidateRewriteReceipt {
    CandidateRewriteReceipt {
        source_chunk: chunk,
        output,
        stage: CandidateRewriteStage::EmissionVendorRewrite,
        before,
        after,
        disposition: CandidateRewriteDisposition::Unsupported,
        elisions: Vec::new(),
        issue: Some(issue),
    }
}

fn extract_residual_job<'a>(
    artifact: &mut ChunkBundle,
    chunk_table: &ChunkTable,
    chunk_id: ChunkId,
    chunk_path: &'a str,
    entry_file: &'a str,
    symbols: &'a BTreeMap<String, PartialSwapSymbol>,
    bundled: Option<&'a ChunkBundledPartialSwapPlan>,
) -> Result<ResidualJob<'a>> {
    let chunk_name = chunk_table.name(chunk_id).to_string();
    let global_uses = artifact
        .chunks
        .iter()
        .find(|chunk_artifact| chunk_artifact.chunk_id == chunk_id)
        .and_then(|chunk_artifact| chunk_artifact.analysis.runtime_facts.global_uses.clone());
    let js_chunk = artifact.js_chunk_mut(chunk_id)?;
    if js_chunk.get_file(entry_file).is_none() {
        bail!(
            "strip_swapped_vendor_exports vendor entry {chunk_path}: entry file {entry_file} missing from chunk {chunk_name}"
        );
    }
    let mut files = Vec::new();
    let mut has_entry_ast = false;
    for file_path in list_chunk_file_paths(js_chunk) {
        let Some(file) = js_chunk.get_file(&file_path) else {
            continue;
        };
        if file.metadata.role == FileRole::Module || file.ast().is_none() {
            continue;
        }
        let (parts, ast) = js_chunk
            .remove_file(&file_path)
            .and_then(|file| file.into_ast_parts())
            .with_context(|| format!("missing AST for {chunk_name}/{file_path}"))?;
        has_entry_ast |= file_path == entry_file;
        files.push((file_path, parts, ast));
    }
    if !has_entry_ast {
        bail!(
            "strip_swapped_vendor_exports vendor entry {chunk_path}: chunk {chunk_name} entry has no AST"
        );
    }
    Ok(ResidualJob {
        chunk_id,
        chunk_path,
        entry_file,
        symbols,
        bundled,
        global_uses,
        files,
    })
}

/// The residual computation as one function body. Per pass-through
/// file of the swapped chunk:
///
/// 1. **canonicalize** — the unified directive rewrite (§2.4), which
///    also applies any consumer-side surgery the chunk's own files need
///    against *other* swapped chunks;
/// 2. **self-rewrite** (bundled family) — facade import plus internal
///    reference re-targeting, seeding the strip's
///    `replacement_import_locals`;
///
/// then, on the entry module:
///
/// 3. **strip** — split var decls, strip swapped export specifiers,
///    sweep unreachable top-level items. The strip-internal soundness
///    gates (split-brain, observable-effect privacy, live-reads,
///    export-surface invariance) live inside the strip and run
///    unchanged on the same module shape the former stage order
///    produced — the composition is structural here instead of a
///    pipeline ordering concern.
fn emit_vendor_residual(
    job: ResidualJob<'_>,
    context: &PassthroughContext<'_>,
) -> Result<ResidualOutput> {
    let ResidualJob {
        chunk_id,
        chunk_path,
        entry_file,
        symbols,
        bundled,
        global_uses,
        mut files,
    } = job;
    let mut references_by_symbol: BTreeMap<(ChunkId, String), usize> = BTreeMap::new();
    let mut replacement_import_locals: BTreeSet<Id> = BTreeSet::new();
    let mut vendor_rewrites = BTreeMap::new();
    for (file_path, _parts, ast) in &mut files {
        let rewrites = rewrite_passthrough_module(
            &mut ast.module,
            chunk_id,
            file_path,
            context,
            &mut references_by_symbol,
        );
        vendor_rewrites.insert(file_path.clone(), rewrites);
        if bundled.is_some() {
            let mut bindings: BTreeMap<Id, IdentRewriteTarget> = BTreeMap::new();
            let mut prelude_imports: Vec<DeferredImport> = Vec::new();
            let mut self_rewrite_import_locals: BTreeSet<Id> = BTreeSet::new();
            seed_bundled_partial_swap_self_rewrites(
                &ast.module,
                bundled,
                context.chunk_table,
                chunk_id,
                file_path,
                SelfRewriteOutputs {
                    bindings: &mut bindings,
                    prelude_imports: &mut prelude_imports,
                    references_by_symbol: &mut references_by_symbol,
                    self_rewrite_import_locals: &mut self_rewrite_import_locals,
                },
            );
            if !prelude_imports.is_empty() {
                let mut prefixed = prelude_imports
                    .drain(..)
                    .map(DeferredImport::into_module_item)
                    .collect::<Vec<_>>();
                prefixed.append(&mut ast.module.body);
                ast.module.body = prefixed;
            }
            if !bindings.is_empty() {
                let mut rewriter = PartialSwapIdentRewriter {
                    bindings: &bindings,
                    references_by_symbol: &mut references_by_symbol,
                    observations: None,
                };
                ast.module.visit_mut_with(&mut rewriter);
            }
            replacement_import_locals.extend(self_rewrite_import_locals);
        }
    }
    let entry_index = files
        .iter()
        .position(|(file_path, _, _)| file_path == entry_file)
        .expect("residual job extraction verified the entry AST is present");
    // Strip the entry last and keep it last in re-insertion order,
    // matching the former stage order (strip re-appended the entry
    // after the pass-through and self-rewrite waves).
    let (_, _, entry_ast) = &mut files[entry_index];
    let parsed_source_file = entry_ast.source_file_name();
    let global_use_elision = strip_one_chunk_with_replacement_imports(
        &mut entry_ast.module,
        symbols,
        chunk_path,
        &replacement_import_locals,
        &parsed_source_file,
        global_uses.as_deref(),
    )?;
    let mut global_use_elisions = BTreeMap::new();
    if let Some(receipt) = global_use_elision {
        global_use_elisions.insert(chunk_id, receipt);
    }
    let entry = files.remove(entry_index);
    files.push(entry);
    Ok(ResidualOutput {
        chunk_id,
        files: files
            .into_iter()
            .map(|(file_path, parts, ast)| (file_path, parts, ast))
            .collect(),
        vendor_rewrites,
        references_by_symbol,
        global_use_elisions,
    })
}

/// Write the plan's generated vendor emission outputs: full-swap
/// wrappers plus bundled-partial-swap bundle copies and facades. The
/// combined manifest is written separately by the pipeline next to its
/// other reports.
pub fn write_planned_vendor_outputs(plan: &VendorResolutionPlan) -> Result<()> {
    for swap in &plan.full_swaps {
        if let Some(wrapper) = &swap.wrapper {
            write_planned_wrapper(&wrapper.abs_path, &wrapper.source)?;
        }
    }
    for bundled in plan.bundled_partial_swaps.values() {
        write_planned_bundled_assets(&bundled.assets)?;
    }
    Ok(())
}
