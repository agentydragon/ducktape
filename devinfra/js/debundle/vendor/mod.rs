//! Compose vendor substitution. Package lookup, export inspection, import resolution,
//! and identifier rewriting live in their named modules; the final consumer gate
//! remains here because it validates the combined post-strip emission result.
use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Result, bail};
mod emission;
mod export_surface;
mod import_rewrites;
mod manifests;
mod output_imports;
mod packages;
mod passthrough;
mod plan;
mod strip;
mod validate;
mod wrappers;

use swc_common::{DUMMY_SP, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use artifact::{ChunkId, ChunkTable, list_chunk_file_paths};
use binding_targets::declaration_ids;
pub use emission::{apply_emission_rewrites_in_place, write_planned_vendor_outputs};
use export_surface::collect_local_idents_by_export_name;
pub use import_rewrites::{
    DeferredImport, IdentRewriteTarget, PartialSwapIdentRewriter, VendorImportRewrites,
};
#[cfg(test)]
use js_ast::{emit_js_module, parse_js_module};
use js_ast::{module_export_name_node, named_export_module_item, named_export_specifier};
pub use manifests::*;
pub use output_imports::{
    MaterializedOutputChunkIndex, bundled_facade_import_source, resolve_partial_swap_import_target,
};
use plan::{ChunkBundledPartialSwapPlan, upstream_property_path};
pub use plan::{
    VendorImportAction, VendorPlanOptions, VendorResolutionPlan, build_vendor_resolution_plan,
};
use spec::PartialSwapKind;

// === partial vendor swap =================================================
//
// A full swap operates on a *whole* chunk — its caller imports point at
// the upstream package and the chunk is excluded from the emission set.
// That doesn't work for mixed chunks: Vite commonly bundles several
// packages (zod + @sentry/browser + react + lodash + katex + mermaid)
// into one ESM chunk. Excluding it would drop every co-bundled package.
//
// Partial swaps emit the chunk as a computed residual and replace
// per-symbol consumer references against upstream packages. The
// consumer side is applied at two construction sites — lowering
// (materialized module bodies) and the pass-through emission rewriter
// (`passthrough.rs`) — both consuming the same `VendorResolutionPlan`.
// What remains here is the bundled family's vendor-chunk
// **self-rewrite seed** (re-target the chunk's own references at the
// facade so the residual strip can drop the old implementation),
// consumed by `emission::emit_vendor_residual`, and the manifest
// projection.

/// Project the plan's partial / bundled-partial resolutions into the
/// wire manifest maps, folding the merged per-symbol rewrite counts
/// from every application site (lowering construction, pass-through
/// rewrite, bundled self-rewrite) into `references_rewritten` — the
/// manifest counts **emitted** references regardless of which site
/// produced them.
pub fn build_partial_swap_resolutions(
    plan: &VendorResolutionPlan,
    rewrite_counts: &BTreeMap<(ChunkId, String), usize>,
) -> Result<(
    BTreeMap<String, ChunkPartialSwapResolution>,
    BTreeMap<String, ChunkBundledPartialSwapResolution>,
)> {
    let mut partial: BTreeMap<String, ChunkPartialSwapResolution> = plan
        .partial_swaps
        .values()
        .map(|entry| (entry.chunk_path.clone(), entry.resolution.clone()))
        .collect();
    let mut bundled: BTreeMap<String, ChunkBundledPartialSwapResolution> = plan
        .bundled_partial_swaps
        .values()
        .map(|entry| (entry.chunk_path.clone(), entry.resolution.clone()))
        .collect();
    for ((chunk_id, chunk_export), count) in rewrite_counts {
        let symbol_resolution = if let Some(entry) = plan.partial_swaps.get(chunk_id) {
            partial
                .get_mut(&entry.chunk_path)
                .and_then(|resolution| resolution.symbols.get_mut(chunk_export))
        } else if let Some(entry) = plan.bundled_partial_swaps.get(chunk_id) {
            bundled
                .get_mut(&entry.chunk_path)
                .and_then(|resolution| resolution.symbols.get_mut(chunk_export))
        } else {
            bail!(
                "vendor rewrite counts reference `{chunk_export}` on chunk {}, which has no partial-swap plan",
                plan_chunk_name(plan, *chunk_id),
            );
        };
        if let Some(symbol_resolution) = symbol_resolution {
            symbol_resolution.references_rewritten += count;
        }
    }
    Ok((partial, bundled))
}

fn plan_chunk_name(plan: &VendorResolutionPlan, chunk_id: ChunkId) -> String {
    plan.partial_swaps
        .get(&chunk_id)
        .map(|entry| entry.resolution.chunk_id.clone())
        .or_else(|| {
            plan.bundled_partial_swaps
                .get(&chunk_id)
                .map(|entry| entry.resolution.chunk_id.clone())
        })
        .unwrap_or_else(|| format!("#{}", chunk_id.0))
}

/// Map each top-level binding name to its hygiene-preserving `Id`
/// (`(atom, SyntaxContext)`). Used to resolve a manifest-recorded
/// `local` name (a bare string) to the actual binding cell, so the
/// self-rewrite map keys on binding identity rather than bare text.
/// If two top-level declarations share a name (illegal in module
/// scope after resolver, but defensive), the last one wins.
fn collect_top_level_binding_ids(module: &Module) -> BTreeMap<String, Id> {
    let mut out = BTreeMap::new();
    for item in &module.body {
        let decl = match item {
            ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => &export_decl.decl,
            _ => continue,
        };
        for id in declaration_ids(decl) {
            out.insert(id.0.to_string(), id);
        }
    }
    out
}

struct SelfRewriteOutputs<'a> {
    bindings: &'a mut BTreeMap<Id, IdentRewriteTarget>,
    prelude_imports: &'a mut Vec<DeferredImport>,
    references_by_symbol: &'a mut BTreeMap<(ChunkId, String), usize>,
    self_rewrite_import_locals: &'a mut BTreeSet<Id>,
}

fn seed_bundled_partial_swap_self_rewrites(
    module: &Module,
    chunk_mapping: Option<&ChunkBundledPartialSwapPlan>,
    chunk_table: &ChunkTable,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    outputs: SelfRewriteOutputs<'_>,
) {
    let Some(chunk_mapping) = chunk_mapping else {
        return;
    };
    let exported_locals = collect_local_idents_by_export_name(module);
    // The manifest records `local` only as a bare name string. Resolve
    // it to the hygiene-preserving binding `Id` of the matching
    // top-level declaration so the self-rewrite map is keyed on binding
    // identity (matching `ident.to_id()` in the rewriter), not bare
    // text — otherwise a same-named binding in a nested scope would be
    // miscompiled.
    let top_level_bindings = collect_top_level_binding_ids(module);
    let mut used_idents = module_used_idents(module);
    for (chunk_export, target) in &chunk_mapping.symbols {
        let local_id = match &target.local {
            Some(local_name) => top_level_bindings.get(local_name),
            None => exported_locals.get(chunk_export),
        };
        let Some(local_id) = local_id else {
            continue;
        };
        let Some(package_coords) = chunk_mapping.packages.get(&target.package) else {
            continue;
        };
        let import_source = bundled_facade_import_source(
            chunk_table,
            caller_chunk_id,
            caller_file_path,
            &package_coords.facade_app_path,
        );
        let local =
            unique_synthetic_ident(&format!("__debundle_bps_{chunk_export}"), &mut used_idents);
        match target.kind {
            PartialSwapKind::Member | PartialSwapKind::Named => {
                let upstream_export = target
                    .upstream_export
                    .as_deref()
                    .expect("kind=member/named validated to carry upstream_export");
                outputs.bindings.insert(
                    local_id.clone(),
                    IdentRewriteTarget::Member {
                        namespace: local.clone(),
                        property_path: upstream_property_path(target.kind, upstream_export),
                        chunk_id: caller_chunk_id,
                        chunk_export: chunk_export.clone(),
                    },
                );
            }
            PartialSwapKind::Namespace | PartialSwapKind::Default => {
                outputs.bindings.insert(
                    local_id.clone(),
                    IdentRewriteTarget::Rename {
                        upstream_export: local.clone(),
                        chunk_id: caller_chunk_id,
                        chunk_export: chunk_export.clone(),
                    },
                );
                *outputs
                    .references_by_symbol
                    .entry((caller_chunk_id, chunk_export.clone()))
                    .or_insert(0) += 1;
            }
        }
        outputs
            .self_rewrite_import_locals
            .insert(Ident::new_no_ctxt(local.clone().into(), DUMMY_SP).to_id());
        outputs.prelude_imports.push(DeferredImport::Default {
            source: import_source,
            local,
        });
    }
}

fn unique_synthetic_ident(base: &str, used: &mut BTreeSet<String>) -> String {
    if used.insert(base.to_string()) {
        return base.to_string();
    }
    let mut i = 2usize;
    loop {
        let candidate = format!("{base}_{i}");
        if used.insert(candidate.clone()) {
            return candidate;
        }
        i += 1;
    }
}

fn module_used_idents(module: &Module) -> BTreeSet<String> {
    struct UsedIdentCollector(BTreeSet<String>);
    impl Visit for UsedIdentCollector {
        fn visit_ident(&mut self, ident: &Ident) {
            self.0.insert(ident.sym.to_string());
        }
    }
    let mut collector = UsedIdentCollector(BTreeSet::new());
    module.visit_with(&mut collector);
    collector.0
}

/// `export { <orig> as <exported> } from "<source>"` (alias omitted when
/// the names match).
fn make_named_reexport(source: &str, orig: &str, exported: &str) -> ModuleItem {
    named_export_module_item(
        vec![named_export_specifier(
            module_export_name_node(orig),
            (orig != exported).then(|| module_export_name_node(exported)),
        )],
        Some(source),
    )
}

/// `new URL("<source>", import.meta.url)` — the pass-through rewriter's
/// replacement for a canonicalized `new Worker("<source>")` string
/// argument (worker URLs must be module-relative at runtime).
fn new_url_expr(source: &str) -> Expr {
    Expr::New(NewExpr {
        span: DUMMY_SP,
        ctxt: SyntaxContext::empty(),
        callee: Box::new(Expr::Ident(Ident::new_no_ctxt("URL".into(), DUMMY_SP))),
        args: Some(vec![
            ExprOrSpread {
                spread: None,
                expr: Box::new(Expr::Lit(Lit::Str(Str {
                    span: DUMMY_SP,
                    value: source.into(),
                    raw: None,
                }))),
            },
            ExprOrSpread {
                spread: None,
                expr: Box::new(Expr::Member(MemberExpr {
                    span: DUMMY_SP,
                    obj: Box::new(Expr::MetaProp(MetaPropExpr {
                        span: DUMMY_SP,
                        kind: MetaPropKind::ImportMeta,
                    })),
                    prop: MemberProp::Ident(IdentName::new("url".into(), DUMMY_SP)),
                })),
            },
        ]),
        type_args: None,
    })
}

/// `export * as <exported> from "<source>"`.
fn make_namespace_reexport(source: &str, exported: &str) -> ModuleItem {
    named_export_module_item(
        vec![ExportSpecifier::Namespace(ExportNamespaceSpecifier {
            span: DUMMY_SP,
            name: module_export_name_node(exported),
        })],
        Some(source),
    )
}

/// Post-strip cross-chunk soundness gate for partial vendor swaps.
///
/// The pass-through emission rewriter and lowering's import
/// construction rewrite `ImportDecl` named specifiers (and, for
/// non-bundled swaps, rewritable `export … from` re-exports); the strip
/// pass then removes every swapped name from the vendor chunk's export
/// surface. Any consumer that survived those rewrites while still
/// referencing the stripped surface yields a broken emitted tree:
///
/// * a named import / re-export of a swapped name link-fails;
/// * a namespace import (`import * as M`) silently reads `undefined`
///   for swapped members;
/// * `export *` silently drops the swapped names from the re-exporter.
///
/// The plan-time gate (`plan.rs::validate_consumer_shapes`) rejects
/// these shapes in input space before any emission. This scan is not a
/// redundant tripwire behind it: it re-checks the post-materialize
/// artifact, covering directives that lowering moved into or
/// synthesized inside materialized module bodies (`export … from`
/// re-exports in moved bodies, `BindingKind::Imported` re-export
/// imports) — shapes with no live rewrite at the construction site,
/// invisible to the plan-time gate's input-space enumeration. Retire it
/// only after those construction paths consult the plan and fixtures
/// pin the synthesized-directive shapes (ARCHITECTURE_BACKLOG.md
/// "Post-strip consumer scan retirement condition").
///
/// Scan every retained file of every chunk and bail with a precise
/// diagnostic on the first surviving consumer. Over-restriction is the
/// accepted failure mode: a namespace consumer that only reads
/// non-swapped members would work at runtime, but is rejected anyway
/// because per-member usage is not analyzed here.
pub fn validate_partial_swap_consumers(
    files: &artifact::EmissionFiles,
    plan: &VendorResolutionPlan,
) -> Result<()> {
    let artifact = files.files();
    let references = files.indexes();
    let chunk_table = &artifact.chunk_table;
    let swapped_by_chunk: BTreeMap<ChunkId, BTreeSet<String>> = plan
        .partial_swaps
        .iter()
        .map(|(chunk_id, partial)| (*chunk_id, partial.symbols.keys().cloned().collect()))
        .chain(
            plan.bundled_partial_swaps
                .iter()
                .map(|(chunk_id, bundled)| (*chunk_id, bundled.symbols.keys().cloned().collect())),
        )
        .collect();
    if swapped_by_chunk.is_empty() {
        return Ok(());
    }
    let materialized_index = MaterializedOutputChunkIndex::build(chunk_table);
    // Full-swap chunks are excluded from the emission set; their files
    // are never emitted, so their directives are not consumers (same
    // rule as the plan-time gate).
    let full_swap_chunks = plan.full_swap_chunk_ids();
    for chunk_artifact in &artifact.chunks {
        let caller_chunk_id = chunk_artifact.chunk_id;
        if full_swap_chunks.contains(&caller_chunk_id) {
            continue;
        }
        let caller_chunk_name = chunk_table.name(caller_chunk_id);
        for file_path in list_chunk_file_paths(&chunk_artifact.js) {
            let Some(ast) = chunk_artifact
                .js
                .get_file(&file_path)
                .and_then(|file| file.ast())
            else {
                continue;
            };
            for item in &ast.module.body {
                let ModuleItem::ModuleDecl(decl) = item else {
                    continue;
                };
                let Some(source) = crate::validate::consumer_directive_source(decl) else {
                    continue;
                };
                let Some(target_chunk_id) = resolve_partial_swap_import_target(
                    &source,
                    caller_chunk_id,
                    &file_path,
                    references,
                    chunk_table,
                    &materialized_index,
                ) else {
                    continue;
                };
                let Some(swapped) = swapped_by_chunk.get(&target_chunk_id) else {
                    continue;
                };
                let consumer = format!("{caller_chunk_name}/{file_path}");
                check_partial_swap_consumer_decl(
                    decl,
                    swapped,
                    &consumer,
                    chunk_table.name(target_chunk_id),
                )?;
            }
        }
    }
    Ok(())
}

fn check_partial_swap_consumer_decl(
    decl: &ModuleDecl,
    swapped: &BTreeSet<String>,
    consumer: &str,
    target_chunk_name: &str,
) -> Result<()> {
    validate::check_consumer_directive(
        decl,
        consumer,
        target_chunk_name,
        swapped.contains("default"),
        || swapped.iter().cloned().collect::<Vec<_>>().join(","),
        |imported| {
            if swapped.contains(imported) {
                bail!(
                    "partial-swap consumer gate: {consumer} imports swapped name `{imported}` from partially-swapped vendor chunk {target_chunk_name}; the rewrite did not cover this consumer and the stripped chunk no longer exports it"
                );
            }
            Ok(())
        },
        |orig| {
            if swapped.contains(orig) {
                bail!(
                    "partial-swap consumer gate: {consumer} re-exports swapped name `{orig}` from partially-swapped vendor chunk {target_chunk_name}; this re-export shape has no live rewrite (kind=member symbols and bundled swaps cannot be expressed as re-exports) and the stripped chunk no longer exports it"
                );
            }
            Ok(())
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_ecma_visit::VisitMutWith;

    #[test]
    fn worker_url_rewrite_uses_import_meta_url_as_base() {
        let Expr::New(new_expr) = new_url_expr("../worker.js") else {
            panic!("expected new URL expression");
        };
        let args = new_expr.args.expect("new URL args");
        assert_eq!(args.len(), 2);
        let Expr::Member(member) = &*args[1].expr else {
            panic!("expected import.meta.url member expression");
        };
        assert!(matches!(
            &*member.obj,
            Expr::MetaProp(MetaPropExpr {
                kind: MetaPropKind::ImportMeta,
                ..
            })
        ));
        let MemberProp::Ident(prop) = &member.prop else {
            panic!("expected ident member property");
        };
        assert_eq!(prop.sym.as_ref(), "url");
    }

    #[test]
    fn partial_swap_rewriter_preserves_namespace_iife_initializer_target() {
        js_ast::with_swc_globals(|| {
            let mut parsed = parse_js_module(
                "vendor.js",
                "var Sa;\n\
                 (function(t) { t[(t.NONE = 0)] = \"NONE\"; })(Sa || (Sa = {}));\n\
                 const direct = Sa.NONE;\n",
            )
            .unwrap();
            let sa_id = collect_top_level_binding_ids(&parsed.module)
                .remove("Sa")
                .expect("`Sa` is declared at top level");
            let bindings = BTreeMap::from([(
                sa_id,
                IdentRewriteTarget::Member {
                    namespace: "__debundle_bps_l3".to_string(),
                    property_path: vec!["DiagLogLevel".to_string()],
                    chunk_id: ChunkId(0),
                    chunk_export: "l3".to_string(),
                },
            )]);
            let mut references_by_symbol = BTreeMap::new();
            parsed.module.visit_mut_with(&mut PartialSwapIdentRewriter {
                bindings: &bindings,
                references_by_symbol: &mut references_by_symbol,
            });

            let emitted = emit_js_module(&parsed, &[]).unwrap();
            assert!(
                emitted.contains("Sa || (Sa = {})"),
                "namespace IIFE target should stay recognizable for DCE:\n{emitted}",
            );
            assert!(
                emitted.contains("__debundle_bps_l3.DiagLogLevel.NONE"),
                "ordinary references should still be rewritten:\n{emitted}",
            );
        });
    }
}
