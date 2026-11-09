//! Unified pass-through emission rewriter: the single position-preserving directive rewriter for files
//! that are emitted without lowering — chunk entries (including
//! materialized chunks' residual entries) and runtime files. Driven by
//! `vendor::emission::apply_emission_rewrites`, which applies it to
//! non-vendor pass-through files as a wave and to vendor-chunk files
//! as the first step of the per-chunk residual composition. Per file
//! it performs:
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
//! hands-off, so their directives pass through byte-identical.

use source_identity::{ChunkId, ChunkTable};
use std::collections::BTreeMap;

use swc_common::{Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{VisitMut, VisitMutWith};

use artifact::paths::{join_module_path, module_path_dirname, relative_module_path};
use artifact::{ArtifactIndexes, ImportReferenceKind};
use binding_targets::module_export_name;
use js_ast::{set_str_value, str_value};
use spec::PartialSwapKind;

use crate::plan::VendorResolutionPlan;
use crate::{
    CandidateVendorDestination, CandidateVendorPlanAction, DeferredImport, DeferredImportMarker,
    MaterializedOutputChunkIndex, PartialSwapIdentRewriter, VendorImportAction,
    VendorImportRewrites, VendorUseRewriteObservation, bundled_facade_import_source,
    checked_boundary_rename_action, checked_vendor_replacement_action, make_named_reexport,
    make_namespace_reexport, new_url_expr, resolve_partial_swap_import_target,
};

pub(crate) struct PassthroughContext<'a> {
    pub(crate) plan: &'a VendorResolutionPlan,
    pub(crate) references: &'a ArtifactIndexes,
    pub(crate) chunk_table: &'a ChunkTable,
    pub(crate) materialized_index: &'a MaterializedOutputChunkIndex,
}

#[derive(Debug, Clone)]
pub(crate) struct PassthroughVendorBindingRewrite {
    pub(crate) declaration_span: Span,
    pub(crate) source_request: String,
    pub(crate) source_request_kind: artifact::reports::SourceStaticRequestKind,
    pub(crate) source_specifier_index: usize,
    pub(crate) source_specifier_span: Span,
    pub(crate) source_binding: Option<Id>,
    pub(crate) source_export: String,
    pub(crate) source_exported_name: Option<String>,
    pub(crate) source_kind: artifact::reports::ImportSpecifierKind,
    pub(crate) target_chunk: ChunkId,
    /// Exact file resolved from this source request, when the artifact
    /// resolver can establish it. Chunk-only materialized fallback is not
    /// enough to authorize a replacement receipt.
    pub(crate) source_target_file: Option<String>,
    /// None means the runtime-selected action could not be tied back to the
    /// checked plan. The caller emits an explicit unsupported receipt.
    pub(crate) checked_action: Option<(CandidateVendorPlanAction, CandidateVendorDestination)>,
    pub(crate) emitted_requests: Vec<DeferredImportMarker>,
    pub(crate) emitted_exports: Vec<PassthroughVendorExportMarker>,
}

#[derive(Debug, Clone)]
pub(crate) struct PassthroughVendorExportMarker {
    pub(crate) declaration_span: Span,
    pub(crate) specifier_span: Span,
    pub(crate) request: String,
    pub(crate) imported_name: Option<String>,
    pub(crate) exported_name: Option<String>,
    pub(crate) kind: artifact::reports::ImportSpecifierKind,
}

#[derive(Debug, Default)]
pub(crate) struct PassthroughRewriteResult {
    pub(crate) vendor_bindings: Vec<PassthroughVendorBindingRewrite>,
    pub(crate) use_rewrites: Vec<VendorUseRewriteObservation>,
}

/// Rewrite one pass-through module in place; rewritten partial-swap
/// consumer reference counts are accumulated into
/// `references_by_symbol`.
pub(crate) fn rewrite_passthrough_module(
    module: &mut Module,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    context: &PassthroughContext<'_>,
    references_by_symbol: &mut BTreeMap<(ChunkId, String), usize>,
) -> PassthroughRewriteResult {
    let mut state = VendorImportRewrites::default();
    let mut vendor_bindings = Vec::new();

    // Pass A: vendor directive surgery — partial/bundled swap import
    // replacement plus boundary-rename name mapping, in position.
    module.body = rewrite_directive_items(
        std::mem::take(&mut module.body),
        caller_chunk_id,
        caller_file_path,
        context,
        &mut state,
        &mut vendor_bindings,
    );

    // Pass B: specifier canonicalization (the old stage 0) over every
    // directive shape, including `import()` and `new Worker` sources.
    // Runs after the surgery so retained directives are canonicalized
    // and replacement package / facade sources (bare specifiers or
    // generated output paths outside the artifact) pass through
    // untouched.
    let mut canonicalizer = SourceCanonicalizer {
        context,
        caller_chunk_id,
        caller_file: caller_file_path,
    };
    module.visit_mut_with(&mut canonicalizer);

    // Pass C: body reference rewrites for member-access / named-rename
    // bindings collected in Pass A.
    let mut use_rewrites = Vec::new();
    if !state.body_rewrites.is_empty() {
        let mut rewriter = PartialSwapIdentRewriter {
            bindings: &state.body_rewrites,
            references_by_symbol: &mut state.references_rewritten,
            observations: Some(&mut use_rewrites),
        };
        module.visit_mut_with(&mut rewriter);
    }

    for (key, count) in state.references_rewritten {
        *references_by_symbol.entry(key).or_insert(0) += count;
    }
    PassthroughRewriteResult {
        vendor_bindings,
        use_rewrites,
    }
}

fn rewrite_directive_items(
    original_body: Vec<ModuleItem>,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    context: &PassthroughContext<'_>,
    state: &mut VendorImportRewrites,
    vendor_bindings: &mut Vec<PassthroughVendorBindingRewrite>,
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
                    vendor_bindings,
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
                    vendor_bindings,
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
    state: &mut VendorImportRewrites,
    vendor_bindings: &mut Vec<PassthroughVendorBindingRewrite>,
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
        for (specifier_index, specifier) in std::mem::take(&mut import_decl.specifiers)
            .into_iter()
            .enumerate()
        {
            let source_specifier_span = specifier.span();
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
                source_specifier_span,
                caller_chunk_id,
                caller_file_path,
                context,
                state,
            ) {
                Some((action, imports, emitted_requests)) => {
                    let source_target_file = resolved
                        .as_ref()
                        .map(|resolved| resolved.target_file.clone());
                    let expected_entry_file = context
                        .plan
                        .partial_swaps
                        .get(&target_chunk_id)
                        .map(|swap| swap.entry_file.as_str())
                        .or_else(|| {
                            context
                                .plan
                                .bundled_partial_swaps
                                .get(&target_chunk_id)
                                .map(|swap| swap.entry_file.as_str())
                        });
                    let checked_action = source_target_file
                        .as_deref()
                        .zip(expected_entry_file)
                        .filter(|(actual, expected)| actual == expected)
                        .and_then(|_| {
                            emitted_requests.first().and_then(|first| {
                                checked_vendor_replacement_action(
                                    context.plan,
                                    target_chunk_id,
                                    &imported_name,
                                    &action,
                                    &first.request,
                                )
                            })
                        });
                    vendor_bindings.push(PassthroughVendorBindingRewrite {
                        declaration_span: import_decl.span,
                        source_request: source.clone(),
                        source_request_kind: artifact::reports::SourceStaticRequestKind::Import,
                        source_specifier_index: specifier_index,
                        source_specifier_span,
                        source_binding: Some(named.local.to_id()),
                        source_export: imported_name,
                        source_exported_name: None,
                        source_kind: artifact::reports::ImportSpecifierKind::Named,
                        target_chunk: target_chunk_id,
                        source_target_file,
                        checked_action,
                        emitted_requests,
                        emitted_exports: Vec::new(),
                    });
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
    // resolution carries the target file; when only the materialized
    // fallback resolved, a full-swap target's sole consumable file is
    // the entry.
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
        for (specifier_index, specifier) in import_decl.specifiers.iter_mut().enumerate() {
            let source_specifier_span = specifier.span();
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
            let checked_action = resolved.as_ref().and_then(|resolved| {
                checked_boundary_rename_action(
                    context.plan,
                    target_chunk_id,
                    &imported_name,
                    mapped,
                    artifact::reports::OutputFileAddress {
                        chunk: target_chunk_id,
                        file: resolved.target_file.clone(),
                    },
                )
            });
            vendor_bindings.push(PassthroughVendorBindingRewrite {
                declaration_span: import_decl.span,
                source_request: source.clone(),
                source_request_kind: artifact::reports::SourceStaticRequestKind::Import,
                source_specifier_index: specifier_index,
                source_specifier_span,
                source_binding: Some(named.local.to_id()),
                source_export: imported_name,
                source_exported_name: None,
                source_kind: artifact::reports::ImportSpecifierKind::Named,
                target_chunk: target_chunk_id,
                source_target_file: resolved
                    .as_ref()
                    .map(|resolved| resolved.target_file.clone()),
                checked_action,
                emitted_requests: vec![DeferredImportMarker {
                    source_span: source_specifier_span,
                    request: source.clone(),
                    local: named.local.to_id(),
                }],
                emitted_exports: Vec::new(),
            });
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
/// re-import. Classification and materialization are shared with lowering.
fn plan_named_import_replacement(
    target_chunk_id: ChunkId,
    imported_name: &str,
    local: &Ident,
    source_span: Span,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    context: &PassthroughContext<'_>,
    state: &mut VendorImportRewrites,
) -> Option<(
    VendorImportAction,
    Vec<DeferredImport>,
    Vec<DeferredImportMarker>,
)> {
    let action = context
        .plan
        .swapped_named_import_action(target_chunk_id, imported_name)?;
    let imports = state.materialize_at(
        action.clone(),
        local.to_id(),
        target_chunk_id,
        imported_name.to_string(),
        source_span,
        |path| {
            bundled_facade_import_source(
                context.chunk_table,
                caller_chunk_id,
                caller_file_path,
                path,
            )
        },
    );
    let markers = state
        .materialized_request_markers
        .get(&local.to_id())
        .cloned()
        .unwrap_or_default();
    Some((action, imports, markers))
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
    state: &mut VendorImportRewrites,
    vendor_bindings: &mut Vec<PassthroughVendorBindingRewrite>,
    new_body: &mut Vec<ModuleItem>,
) {
    let Some(src) = named.src.as_deref() else {
        new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
        return;
    };
    let source = str_value(src);
    let resolved = context.references.resolve_runtime_import_reference(
        &source,
        caller_chunk_id,
        caller_file_path,
        context.chunk_table,
    );
    let Some(target_chunk_id) = resolved
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
        })
    else {
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
    for (specifier_index, specifier) in std::mem::take(&mut named.specifiers)
        .into_iter()
        .enumerate()
    {
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
        let source_specifier_span = specifier.span();
        let upstream_export = match target.kind {
            PartialSwapKind::Named => target.upstream_export.clone(),
            PartialSwapKind::Default => Some("default".to_owned()),
            PartialSwapKind::Namespace => None,
            PartialSwapKind::Member => None,
        };
        let check_action =
            match target.kind {
                PartialSwapKind::Named => upstream_export.as_ref().map(|upstream_export| {
                    VendorImportAction::PackageNamed {
                        package: target.package.clone(),
                        upstream_export: upstream_export.clone(),
                    }
                }),
                PartialSwapKind::Default => Some(VendorImportAction::PackageDefault {
                    package: target.package.clone(),
                }),
                PartialSwapKind::Namespace => Some(VendorImportAction::PackageNamespace {
                    package: target.package.clone(),
                }),
                PartialSwapKind::Member => None,
            };
        let source_target_file = resolved
            .as_ref()
            .map(|resolved| resolved.target_file.clone());
        let checked_action = source_target_file
            .as_deref()
            .filter(|actual| *actual == chunk_mapping.entry_file)
            .and_then(|_| check_action)
            .and_then(|action| {
                checked_vendor_replacement_action(
                    context.plan,
                    target_chunk_id,
                    &orig_name,
                    &action,
                    &target.package,
                )
            });
        let mut replacement = replacement;
        let output_specifier =
            set_replacement_export_spans(&mut replacement, named.span, source_specifier_span)
                .then_some(source_specifier_span);
        let emitted_exports = output_specifier
            .map(|specifier_span| {
                vec![PassthroughVendorExportMarker {
                    declaration_span: named.span,
                    specifier_span,
                    request: target.package.clone(),
                    imported_name: upstream_export.clone(),
                    exported_name: Some(exported_name.clone()),
                    kind: match target.kind {
                        PartialSwapKind::Namespace => {
                            artifact::reports::ImportSpecifierKind::Namespace
                        }
                        _ => artifact::reports::ImportSpecifierKind::Named,
                    },
                }]
            })
            .unwrap_or_default();
        vendor_bindings.push(PassthroughVendorBindingRewrite {
            declaration_span: named.span,
            source_request: source.clone(),
            source_request_kind: artifact::reports::SourceStaticRequestKind::NamedReExport,
            source_specifier_index: specifier_index,
            source_specifier_span,
            source_binding: None,
            source_export: orig_name.clone(),
            source_exported_name: Some(exported_name),
            source_kind: artifact::reports::ImportSpecifierKind::Named,
            target_chunk: target_chunk_id,
            source_target_file,
            checked_action,
            emitted_requests: Vec::new(),
            emitted_exports,
        });
        replacements.push(replacement);
        *state
            .references_rewritten
            .entry((target_chunk_id, orig_name))
            .or_insert(0) += 1;
    }
    new_body.extend(replacements);
    if !retained.is_empty() {
        named.specifiers = retained;
        new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
    }
}

fn set_replacement_export_spans(
    item: &mut ModuleItem,
    declaration_span: Span,
    specifier_span: Span,
) -> bool {
    let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) = item else {
        return false;
    };
    if export.specifiers.len() != 1 {
        return false;
    }
    export.span = declaration_span;
    match &mut export.specifiers[0] {
        ExportSpecifier::Named(named) => named.span = specifier_span,
        ExportSpecifier::Namespace(namespace) => namespace.span = specifier_span,
        ExportSpecifier::Default(default) => default.span = specifier_span,
    }
    true
}

/// Specifier canonicalization visitor — the relocated stage-0
/// `RuntimeSourceRewriter`. Rewrites relative directive sources that
/// resolve to another chunk into the artifact-relative form; sources
/// already spelled as artifact output paths keep their spelling.
/// Directives targeting a full-swap chunk that index resolution misses
/// (the full-swap dangling-import contract) canonicalize against the
/// plan's recorded entry path instead.
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
                // Full-swap targets the index misses resolve through
                // the chunk-table prefix fallback; canonical entry
                // path comes from the plan.
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
            *first.expr = new_url_expr(&rewritten);
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
