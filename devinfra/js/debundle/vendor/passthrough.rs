//! Unified pass-through emission rewriter (vendor_into_emission §2.4,
//! PR 4): the single position-preserving directive rewriter for files
//! that are emitted without lowering — chunk entries (including
//! materialized chunks' residual entries) and runtime files. One wave
//! performs, per file:
//!
//! * **Specifier canonicalization** (the old always-on stage 0):
//!   relative `import` / `export … from` / `export *` / `import()` /
//!   `new Worker` sources that resolve to another chunk are rewritten
//!   to the artifact-relative form.
//! * **Boundary-rename name mapping** (the old `rename_vendor_exports`
//!   caller side): named imports of a `boundary_rename` / `swap`
//!   chunk's entry are renamed from vendor-local to public export
//!   names.
//! * **Partial-swap directive surgery** (the old
//!   `apply_partial_vendor_swaps` / `apply_bundled_partial_vendor_swaps`
//!   consumer side): named imports / re-exports of swapped names are
//!   replaced in position with package / facade imports and the
//!   matching body reference rewrites.
//!
//! All three consult the same `VendorResolutionPlan` oracle; lowering's
//! import construction is the other application site for materialized
//! module bodies (`FileRole::Module` files are skipped here). Files of
//! `suppress`-marked chunks are skipped entirely — suppress means
//! hands-off, so their directives pass through byte-identical
//! (vendor_into_emission open question 3).

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail};
use rayon::prelude::*;
use swc_common::GLOBALS;
use swc_ecma_ast::*;
use swc_ecma_visit::{VisitMut, VisitMutWith};

use artifact::{
    ArtifactIndexes, ChunkBundle, ChunkId, ChunkTable, FileRole, ImportReferenceKind, JsFile,
    JsFileAstParts, join_module_path, list_chunk_file_paths, module_path_dirname,
    relative_module_path,
};
use binding_targets::module_export_name;
use js_ast::{ParsedJsModule, set_str_value, str_value};
use spec::PartialSwapKind;

use crate::plan::VendorResolutionPlan;
use crate::{
    DeferredImport, IdentRewriteTarget, MaterializedOutputChunkIndex, PartialSwapIdentRewriter,
    bundled_facade_import_source, is_valid_identifier, make_named_reexport,
    make_namespace_reexport, new_url_expr, resolve_partial_swap_import_target,
};

pub struct PassthroughRewriteResult {
    pub artifact: ChunkBundle,
    /// (swapped chunk, chunk export) → count of consumer references
    /// rewritten in pass-through files; folded into the partial-swap
    /// manifests' `references_rewritten` alongside lowering's
    /// construction-time counts.
    pub references_by_symbol: BTreeMap<(ChunkId, String), usize>,
}

pub fn rewrite_passthrough_directives(
    mut artifact: ChunkBundle,
    plan: &VendorResolutionPlan,
    references: &ArtifactIndexes,
) -> Result<PassthroughRewriteResult> {
    let chunk_table = artifact.chunk_table.clone();
    let materialized_index = MaterializedOutputChunkIndex::build(&chunk_table);
    let context = PassthroughContext {
        plan,
        references,
        chunk_table: &chunk_table,
        materialized_index: &materialized_index,
    };

    let mut jobs = Vec::new();
    for (chunk_index, chunk_artifact) in artifact.chunks.iter_mut().enumerate() {
        let chunk_id = chunk_artifact.chunk_id;
        if plan.is_suppressed(chunk_id) {
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
            .map(|job| GLOBALS.set(globals, || rewrite_passthrough_file(job, context_ref)))
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

    Ok(PassthroughRewriteResult {
        artifact,
        references_by_symbol,
    })
}

struct PassthroughContext<'a> {
    plan: &'a VendorResolutionPlan,
    references: &'a ArtifactIndexes,
    chunk_table: &'a ChunkTable,
    materialized_index: &'a MaterializedOutputChunkIndex,
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

fn rewrite_passthrough_file(
    mut job: PassthroughFileJob,
    context: &PassthroughContext<'_>,
) -> PassthroughFileResult {
    let module = &mut job.ast.module;
    let mut state = FileRewriteState {
        bindings: BTreeMap::new(),
        emitted_member_namespace_for: BTreeSet::new(),
        emitted_default_namespace_for: BTreeSet::new(),
        references_by_symbol: BTreeMap::new(),
    };

    // Pass A: vendor directive surgery — partial/bundled swap import
    // replacement plus boundary-rename name mapping, in position.
    module.body = rewrite_directive_items(
        std::mem::take(&mut module.body),
        job.chunk_id,
        &job.file_path,
        context,
        &mut state,
    );

    // Pass B: specifier canonicalization (the old stage 0) over every
    // directive shape, including `import()` and `new Worker` sources.
    // Runs after the surgery so retained directives are canonicalized
    // and replacement package / facade sources (bare specifiers or
    // generated output paths outside the artifact) pass through
    // untouched.
    let mut canonicalizer = SourceCanonicalizer {
        context,
        caller_chunk_id: job.chunk_id,
        caller_file: &job.file_path,
    };
    module.visit_mut_with(&mut canonicalizer);

    // Pass C: body reference rewrites for member-access / named-rename
    // bindings collected in Pass A.
    if !state.bindings.is_empty() {
        let mut rewriter = PartialSwapIdentRewriter {
            bindings: &state.bindings,
            references_by_symbol: &mut state.references_by_symbol,
        };
        module.visit_mut_with(&mut rewriter);
    }

    PassthroughFileResult {
        chunk_index: job.chunk_index,
        parts: job.parts,
        ast: job.ast,
        references_by_symbol: state.references_by_symbol,
    }
}

struct FileRewriteState {
    bindings: BTreeMap<Id, IdentRewriteTarget>,
    /// Packages whose shared `import * as <ns> from "<pkg>"` was
    /// already emitted in this file (partial `kind=member`).
    emitted_member_namespace_for: BTreeSet<String>,
    /// Packages whose shared `import <ns> from "<facade>"` was already
    /// emitted in this file (bundled `kind=member|named`).
    emitted_default_namespace_for: BTreeSet<String>,
    references_by_symbol: BTreeMap<(ChunkId, String), usize>,
}

fn rewrite_directive_items(
    original_body: Vec<ModuleItem>,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    context: &PassthroughContext<'_>,
    state: &mut FileRewriteState,
) -> Vec<ModuleItem> {
    let mut new_body: Vec<ModuleItem> = Vec::with_capacity(original_body.len() + 4);
    for item in original_body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::Import(import_decl)) => {
                rewrite_import_decl(
                    import_decl,
                    caller_chunk_id,
                    caller_file_path,
                    context,
                    state,
                    &mut new_body,
                );
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => {
                rewrite_export_from_decl(
                    named,
                    caller_chunk_id,
                    caller_file_path,
                    context,
                    state,
                    &mut new_body,
                );
            }
            other => new_body.push(other),
        }
    }
    new_body
}

fn rewrite_import_decl(
    mut import_decl: ImportDecl,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    context: &PassthroughContext<'_>,
    state: &mut FileRewriteState,
    new_body: &mut Vec<ModuleItem>,
) {
    let source = str_value(&import_decl.src);
    let resolved = context.references.resolve_runtime_import_reference(
        &source,
        caller_chunk_id,
        caller_file_path,
        context.chunk_table,
    );
    let target_chunk_id = resolved
        .as_ref()
        .map(|resolved| resolved.target_chunk_id)
        .or_else(|| {
            resolve_partial_swap_import_target(
                &source,
                caller_chunk_id,
                caller_file_path,
                context.references,
                context.chunk_table,
                context.materialized_index,
            )
        });
    let Some(target_chunk_id) = target_chunk_id else {
        new_body.push(ModuleItem::ModuleDecl(ModuleDecl::Import(import_decl)));
        return;
    };
    if target_chunk_id == caller_chunk_id {
        new_body.push(ModuleItem::ModuleDecl(ModuleDecl::Import(import_decl)));
        return;
    }

    // Partial / bundled swap surgery for named specifiers.
    let is_swapped = context.plan.partial_swaps.contains_key(&target_chunk_id)
        || context
            .plan
            .bundled_partial_swaps
            .contains_key(&target_chunk_id);
    if is_swapped {
        let mut retained_specifiers: Vec<ImportSpecifier> = Vec::new();
        for specifier in std::mem::take(&mut import_decl.specifiers) {
            let ImportSpecifier::Named(named) = specifier else {
                retained_specifiers.push(specifier);
                continue;
            };
            let imported_name = named
                .imported
                .as_ref()
                .map(module_export_name)
                .unwrap_or_else(|| named.local.sym.to_string());
            match plan_named_import_replacement(
                target_chunk_id,
                &imported_name,
                &named.local,
                caller_chunk_id,
                caller_file_path,
                context,
                state,
            ) {
                Some(imports) => {
                    for deferred in imports {
                        new_body.push(deferred.into_module_item());
                    }
                }
                None => retained_specifiers.push(ImportSpecifier::Named(named)),
            }
        }
        if !retained_specifiers.is_empty() {
            import_decl.specifiers = retained_specifiers;
            new_body.push(ModuleItem::ModuleDecl(ModuleDecl::Import(import_decl)));
        }
        return;
    }

    // Boundary-rename name mapping (vendor-local → public) for named
    // specifiers targeting the boundary chunk's entry file. Index
    // resolution carries the target file; the materialized fallback
    // only fires for removed full-swap chunks, whose only consumable
    // file is the entry.
    let targets_entry_file = match &resolved {
        Some(resolved) => context
            .plan
            .boundary_entry_file(target_chunk_id)
            .is_some_and(|entry_file| entry_file == resolved.target_file),
        None => context
            .plan
            .full_swap_target_path(target_chunk_id)
            .is_some(),
    };
    if targets_entry_file {
        for specifier in &mut import_decl.specifiers {
            let ImportSpecifier::Named(named) = specifier else {
                continue;
            };
            let imported_name = named
                .imported
                .as_ref()
                .map(module_export_name)
                .unwrap_or_else(|| named.local.sym.to_string());
            let Some(mapped) = context
                .plan
                .boundary_public_export_name(target_chunk_id, &imported_name)
            else {
                continue;
            };
            if mapped == imported_name {
                continue;
            }
            named.imported = Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                mapped.into(),
                swc_common::DUMMY_SP,
            )));
        }
    }
    new_body.push(ModuleItem::ModuleDecl(ModuleDecl::Import(import_decl)));
}

/// Classify one named import of `imported_name` targeting a partially /
/// bundled-partially swapped chunk: `Some(replacement imports)` when a
/// live rewrite exists (the specifier is dropped and the imports are
/// spliced in position), `None` to retain the specifier on the chunk
/// re-import. Mirrors `VendorResolutionPlan::swapped_named_import_action`
/// (lowering's construction-time classification) exactly.
fn plan_named_import_replacement(
    target_chunk_id: ChunkId,
    imported_name: &str,
    local: &Ident,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    context: &PassthroughContext<'_>,
    state: &mut FileRewriteState,
) -> Option<Vec<DeferredImport>> {
    if let Some(chunk_mapping) = context.plan.partial_swaps.get(&target_chunk_id) {
        let target = chunk_mapping.symbols.get(imported_name)?;
        let package_coords = chunk_mapping.packages.get(&target.package)?;
        let mut imports = Vec::new();
        match target.kind {
            PartialSwapKind::Member => {
                let upstream_export = target.upstream_export.as_deref()?;
                let namespace = package_coords.namespace.as_deref()?;
                state.bindings.insert(
                    local.to_id(),
                    IdentRewriteTarget::Member {
                        namespace: namespace.to_string(),
                        upstream_export: upstream_export.to_string(),
                        chunk_id: target_chunk_id,
                        chunk_export: imported_name.to_string(),
                    },
                );
                if state
                    .emitted_member_namespace_for
                    .insert(target.package.clone())
                {
                    imports.push(DeferredImport::Namespace {
                        source: target.package.clone(),
                        local: namespace.to_string(),
                    });
                }
            }
            PartialSwapKind::Namespace => {
                imports.push(DeferredImport::Namespace {
                    source: target.package.clone(),
                    local: local.sym.to_string(),
                });
                *state
                    .references_by_symbol
                    .entry((target_chunk_id, imported_name.to_string()))
                    .or_insert(0) += 1;
            }
            PartialSwapKind::Default => {
                imports.push(DeferredImport::Default {
                    source: target.package.clone(),
                    local: local.sym.to_string(),
                });
                *state
                    .references_by_symbol
                    .entry((target_chunk_id, imported_name.to_string()))
                    .or_insert(0) += 1;
            }
            PartialSwapKind::Named => {
                let upstream_export = target.upstream_export.as_deref()?;
                imports.push(DeferredImport::Named {
                    source: target.package.clone(),
                    local: upstream_export.to_string(),
                    upstream_export: upstream_export.to_string(),
                });
                if local.sym.as_ref() != upstream_export {
                    state.bindings.insert(
                        local.to_id(),
                        IdentRewriteTarget::Rename {
                            upstream_export: upstream_export.to_string(),
                            chunk_id: target_chunk_id,
                            chunk_export: imported_name.to_string(),
                        },
                    );
                } else {
                    *state
                        .references_by_symbol
                        .entry((target_chunk_id, imported_name.to_string()))
                        .or_insert(0) += 1;
                }
            }
        }
        return Some(imports);
    }

    let chunk_mapping = context.plan.bundled_partial_swaps.get(&target_chunk_id)?;
    let target = chunk_mapping.symbols.get(imported_name)?;
    let package_coords = chunk_mapping.packages.get(&target.package)?;
    let import_source = bundled_facade_import_source(
        context.chunk_table,
        caller_chunk_id,
        caller_file_path,
        &package_coords.facade_app_path,
    );
    let mut imports = Vec::new();
    match target.kind {
        PartialSwapKind::Member | PartialSwapKind::Named => {
            let upstream_export = target.upstream_export.as_deref()?;
            let namespace = package_coords.namespace.as_deref()?;
            state.bindings.insert(
                local.to_id(),
                IdentRewriteTarget::Member {
                    namespace: namespace.to_string(),
                    upstream_export: upstream_export.to_string(),
                    chunk_id: target_chunk_id,
                    chunk_export: imported_name.to_string(),
                },
            );
            if state
                .emitted_default_namespace_for
                .insert(target.package.clone())
            {
                imports.push(DeferredImport::Default {
                    source: import_source,
                    local: namespace.to_string(),
                });
            }
        }
        PartialSwapKind::Namespace | PartialSwapKind::Default => {
            imports.push(DeferredImport::Default {
                source: import_source,
                local: local.sym.to_string(),
            });
            *state
                .references_by_symbol
                .entry((target_chunk_id, imported_name.to_string()))
                .or_insert(0) += 1;
        }
    }
    Some(imports)
}

/// Rewrite `export { <chunk_export> as <name> } from "<vendor-chunk>"`
/// re-exports of swapped names against the upstream package:
///
/// * `kind: named`     → `export { <upstream_export> as <name> } from "<pkg>"`
/// * `kind: default`   → `export { default as <name> } from "<pkg>"`
/// * `kind: namespace` → `export * as <name> from "<pkg>"`
/// * `kind: member`    → retained — a member access off a namespace import
///   has no live re-export equivalent; the plan-time consumer gate bails
///   with a precise diagnostic instead.
///
/// Bundled partial swaps are not rewritten here (their facade default is
/// only member-addressable); the consumer gate covers them.
fn rewrite_export_from_decl(
    mut named: NamedExport,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    context: &PassthroughContext<'_>,
    state: &mut FileRewriteState,
    new_body: &mut Vec<ModuleItem>,
) {
    let Some(src) = named.src.as_deref() else {
        new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
        return;
    };
    let source = str_value(src);
    let Some(target_chunk_id) = resolve_partial_swap_import_target(
        &source,
        caller_chunk_id,
        caller_file_path,
        context.references,
        context.chunk_table,
        context.materialized_index,
    ) else {
        new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
        return;
    };
    let chunk_mapping = if target_chunk_id == caller_chunk_id {
        None
    } else {
        context.plan.partial_swaps.get(&target_chunk_id)
    };
    let Some(chunk_mapping) = chunk_mapping else {
        new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
        return;
    };
    let mut retained: Vec<ExportSpecifier> = Vec::new();
    let mut replacements: Vec<ModuleItem> = Vec::new();
    for specifier in std::mem::take(&mut named.specifiers) {
        let ExportSpecifier::Named(named_spec) = &specifier else {
            retained.push(specifier);
            continue;
        };
        let orig_name = module_export_name(&named_spec.orig);
        let exported_name = named_spec
            .exported
            .as_ref()
            .map(module_export_name)
            .unwrap_or_else(|| orig_name.clone());
        let Some(target) = chunk_mapping.symbols.get(&orig_name) else {
            retained.push(specifier);
            continue;
        };
        if !is_valid_identifier(&exported_name) {
            retained.push(specifier);
            continue;
        }
        let replacement = match target.kind {
            PartialSwapKind::Named => target
                .upstream_export
                .as_deref()
                .map(|upstream| make_named_reexport(&target.package, upstream, &exported_name)),
            PartialSwapKind::Default => Some(make_named_reexport(
                &target.package,
                "default",
                &exported_name,
            )),
            PartialSwapKind::Namespace => {
                Some(make_namespace_reexport(&target.package, &exported_name))
            }
            PartialSwapKind::Member => None,
        };
        let Some(replacement) = replacement else {
            retained.push(specifier);
            continue;
        };
        replacements.push(replacement);
        *state
            .references_by_symbol
            .entry((target_chunk_id, orig_name))
            .or_insert(0) += 1;
    }
    new_body.extend(replacements);
    if !retained.is_empty() {
        named.specifiers = retained;
        new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
    }
}

/// Specifier canonicalization visitor — the relocated stage-0
/// `RuntimeSourceRewriter`. Rewrites relative directive sources that
/// resolve to another chunk into the artifact-relative form; sources
/// already spelled as artifact output paths keep their spelling.
/// Directives targeting a removed full-swap chunk (index resolution
/// misses — the live-proxy dangling-import contract) canonicalize
/// against the plan's recorded entry path instead.
struct SourceCanonicalizer<'a> {
    context: &'a PassthroughContext<'a>,
    caller_chunk_id: ChunkId,
    caller_file: &'a str,
}

impl SourceCanonicalizer<'_> {
    /// `Some(rewritten)` when the source canonicalizes to a different
    /// spelling; `None` to keep the source untouched.
    fn rewrite_source(&self, source: &str) -> Option<String> {
        if source.is_empty() || (!source.starts_with('.') && !source.starts_with('/')) {
            return None;
        }
        let target_path = match self.context.references.resolve_runtime_import_reference(
            source,
            self.caller_chunk_id,
            self.caller_file,
            self.context.chunk_table,
        ) {
            Some(resolved) if resolved.kind == ImportReferenceKind::ArtifactPath => return None,
            Some(resolved) => resolved.target_path,
            None => {
                // Removed full-swap chunks resolve through the
                // chunk-table prefix fallback; canonical entry path
                // comes from the plan.
                let chunk = resolve_partial_swap_import_target(
                    source,
                    self.caller_chunk_id,
                    self.caller_file,
                    self.context.references,
                    self.context.chunk_table,
                    self.context.materialized_index,
                )?;
                self.context.plan.full_swap_target_path(chunk)?
            }
        };
        let caller_chunk_name = self.context.chunk_table.name(self.caller_chunk_id);
        let caller_dir = join_module_path(&[
            caller_chunk_name,
            module_path_dirname(self.caller_file).as_str(),
        ]);
        let mut rewritten = relative_module_path(&caller_dir, &target_path);
        if !rewritten.starts_with('.') {
            rewritten = format!("./{rewritten}");
        }
        (rewritten != source).then_some(rewritten)
    }

    fn rewrite_str(&self, string: &mut Str) {
        if let Some(rewritten) = self.rewrite_source(&str_value(string)) {
            set_str_value(string, rewritten);
        }
    }
}

impl VisitMut for SourceCanonicalizer<'_> {
    fn visit_mut_import_decl(&mut self, node: &mut ImportDecl) {
        self.rewrite_str(&mut node.src);
        node.visit_mut_children_with(self);
    }

    fn visit_mut_named_export(&mut self, node: &mut NamedExport) {
        if let Some(src) = &mut node.src {
            self.rewrite_str(src);
        }
        node.visit_mut_children_with(self);
    }

    fn visit_mut_export_all(&mut self, node: &mut ExportAll) {
        self.rewrite_str(&mut node.src);
        node.visit_mut_children_with(self);
    }

    fn visit_mut_call_expr(&mut self, node: &mut CallExpr) {
        if let Some(string) = dynamic_import_str_mut(node) {
            self.rewrite_str(string);
        }
        node.visit_mut_children_with(self);
    }

    fn visit_mut_new_expr(&mut self, node: &mut NewExpr) {
        if let Some(source) = worker_new_str(node).map(str_value)
            && let Some(rewritten) = self.rewrite_source(&source)
            && let Some(args) = &mut node.args
            && let Some(first) = args.first_mut()
        {
            first.expr = Box::new(new_url_expr(&rewritten));
        }
        node.visit_mut_children_with(self);
    }
}

fn dynamic_import_str_mut(node: &mut CallExpr) -> Option<&mut Str> {
    if matches!(node.callee, Callee::Import(_))
        && let Some(first) = node.args.first_mut()
        && first.spread.is_none()
        && let Expr::Lit(Lit::Str(s)) = &mut *first.expr
    {
        Some(s)
    } else {
        None
    }
}

fn worker_new_str(node: &NewExpr) -> Option<&Str> {
    if matches!(&*node.callee, Expr::Ident(ident) if ident.sym == *"Worker" || ident.sym == *"SharedWorker")
        && let Some(args) = &node.args
        && let Some(first) = args.first()
        && first.spread.is_none()
        && let Expr::Lit(Lit::Str(s)) = &*first.expr
    {
        Some(s)
    } else {
        None
    }
}
