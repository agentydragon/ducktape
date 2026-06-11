use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

use analysis::{
    AnalysisHints, ChunkId, EffectCell, LocalEffectPolicy, StatementFacts, analyze_chunk,
};
use anyhow::{Context, Result, bail};
use binding_targets::{declaration_ids, declaration_name_strings, module_export_name};
use rayon::prelude::*;
use serde::Serialize;
use swc_common::sync::Lrc;
use swc_common::{DUMMY_SP, GLOBALS, SourceMap};
use swc_ecma_ast::*;
use swc_ecma_codegen::text_writer::JsWriter;
use swc_ecma_codegen::{Config, Emitter};
use swc_ecma_visit::{Visit, VisitWith};

use artifact::{ChunkBundle, JsFile, JsFileAstParts};
use js_ast::ParsedJsModule;
use spec::PartialSwapSymbol;

use crate::plan::VendorResolutionPlan;

pub struct StripSwappedVendorExportsResult {
    pub artifact: ChunkBundle,
    pub manifest: StripSwappedVendorExportsManifest,
}

#[derive(Debug, Clone, Default)]
pub struct StripSwappedVendorExportsOptions {
    pub replacement_import_locals_by_chunk_path: BTreeMap<String, BTreeSet<Id>>,
}

#[derive(Debug, Clone, Serialize)]
pub struct StripSwappedVendorExportsManifest {
    pub per_chunk: BTreeMap<String, ChunkStripStats>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkStripStats {
    pub chunk_path: String,
    pub stripped_export_specifiers: usize,
    pub dropped_top_level_items: usize,
    pub retained_top_level_items: usize,
}

/// Per-chunk pass that drops swapped names from the vendor entry's
/// trailing `export { … }` block (Phase 1) and sweeps top-level
/// bindings that are no longer reachable from the residual export
/// surface plus retained side-effect statements (Phase 2).
///
/// Runs after `apply_partial_vendor_swaps` — the consumer side has
/// already been rewritten to import each swapped name from upstream,
/// so the chunk's residual `export { … }` entries for those names
/// are dead weight. Without this pass the on-disk vendor blob stays
/// byte-identical to pre-swap.
pub fn strip_swapped_vendor_exports_with_options(
    mut artifact: ChunkBundle,
    plan: &VendorResolutionPlan,
    options: StripSwappedVendorExportsOptions,
) -> Result<StripSwappedVendorExportsResult> {
    // Strip inputs come straight from the vendor plan: partial and
    // bundled-partial entries, merged back into chunk-path order.
    #[derive(Clone, Copy)]
    struct StripSource<'a> {
        chunk_path: &'a str,
        chunk_name: &'a str,
        chunk_id: ChunkId,
        entry_file: &'a str,
        symbols: &'a BTreeMap<String, PartialSwapSymbol>,
    }
    let sources: BTreeMap<&str, StripSource<'_>> = plan
        .partial_swaps
        .iter()
        .map(|(chunk_id, partial)| StripSource {
            chunk_path: &partial.chunk_path,
            chunk_name: &partial.resolution.chunk_id,
            chunk_id: *chunk_id,
            entry_file: &partial.entry_file,
            symbols: &partial.symbols,
        })
        .chain(
            plan.bundled_partial_swaps
                .iter()
                .map(|(chunk_id, bundled)| StripSource {
                    chunk_path: &bundled.chunk_path,
                    chunk_name: &bundled.resolution.chunk_id,
                    chunk_id: *chunk_id,
                    entry_file: &bundled.entry_file,
                    symbols: &bundled.symbols,
                }),
        )
        .map(|source| (source.chunk_path, source))
        .collect();

    // Phase 1 (sequential): pull every vendor entry's entry-file AST
    // out of the artifact and bundle the per-chunk inputs into a
    // `StripJob`. `remove_file`/`insert_file` on `ChunkBundle` need
    // `&mut artifact`; doing the round-trip here means the actual
    // strip work can borrow only owned data and run in parallel.
    let mut jobs: Vec<StripJob<'_>> = Vec::new();
    for source in sources.values() {
        let StripSource {
            chunk_path,
            chunk_name,
            chunk_id,
            entry_file,
            symbols,
        } = *source;
        let js_chunk = artifact.js_chunk_mut(chunk_id)?;
        let file = js_chunk.remove_file(entry_file).with_context(|| {
            format!(
                "strip_swapped_vendor_exports vendor entry {chunk_path}: entry file {entry_file} missing from chunk {chunk_name}"
            )
        })?;
        let (parts, ast) = file.into_ast_parts().with_context(|| {
            format!(
                "strip_swapped_vendor_exports vendor entry {chunk_path}: chunk {chunk_name} entry has no AST"
            )
        })?;

        let replacement_import_locals = options
            .replacement_import_locals_by_chunk_path
            .get(chunk_path)
            .cloned()
            .unwrap_or_default();

        jobs.push(StripJob {
            chunk_path,
            chunk_id,
            symbols,
            replacement_import_locals,
            parts,
            ast,
        });
    }

    // Phase 2 (parallel): per-job strip work is pure data transformation —
    // each job owns its `parts`/`ast` and reads only its own `symbols` and
    // `replacement_import_locals`. No two jobs target the same chunk_id
    // (vendor is a BTreeMap keyed by chunk_path, and chunk_path -> chunk_id
    // is injective via `vendor_chunk_name`), so collecting outputs
    // back into the artifact in Phase 3 doesn't risk write-write races.
    //
    // `swc_common::GLOBALS` is a `scoped_tls` thread-local that does NOT
    // carry into rayon worker threads. The strip path currently doesn't
    // mint fresh marks, but `Id` comparisons and any future swc work
    // expect a live `Globals`; capture the parent thread's globals and
    // re-set inside each worker, mirroring `lowering/mod.rs` and
    // `lower_chunk`.
    let outputs: Vec<StripOutput> = GLOBALS.with(|globals| -> Result<Vec<StripOutput>> {
        jobs.into_par_iter()
            .map(|job| GLOBALS.set(globals, || strip_one_job(job)))
            .collect()
    })?;

    // Phase 3 (sequential): re-insert the stripped entry files and
    // collect per-chunk stats in `vendor` iteration order (preserved by
    // `Vec::into_par_iter().collect()`).
    let mut per_chunk = BTreeMap::new();
    for output in outputs {
        let StripOutput {
            chunk_path,
            chunk_id,
            parts,
            ast,
            stats,
        } = output;
        let js_chunk = artifact.js_chunk_mut(chunk_id)?;
        js_chunk.insert_file(JsFile::from_ast_parts(parts, ast));
        per_chunk.insert(chunk_path, stats);
    }

    Ok(StripSwappedVendorExportsResult {
        artifact,
        manifest: StripSwappedVendorExportsManifest { per_chunk },
    })
}

struct StripJob<'a> {
    chunk_path: &'a str,
    chunk_id: ChunkId,
    symbols: &'a BTreeMap<String, PartialSwapSymbol>,
    replacement_import_locals: BTreeSet<Id>,
    parts: JsFileAstParts,
    ast: ParsedJsModule,
}

struct StripOutput {
    chunk_path: String,
    chunk_id: ChunkId,
    parts: JsFileAstParts,
    ast: ParsedJsModule,
    stats: ChunkStripStats,
}

fn strip_one_job(job: StripJob<'_>) -> Result<StripOutput> {
    let StripJob {
        chunk_path,
        chunk_id,
        symbols,
        replacement_import_locals,
        parts,
        mut ast,
    } = job;
    let stats = strip_one_chunk_with_replacement_imports(
        &mut ast.module,
        symbols,
        chunk_path,
        &replacement_import_locals,
    )?;
    Ok(StripOutput {
        chunk_path: chunk_path.to_string(),
        chunk_id,
        parts,
        ast,
        stats,
    })
}

#[cfg(test)]
fn strip_one_chunk(
    module: &mut Module,
    symbols: &BTreeMap<String, PartialSwapSymbol>,
    chunk_path: &str,
) -> Result<ChunkStripStats> {
    strip_one_chunk_with_replacement_imports(module, symbols, chunk_path, &BTreeSet::new())
}

fn strip_one_chunk_with_replacement_imports(
    module: &mut Module,
    symbols: &BTreeMap<String, PartialSwapSymbol>,
    chunk_path: &str,
    replacement_import_locals: &BTreeSet<Id>,
) -> Result<ChunkStripStats> {
    let swapped: BTreeSet<String> = symbols.keys().cloned().collect();

    split_top_level_var_decls(module);
    let stripped = strip_export_specifiers(module, symbols, chunk_path)?;
    let stripped_export_specifiers = stripped.len();
    let post_strip_exports = super::collect_exported_names(module);

    let dropped_total_before = module.body.len();
    sweep_unreachable_top_level(
        module,
        &post_strip_exports,
        &stripped,
        chunk_path,
        replacement_import_locals,
    )?;
    let retained = module.body.len();
    let dropped = dropped_total_before - retained;

    // Phase 2 must not change the export surface relative to Phase 1.
    let post_dce_exports = super::collect_exported_names(module);
    if post_dce_exports != post_strip_exports {
        let removed: Vec<_> = post_strip_exports
            .difference(&post_dce_exports)
            .cloned()
            .collect();
        let added: Vec<_> = post_dce_exports
            .difference(&post_strip_exports)
            .cloned()
            .collect();
        bail!(
            "strip_swapped_vendor_exports vendor entry {chunk_path}: DCE pass changed the export surface (removed=[{}], added=[{}])",
            removed.join(","),
            added.join(","),
        );
    }

    // Sanity: stripped names should not appear in pre or post export set.
    let leaked: Vec<_> = swapped.intersection(&post_strip_exports).cloned().collect();
    if !leaked.is_empty() {
        bail!(
            "strip_swapped_vendor_exports vendor entry {chunk_path}: swapped names still exported after strip: [{}]",
            leaked.join(","),
        );
    }

    Ok(ChunkStripStats {
        chunk_path: chunk_path.to_string(),
        stripped_export_specifiers,
        dropped_top_level_items: dropped,
        retained_top_level_items: retained,
    })
}

#[derive(Debug, Clone)]
struct StrippedExport {
    package: String,
    locals: BTreeSet<Id>,
}

/// Walk `module.body` once and strip the chunk's *local* re-exports of
/// every name in `swapped`. Two shapes are handled:
///
/// - `export { x, y as z }` (`ExportNamed` with `src.is_none()`): the
///   matching specifier is dropped from the list; an empty list collapses
///   the statement.
/// - `export const x = …` / `export function x() {}` / `export class x {}`
///   (`ExportDecl`): the `export` prefix is dropped — the declaration
///   itself stays, becoming a chunk-local binding the DCE pass can
///   collect if no live item references it.
///
/// `export { x } from "./y"` (`ExportNamed` with `src.is_some()`) is left
/// alone — those forward upstream names through a side import, not from
/// a chunk-local binding.
fn strip_export_specifiers(
    module: &mut Module,
    symbols: &BTreeMap<String, PartialSwapSymbol>,
    chunk_path: &str,
) -> Result<BTreeMap<String, StrippedExport>> {
    let swapped: BTreeSet<String> = symbols.keys().cloned().collect();
    let mut found: BTreeMap<String, StrippedExport> = BTreeMap::new();
    let mut new_body = Vec::with_capacity(module.body.len());

    for item in std::mem::take(&mut module.body) {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(mut named)) => {
                if named.src.is_some() {
                    new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
                    continue;
                }
                let mut kept = Vec::with_capacity(named.specifiers.len());
                for spec in std::mem::take(&mut named.specifiers) {
                    let ExportSpecifier::Named(ref named_spec) = spec else {
                        kept.push(spec);
                        continue;
                    };
                    let exported = named_spec
                        .exported
                        .as_ref()
                        .map(module_export_name)
                        .unwrap_or_else(|| module_export_name(&named_spec.orig));
                    if swapped.contains(&exported) {
                        let Some(symbol) = symbols.get(&exported) else {
                            unreachable!("swapped names are derived from symbols");
                        };
                        let export_local = match &named_spec.orig {
                            ModuleExportName::Ident(ident) => ident.to_id(),
                            ModuleExportName::Str(orig) => {
                                bail!(
                                    "strip_swapped_vendor_exports vendor entry {chunk_path}: swapped export {exported} uses string-literal local name {:?}, which cannot be mapped to a chunk binding",
                                    orig.value,
                                );
                            }
                        };
                        found.insert(
                            exported,
                            StrippedExport {
                                package: symbol.package.clone(),
                                locals: BTreeSet::from([stripped_symbol_local(
                                    symbol,
                                    export_local,
                                )]),
                            },
                        );
                    } else {
                        kept.push(spec);
                    }
                }
                if kept.is_empty() {
                    continue;
                }
                named.specifiers = kept;
                new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                let inline_names = declaration_name_strings(&export_decl.decl);
                // For an `ExportDecl`, every declared name is exported
                // under that same name. Drop the `export` prefix only
                // if *all* names declared by the statement are swapped;
                // otherwise we'd silently un-export a non-swapped
                // sibling (legal but surprising for a multi-declarator
                // `export const a = …, b = …`).
                if !inline_names.is_empty() && inline_names.iter().all(|n| swapped.contains(n)) {
                    for n in &inline_names {
                        let Some(symbol) = symbols.get(n) else {
                            unreachable!("inline names were checked against symbols");
                        };
                        found.insert(
                            n.clone(),
                            StrippedExport {
                                package: symbol.package.clone(),
                                locals: symbol
                                    .local
                                    .as_ref()
                                    .map(|local| BTreeSet::from([synthetic_id(local)]))
                                    .unwrap_or_else(|| export_decl_declared_ids(&export_decl.decl)),
                            },
                        );
                    }
                    new_body.push(ModuleItem::Stmt(Stmt::Decl(export_decl.decl)));
                } else {
                    new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)));
                }
            }
            other => new_body.push(other),
        }
    }
    module.body = new_body;

    let found_names = found.keys().cloned().collect::<BTreeSet<_>>();
    for missing_local in swapped.difference(&found_names) {
        let Some(symbol) = symbols.get(missing_local) else {
            unreachable!("swapped names are derived from symbols");
        };
        if let Some(local) = &symbol.local {
            found.insert(
                missing_local.clone(),
                StrippedExport {
                    package: symbol.package.clone(),
                    locals: BTreeSet::from([synthetic_id(local)]),
                },
            );
        }
    }

    let found_names = found.keys().cloned().collect::<BTreeSet<_>>();
    let missing: Vec<String> = swapped.difference(&found_names).cloned().collect();
    if !missing.is_empty() {
        bail!(
            "strip_swapped_vendor_exports vendor entry {chunk_path}: swapped symbols not found in any chunk-local export: [{}]",
            missing.join(","),
        );
    }
    Ok(found)
}

fn stripped_symbol_local(symbol: &PartialSwapSymbol, export_local: Id) -> Id {
    symbol
        .local
        .as_ref()
        .map(|local| synthetic_id(local))
        .unwrap_or(export_local)
}

fn synthetic_id(local: &str) -> Id {
    Ident::new_no_ctxt(local.into(), DUMMY_SP).to_id()
}

fn export_decl_declared_ids(decl: &Decl) -> BTreeSet<Id> {
    declaration_ids(decl).into_iter().collect()
}

fn split_top_level_var_decls(module: &mut Module) {
    let mut out = Vec::with_capacity(module.body.len());
    for item in std::mem::take(&mut module.body) {
        match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) if var.decls.len() > 1 => {
                for decl in var.decls {
                    out.push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(VarDecl {
                        span: var.span,
                        ctxt: var.ctxt,
                        kind: var.kind,
                        declare: var.declare,
                        decls: vec![decl],
                    })))));
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => match export_decl.decl {
                Decl::Var(var) if var.decls.len() > 1 => {
                    for decl in var.decls {
                        out.push(ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                            span: export_decl.span,
                            decl: Decl::Var(Box::new(VarDecl {
                                span: var.span,
                                ctxt: var.ctxt,
                                kind: var.kind,
                                declare: var.declare,
                                decls: vec![decl],
                            })),
                        })));
                    }
                }
                decl => out.push(ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                    span: export_decl.span,
                    decl,
                }))),
            },
            other => out.push(other),
        }
    }
    module.body = out;
}

/// Conservative top-level dead-code sweep. Each `module.body[i]` is
/// either a **side-effect** anchor (must stay), or a **declaration**
/// whose retention depends on whether anything live reads its names.
///
/// Algorithm:
/// 1. Classify each `body[i]` into `ItemClass::Decl { names, reads }`
///    or `ItemClass::SideEffect { reads }`. Hoistable, side-effect-free
///    shapes (`function X`, `class X`, `var/let/const X = <pure_init>`,
///    `export const X = <pure_init>`, etc.) go to `Decl`; everything
///    else (top-level expressions, `Object.defineProperty(...)` calls,
///    imports, side-effecting var inits) goes to `SideEffect`.
/// 2. Seed the live set with all `SideEffect` items, plus any `Decl`
///    that introduces a name in `live_exports`.
/// 3. Fixpoint: while there's a `Decl` not yet live whose declared
///    names are referenced by a live item, mark it live.
/// 4. Filter `module.body` to keep only live items in source order.
///
/// Reads are over-approximated to all free identifier names appearing
/// anywhere in the item — no scope analysis. This is safe (keeps more
/// code than strictly necessary) and avoids re-implementing lexical
/// scoping.
fn sweep_unreachable_top_level(
    module: &mut Module,
    live_exports: &BTreeSet<String>,
    stripped: &BTreeMap<String, StrippedExport>,
    chunk_path: &str,
    replacement_import_locals: &BTreeSet<Id>,
) -> Result<()> {
    let analyses = analyze_prune_items(module, chunk_path)?;

    // Binding id -> index that declares it. If two items declare the
    // same binding (legal for `var`), prefer the last declaration; later
    // writes shadow earlier ones for reachability purposes.
    let mut declarer: BTreeMap<Id, usize> = BTreeMap::new();
    for (i, an) in analyses.iter().enumerate() {
        for id in &an.declared {
            declarer.insert(id.clone(), i);
        }
    }
    let shareable_items = compute_shareable_items(
        &analyses,
        &module.body,
        &declarer,
        replacement_import_locals,
    );

    let mut mutation_items_by_target: BTreeMap<Id, Vec<usize>> = BTreeMap::new();
    for (i, an) in analyses.iter().enumerate() {
        for id in &an.local_effects {
            if declarer.contains_key(id) {
                mutation_items_by_target
                    .entry(id.clone())
                    .or_default()
                    .push(i);
            }
        }
    }

    let mut swapped_reachability = vec![BTreeSet::new(); analyses.len()];
    let mut swapped_root_items = BTreeSet::new();
    let mut queue: Vec<(usize, String)> = Vec::new();
    for (alias, stripped_export) in stripped {
        for local in &stripped_export.locals {
            let decl_idx = resolve_declared_local(local, &declarer, chunk_path, alias)?;
            swapped_root_items.insert(decl_idx);
            if swapped_reachability[decl_idx].insert(stripped_export.package.clone()) {
                queue.push((decl_idx, stripped_export.package.clone()));
            }
        }
    }

    propagate_swapped_dependency_reachability(
        &mut swapped_reachability,
        &mut queue,
        &analyses,
        &declarer,
        &mutation_items_by_target,
    );
    let residual_export_closure = residual_export_dependency_closure(
        &analyses,
        live_exports,
        &declarer,
        &mutation_items_by_target,
    );
    absorb_swapped_dependent_items(
        &mut swapped_reachability,
        &analyses,
        &declarer,
        &mutation_items_by_target,
        &residual_export_closure,
        &shareable_items,
    );

    // An item carries a globally-observable side effect when it is either a
    // `Hard` statement (impure / module linkage) or a `LocalMutation` that
    // writes a target which is neither a chunk-local declaration nor a
    // replacement-import local — i.e. the write lands on a global / external
    // object whose post-strip value residual or external code may read.
    let observable_side_effect = |i: usize| -> bool {
        let an = &analyses[i];
        an.side_effect == SideEffectKind::Hard
            || (an.side_effect == SideEffectKind::LocalMutation
                && an.local_effects.iter().any(|id| {
                    !declarer.contains_key(id) && !replacement_import_locals.contains(id)
                }))
    };

    let mut live = vec![false; analyses.len()];
    let mut live_reasons = vec![None; analyses.len()];
    for (i, an) in analyses.iter().enumerate() {
        let residual_exports = an
            .export_aliases
            .iter()
            .filter(|alias| live_exports.contains(*alias))
            .cloned()
            .collect::<Vec<_>>();
        let residual_export = !residual_exports.is_empty();
        let hard_side_effect = observable_side_effect(i) && swapped_reachability[i].is_empty();
        if residual_export {
            live[i] = true;
            live_reasons[i] = Some(LiveReason::ResidualExport(residual_exports));
        } else if hard_side_effect {
            live[i] = true;
            live_reasons[i] = Some(LiveReason::HardSideEffect);
        }
    }

    let mut queue: Vec<usize> = (0..analyses.len()).filter(|&i| live[i]).collect();
    while let Some(i) = queue.pop() {
        for (dep_idx, via) in dependency_edges(&analyses[i], &declarer, &mutation_items_by_target) {
            if !live[dep_idx] {
                live[dep_idx] = true;
                live_reasons[dep_idx] = Some(LiveReason::Dependency {
                    from: i,
                    via: via.into_iter().map(|id| id_name(&id)).collect(),
                });
                queue.push(dep_idx);
            }
        }
    }

    for (i, packages) in swapped_reachability.iter().enumerate() {
        if live[i]
            && packages.len() == 1
            && (!shareable_items[i] || swapped_root_items.contains(&i))
        {
            let declared = analyses[i]
                .declared
                .iter()
                .map(id_name)
                .collect::<Vec<_>>()
                .join(",");
            let exports = analyses[i]
                .export_aliases
                .iter()
                .cloned()
                .collect::<Vec<_>>()
                .join(",");
            let packages = packages.iter().cloned().collect::<Vec<_>>().join(",");
            let residual_path = format_live_trace(i, &analyses, &module.body, &live_reasons);
            bail!(
                "strip_swapped_vendor_exports vendor entry {chunk_path}: split-brain vendor swap: top-level item {i} remains reachable from the residual chunk while also belonging to swapped package(s) [{packages}] (declared=[{declared}], exports=[{exports}]); residual path: {residual_path}",
            );
        }
    }

    // Soundness gate: if any *kept* item reads a name declared only by
    // a *dropped* item, the classification missed a side-effect or the
    // fixpoint didn't converge. Bail with the offending pair.
    for (i, is_live) in live.iter().enumerate() {
        if !is_live {
            continue;
        }
        for id in analyses[i]
            .reads
            .iter()
            .chain(analyses[i].local_effects.iter())
        {
            if let Some(&decl_idx) = declarer.get(id)
                && !live[decl_idx]
            {
                bail!(
                    "strip_swapped_vendor_exports vendor entry {chunk_path}: live item {i} reads `{}` declared by dropped item {decl_idx}",
                    id_name(id),
                );
            }
        }
    }

    // Soundness gate: a top-level item carrying a globally-observable side
    // effect must never be *silently* dropped. The keep-pass above honors such
    // an item only when it is not swap-reachable; a swap-reachable one falls
    // through to deletion. Dropping is sound only when the effect is provably
    // swap-private — every storage cell it writes stays inside the swapped
    // island, so no retained / external code can witness its post-strip value:
    //
    //   * a static-key `globalThis.<prop>` write is never private (external or
    //     other-chunk code may read the global back — e.g. `window.foo =
    //     <swappedThing>` read via `window.foo`);
    //   * a binding write is private unless some *retained* (`live`) item reads
    //     that binding; writes to a replacement-import local stay private
    //     (the old island configuring the new facade has no residual reader);
    //   * a statement the analyzer cannot summarize (dynamic `globalThis[expr]`,
    //     `eval`, `with`, `Function(...)`) may touch any cell and is never
    //     private.
    //
    // A swap-reachable observable effect that is not provably swap-private would
    // be silently deleted — an under-restriction soundness violation. Bail so
    // the spec author restructures rather than shipping a broken bundle.
    let live_reads: BTreeSet<Id> = analyses
        .iter()
        .enumerate()
        .filter(|&(i, _)| live[i])
        .flat_map(|(_, an)| an.reads.iter().chain(an.local_effects.iter()).cloned())
        .collect();
    let swap_private_effect = |i: usize| -> bool {
        let an = &analyses[i];
        if !an.effects_summarizable {
            return false;
        }
        an.observable_writes.iter().all(|cell| match cell {
            EffectCell::GlobalProp(_) => false,
            EffectCell::Binding(id) => {
                replacement_import_locals.contains(id) || !live_reads.contains(id)
            }
        })
    };
    for i in 0..analyses.len() {
        if live[i]
            || swapped_reachability[i].is_empty()
            || !observable_side_effect(i)
            || swap_private_effect(i)
        {
            continue;
        }
        let packages = swapped_reachability[i]
            .iter()
            .cloned()
            .collect::<Vec<_>>()
            .join(",");
        let declared = analyses[i]
            .declared
            .iter()
            .map(id_name)
            .collect::<Vec<_>>()
            .join(",");
        bail!(
            "strip_swapped_vendor_exports vendor entry {chunk_path}: observable side-effect item {i} is swap-reachable from package(s) [{packages}] (declared=[{declared}]) but its observable effect is not provably swap-private (it writes a global / external cell residual code may read), so it would be silently dropped — restructure the spec so the side effect does not read swapped binding(s)",
        );
    }

    module.body = std::mem::take(&mut module.body)
        .into_iter()
        .zip(live.iter())
        .filter_map(|(item, &is_live)| is_live.then_some(item))
        .collect();
    Ok(())
}

fn compute_shareable_items(
    analyses: &[ItemAnalysis],
    items: &[ModuleItem],
    declarer: &BTreeMap<Id, usize>,
    replacement_import_locals: &BTreeSet<Id>,
) -> Vec<bool> {
    let mut shareable = analyses
        .iter()
        .map(|analysis| analysis.shareable_helper)
        .collect::<Vec<_>>();
    for i in vite_preload_dependency_items(analyses, items, declarer) {
        shareable[i] = true;
    }

    loop {
        let mut changed = false;
        for (i, analysis) in analyses.iter().enumerate() {
            if shareable[i] {
                continue;
            }
            if replacement_facade_shareable(
                &items[i],
                analysis,
                declarer,
                replacement_import_locals,
                &shareable,
            ) {
                shareable[i] = true;
                changed = true;
            }
        }
        if !changed {
            break;
        }
    }

    shareable
}

fn vite_preload_dependency_items(
    analyses: &[ItemAnalysis],
    items: &[ModuleItem],
    declarer: &BTreeMap<Id, usize>,
) -> BTreeSet<usize> {
    let mut out = BTreeSet::new();
    for (i, item) in items.iter().enumerate() {
        if !item_is_vite_preload_helper(item) {
            continue;
        }
        for id in analyses[i]
            .reads
            .iter()
            .chain(analyses[i].local_effects.iter())
        {
            if let Some(&dep_idx) = declarer.get(id)
                && item_is_vite_preload_dependency_cell(&items[dep_idx])
            {
                out.insert(dep_idx);
            }
        }
    }
    out
}

fn replacement_facade_shareable(
    item: &ModuleItem,
    analysis: &ItemAnalysis,
    declarer: &BTreeMap<Id, usize>,
    replacement_import_locals: &BTreeSet<Id>,
    shareable: &[bool],
) -> bool {
    if replacement_import_locals.is_empty()
        || !item_is_var_decl(item)
        || !analysis
            .reads
            .iter()
            .any(|id| replacement_import_locals.contains(id))
    {
        return false;
    }
    analysis.reads.iter().all(|id| {
        replacement_import_locals.contains(id)
            || matches_global_intrinsic(id.0.as_ref())
            || declarer
                .get(id)
                .is_some_and(|idx| shareable.get(*idx).copied().unwrap_or(false))
    })
}

fn item_is_var_decl(item: &ModuleItem) -> bool {
    matches!(
        item,
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(_)))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                decl: Decl::Var(_),
                ..
            }))
    )
}

fn dependency_items(
    analysis: &ItemAnalysis,
    declarer: &BTreeMap<Id, usize>,
    mutation_items_by_target: &BTreeMap<Id, Vec<usize>>,
) -> BTreeSet<usize> {
    dependency_edges(analysis, declarer, mutation_items_by_target)
        .into_keys()
        .collect()
}

fn propagate_swapped_dependency_reachability(
    swapped_reachability: &mut [BTreeSet<String>],
    queue: &mut Vec<(usize, String)>,
    analyses: &[ItemAnalysis],
    declarer: &BTreeMap<Id, usize>,
    mutation_items_by_target: &BTreeMap<Id, Vec<usize>>,
) {
    while let Some((i, package)) = queue.pop() {
        for dep_idx in dependency_items(&analyses[i], declarer, mutation_items_by_target) {
            if swapped_reachability[dep_idx].insert(package.clone()) {
                queue.push((dep_idx, package.clone()));
            }
        }
    }
}

fn absorb_swapped_dependent_items(
    swapped_reachability: &mut [BTreeSet<String>],
    analyses: &[ItemAnalysis],
    declarer: &BTreeMap<Id, usize>,
    mutation_items_by_target: &BTreeMap<Id, Vec<usize>>,
    residual_export_closure: &BTreeSet<usize>,
    shareable_items: &[bool],
) {
    loop {
        let mut queue = Vec::new();
        for (i, analysis) in analyses.iter().enumerate() {
            if !swapped_reachability[i].is_empty() || residual_export_closure.contains(&i) {
                continue;
            }
            let mut packages = BTreeSet::new();
            let mut saw_declared_dependency = false;
            let mut blocked_by_residual_dependency = false;
            for dep_idx in dependency_items(analysis, declarer, mutation_items_by_target) {
                saw_declared_dependency = true;
                if residual_export_closure.contains(&dep_idx)
                    && !shareable_items.get(dep_idx).copied().unwrap_or(false)
                {
                    blocked_by_residual_dependency = true;
                    break;
                }
                packages.extend(swapped_reachability[dep_idx].iter().cloned());
            }
            if saw_declared_dependency && !blocked_by_residual_dependency && packages.len() == 1 {
                let package = packages
                    .iter()
                    .next()
                    .expect("one package after len check")
                    .clone();
                swapped_reachability[i].insert(package.clone());
                queue.push((i, package));
            }
        }
        if queue.is_empty() {
            break;
        }
        propagate_swapped_dependency_reachability(
            swapped_reachability,
            &mut queue,
            analyses,
            declarer,
            mutation_items_by_target,
        );
    }
}

fn residual_export_dependency_closure(
    analyses: &[ItemAnalysis],
    live_exports: &BTreeSet<String>,
    declarer: &BTreeMap<Id, usize>,
    mutation_items_by_target: &BTreeMap<Id, Vec<usize>>,
) -> BTreeSet<usize> {
    let mut out = BTreeSet::new();
    let mut queue = Vec::new();
    for (i, analysis) in analyses.iter().enumerate() {
        if analysis
            .export_aliases
            .iter()
            .any(|alias| live_exports.contains(alias))
            && out.insert(i)
        {
            queue.push(i);
        }
    }
    while let Some(i) = queue.pop() {
        for dep_idx in dependency_items(&analyses[i], declarer, mutation_items_by_target) {
            if out.insert(dep_idx) {
                queue.push(dep_idx);
            }
        }
    }
    out
}

fn resolve_declared_local(
    local: &Id,
    declarer: &BTreeMap<Id, usize>,
    chunk_path: &str,
    alias: &str,
) -> Result<usize> {
    if let Some(&decl_idx) = declarer.get(local) {
        return Ok(decl_idx);
    }

    let local_name = id_name(local);
    let matches = declarer
        .iter()
        .filter_map(|(declared, &idx)| (id_name(declared) == local_name).then_some(idx))
        .collect::<BTreeSet<_>>();
    let prefix = format!(
        "strip_swapped_vendor_exports vendor entry {chunk_path}: \
         swapped export {alias} maps to local `{local_name}`"
    );
    match matches.len() {
        1 => Ok(*matches.iter().next().expect("one match")),
        0 => bail!("{prefix} but that binding has no top-level declaration"),
        _ => bail!("{prefix} but that name has multiple top-level declarations"),
    }
}

fn dependency_edges(
    analysis: &ItemAnalysis,
    declarer: &BTreeMap<Id, usize>,
    mutation_items_by_target: &BTreeMap<Id, Vec<usize>>,
) -> BTreeMap<usize, BTreeSet<Id>> {
    let mut out = BTreeMap::<usize, BTreeSet<Id>>::new();
    for id in analysis.reads.iter().chain(analysis.local_effects.iter()) {
        if let Some(&decl_idx) = declarer.get(id) {
            out.entry(decl_idx).or_default().insert(id.clone());
        }
    }
    for id in &analysis.declared {
        if let Some(mutation_items) = mutation_items_by_target.get(id) {
            for mutation_item in mutation_items {
                out.entry(*mutation_item).or_default().insert(id.clone());
            }
        }
    }
    out
}

#[derive(Debug, Clone)]
enum LiveReason {
    ResidualExport(Vec<String>),
    HardSideEffect,
    Dependency { from: usize, via: Vec<String> },
}

impl fmt::Display for LiveReason {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LiveReason::ResidualExport(aliases) => {
                write!(f, "residual export [{}]", aliases.join(","))
            }
            LiveReason::HardSideEffect => write!(f, "retained side effect"),
            LiveReason::Dependency { from: _, via } => write!(f, "reads [{}]", via.join(",")),
        }
    }
}

fn format_live_trace(
    start: usize,
    analyses: &[ItemAnalysis],
    items: &[ModuleItem],
    live_reasons: &[Option<LiveReason>],
) -> String {
    let mut node = start;
    let mut seen = BTreeSet::new();
    let mut trace = Vec::new();

    loop {
        if !seen.insert(node) {
            trace.push(format!("cycle at item {node}"));
            break;
        }

        trace.push(format_item_summary(node, &analyses[node], &items[node]));
        match live_reasons.get(node).and_then(|reason| reason.as_ref()) {
            Some(reason @ (LiveReason::ResidualExport(_) | LiveReason::HardSideEffect)) => {
                trace.push(reason.to_string());
                break;
            }
            Some(LiveReason::Dependency { from, via: _ }) => {
                trace.push(live_reasons[node].as_ref().unwrap().to_string());
                node = *from;
            }
            None => {
                trace.push("unknown liveness root".to_string());
                break;
            }
        }
    }

    trace.reverse();
    trace.join(" -> ")
}

fn format_item_summary(i: usize, analysis: &ItemAnalysis, item: &ModuleItem) -> String {
    let declared = analysis
        .declared
        .iter()
        .map(id_name)
        .collect::<Vec<_>>()
        .join(",");
    let exports = analysis
        .export_aliases
        .iter()
        .cloned()
        .collect::<Vec<_>>()
        .join(",");
    let local_effects = analysis
        .local_effects
        .iter()
        .map(id_name)
        .collect::<Vec<_>>()
        .join(",");
    let snippet = module_item_snippet(item);
    format!(
        "item {i} declared=[{declared}] exports=[{exports}] side_effect={:?} local_effects=[{local_effects}] snippet=`{snippet}`",
        analysis.side_effect,
    )
}

fn module_item_snippet(item: &ModuleItem) -> String {
    let module = Module {
        span: DUMMY_SP,
        body: vec![item.clone()],
        shebang: None,
    };
    let cm: Lrc<SourceMap> = Default::default();
    let mut buf = Vec::new();
    let emitted = {
        let writer = JsWriter::new(cm.clone(), "\n", &mut buf, None);
        let mut emitter = Emitter {
            cfg: Config::default(),
            cm,
            comments: None,
            wr: writer,
        };
        emitter.emit_module(&module)
    };
    if emitted.is_err() {
        return "<emit failed>".to_string();
    }
    let mut snippet = String::from_utf8_lossy(&buf)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    const MAX_LEN: usize = 240;
    if snippet.len() > MAX_LEN {
        snippet.truncate(MAX_LEN);
        snippet.push_str("...");
    }
    snippet
}

struct ItemAnalysis {
    declared: BTreeSet<Id>,
    reads: BTreeSet<Id>,
    local_effects: BTreeSet<Id>,
    export_aliases: BTreeSet<String>,
    side_effect: SideEffectKind,
    shareable_helper: bool,
    /// Outer-observable storage cells this statement writes at-init
    /// (binding rebinds and static-key `globalThis.<prop>` writes).
    /// Empty/incomplete unless `effects_summarizable` is true.
    /// Carries the WRITE-cell view (`cell_writes_summarizable`):
    /// the strip's call side effects are covered by its own
    /// island-reachability analysis, so the S-chain's stronger
    /// opaque-call bail does not apply here.
    observable_writes: BTreeSet<EffectCell>,
    /// False when the statement contains a shape the analyzer cannot
    /// statically summarize (dynamic `globalThis[expr]`, `with`,
    /// direct `eval`, `Function(...)`, etc.); in that case the write
    /// set is unreliable and the statement must be treated as touching
    /// every observable cell.
    effects_summarizable: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SideEffectKind {
    None,
    LocalMutation,
    Hard,
}

fn analyze_prune_items(module: &Module, chunk_path: &str) -> Result<Vec<ItemAnalysis>> {
    let hints = AnalysisHints {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisHints::default()
    };
    let facts = analyze_chunk(module, &hints, None, |_| None).facts;
    if facts.len() != module.body.len() {
        bail!(
            "strip_swapped_vendor_exports vendor entry {chunk_path}: analysis produced {} top-level facts for {} module items",
            facts.len(),
            module.body.len(),
        );
    }

    Ok(module
        .body
        .iter()
        .zip(facts.iter())
        .map(|(item, fact)| item_analysis_from_fact(item, fact))
        .collect())
}

fn item_analysis_from_fact(item: &ModuleItem, fact: &StatementFacts) -> ItemAnalysis {
    let mut reads = fact.reads.eager.clone();
    reads.extend(fact.reads.lazy.iter().cloned());
    reads.extend(fact.rebinds.eager.iter().cloned());
    reads.extend(fact.rebinds.lazy.iter().cloned());
    reads.extend(fact.calls.eager.iter().cloned());
    reads.extend(fact.calls.lazy.iter().cloned());
    for id in &fact.declared {
        reads.remove(id);
    }

    let side_effect = if module_linkage_item(item) || !fact.purity.is_pure() {
        SideEffectKind::Hard
    } else if !fact.local_effects.is_empty() {
        SideEffectKind::LocalMutation
    } else {
        SideEffectKind::None
    };

    ItemAnalysis {
        declared: fact.declared.clone(),
        reads,
        local_effects: fact.local_effects.clone(),
        export_aliases: export_aliases_for_item(item),
        side_effect,
        shareable_helper: item_is_shareable_helper(item),
        observable_writes: fact.effects().writes,
        effects_summarizable: fact.cell_writes_summarizable,
    }
}

fn module_linkage_item(item: &ModuleItem) -> bool {
    matches!(
        item,
        ModuleItem::ModuleDecl(ModuleDecl::Import(_))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(NamedExport { src: Some(_), .. }))
    )
}

fn export_aliases_for_item(item: &ModuleItem) -> BTreeSet<String> {
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            declaration_name_strings(&export_decl.decl)
                .into_iter()
                .collect()
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) if named.src.is_none() => {
            export_aliases_from_named(named)
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_))
        | ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(_)) => {
            BTreeSet::from(["default".to_string()])
        }
        _ => BTreeSet::new(),
    }
}

fn item_is_shareable_helper(item: &ModuleItem) -> bool {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_)))
        | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            decl: Decl::Fn(_), ..
        })) => true,
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var)))
        | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            decl: Decl::Var(var),
            ..
        })) => {
            is_shareable_literal_var(var)
                || is_shareable_function_var(var)
                || is_shareable_intrinsic_alias_var(var)
                || is_shareable_vite_map_deps_var(var)
                || is_shareable_global_object_fallback_var(var)
        }
        _ => false,
    }
}

fn item_is_vite_preload_dependency_cell(item: &ModuleItem) -> bool {
    let Some(var) = item_var_decl(item) else {
        return false;
    };
    is_shareable_literal_var(var) || is_shareable_function_var(var) || is_empty_object_var(var)
}

fn item_is_vite_preload_helper(item: &ModuleItem) -> bool {
    let Some(var) = item_var_decl(item) else {
        return false;
    };
    let [decl] = var.decls.as_slice() else {
        return false;
    };
    let Some(init) = decl.init.as_deref() else {
        return false;
    };
    let Some(body) = function_like_body(init) else {
        return false;
    };
    let mut probe = VitePreloadProbe::default();
    body.visit_with(&mut probe);
    probe.promise_all_settled && probe.document_create_element && probe.window_dispatch_event
}

fn item_var_decl(item: &ModuleItem) -> Option<&VarDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var)))
        | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            decl: Decl::Var(var),
            ..
        })) => Some(var),
        _ => None,
    }
}

fn function_like_body(expr: &Expr) -> Option<&BlockStmt> {
    match expr {
        Expr::Fn(function) => function.function.body.as_ref(),
        Expr::Arrow(arrow) => match arrow.body.as_ref() {
            BlockStmtOrExpr::BlockStmt(body) => Some(body),
            BlockStmtOrExpr::Expr(_) => None,
        },
        _ => None,
    }
}

#[derive(Default)]
struct VitePreloadProbe {
    promise_all_settled: bool,
    document_create_element: bool,
    window_dispatch_event: bool,
}

impl Visit for VitePreloadProbe {
    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee {
            self.promise_all_settled |= is_static_member_call(callee, "Promise", "allSettled");
            self.document_create_element |=
                is_static_member_call(callee, "document", "createElement");
            self.window_dispatch_event |= is_static_member_call(callee, "window", "dispatchEvent");
        }
        call.visit_children_with(self);
    }
}

fn static_member_name(prop: &MemberProp) -> Option<String> {
    match prop {
        MemberProp::Ident(ident) => Some(ident.sym.to_string()),
        MemberProp::PrivateName(name) => Some(name.name.to_string()),
        MemberProp::Computed(computed) => match &*computed.expr {
            Expr::Lit(Lit::Str(value)) => Some(value.value.to_string_lossy().into_owned()),
            _ => None,
        },
    }
}

fn export_aliases_from_named(named: &NamedExport) -> BTreeSet<String> {
    named
        .specifiers
        .iter()
        .filter_map(|spec| match spec {
            ExportSpecifier::Named(named_spec) => Some(
                named_spec
                    .exported
                    .as_ref()
                    .map(module_export_name)
                    .unwrap_or_else(|| module_export_name(&named_spec.orig)),
            ),
            _ => None,
        })
        .collect()
}

fn id_name(id: &Id) -> String {
    id.0.to_string()
}

fn is_shareable_intrinsic_alias_var(var: &VarDecl) -> bool {
    !var.decls.is_empty()
        && var.decls.iter().all(|decl| {
            matches!(&decl.name, Pat::Ident(_))
                && decl
                    .init
                    .as_deref()
                    .is_some_and(is_shareable_intrinsic_alias_expr)
        })
}

fn is_shareable_literal_var(var: &VarDecl) -> bool {
    !var.decls.is_empty()
        && var.decls.iter().all(|decl| {
            matches!(&decl.name, Pat::Ident(_))
                && decl.init.as_deref().is_some_and(is_primitive_literal_expr)
        })
}

fn is_shareable_function_var(var: &VarDecl) -> bool {
    !var.decls.is_empty()
        && var.decls.iter().all(|decl| {
            matches!(&decl.name, Pat::Ident(_))
                && matches!(
                    decl.init.as_deref(),
                    Some(Expr::Fn(_)) | Some(Expr::Arrow(_))
                )
        })
}

fn is_empty_object_var(var: &VarDecl) -> bool {
    !var.decls.is_empty() && var.decls.iter().all(|decl| {
        matches!(&decl.name, Pat::Ident(_))
            && matches!(decl.init.as_deref(), Some(Expr::Object(object)) if object.props.is_empty())
    })
}

fn is_primitive_literal_expr(expr: &Expr) -> bool {
    match expr {
        Expr::Paren(paren) => is_primitive_literal_expr(&paren.expr),
        Expr::Lit(
            Lit::Str(_)
            | Lit::Num(_)
            | Lit::Bool(_)
            | Lit::Null(_)
            | Lit::BigInt(_)
            | Lit::Regex(_),
        ) => true,
        _ => false,
    }
}

fn is_shareable_intrinsic_alias_expr(expr: &Expr) -> bool {
    match expr {
        Expr::Paren(paren) => is_shareable_intrinsic_alias_expr(&paren.expr),
        Expr::Ident(ident) => matches_global_intrinsic(ident.sym.as_ref()),
        Expr::Member(member) => {
            !member.prop.is_computed()
                && is_shareable_intrinsic_alias_expr(&member.obj)
                && static_member_name(&member.prop).is_some()
        }
        _ => false,
    }
}

fn is_shareable_vite_map_deps_var(var: &VarDecl) -> bool {
    let [decl] = var.decls.as_slice() else {
        return false;
    };
    let Pat::Ident(declared) = &decl.name else {
        return false;
    };
    let Some(Expr::Arrow(arrow)) = decl.init.as_deref() else {
        return false;
    };
    let Some(input_param) = arrow
        .params
        .first()
        .and_then(pat_ident_name)
        .map(str::to_owned)
    else {
        return false;
    };
    if !arrow_has_default_to_ident(arrow, declared.id.sym.as_ref()) {
        return false;
    }
    let Some((cache_param, callback_param)) = vite_map_deps_body_access(arrow, &input_param) else {
        return false;
    };
    arrow.params.iter().any(|param| {
        pat_ident_name(param)
            .is_some_and(|param_name| param_name == cache_param && param_name != callback_param)
    })
}

fn is_shareable_global_object_fallback_var(var: &VarDecl) -> bool {
    !var.decls.is_empty()
        && var.decls.iter().all(|decl| {
            matches!(&decl.name, Pat::Ident(_))
                && decl
                    .init
                    .as_deref()
                    .is_some_and(is_global_object_fallback_expr)
        })
}

fn is_global_object_fallback_expr(expr: &Expr) -> bool {
    match expr {
        Expr::Paren(paren) => is_global_object_fallback_expr(&paren.expr),
        Expr::Object(object) => object.props.is_empty(),
        Expr::Cond(cond) => {
            let Some(global_name) = defined_global_typeof_test(&cond.test) else {
                return false;
            };
            matches!(
                cond.cons.as_ref(),
                Expr::Ident(ident) if ident.sym.as_ref() == global_name
            ) && is_global_object_fallback_expr(&cond.alt)
        }
        _ => false,
    }
}

fn defined_global_typeof_test(expr: &Expr) -> Option<&str> {
    let Expr::Bin(bin) = expr else {
        return None;
    };
    let Expr::Unary(unary) = bin.left.as_ref() else {
        return None;
    };
    if unary.op != UnaryOp::TypeOf {
        return None;
    }
    let Expr::Ident(ident) = unary.arg.as_ref() else {
        return None;
    };
    let name = ident.sym.as_ref();
    if !matches!(name, "globalThis" | "window" | "global" | "self") {
        return None;
    }
    let Expr::Lit(Lit::Str(value)) = bin.right.as_ref() else {
        return None;
    };
    let literal = value.value.as_str();
    let is_defined_check = matches!(bin.op, BinaryOp::Lt | BinaryOp::NotEq | BinaryOp::NotEqEq)
        && matches!(literal, Some("u" | "undefined"));
    is_defined_check.then_some(name)
}

fn arrow_has_default_to_ident(arrow: &ArrowExpr, name: &str) -> bool {
    arrow.params.iter().any(|param| {
        let Pat::Assign(assign) = param else {
            return false;
        };
        matches!(
            assign.right.as_ref(),
            Expr::Ident(ident) if ident.sym.as_ref() == name
        )
    })
}

fn vite_map_deps_body_access(arrow: &ArrowExpr, input_param: &str) -> Option<(String, String)> {
    let BlockStmtOrExpr::Expr(body) = arrow.body.as_ref() else {
        return None;
    };
    let Expr::Call(call) = body.as_ref() else {
        return None;
    };
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    if !is_static_member_call(callee, input_param, "map") || call.args.len() != 1 {
        return None;
    }
    let Expr::Arrow(callback) = call.args.first()?.expr.as_ref() else {
        return None;
    };
    let [callback_param] = callback.params.as_slice() else {
        return None;
    };
    let callback_param = pat_ident_name(callback_param)?.to_owned();
    let BlockStmtOrExpr::Expr(callback_body) = callback.body.as_ref() else {
        return None;
    };
    let Expr::Member(member) = callback_body.as_ref() else {
        return None;
    };
    let Expr::Ident(cache_param) = member.obj.as_ref() else {
        return None;
    };
    let MemberProp::Computed(computed) = &member.prop else {
        return None;
    };
    if !matches!(
        computed.expr.as_ref(),
        Expr::Ident(index) if index.sym.as_ref() == callback_param
    ) {
        return None;
    }
    Some((cache_param.sym.to_string(), callback_param))
}

fn is_static_member_call(expr: &Expr, obj_name: &str, prop_name: &str) -> bool {
    let Expr::Member(member) = expr else {
        return false;
    };
    matches!(
        member.obj.as_ref(),
        Expr::Ident(obj) if obj.sym.as_ref() == obj_name
    ) && static_member_name(&member.prop).as_deref() == Some(prop_name)
}

fn pat_ident_name(pat: &Pat) -> Option<&str> {
    match pat {
        Pat::Ident(ident) => Some(ident.id.sym.as_ref()),
        Pat::Assign(assign) => pat_ident_name(&assign.left),
        _ => None,
    }
}

fn matches_global_intrinsic(name: &str) -> bool {
    matches!(
        name,
        "AggregateError"
            | "Array"
            | "ArrayBuffer"
            | "Atomics"
            | "BigInt"
            | "BigInt64Array"
            | "BigUint64Array"
            | "Boolean"
            | "DataView"
            | "Date"
            | "Error"
            | "EvalError"
            | "FinalizationRegistry"
            | "Float32Array"
            | "Float64Array"
            | "Function"
            | "Int8Array"
            | "Int16Array"
            | "Int32Array"
            | "Intl"
            | "JSON"
            | "Map"
            | "Math"
            | "Number"
            | "Object"
            | "Promise"
            | "Proxy"
            | "RangeError"
            | "ReferenceError"
            | "Reflect"
            | "RegExp"
            | "Set"
            | "SharedArrayBuffer"
            | "String"
            | "Symbol"
            | "SyntaxError"
            | "TypeError"
            | "URIError"
            | "Uint8Array"
            | "Uint8ClampedArray"
            | "Uint16Array"
            | "Uint32Array"
            | "WeakMap"
            | "WeakRef"
            | "WeakSet"
    )
}

#[cfg(test)]
mod tests {
    use spec::{PartialSwapKind, PartialSwapSymbol};
    use swc_common::sync::Lrc;
    use swc_common::{DUMMY_SP, FileName, SourceMap};
    use swc_ecma_ast::EsVersion;
    use swc_ecma_codegen::text_writer::JsWriter;
    use swc_ecma_codegen::{Config, Emitter};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    use super::*;

    fn parse(source: &str) -> Module {
        let cm: Lrc<SourceMap> = Default::default();
        let fm = cm.new_source_file(Lrc::new(FileName::Anon), source.to_string());
        let lexer = Lexer::new(
            Syntax::Es(Default::default()),
            EsVersion::latest(),
            StringInput::from(&*fm),
            None,
        );
        let mut parser = Parser::new_from(lexer);
        parser.parse_module().expect("parse")
    }

    fn emit(module: &Module) -> String {
        let cm: Lrc<SourceMap> = Default::default();
        let mut buf = Vec::new();
        {
            let writer = JsWriter::new(cm.clone(), "\n", &mut buf, None);
            let mut emitter = Emitter {
                cfg: Config::default(),
                cm,
                comments: None,
                wr: writer,
            };
            emitter.emit_module(module).expect("emit");
        }
        String::from_utf8(buf).expect("utf8")
    }

    fn mk_symbols(swapped: &[&str]) -> BTreeMap<String, PartialSwapSymbol> {
        let mut symbols = BTreeMap::new();
        for s in swapped {
            symbols.insert(
                (*s).to_string(),
                PartialSwapSymbol {
                    package: "pkg".to_string(),
                    kind: PartialSwapKind::Named,
                    upstream_export: Some((*s).to_string()),
                    local: None,
                },
            );
        }
        symbols
    }

    fn mk_symbols_with_packages(swapped: &[(&str, &str)]) -> BTreeMap<String, PartialSwapSymbol> {
        let mut symbols = BTreeMap::new();
        for (name, package) in swapped {
            symbols.insert(
                (*name).to_string(),
                PartialSwapSymbol {
                    package: (*package).to_string(),
                    kind: PartialSwapKind::Named,
                    upstream_export: Some((*name).to_string()),
                    local: None,
                },
            );
        }
        symbols
    }

    fn id(name: &str) -> Id {
        Ident::new_no_ctxt(name.into(), DUMMY_SP).to_id()
    }

    #[test]
    fn strips_named_export_specifier() {
        let mut module = parse("const a = 1;\nconst b = 2;\nexport { a as foo, b as bar };\n");
        let stats = strip_one_chunk(&mut module, &mk_symbols(&["foo"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(!emitted.contains("foo"), "stripped name leaked:\n{emitted}");
        assert!(emitted.contains("bar"), "kept name missing:\n{emitted}");
        assert_eq!(stats.stripped_export_specifiers, 1);
    }

    #[test]
    fn drops_inline_export_decl_and_dce_kills_pure_body() {
        let mut module = parse("export const e6 = () => true;\nexport const k = 7;\n");
        strip_one_chunk(&mut module, &mk_symbols(&["e6"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("e6"),
            "swapped const should be DCE'd:\n{emitted}",
        );
        assert!(
            emitted.contains("export const k"),
            "non-swapped const dropped:\n{emitted}",
        );
    }

    #[test]
    fn bails_when_swapped_implementation_is_residually_reachable() {
        let mut module = parse(
            "class ZodObject {}\nconst object = ()=>new ZodObject();\nexport { object as o, ZodObject as Z };\n",
        );
        let err = strip_one_chunk(&mut module, &mk_symbols(&["o"]), "chunk.js")
            .expect_err("split-brain residual reachability should fail");
        assert!(
            err.to_string().contains("split-brain vendor swap"),
            "wrong error: {err}",
        );
        assert!(
            err.to_string().contains("residual path:"),
            "split-brain diagnostic should include liveness provenance: {err}",
        );
    }

    #[test]
    fn drops_non_exported_local_swap_after_self_rewrite() {
        let mut module = parse(
            "function nY(t) { return `vendor:${t.name}`; }\nconst schema = Zod.instanceof(URL);\nexport { schema };\n",
        );
        let symbols = BTreeMap::from([(
            "zodInstanceof".to_string(),
            PartialSwapSymbol {
                package: "zod".to_string(),
                kind: PartialSwapKind::Named,
                upstream_export: Some("instanceof".to_string()),
                local: Some("nY".to_string()),
            },
        )]);

        strip_one_chunk(&mut module, &symbols, "chunk.js").unwrap();

        let emitted = emit(&module);
        assert!(
            !emitted.contains("function nY"),
            "chunk-local swapped helper should be DCE'd:\n{emitted}",
        );
        assert!(
            emitted.contains("schema"),
            "residual schema export should remain:\n{emitted}",
        );
    }

    #[test]
    fn local_swap_split_brain_reports_unrewritten_residual_call() {
        let mut module = parse(
            "function nY(t) { return `vendor:${t.name}`; }\nconst schema = nY(URL);\nexport { schema };\n",
        );
        let symbols = BTreeMap::from([(
            "zodInstanceof".to_string(),
            PartialSwapSymbol {
                package: "zod".to_string(),
                kind: PartialSwapKind::Named,
                upstream_export: Some("instanceof".to_string()),
                local: Some("nY".to_string()),
            },
        )]);

        let err = strip_one_chunk(&mut module, &symbols, "chunk.js")
            .expect_err("unrewritten local call should be split-brain");

        assert!(
            err.to_string().contains("split-brain vendor swap"),
            "wrong error: {err}",
        );
        assert!(
            err.to_string().contains("reads [nY]"),
            "diagnostic should show the residual read of the local helper: {err}",
        );
    }

    #[test]
    fn allows_shared_pure_function_helper() {
        let mut module = parse(
            "function helper(x) { return x; }\nconst oldImpl = () => helper(\"old\");\nconst keep = () => helper(\"keep\");\nexport { oldImpl as swapped, keep };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("function helper"),
            "residual export should keep shared helper:\n{emitted}",
        );
        assert!(
            emitted.contains("keep"),
            "residual export should remain:\n{emitted}",
        );
        assert!(
            !emitted.contains("oldImpl"),
            "swapped old implementation should be removed:\n{emitted}",
        );
    }

    #[test]
    fn allows_shared_pure_function_expression_helper() {
        let mut module = parse(
            "const helper = function(x) { return x; };\nconst oldImpl = () => helper(\"old\");\nconst keep = () => helper(\"keep\");\nexport { oldImpl as swapped, keep };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("function"),
            "residual export should keep shared function-expression helper:\n{emitted}",
        );
        assert!(
            !emitted.contains("oldImpl"),
            "swapped old implementation should be removed:\n{emitted}",
        );
    }

    #[test]
    fn allows_shared_intrinsic_alias_helper() {
        let mut module = parse(
            "const assign = Object.assign;\nconst oldImpl = () => assign({}, { old: true });\nconst keep = () => assign({}, { keep: true });\nexport { oldImpl as swapped, keep };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("Object.assign"),
            "residual export should keep intrinsic alias:\n{emitted}",
        );
        assert!(
            !emitted.contains("oldImpl"),
            "swapped old implementation should be removed:\n{emitted}",
        );
    }

    #[test]
    fn allows_shared_primitive_literal_helper() {
        let mut module = parse(
            "const preloadRel = \"modulepreload\";\nconst oldImpl = () => preloadRel;\nconst keep = () => preloadRel;\nexport { oldImpl as swapped, keep };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("modulepreload"),
            "residual export should keep the inert shared literal:\n{emitted}",
        );
        assert!(
            !emitted.contains("oldImpl"),
            "swapped old implementation should be removed:\n{emitted}",
        );
    }

    #[test]
    fn allows_shared_vite_dependency_map_helper() {
        let mut module = parse(
            "const mapDeps = (i, m = mapDeps, d = m.f || (m.f = [\"a.js\", \"b.js\"])) => i.map((i) => d[i]);\n\
             const oldImpl = () => mapDeps([0]);\n\
             const keep = () => mapDeps([1]);\n\
             export { oldImpl as swapped, keep };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("mapDeps"),
            "residual export should keep the shared runtime helper:\n{emitted}",
        );
        assert!(
            !emitted.contains("oldImpl"),
            "swapped old implementation should be removed:\n{emitted}",
        );
    }

    #[test]
    fn allows_shared_vite_preload_helper_cluster() {
        let mut module = parse(
            "const rel = \"modulepreload\";\n\
             const base = function(path) { return \"/\" + path; };\n\
             const seen = {};\n\
             const preload = function(load, deps) {\n\
                 let promise = Promise.resolve();\n\
                 if (deps && deps.length > 0) {\n\
                     promise = Promise.allSettled(deps.map((dep) => {\n\
                         dep = base(dep);\n\
                         if (dep in seen) return;\n\
                         seen[dep] = true;\n\
                         const link = document.createElement(\"link\");\n\
                         link.rel = rel;\n\
                     }));\n\
                 }\n\
                 function onError(error) { window.dispatchEvent(error); throw error; }\n\
                 return promise.then(() => load().catch(onError));\n\
             };\n\
             const oldImpl = () => preload(() => Promise.resolve(\"old\"), [\"old.js\"]);\n\
             const keep = () => preload(() => Promise.resolve(\"keep\"), [\"keep.js\"]);\n\
             export { oldImpl as swapped, keep };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("modulepreload") && emitted.contains("seen"),
            "residual export should keep the shared Vite preload helper cluster:\n{emitted}",
        );
        assert!(
            !emitted.contains("oldImpl"),
            "swapped old implementation should be removed:\n{emitted}",
        );
    }

    #[test]
    fn allows_multi_package_shared_dependency_cell() {
        let mut module = parse(
            "const shared = {};\nconst oldA = () => shared;\nconst oldB = () => shared;\nconst keep = () => shared;\nexport { oldA as swappedA, oldB as swappedB, keep };\n",
        );
        strip_one_chunk(
            &mut module,
            &mk_symbols_with_packages(&[("swappedA", "pkg-a"), ("swappedB", "pkg-b")]),
            "chunk.js",
        )
        .unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("shared"),
            "residual export should keep shared dependency cell:\n{emitted}",
        );
        assert!(
            !emitted.contains("oldA") && !emitted.contains("oldB"),
            "swapped package roots should be removed:\n{emitted}",
        );
    }

    #[test]
    fn retains_side_effect_init_among_swapped() {
        let mut module = parse("console.log(\"keep\");\nexport const e6 = ()=>true;\n");
        strip_one_chunk(&mut module, &mk_symbols(&["e6"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("console.log"),
            "side-effect should be retained:\n{emitted}",
        );
    }

    #[test]
    fn drops_side_effect_dependent_on_single_swapped_package_island() {
        let mut module = parse(
            "const internals = {};\n\
             const oldImpl = () => internals;\n\
             if (globalThis.__HOOK__) globalThis.__HOOK__.inject({ internals });\n\
             export { oldImpl as swapped };\n\
             export const keep = 1;\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("__HOOK__"),
            "hard side effect that only touches the swapped island should be dropped:\n{emitted}",
        );
        assert!(
            emitted.contains("export const keep"),
            "residual export should remain:\n{emitted}",
        );
    }

    #[test]
    fn drops_private_side_effect_chain_for_single_swapped_package_island() {
        let mut module = parse(
            "const table = {};\n\
             const internals = {};\n\
             function register(name) { table[name] = internals; }\n\
             for (var i = 0; i < 2; i++) register(String(i));\n\
             const oldImpl = () => internals;\n\
             export { oldImpl as swapped };\n\
             export const keep = 1;\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("register")
                && !emitted.contains("table")
                && !emitted.contains("internals"),
            "private registration chain should be dropped with the swapped island:\n{emitted}",
        );
        assert!(
            emitted.contains("export const keep"),
            "residual export should remain:\n{emitted}",
        );
    }

    #[test]
    fn drops_local_member_writes_in_swapped_island() {
        let mut module = parse(
            "class Widget {}\nWidget.displayName = \"Widget\";\nconst make = () => Widget;\nexport { make as swapped };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("Widget"),
            "swapped implementation island should be removed:\n{emitted}",
        );
        assert!(
            !emitted.contains("displayName"),
            "local class metadata write should be removed with the class:\n{emitted}",
        );
    }

    #[test]
    fn drops_local_object_freeze_in_swapped_island() {
        let mut module = parse(
            "const EMPTY = {};\nObject.freeze(EMPTY);\nconst make = () => EMPTY;\nexport { make as swapped };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("EMPTY") && !emitted.contains("freeze"),
            "local freeze should be dropped with its target:\n{emitted}",
        );
    }

    #[test]
    fn drops_intrinsic_assign_mutation_in_swapped_island() {
        let mut module = parse(
            "const tag = \"computed\";\nconst decorator = make(tag);\nconst computed = () => decorator;\nObject.assign(computed, decorator);\ncomputed.struct = wrap(decorator);\nexport { computed as swapped };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("computed")
                && !emitted.contains("decorator")
                && !emitted.contains("Object.assign"),
            "intrinsic local mutations should be dropped with the swapped target:\n{emitted}",
        );
    }

    #[test]
    fn drops_var_init_prototype_mutation_in_swapped_island() {
        let mut module = parse(
            "function Base() {}\nfunction Derived() {}\nvar proto = (Derived.prototype = new Base());\nconst make = () => Derived;\nexport { make as swapped };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("Derived")
                && !emitted.contains("prototype")
                && !emitted.contains("Base"),
            "prototype inheritance initializer should be dropped with the swapped target:\n{emitted}",
        );
    }

    #[test]
    fn drops_local_binding_write_in_swapped_island() {
        let mut module = parse(
            "let assigned;\nconst source = { value: 1 };\nassigned = source.value;\nconst make = () => assigned;\nexport { make as swapped };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("assigned") && !emitted.contains("source"),
            "local binding assignment should be dropped with the swapped island:\n{emitted}",
        );
    }

    #[test]
    fn drops_commonjs_module_iife_in_swapped_island() {
        let mut module = parse(
            "var module = { exports: {} };\n(function (target) { (function () { var has = {}.hasOwnProperty; function clsx() {} target.exports ? ((clsx.default = clsx), (target.exports = clsx)) : (window.classNames = clsx); })(); })(module);\nvar clsx = module.exports;\nconst make = () => clsx;\nexport { make as swapped };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("module")
                && !emitted.contains("clsx")
                && !emitted.contains("classNames"),
            "CommonJS module wrapper should be dropped with the swapped island:\n{emitted}",
        );
    }

    #[test]
    fn drops_commonjs_iife_with_shared_global_fallback_dependency() {
        let mut module = parse(
            "var root = typeof globalThis < \"u\" ? globalThis : typeof window < \"u\" ? window : typeof global < \"u\" ? global : typeof self < \"u\" ? self : {};\n\
             var module = { exports: {} };\n\
             (function (target) { (function (global, factory) { target.exports = factory(); })(root, function () { function dayjs() {} return dayjs; }); })(module);\n\
             var dayjs = module.exports;\n\
             const keep = root;\n\
             export { dayjs as swapped, keep };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("globalThis") && emitted.contains("keep"),
            "residual global fallback helper should remain:\n{emitted}",
        );
        assert!(
            !emitted.contains("dayjs")
                && !emitted.contains("factory")
                && !emitted.contains("module"),
            "CommonJS wrapper should be dropped even when it reads the shared global fallback:\n{emitted}",
        );
    }

    #[test]
    fn drops_object_iteration_prototype_mutation_in_swapped_island() {
        let mut module = parse(
            "const define = Object.defineProperty;\nvar methods = { clear: function () { return this.splice(0); } };\nfunction ObservableArray() {}\nObject.entries(methods).forEach(function (entry) { var key = entry[0], value = entry[1]; key !== \"concat\" && define(ObservableArray.prototype, key, value); });\nconst make = () => ObservableArray;\nexport { make as swapped };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("ObservableArray")
                && !emitted.contains("Object.entries")
                && !emitted.contains("methods"),
            "object-iteration prototype mutation should be dropped with the swapped target:\n{emitted}",
        );
    }

    #[test]
    fn drops_object_iteration_wrapper_prototype_mutation_in_swapped_island() {
        let mut module = parse(
            "function define(target, key, value) { Object.defineProperty(target, key, { configurable: true, value }); }\nvar methods = { clear: function () { return this.splice(0); } };\nfunction ObservableArray() {}\nObject.entries(methods).forEach(function (entry) { var key = entry[0], value = entry[1]; key !== \"concat\" && define(ObservableArray.prototype, key, value); });\nconst make = () => ObservableArray;\nexport { make as swapped };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("ObservableArray")
                && !emitted.contains("Object.entries")
                && !emitted.contains("methods"),
            "object-iteration wrapper mutation should be dropped with the swapped target:\n{emitted}",
        );
    }

    #[test]
    fn drops_mutation_targeting_swapped_import_rewrite() {
        let mut module = parse(
            "import __debundle_bps_swapped from \"./vendor/swapped.js\";\nconst tag = \"computed\";\nconst decorator = makeDecorator(tag);\nfunction swapped() { return decorator; }\nObject.assign(__debundle_bps_swapped, decorator);\nexport { swapped };\n",
        );
        let replacement_import_locals = BTreeSet::from([id("__debundle_bps_swapped")]);
        strip_one_chunk_with_replacement_imports(
            &mut module,
            &mk_symbols(&["swapped"]),
            "chunk.js",
            &replacement_import_locals,
        )
        .unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("makeDecorator")
                && !emitted.contains("Object.assign")
                && !emitted.contains("tag"),
            "mutation of the imported swapped facade should not keep the old implementation:\n{emitted}",
        );
    }

    #[test]
    fn allows_residual_use_of_replacement_facade() {
        let mut module = parse(
            "import __debundle_bps_react_default from \"./vendor/react.js\";\nimport __debundle_bps_react_ns from \"./vendor/react.js\";\nfunction merge(ns, extras) { return ns; }\nconst ReactFacade = merge({ __proto__: null, default: __debundle_bps_react_default }, [__debundle_bps_react_ns]);\nconst oldImpl = () => ReactFacade;\nconst residualHook = ReactFacade.useInsertionEffect ? ReactFacade.useInsertionEffect : false;\nexport { oldImpl as swapped, residualHook };\n",
        );
        let replacement_import_locals = BTreeSet::from([
            id("__debundle_bps_react_default"),
            id("__debundle_bps_react_ns"),
        ]);
        strip_one_chunk_with_replacement_imports(
            &mut module,
            &mk_symbols(&["swapped"]),
            "chunk.js",
            &replacement_import_locals,
        )
        .unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("ReactFacade") && emitted.contains("residualHook"),
            "residual use of replacement facade should remain:\n{emitted}",
        );
        assert!(
            !emitted.contains("oldImpl"),
            "swapped original facade user should be removed:\n{emitted}",
        );
    }

    #[test]
    fn drops_local_namespace_iife_in_swapped_island() {
        let mut module = parse(
            "var ns = {};\n(function (target) { target.reject = wrap(\"reject\"); function resolve() {} target.resolve = wrap(resolve); })(ns || (ns = {}));\nconst make = () => ns;\nexport { make as swapped };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("reject") && !emitted.contains("resolve") && !emitted.contains("ns"),
            "local namespace augmentation should be dropped with its target:\n{emitted}",
        );
    }

    #[test]
    fn retains_local_namespace_iife_for_residual_export() {
        let mut module = parse(
            "var ns = {};\n(function (target) { target.reject = wrap(\"reject\"); function resolve() {} target.resolve = wrap(resolve); })(ns || (ns = {}));\nexport { ns as keep };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&[]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("reject") && emitted.contains("resolve"),
            "residual namespace export should keep augmentation:\n{emitted}",
        );
    }

    #[test]
    fn bails_when_swapped_name_not_locally_exported() {
        let mut module = parse("export { stuff } from \"./peer.js\";\n");
        let err = strip_one_chunk(&mut module, &mk_symbols(&["stuff"]), "chunk.js")
            .expect_err("should fail");
        assert!(
            err.to_string()
                .contains("not found in any chunk-local export"),
            "wrong error: {err}",
        );
    }

    #[test]
    fn call_init_classifies_as_side_effect() {
        let module = parse("const a = sideEffect();\n");
        let analyses = analyze_prune_items(&module, "chunk.js").unwrap();
        let an = &analyses[0];
        assert!(
            an.side_effect == SideEffectKind::Hard,
            "call init should be a side-effect anchor"
        );
        assert_eq!(
            an.declared.iter().map(id_name).collect::<Vec<_>>(),
            vec!["a".to_string()]
        );
        assert!(an.reads.iter().any(|id| id_name(id) == "sideEffect"));
    }

    #[test]
    fn pure_object_literal_init_is_not_side_effect() {
        let module = parse("const a = { x: 1 };\n");
        let analyses = analyze_prune_items(&module, "chunk.js").unwrap();
        let an = &analyses[0];
        assert!(
            an.side_effect == SideEffectKind::None,
            "object literal init should be a pure decl",
        );
    }
}
