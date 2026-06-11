//! Program-level cross-module purity pass: resolve every chunk's imports
//! against the artifact and run the purity fixpoint
//! (`analysis::cross_module_purity`). Output feeds each chunk's
//! `AnalysisHints::imported_purities` so the per-chunk classifier can see
//! through calls to imported functions instead of bailing `unknown_call`.

use std::collections::{BTreeMap, BTreeSet};

use analysis::cross_module_purity::{
    ModulePurityFacts, ResolvedImport, resolve_asserted_fluent_bindings,
    resolve_asserted_member_purities, resolve_imported_purities,
};
use analysis::purity::Purity;
use artifact::{
    ArtifactIndexes, ChunkBundle, ChunkId, ExportAliasRecord, ImportRecord, ImportSpecifierKind,
};
use js_ast::ParsedJsModule;
use program_analysis::analyze_program_shallow;
use spec::ChunkExportPurity;
use swc_ecma_ast::{Decl, ModuleDecl, ModuleItem, Pat};

/// Binding names introduced by an exported declaration (`export function`/
/// `export const`/`export class`), whose export name equals the local name.
/// Destructuring exports are skipped — conservative (the binding stays
/// unresolved, i.e. `unknown_call`).
fn exported_decl_names(decl: &Decl) -> Vec<String> {
    match decl {
        Decl::Fn(function) => vec![function.ident.sym.to_string()],
        Decl::Class(class) => vec![class.ident.sym.to_string()],
        Decl::Var(var) => var
            .decls
            .iter()
            .filter_map(|declarator| match &declarator.name {
                Pat::Ident(binding) => Some(binding.id.sym.to_string()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// Parsed entry + import/export records for a chunk whose entry the artifact
/// kept as raw source (non-selected chunks). The oracle parses these itself —
/// every listed chunk participates in cross-module purity, not just the
/// selected ones.
struct ReparsedEntry {
    parsed: ParsedJsModule,
    imports: Vec<ImportRecord>,
    export_aliases: Vec<ExportAliasRecord>,
}

/// Output of the program-level purity pass, keyed by chunk name.
pub(super) struct CrossModulePurities {
    /// Per-chunk imported-binding verdicts (`AnalysisHints::imported_purities`).
    pub(super) bindings: BTreeMap<String, BTreeMap<String, Purity>>,
    /// Per-chunk pure-member sets for imported namespace-like bindings,
    /// merged into `AnalysisHints::declared_pure_members`.
    pub(super) members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
    /// Per-chunk fluent-trusted import binding names (deep-purity roots),
    /// merged into `AnalysisHints::fluent_bindings`.
    pub(super) fluent: BTreeMap<String, BTreeSet<String>>,
}

/// Per-chunk-name imported-binding purity maps for the whole artifact.
/// A chunk whose entry can neither be reused (retained AST) nor re-parsed
/// stays opaque: its exports get no verdicts and imports of it stay
/// unresolved (`unknown_call`), exactly as before this pass existed.
pub(super) fn collect_cross_module_imported_purities(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    chunk_export_purity: &BTreeMap<String, ChunkExportPurity>,
) -> CrossModulePurities {
    // Pass 1: re-parse entries stored as raw source so every chunk's body is
    // analyzable. Parse failures only warn — the pass is an analysis
    // refinement, and an unparseable chunk degrades to today's conservative
    // behavior rather than failing the build.
    let mut reparsed: BTreeMap<ChunkId, ReparsedEntry> = BTreeMap::new();
    for chunk_artifact in &artifact.chunks {
        let entry_file = &chunk_artifact.analysis.entry_file;
        let Some(file) = chunk_artifact.js.get_file(entry_file) else {
            continue;
        };
        if file.ast().is_some() {
            continue;
        }
        let Some(source) = file.source() else {
            continue;
        };
        let chunk_name = artifact.chunk_table.name(chunk_artifact.chunk_id);
        match js_ast::parse_js_module(chunk_name, source) {
            Ok(parsed) => {
                let analysis = analyze_program_shallow(&parsed);
                reparsed.insert(
                    chunk_artifact.chunk_id,
                    ReparsedEntry {
                        parsed,
                        imports: analysis.imports,
                        export_aliases: analysis.export_aliases,
                    },
                );
            }
            Err(error) => {
                eprintln!(
                    "cross_module_purity: chunk {chunk_name} entry failed to parse; \
                     leaving it opaque to the purity oracle: {error:#}"
                );
            }
        }
    }

    // Pass 2: assemble each chunk's oracle inputs from whichever body is
    // available (retained AST or re-parsed entry).
    let mut modules = BTreeMap::new();
    for chunk_artifact in &artifact.chunks {
        let chunk_name = artifact.chunk_table.name(chunk_artifact.chunk_id);
        let entry_file = &chunk_artifact.analysis.entry_file;
        let retained_ast = chunk_artifact
            .js
            .get_file(entry_file)
            .and_then(|file| file.ast());
        let (body, import_records, export_alias_records) =
            match (retained_ast, reparsed.get(&chunk_artifact.chunk_id)) {
                (Some(ast), _) => (
                    &ast.module.body,
                    chunk_artifact.analysis.imports.as_slice(),
                    chunk_artifact.analysis.export_aliases.as_slice(),
                ),
                (None, Some(entry)) => (
                    &entry.parsed.module.body,
                    entry.imports.as_slice(),
                    entry.export_aliases.as_slice(),
                ),
                (None, None) => continue,
            };
        let mut imports = BTreeMap::new();
        for import in import_records {
            // Same artifact-output resolution the specifier rewriter uses;
            // imports of paths outside the artifact (vendor packages) don't
            // resolve and are skipped — conservative.
            let Some(target) = indexes.resolve_runtime_import_reference(
                &import.source,
                chunk_artifact.chunk_id,
                entry_file,
                &artifact.chunk_table,
            ) else {
                continue;
            };
            let target_chunk_name = artifact.chunk_table.name(target.target_chunk_id);
            for specifier in &import.specifiers {
                let export = match specifier.kind {
                    ImportSpecifierKind::Named => specifier
                        .imported
                        .clone()
                        .unwrap_or_else(|| specifier.local.clone()),
                    ImportSpecifierKind::Default => "default".to_string(),
                    // Namespace imports are member-call sites (`ns.foo()`),
                    // outside the bare-Ident callee arm the verdicts feed.
                    ImportSpecifierKind::Namespace => continue,
                };
                imports.insert(
                    specifier.local.clone(),
                    ResolvedImport {
                        module: target_chunk_name.to_string(),
                        export,
                    },
                );
            }
        }
        // `export_aliases` only carries `export { x as y }` specifiers and
        // `export default`. Exported declarations (`export function foo`,
        // `export const foo`, `export class Foo`) are not aliases, so add them
        // directly from the body — their export name is the local name.
        let mut exports: BTreeMap<String, String> = export_alias_records
            .iter()
            .filter_map(|alias| {
                alias
                    .local
                    .clone()
                    .map(|local| (alias.exported.clone(), local))
            })
            .collect();
        for item in body {
            if let ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) = item {
                for name in exported_decl_names(&export_decl.decl) {
                    exports.entry(name.clone()).or_insert(name);
                }
            }
        }
        modules.insert(
            chunk_name.to_string(),
            ModulePurityFacts {
                body,
                imports,
                exports,
            },
        );
    }
    let asserted_pure: BTreeMap<String, BTreeSet<String>> = chunk_export_purity
        .iter()
        .filter(|(_, assertion)| !assertion.pure_exports.is_empty())
        .map(|(chunk, assertion)| (chunk.clone(), assertion.pure_exports.clone()))
        .collect();
    let asserted_members: BTreeMap<String, BTreeMap<String, BTreeSet<String>>> =
        chunk_export_purity
            .iter()
            .filter(|(_, assertion)| !assertion.pure_members.is_empty())
            .map(|(chunk, assertion)| (chunk.clone(), assertion.pure_members.clone()))
            .collect();
    let asserted_fluent: BTreeMap<String, BTreeSet<String>> = chunk_export_purity
        .iter()
        .filter(|(_, assertion)| !assertion.fluent_exports.is_empty())
        .map(|(chunk, assertion)| (chunk.clone(), assertion.fluent_exports.clone()))
        .collect();
    CrossModulePurities {
        bindings: resolve_imported_purities(&modules, &asserted_pure),
        members: resolve_asserted_member_purities(&modules, &asserted_members),
        fluent: resolve_asserted_fluent_bindings(&modules, &asserted_fluent),
    }
}
