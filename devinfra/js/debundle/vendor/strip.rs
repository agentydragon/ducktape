use std::collections::{BTreeMap, BTreeSet};

use analysis::EffectCell;
use anyhow::{Result, bail};
use binding_targets::{declaration_ids, declaration_name_strings, public_export_name};
use js_ast::{GlobalUseElisions, ParsedGlobalUses, VarDeclShape};
use swc_common::{BytePos, DUMMY_SP, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use spec::vendor_marks::PartialSwapSymbol;

mod item_analysis;
mod shareable;
#[cfg(test)]
mod tests;
mod trace;

use item_analysis::{
    ExposedBindings, HostCalls, ItemAnalysis, RegionStep, SideEffectKind, analyze_prune_items,
    id_name, item_declares_code_or_primitive, item_declares_native_container,
    item_is_vite_preload_dependency_cell, item_is_vite_preload_helper, run_first_order_body,
};
use shareable::matches_global_intrinsic;
use trace::{
    LiveReason, SwappedReason, format_effect_cell, format_live_trace, format_swapped_trace,
    module_item_snippet,
};

#[cfg(test)]
fn strip_one_chunk(
    module: &mut Module,
    symbols: &BTreeMap<String, PartialSwapSymbol>,
    chunk_path: &str,
) -> Result<()> {
    strip_one_chunk_with_replacement_imports(module, symbols, chunk_path, &BTreeSet::new(), None)
        .map(|_| ())
}

/// Residual strip for one partially-swapped vendor entry: drops
/// swapped names from the entry's `export { … }` surface (Phase 1) and
/// sweeps top-level bindings no longer reachable from the residual
/// export surface plus retained side-effect statements (Phase 2).
/// Runs as the final step of the vendor residual composition
/// (`vendor::emission::emit_vendor_residual`) — every consumer already
/// imports each swapped name from upstream, so the entry's residual
/// `export { … }` entries for those names are dead weight.
pub(crate) fn strip_one_chunk_with_replacement_imports(
    module: &mut Module,
    symbols: &BTreeMap<String, PartialSwapSymbol>,
    chunk_path: &str,
    replacement_import_locals: &BTreeSet<Id>,
    global_uses: Option<&ParsedGlobalUses>,
) -> Result<GlobalUseElisions> {
    let swapped: BTreeSet<String> = symbols.keys().cloned().collect();
    let mut removed_global_uses = RemovedGlobalUseRecorder::new(global_uses);

    split_top_level_var_decls(module);
    let stripped = strip_export_specifiers(module, symbols, chunk_path, &mut removed_global_uses)?;
    let post_strip_exports = super::export_surface::collect_exported_names(module);

    sweep_unreachable_top_level(
        module,
        &post_strip_exports,
        &stripped,
        chunk_path,
        replacement_import_locals,
        &mut removed_global_uses,
    )?;

    // Phase 2 must not change the export surface relative to Phase 1.
    let post_dce_exports = super::export_surface::collect_exported_names(module);
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

    Ok(removed_global_uses.uses)
}

/// Mutation bookkeeping only: visit nodes the strip actually removes and
/// intersect their identifier sites with the same chunk's prepared-parse
/// inventory. This does not classify names or re-derive source uses. Sites
/// match by span and spelling, so an AST re-parsed from the same bytes (with
/// fresh hygiene marks) still lines up with the prepared parse.
struct RemovedGlobalUseRecorder<'a> {
    sites: BTreeMap<(BytePos, BytePos), &'a Id>,
    uses: GlobalUseElisions,
}

impl<'a> RemovedGlobalUseRecorder<'a> {
    fn new(manifest: Option<&'a ParsedGlobalUses>) -> Self {
        let sites = manifest
            .into_iter()
            .flat_map(|manifest| &manifest.uses)
            .flat_map(|(id, spans)| spans.iter().map(move |span| ((span.lo, span.hi), id)))
            .collect();
        Self {
            sites,
            uses: GlobalUseElisions::default(),
        }
    }

    fn record<T: VisitWith<Self>>(&mut self, removed_node: &T) {
        if !self.sites.is_empty() {
            removed_node.visit_with(self);
        }
    }
}

impl Visit for RemovedGlobalUseRecorder<'_> {
    fn visit_ident(&mut self, ident: &Ident) {
        let Some(&id) = self.sites.get(&(ident.span.lo, ident.span.hi)) else {
            return;
        };
        if id.0 == ident.sym {
            let spans = self.uses.uses.entry(id.clone()).or_default();
            if !spans.contains(&ident.span) {
                spans.push(ident.span);
            }
        }
    }
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
    removed_global_uses: &mut RemovedGlobalUseRecorder<'_>,
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
                    let exported = public_export_name(named_spec);
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
                        removed_global_uses.record(&spec);
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
                let shape = VarDeclShape::of(&var);
                for decl in var.decls {
                    let single = shape.with_decls(shape.span, vec![decl]);
                    out.push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(single)))));
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => match export_decl.decl {
                Decl::Var(var) if var.decls.len() > 1 => {
                    let shape = VarDeclShape::of(&var);
                    for decl in var.decls {
                        out.push(ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                            span: export_decl.span,
                            decl: Decl::Var(Box::new(shape.with_decls(shape.span, vec![decl]))),
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
    removed_global_uses: &mut RemovedGlobalUseRecorder<'_>,
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
    let sweep = Sweep {
        body: &module.body,
        analyses,
        declarer,
        mutation_items_by_target,
        replacement_import_locals,
        chunk_path,
    };
    let swapped = sweep.swapped_reachability(stripped, live_exports, &shareable_items)?;
    let (live, live_reasons) = sweep.live_items(live_exports, &swapped);
    sweep.check_split_brain(&swapped, &shareable_items, &live, &live_reasons)?;
    sweep.check_live_reads(&live)?;
    sweep.check_swap_private_effects(&swapped, &live)?;

    let mut retained = Vec::with_capacity(module.body.len());
    for (item, &is_live) in std::mem::take(&mut module.body)
        .into_iter()
        .zip(live.iter())
    {
        if is_live {
            retained.push(item);
        } else {
            removed_global_uses.record(&item);
        }
    }
    module.body = retained;
    Ok(())
}

/// What every phase of `sweep_unreachable_top_level` reads.
struct Sweep<'a> {
    body: &'a [ModuleItem],
    analyses: Vec<ItemAnalysis>,
    /// Binding id -> index of the item that declares it.
    declarer: BTreeMap<Id, usize>,
    mutation_items_by_target: BTreeMap<Id, Vec<usize>>,
    replacement_import_locals: &'a BTreeSet<Id>,
    chunk_path: &'a str,
}

/// Per item, the swapped packages that reach it, and why.
struct Swapped {
    reachability: Vec<BTreeSet<String>>,
    reasons: BTreeMap<(usize, String), SwappedReason>,
    root_items: BTreeSet<usize>,
}

impl Sweep<'_> {
    fn swapped_reachability(
        &self,
        stripped: &BTreeMap<String, StrippedExport>,
        live_exports: &BTreeSet<String>,
        shareable_items: &[bool],
    ) -> Result<Swapped> {
        let Self {
            analyses,
            declarer,
            mutation_items_by_target,
            chunk_path,
            ..
        } = self;
        let mut swapped_reachability = vec![BTreeSet::new(); analyses.len()];
        let mut swapped_reasons = BTreeMap::new();
        let mut swapped_root_items = BTreeSet::new();
        let mut queue: Vec<(usize, String)> = Vec::new();
        for (alias, stripped_export) in stripped {
            for local in &stripped_export.locals {
                let decl_idx = resolve_declared_local(local, declarer, chunk_path, alias)?;
                swapped_root_items.insert(decl_idx);
                if swapped_reachability[decl_idx].insert(stripped_export.package.clone()) {
                    swapped_reasons.insert(
                        (decl_idx, stripped_export.package.clone()),
                        SwappedReason::Export(alias.clone()),
                    );
                    queue.push((decl_idx, stripped_export.package.clone()));
                }
            }
        }

        propagate_swapped_dependency_reachability(
            &mut swapped_reachability,
            &mut swapped_reasons,
            &mut queue,
            analyses,
            declarer,
            mutation_items_by_target,
        );
        let residual_export_closure = residual_export_dependency_closure(
            analyses,
            live_exports,
            declarer,
            mutation_items_by_target,
        );
        absorb_swapped_dependent_items(
            &mut swapped_reachability,
            &mut swapped_reasons,
            analyses,
            declarer,
            mutation_items_by_target,
            &residual_export_closure,
            shareable_items,
        );
        Ok(Swapped {
            reachability: swapped_reachability,
            reasons: swapped_reasons,
            root_items: swapped_root_items,
        })
    }

    /// An item carries a globally-observable side effect when it is either a
    /// `Hard` statement (impure / module linkage) or a `LocalMutation` that
    /// writes a target which is neither a chunk-local declaration nor a
    /// replacement-import local — i.e. the write lands on a global / external
    /// object whose post-strip value residual or external code may read.
    fn observable_side_effect(&self, i: usize) -> bool {
        let Self {
            analyses,
            declarer,
            replacement_import_locals,
            ..
        } = self;
        let an = &analyses[i];
        an.side_effect == SideEffectKind::Hard
            || (an.side_effect == SideEffectKind::LocalMutation
                && an.local_effects.iter().any(|id| {
                    !declarer.contains_key(id) && !replacement_import_locals.contains(id)
                }))
    }

    /// Returns `(live, live_reasons)`: residual exports and hard side effects
    /// not reachable from a swapped package, closed over dependencies.
    fn live_items(
        &self,
        live_exports: &BTreeSet<String>,
        swapped: &Swapped,
    ) -> (Vec<bool>, Vec<Option<LiveReason>>) {
        let Self {
            analyses,
            declarer,
            mutation_items_by_target,
            ..
        } = self;
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
            let hard_side_effect =
                self.observable_side_effect(i) && swapped.reachability[i].is_empty();
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
            for (dep_idx, via) in dependency_edges(&analyses[i], declarer, mutation_items_by_target)
            {
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
        (live, live_reasons)
    }

    fn check_split_brain(
        &self,
        swapped: &Swapped,
        shareable_items: &[bool],
        live: &[bool],
        live_reasons: &[Option<LiveReason>],
    ) -> Result<()> {
        let Self {
            body,
            analyses,
            chunk_path,
            ..
        } = self;
        for (i, packages) in swapped.reachability.iter().enumerate() {
            if live[i]
                && packages.len() == 1
                && (!shareable_items[i] || swapped.root_items.contains(&i))
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
                let swapped_paths = packages
                    .iter()
                    .map(|package| {
                        format_swapped_trace(i, package, analyses, body, &swapped.reasons)
                    })
                    .collect::<Vec<_>>()
                    .join("; ");
                let packages = packages.iter().cloned().collect::<Vec<_>>().join(",");
                let residual_path = format_live_trace(i, analyses, body, live_reasons);
                bail!(
                    "strip_swapped_vendor_exports vendor entry {chunk_path}: split-brain vendor swap: top-level item {i} remains reachable from the residual chunk while also belonging to swapped package(s) [{packages}] (declared=[{declared}], exports=[{exports}]); swapped path: {swapped_paths}; residual path: {residual_path}",
                );
            }
        }
        Ok(())
    }

    fn check_live_reads(&self, live: &[bool]) -> Result<()> {
        let Self {
            analyses,
            declarer,
            chunk_path,
            ..
        } = self;
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
        Ok(())
    }

    fn check_swap_private_effects(&self, swapped: &Swapped, live: &[bool]) -> Result<()> {
        let Self {
            body,
            analyses,
            declarer,
            replacement_import_locals,
            chunk_path,
            ..
        } = self;
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
        //   * an array or Set content write is private when that allocation is
        //     born in a dropped item: the residual never creates it, and any live
        //     path to it would cross a binding, global or content cell written by
        //     a dropped item, which the other cases and the soundness gate above
        //     check;
        //   * a statement the analyzer cannot summarize (dynamic `globalThis[expr]`,
        //     `eval`, `with`, `Function(...)`) may touch any cell and is never
        //     private;
        //   * the cell summary covers the statement's own writes, not its
        //     callees' in-place mutations (`registry[name] = deps` inside a called
        //     function). A callee can only mutate state rooted at a binding it or
        //     the statement reaches, so the effect is private only when none of
        //     those bindings is retained state (`shared_roots`). External callees
        //     see only what the statement hands them; replacing the package
        //     promises their package-side effects.
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
        // Allocation origins are the allocation node's `span.lo`.
        let born_in_dropped_item = |origin: u32| -> bool {
            body.iter().zip(live).any(|(item, &is_live)| {
                let span = item.span();
                !is_live && span.lo.0 <= origin && origin < span.hi.0
            })
        };
        let private_cell = |cell: &EffectCell| -> bool {
            match cell {
                EffectCell::GlobalProp(_) => false,
                EffectCell::ArrayContents(origin) | EffectCell::SetContents(origin) => {
                    born_in_dropped_item(*origin)
                }
                // Property origins can be shared by repeated factory allocations;
                // source containment alone does not prove their results private.
                EffectCell::OwnProperty { .. } | EffectCell::ObjectShape(_) => false,
                EffectCell::Binding(id) => {
                    replacement_import_locals.contains(id) || !live_reads.contains(id)
                }
            }
        };
        let rebound: BTreeSet<&Id> = analyses.iter().flat_map(|an| &an.rebinds).collect();
        // Items whose code mutates the object a binding holds in place.
        let mut writers: BTreeMap<Id, Vec<usize>> = BTreeMap::new();
        for (j, item) in body.iter().enumerate() {
            let mut exposed = ExposedBindings::default();
            item.visit_with(&mut exposed);
            for root in exposed.mutated {
                writers.entry(root).or_default().push(j);
            }
        }
        let swap_private_effect = |i: usize| -> bool {
            let an = &analyses[i];
            an.effects_summarizable
                && an.observable_writes.iter().all(private_cell)
                && self.shared_roots(i, live, &rebound, &writers).is_empty()
        };
        // Every refusal of the pass, so one run shows them all.
        let mut refusals = Vec::new();
        for i in 0..analyses.len() {
            if live[i]
                || swapped.reachability[i].is_empty()
                || !self.observable_side_effect(i)
                || swap_private_effect(i)
            {
                continue;
            }
            let packages = swapped.reachability[i]
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
            let public_writes = if analyses[i].effects_summarizable {
                analyses[i]
                    .observable_writes
                    .iter()
                    .filter(|cell| !private_cell(cell))
                    .map(format_effect_cell)
                    .collect::<Vec<_>>()
                    .join(", ")
            } else {
                "unsummarizable".to_string()
            };
            let shared = self
                .shared_roots(i, live, &rebound, &writers)
                .iter()
                .map(|(id, exposure)| {
                    format!(
                        "`{}` (declared by retained item {}, {exposure})",
                        id_name(id),
                        declarer[id]
                    )
                })
                .collect::<Vec<_>>()
                .join(", ");
            let swapped_paths = swapped.reachability[i]
                .iter()
                .map(|package| format_swapped_trace(i, package, analyses, body, &swapped.reasons))
                .collect::<Vec<_>>()
                .join("; ");
            refusals.push(format!(
            "observable side-effect item {i} `{}` is swap-reachable from package(s) [{packages}] (declared=[{declared}]) but its observable effect is not provably swap-private; writes not private: [{public_writes}]; retained state it or its callees may mutate: [{shared}]; swapped path: {swapped_paths}",
            module_item_snippet(&body[i]),
        ));
        }
        if !refusals.is_empty() {
            bail!(
                "strip_swapped_vendor_exports vendor entry {chunk_path}: {} side effect(s) would be silently dropped although they may write a global / external cell residual code may read — restructure the spec so they do not read swapped binding(s):\n{}",
                refusals.len(),
                refusals.join("\n"),
            );
        }
        Ok(())
    }

    /// Each retained binding dropped item `i` may mutate, with what exposes
    /// it.
    ///
    /// Retained state the statement or its callees may mutate in place: a
    /// binding they reach that a retained item declares, unless its value is
    /// code or a primitive (shareable `{}` cells are state) and that some code
    /// could mutate (`ExposedBindings`). Callees follow
    /// at-init call promotion: a direct, never-rebound chunk function runs its
    /// first-order body; nested closures run only when handed to an unresolved
    /// call. What an unresolved call receives, and a chunk callee that is not
    /// a direct function, are followed through lexical reads instead.
    fn shared_roots(
        &self,
        i: usize,
        live: &[bool],
        rebound: &BTreeSet<&Id>,
        writers: &BTreeMap<Id, Vec<usize>>,
    ) -> BTreeMap<Id, String> {
        let Self {
            body: items,
            analyses,
            declarer,
            replacement_import_locals,
            ..
        } = self;
        let an = &analyses[i];
        let retained_state = |id: &Id| {
            !replacement_import_locals.contains(id)
                && declarer
                    .get(id)
                    .is_some_and(|&j| live[j] && !item_declares_code_or_primitive(&items[j]))
        };
        // The statement's own writes are its cells; what it reads at init
        // matters only as an argument its callees may mutate.
        let mut reached: Vec<&Id> = if an.at_init.calls.is_empty() {
            Vec::new()
        } else {
            an.eager_bindings.iter().collect()
        };
        // What the statement hands only to a host receiver (a global outside
        // the chunk) is followed as a value: host code calling back into it is
        // outside the analysed program (SPEC § Partial vendor-swap ownership).
        let host = HostCalls::of(&items[i], |id| declarer.contains_key(id));
        let (handed_to_host, mut lexical): (Vec<&Id>, Vec<&Id>) = an
            .at_init
            .unresolved_sources
            .iter()
            .filter(|id| !host.host_aliases.contains(*id))
            .partition(|id| host.handed_only_to_host(id));
        reached.extend(handed_to_host);
        let mut callees: Vec<&Id> = an.at_init.calls.iter().collect();
        // A closure handed to an unresolved call at init (an IIFE among them)
        // runs this statement's own first-order body, unless every such closure
        // goes to a host receiver.
        if an.at_init.unresolved_inline_fn && !host.only_host_closures() {
            run_first_order_body(an, &items[i], &mut reached, &mut lexical, &mut callees);
        }
        let mut entered = BTreeSet::new();
        while let Some(callee) = callees.pop() {
            let Some(&j) = declarer.get(callee) else {
                continue;
            };
            let body = &analyses[j];
            if !body.direct_function || rebound.contains(callee) {
                lexical.push(callee);
                continue;
            }
            if entered.insert(j) {
                run_first_order_body(body, &items[j], &mut reached, &mut lexical, &mut callees);
            }
        }
        // Follow every reached binding to the retained state behind it: a
        // binding the statement does not retain may alias state through its
        // initializer or what is written into it. Where unresolved code may
        // run, functions it reaches may run too.
        // Items whose code may run (`true`), or whose values only are followed.
        let mut region: BTreeMap<usize, bool> = entered.into_iter().map(|j| (j, true)).collect();
        region.insert(i, true);
        let mut candidates = BTreeSet::new();
        // Each item followed into the region: the binding that led there and
        // the item that binding was read from (`None`: the statement's reach).
        let mut via: BTreeMap<RegionStep, (&Id, Option<RegionStep>)> = BTreeMap::new();
        let mut worklist: Vec<(&Id, bool, Option<RegionStep>)> = reached
            .into_iter()
            .map(|id| (id, false, None))
            .chain(lexical.into_iter().map(|id| (id, true, None)))
            .collect();
        let mut seen = BTreeSet::new();
        let mut expanded = BTreeSet::new();
        while let Some((id, code_may_run, from)) = worklist.pop() {
            if !seen.insert((id, code_may_run)) {
                continue;
            }
            if retained_state(id) {
                candidates.insert(id);
                continue;
            }
            let Some(&j) = declarer.get(id) else {
                continue;
            };
            if !code_may_run && item_declares_code_or_primitive(&items[j]) {
                continue;
            }
            // Calling a native container's methods runs no chunk code, but what
            // is stored in it may still alias retained state.
            let code_may_run = code_may_run && !item_declares_native_container(&items[j]);
            for &k in std::iter::once(&j).chain(writers.get(id).into_iter().flatten()) {
                if expanded.insert((k, code_may_run)) {
                    *region.entry(k).or_default() |= code_may_run;
                    via.insert((k, code_may_run), (id, from));
                    // A value this statement itself computes (a loop variable,
                    // say) derives from what it reads at init; its own calls
                    // and closures are covered above.
                    let step = Some((k, code_may_run));
                    if k == i {
                        worklist.extend(
                            an.eager_bindings
                                .iter()
                                .filter(|read| !an.at_init.calls.contains(*read))
                                .map(|read| (read, code_may_run, step)),
                        );
                    } else {
                        worklist.extend(
                            analyses[k]
                                .reads
                                .iter()
                                .map(|read| (read, code_may_run, step)),
                        );
                    }
                }
            }
        }
        // Retained state is at risk only when the code that may run could
        // mutate it: a member write, `delete`, method call or hand-off of it
        // there, or a rebind anywhere.
        let mut roots = BTreeMap::new();
        for (&k, &code_may_run) in &region {
            // Where only values are followed, code in closures never runs.
            let mut exposed = ExposedBindings {
                skip_closures: !code_may_run,
                ..ExposedBindings::default()
            };
            items[k].visit_with(&mut exposed);
            for id in candidates.iter().filter(|id| exposed.uses.contains(**id)) {
                roots.entry((*id).clone()).or_insert_with(|| {
                    let mut trail = Vec::new();
                    let mut step = [(k, true), (k, false)]
                        .into_iter()
                        .find(|key| via.contains_key(key));
                    while let Some(key) = step
                        && let Some(&(binding, from)) = via.get(&key)
                        && trail.len() < 12
                    {
                        let running = if key.1 { "" } else { " (value)" };
                        trail.push(format!(
                            "item {}{running} via `{}`",
                            key.0,
                            id_name(binding)
                        ));
                        step = from;
                    }
                    format!(
                        "exposed by item {k} `{}`{}",
                        module_item_snippet(&items[k]),
                        if trail.is_empty() {
                            String::new()
                        } else {
                            format!(", reached as {}", trail.join(" <- "))
                        }
                    )
                });
            }
        }
        for id in candidates.into_iter().filter(|id| rebound.contains(*id)) {
            roots
                .entry(id.clone())
                .or_insert_with(|| "rebound".to_string());
        }
        roots
    }
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
    swapped_reasons: &mut BTreeMap<(usize, String), SwappedReason>,
    queue: &mut Vec<(usize, String)>,
    analyses: &[ItemAnalysis],
    declarer: &BTreeMap<Id, usize>,
    mutation_items_by_target: &BTreeMap<Id, Vec<usize>>,
) {
    while let Some((i, package)) = queue.pop() {
        for dep_idx in dependency_items(&analyses[i], declarer, mutation_items_by_target) {
            if swapped_reachability[dep_idx].insert(package.clone()) {
                swapped_reasons.insert(
                    (dep_idx, package.clone()),
                    SwappedReason::Dependency { from: i },
                );
                queue.push((dep_idx, package.clone()));
            }
        }
    }
}

fn absorb_swapped_dependent_items(
    swapped_reachability: &mut [BTreeSet<String>],
    swapped_reasons: &mut BTreeMap<(usize, String), SwappedReason>,
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
            let dependencies = dependency_items(analysis, declarer, mutation_items_by_target);
            for &dep_idx in &dependencies {
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
                let dependency = dependencies
                    .into_iter()
                    .find(|&dependency| swapped_reachability[dependency].contains(&package))
                    .expect("an absorbed item has a dependency owned by its package");
                swapped_reachability[i].insert(package.clone());
                swapped_reasons.insert(
                    (i, package.clone()),
                    SwappedReason::Dependent { dependency },
                );
                queue.push((i, package));
            }
        }
        if queue.is_empty() {
            break;
        }
        propagate_swapped_dependency_reachability(
            swapped_reachability,
            swapped_reasons,
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
