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

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail};
use rayon::prelude::*;
use swc_common::GLOBALS;
use swc_ecma_ast::Id;
use swc_ecma_visit::VisitMutWith;

use artifact::{
    ArtifactIndexes, ChunkBundle, ChunkId, ChunkTable, FileRole, JsFile, JsFileAstParts,
    list_chunk_file_paths,
};
use js_ast::ParsedJsModule;
use spec::PartialSwapSymbol;

use crate::passthrough::{PassthroughContext, rewrite_passthrough_module};
use crate::plan::{ChunkBundledPartialSwapPlan, VendorResolutionPlan};
use crate::strip::{ChunkStripStats, strip_one_chunk_with_replacement_imports};
use crate::wrappers::{write_planned_bundled_assets, write_planned_wrapper};
use crate::{
    DeferredImport, IdentRewriteTarget, MaterializedOutputChunkIndex, PartialSwapIdentRewriter,
    SelfRewriteOutputs, seed_bundled_partial_swap_self_rewrites,
};

pub struct EmissionRewriteResult {
    pub artifact: ChunkBundle,
    /// (swapped chunk, chunk export) → count of references rewritten in
    /// pass-through files and vendor self-rewrites; folded into the
    /// partial-swap manifests' `references_rewritten` alongside
    /// lowering's construction-time counts.
    pub references_by_symbol: BTreeMap<(ChunkId, String), usize>,
    /// Per-chunk residual strip stats keyed by chunk path.
    pub strip_stats: BTreeMap<String, ChunkStripStats>,
}

pub fn apply_emission_rewrites(
    mut artifact: ChunkBundle,
    plan: &VendorResolutionPlan,
    references: &ArtifactIndexes,
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
                    rewrite_passthrough_module(
                        &mut job.ast.module,
                        job.chunk_id,
                        &job.file_path,
                        context_ref,
                        &mut references_by_symbol,
                    );
                    PassthroughFileResult {
                        chunk_index: job.chunk_index,
                        parts: job.parts,
                        ast: job.ast,
                        references_by_symbol,
                    }
                })
            })
            .collect()
    });

    let mut references_by_symbol = BTreeMap::new();
    for result in results {
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
            &mut artifact,
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
            &mut artifact,
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

    let mut strip_stats = BTreeMap::new();
    for output in outputs {
        let js_chunk = artifact.js_chunk_mut(output.chunk_id)?;
        for (parts, ast) in output.files {
            js_chunk.insert_file(JsFile::from_ast_parts(parts, ast));
        }
        for (key, count) in output.references_by_symbol {
            *references_by_symbol.entry(key).or_insert(0) += count;
        }
        strip_stats.insert(output.chunk_path, output.stats);
    }

    Ok(EmissionRewriteResult {
        artifact,
        references_by_symbol,
        strip_stats,
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
    parts: JsFileAstParts,
    ast: ParsedJsModule,
    references_by_symbol: BTreeMap<(ChunkId, String), usize>,
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
    files: Vec<(String, JsFileAstParts, ParsedJsModule)>,
}

struct ResidualOutput {
    chunk_id: ChunkId,
    chunk_path: String,
    files: Vec<(JsFileAstParts, ParsedJsModule)>,
    references_by_symbol: BTreeMap<(ChunkId, String), usize>,
    stats: ChunkStripStats,
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
        mut files,
    } = job;
    let mut references_by_symbol: BTreeMap<(ChunkId, String), usize> = BTreeMap::new();
    let mut replacement_import_locals: BTreeSet<Id> = BTreeSet::new();
    for (file_path, _parts, ast) in &mut files {
        rewrite_passthrough_module(
            &mut ast.module,
            chunk_id,
            file_path,
            context,
            &mut references_by_symbol,
        );
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
    let stats = strip_one_chunk_with_replacement_imports(
        &mut entry_ast.module,
        symbols,
        chunk_path,
        &replacement_import_locals,
    )?;
    let entry = files.remove(entry_index);
    files.push(entry);
    Ok(ResidualOutput {
        chunk_id,
        chunk_path: chunk_path.to_string(),
        files: files
            .into_iter()
            .map(|(_, parts, ast)| (parts, ast))
            .collect(),
        references_by_symbol,
        stats,
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
