//! Source-chunk-side imports for a moved module body.
//! When code moves out of a chunks entry into a logical module, any
//! reference to a binding the source chunk imported via
//! `import { x } from "..."` needs to be re-imported by the moved module.
//! `resolve_imported_binding` traces the binding to its original source;
//! `source_chunk_imports_for_moved_body` emits the re-import declarations.

use crate::body_facts::{PreparedImportBinding, PreparedImportTarget};
use crate::imports::{
    ArtifactSourceImportResolutionCache, runtime_reimport_named_specifier,
    runtime_reimport_specifier,
};
use anyhow::{Result, bail};
use artifact::paths::{
    join_module_path, module_path_dirname, normalize_relative_module_specifier,
    relative_module_path,
};
use frontend_elaboration::source_ast::{RuntimeImportFacts, RuntimeImportKind};
use js_ast::import_decl_module_item;
use std::collections::BTreeMap;
use swc_ecma_ast::*;

pub(crate) fn resolve_imported_binding(
    source_import_cache: &mut ArtifactSourceImportResolutionCache<'_>,
    runtime_import_facts: &RuntimeImportFacts,
    source_chunk_id: &str,
    source_runtime_file: &str,
    source_local: &str,
    imported_from_by_src: &mut BTreeMap<String, String>,
) -> Result<(String, String)> {
    let Some(info) = runtime_import_facts.lookup_by_sym(source_local) else {
        bail!("no import specifier found for `{source_local}` in source chunk");
    };
    let RuntimeImportKind::Named { imported } = &info.kind else {
        bail!("no named import specifier found for `{source_local}` in source chunk");
    };
    let imported_from = if let Some(imported_from) = imported_from_by_src.get(&info.src) {
        imported_from.clone()
    } else {
        let imported_from = if let Some(target) =
            source_import_cache.resolve(&info.src, source_chunk_id, source_runtime_file)?
        {
            target.path
        } else {
            // Source path doesn't reference a known chunk (e.g. a
            // synthetic e2e snapshot file with no entry in the artifact).
            // Resolve relative to the source chunk's directory in the
            // output tree (chunk_id includes the directory prefix; the
            // runtime file is chunk-relative).
            let chunk_runtime_abs = join_module_path(&[
                &module_path_dirname(source_chunk_id),
                &module_path_dirname(source_runtime_file),
            ]);
            join_module_path(&[&chunk_runtime_abs, &info.src])
        };
        imported_from_by_src.insert(info.src.clone(), imported_from.clone());
        imported_from
    };
    Ok((imported.clone(), imported_from))
}

/// Build re-imports for source-chunk ImportSpecifier-bound locals that
/// `body` (the moved code for this destination module) references but
/// no enclosing import or local decl provides. Each emitted import
/// uses a destination-relative path resolved through the artifact's
/// source-chunk index, so it stays correct after the rewriter (which
/// skips materialized files).
///
/// Bindings sharing the same rewritten source are consolidated into a
/// single `ImportDecl` (one statement with all specifiers) so the
/// emitter matches what an author would write — not one statement per
/// binding. See [`group_specifiers_into_import_decls`] for the grouping
/// and namespace-split rules.
pub(crate) fn source_chunk_imports_for_moved_body(
    source_import_cache: &mut ArtifactSourceImportResolutionCache<'_>,
    source_chunk_id: &str,
    source_runtime_file: &str,
    dest_target_file: &str,
    needed: BTreeMap<Id, super::plan_references::RuntimeReimport<'_>>,
    imported_overrides: &BTreeMap<Id, String>,
) -> Result<Vec<PreparedImportDecl>> {
    let mut pairs: Vec<(String, ImportSpecifier, PreparedImportBinding)> =
        Vec::with_capacity(needed.len());
    for (local_id, reimport) in needed {
        let info = reimport.info;
        let rewritten_source = source_chunk_import_for_target(
            source_import_cache,
            source_chunk_id,
            source_runtime_file,
            dest_target_file,
            &info.src,
        )?;
        // Boundary-rename name mapping from the vendor plan overrides
        // the recorded imported name with the vendor chunk's public
        // export name.
        let specifier = match imported_overrides.get(&local_id) {
            Some(public) => runtime_reimport_named_specifier(&local_id, public),
            None => runtime_reimport_specifier(&local_id, info),
        };
        let imported_name = match imported_overrides.get(&local_id) {
            Some(public) => public.clone(),
            None => match &info.kind {
                RuntimeImportKind::Named { imported } => imported.clone(),
                RuntimeImportKind::Default => "default".to_owned(),
                RuntimeImportKind::Namespace => "*".to_owned(),
            },
        };
        // Vendor/boundary overrides change the source export identity. The
        // module-cell gate separately excludes vendor rewrites; target-only
        // consumers must also refuse this name unless that mapping is carried
        // as a checked provider-export proof.
        let target = if imported_overrides.contains_key(&local_id) {
            PreparedImportTarget::Unsupported
        } else {
            PreparedImportTarget::OriginalSource {
                specifier: info.src.clone(),
            }
        };
        pairs.push((
            rewritten_source,
            specifier,
            PreparedImportBinding {
                source_binding: reimport.source_binding,
                candidate_binding: local_id.clone(),
                imported_name,
                kind: match &info.kind {
                    RuntimeImportKind::Named { .. } => {
                        artifact::reports::ImportSpecifierKind::Named
                    }
                    RuntimeImportKind::Default => artifact::reports::ImportSpecifierKind::Default,
                    RuntimeImportKind::Namespace => {
                        artifact::reports::ImportSpecifierKind::Namespace
                    }
                },
                exported_name: None,
                target,
            },
        ));
    }
    Ok(group_specifiers_into_import_decls(pairs))
}

pub(crate) struct PreparedImportDecl {
    pub(crate) item: ModuleItem,
    pub(crate) targets: Vec<PreparedImportTarget>,
    pub(crate) bindings: Vec<PreparedImportBinding>,
}

pub(crate) fn source_chunk_import_for_target(
    source_import_cache: &mut ArtifactSourceImportResolutionCache<'_>,
    source_chunk_id: &str,
    source_runtime_file: &str,
    dest_target_file: &str,
    source: &str,
) -> Result<String> {
    let dest_dir = join_module_path(&[source_chunk_id, &module_path_dirname(dest_target_file)]);
    Ok(
        if let Some(target) =
            source_import_cache.resolve(source, source_chunk_id, source_runtime_file)?
        {
            let target_path = join_module_path(&[&target.chunk_id, &target.entry_file]);
            let mut rel = relative_module_path(&dest_dir, &target_path);
            if !rel.starts_with('.') {
                rel = format!("./{rel}");
            }
            rel
        } else if source.starts_with('.') {
            // Relative specifier that didn't resolve through the chunk
            // artifact (e.g. it points at an extra_files asset). Walk up
            // to the chunk root then re-attach `source`. Naive string
            // concatenation can yield non-canonical spellings like
            // `".././foo.js"` when `source` itself starts with `./`,
            // so normalize before emitting.
            let depth = std::path::Path::new(dest_target_file)
                .parent()
                .map(|parent| parent.iter().count())
                .unwrap_or(0);
            let raw = format!("{}{}", "../".repeat(depth), source);
            let mut rel = normalize_relative_module_specifier(&raw);
            if !rel.starts_with('.') {
                rel = format!("./{rel}");
            }
            rel
        } else {
            // Bare specifier (npm package etc.) — pass through unchanged.
            source.to_string()
        },
    )
}

struct ImportSpecifierGroup {
    source: String,
    named_or_default: Vec<(ImportSpecifier, PreparedImportBinding)>,
    namespace: Vec<(ImportSpecifier, PreparedImportBinding)>,
}

/// Consolidate `(rewritten_source, specifier)` pairs into `ImportDecl`
/// `ModuleItem`s, one statement per source group. First-occurrence order
/// is preserved both for the source groups and for specifiers within each
/// group.
///
/// Namespace specifiers (`import * as ns from "src"`) are always emitted
/// as their own `ImportDecl`, even when a same-source group also has
/// named/default specifiers: ESM grammar forbids mixing a `NameSpaceImport`
/// with `NamedImports` in one `ImportClause`, and one `ImportClause` holds
/// at most one `NameSpaceImport`. Within a same-source named/default group,
/// default specifiers are sorted before named to satisfy ESM grammar
/// (`import D, { x } from "src"`, not the reverse).
pub(crate) fn group_specifiers_into_import_decls(
    pairs: Vec<(String, ImportSpecifier, PreparedImportBinding)>,
) -> Vec<PreparedImportDecl> {
    let mut groups = Vec::<ImportSpecifierGroup>::new();
    let mut index_by_source: BTreeMap<String, usize> = BTreeMap::new();
    for (src, specifier, mut binding) in pairs {
        let (candidate_binding, kind) = match &specifier {
            ImportSpecifier::Named(named) => (
                named.local.to_id(),
                artifact::reports::ImportSpecifierKind::Named,
            ),
            ImportSpecifier::Default(default) => (
                default.local.to_id(),
                artifact::reports::ImportSpecifierKind::Default,
            ),
            ImportSpecifier::Namespace(namespace) => (
                namespace.local.to_id(),
                artifact::reports::ImportSpecifierKind::Namespace,
            ),
        };
        binding.candidate_binding = candidate_binding;
        binding.kind = kind;
        let group_index = *index_by_source.entry(src.clone()).or_insert_with(|| {
            groups.push(ImportSpecifierGroup {
                source: src.clone(),
                named_or_default: Vec::new(),
                namespace: Vec::new(),
            });
            groups.len() - 1
        });
        let group = &mut groups[group_index];
        match specifier {
            ImportSpecifier::Namespace(_) => group.namespace.push((specifier, binding)),
            _ => group.named_or_default.push((specifier, binding)),
        }
    }
    let mut result = Vec::with_capacity(groups.len());
    for ImportSpecifierGroup {
        source: src,
        mut named_or_default,
        namespace,
    } in groups
    {
        for (ns_specifier, binding) in namespace {
            result.push(PreparedImportDecl {
                item: import_decl_module_item(vec![ns_specifier], &src),
                targets: vec![binding.target.clone()],
                bindings: vec![binding],
            });
        }
        if !named_or_default.is_empty() {
            named_or_default.sort_by_key(|(specifier, _)| match specifier {
                ImportSpecifier::Default(_) => 0,
                _ => 1,
            });
            let (specifiers, bindings): (Vec<_>, Vec<_>) = named_or_default.into_iter().unzip();
            let targets = bindings
                .iter()
                .map(|binding| binding.target.clone())
                .collect();
            result.push(PreparedImportDecl {
                item: import_decl_module_item(specifiers, &src),
                targets,
                bindings,
            });
        }
    }
    result
}
