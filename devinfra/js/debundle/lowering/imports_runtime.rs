//! Source-chunk-side imports for a moved module body.
//! When code moves out of a chunks entry into a logical module, any
//! reference to a binding the source chunk imported via
//! `import { x } from "..."` needs to be re-imported by the moved module.
//! `resolve_imported_binding` traces the binding to its original source;
//! `source_chunk_imports_for_moved_body` emits the re-import declarations.

use super::*;

pub(super) fn resolve_imported_binding(
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
        let imported_from = if let Some((_, _, path)) =
            source_import_cache.resolve(&info.src, source_chunk_id, source_runtime_file)?
        {
            path
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
pub(super) fn source_chunk_imports_for_moved_body(
    source_import_cache: &mut ArtifactSourceImportResolutionCache<'_>,
    source_chunk_id: &str,
    source_runtime_file: &str,
    dest_target_file: &str,
    needed: BTreeMap<Id, &RuntimeImportInfo>,
    imported_overrides: &BTreeMap<Id, String>,
) -> Result<Vec<ModuleItem>> {
    let dest_dir = join_module_path(&[source_chunk_id, &module_path_dirname(dest_target_file)]);
    let mut pairs: Vec<(String, ImportSpecifier)> = Vec::with_capacity(needed.len());
    for (local_id, info) in needed {
        let rewritten_source = if let Some((target_chunk_id, target_entry_file, _path)) =
            source_import_cache.resolve(&info.src, source_chunk_id, source_runtime_file)?
        {
            let target_path = join_module_path(&[&target_chunk_id, &target_entry_file]);
            let mut rel = relative_module_path(&dest_dir, &target_path);
            if !rel.starts_with('.') {
                rel = format!("./{rel}");
            }
            rel
        } else if info.src.starts_with('.') {
            // Relative specifier that didn't resolve through the chunk
            // artifact (e.g. it points at an extra_files asset). Walk up
            // to the chunk root then re-attach `info.src`. Naive string
            // concatenation can yield non-canonical spellings like
            // `".././foo.js"` when `info.src` itself starts with `./`,
            // so normalize before emitting.
            let depth = std::path::Path::new(dest_target_file)
                .parent()
                .map(|parent| parent.iter().count())
                .unwrap_or(0);
            let raw = format!("{}{}", "../".repeat(depth), info.src);
            let mut rel = normalize_relative_module_specifier(&raw);
            if !rel.starts_with('.') {
                rel = format!("./{rel}");
            }
            rel
        } else {
            // Bare specifier (npm package etc.) — pass through unchanged.
            info.src.clone()
        };
        // Boundary-rename name mapping from the vendor plan
        // (vendor_into_emission §2.4 "boundary-renamed construction"):
        // overrides the recorded imported name with the vendor chunk's
        // public export name.
        let specifier = match imported_overrides.get(&local_id) {
            Some(public) => runtime_reimport_named_specifier(&local_id, public),
            None => runtime_reimport_specifier(&local_id, info),
        };
        pairs.push((rewritten_source, specifier));
    }
    Ok(group_specifiers_into_import_decls(pairs))
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
pub(super) fn group_specifiers_into_import_decls(
    pairs: Vec<(String, ImportSpecifier)>,
) -> Vec<ModuleItem> {
    let mut groups: Vec<(String, Vec<ImportSpecifier>, Vec<ImportSpecifier>)> = Vec::new();
    let mut index_by_source: BTreeMap<String, usize> = BTreeMap::new();
    for (src, specifier) in pairs {
        let group_index = *index_by_source.entry(src.clone()).or_insert_with(|| {
            groups.push((src.clone(), Vec::new(), Vec::new()));
            groups.len() - 1
        });
        let (_, named_or_default, namespace) = &mut groups[group_index];
        match specifier {
            ImportSpecifier::Namespace(_) => namespace.push(specifier),
            _ => named_or_default.push(specifier),
        }
    }
    let mut result = Vec::with_capacity(groups.len());
    for (src, mut named_or_default, mut namespace) in groups {
        for ns_specifier in namespace.drain(..) {
            result.push(import_decl_module_item(vec![ns_specifier], &src));
        }
        if !named_or_default.is_empty() {
            named_or_default.sort_by_key(|specifier| match specifier {
                ImportSpecifier::Default(_) => 0,
                _ => 1,
            });
            result.push(import_decl_module_item(named_or_default, &src));
        }
    }
    result
}

pub(super) fn import_decl_module_item(specifiers: Vec<ImportSpecifier>, src: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::Import(ImportDecl {
        span: DUMMY_SP,
        specifiers,
        src: Box::new(Str {
            span: DUMMY_SP,
            value: src.into(),
            raw: None,
        }),
        type_only: false,
        with: None,
        phase: ImportPhase::Evaluation,
    }))
}
