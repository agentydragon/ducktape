//! Make imports of spec-named cross-chunk exports readable without changing
//! the chunk's legacy public surface. The target entry retains its original
//! export and adds a second name for the same binding; this pass then uses
//! that alias only in importers that are part of the materialization set.

use super::super::scope_names::{collect_nested_binding_names, collect_occupied_local_names};
use crate::rename_ledger::{
    RenameIntent, RenameLedger, RenameOrigin, RenameScope, ScopeOccupancy, SealValidation,
};
use crate::visitors::{IdentifierRenamer, RenameCaptureProbe};
use analysis::top_level_id;
use anyhow::{Context, Result};
use artifact::reports::{CandidateImportNaturalizationReceipt, SelectedModuleLowering};
use artifact::{ArtifactIndexes, ChunkBundle};
use binding_targets::module_export_name;
use js_ast::is_binding_identifier;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::{DUMMY_SP, Span, Spanned, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMutWith, VisitWith};

const CROSS_CHUNK_IMPORT_RENAME_CONTRIBUTOR: &str = "spec-named cross-chunk import readability";

/// Rewrite named imports in processed chunks when the resolved target entry
/// exposes both its legacy export and a spec-assigned readable alias.
///
/// The selected target-export names come from the lowering records; the
/// same-local export pair in the target entry associates each readable name
/// with its legacy export. Import rewrites are cosmetic and submitted as
/// heuristic intents. Ambiguous target aliases, occupied names, nested
/// bindings, and same-name collisions therefore conservatively retain the
/// original import and local binding.
pub fn naturalize_cross_chunk_imports(
    artifact: &mut ChunkBundle,
    indexes: &ArtifactIndexes,
    selected_lowerings: &[SelectedModuleLowering],
    processed_chunks: &BTreeSet<String>,
) -> Result<Vec<CandidateImportNaturalizationReceipt>> {
    let mut receipts = Vec::new();
    let mut alias_candidates = BTreeMap::<String, BTreeMap<String, BTreeSet<String>>>::new();
    for lowering in selected_lowerings {
        let aliases = alias_candidates
            .entry(lowering.chunk_id.clone())
            .or_default();
        for (legacy, readable) in &lowering.cross_chunk_import_aliases {
            aliases
                .entry(legacy.clone())
                .or_default()
                .insert(readable.clone());
        }
    }
    let legacy_to_readable_by_chunk: BTreeMap<String, BTreeMap<String, String>> = alias_candidates
        .into_iter()
        .map(|(chunk, candidates)| {
            let aliases = candidates
                .into_iter()
                .filter_map(|(legacy, names)| {
                    (names.len() == 1).then(|| (legacy, names.into_iter().next().unwrap()))
                })
                .collect();
            (chunk, aliases)
        })
        .collect();

    // Resolve original static source imports first, then emitted entry paths
    // already produced by lowering. Both use artifact indexes; entry aliases
    // never authorize changing an import from a different logical-module file.
    let mut pending = Vec::<PendingImportRename>::new();
    let resolver = artifact.source_import_resolver(indexes);
    for chunk in &artifact.chunks {
        let chunk_name = artifact.chunk_table.name(chunk.chunk_id).to_string();
        if !processed_chunks.contains(&chunk_name) {
            continue;
        }
        for file in &chunk.js.files {
            let Some(module) = file.ast().map(|parsed| &parsed.module) else {
                continue;
            };
            for (item_index, item) in module.body.iter().enumerate() {
                let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
                    continue;
                };
                let Some(src) = import.src.value.as_str() else {
                    continue;
                };
                let target_chunk = if let Some((target_chunk, _, _)) =
                    resolver.resolve(src, chunk.chunk_id, &file.path)
                {
                    target_chunk
                } else {
                    // Lowered modules already import emitted paths. The
                    // aliases below describe the provider's entry exports,
                    // so a resolved logical-module file is not eligible.
                    let Some(target) = indexes.resolve_runtime_import_reference(
                        src,
                        chunk.chunk_id,
                        &file.path,
                        &artifact.chunk_table,
                    ) else {
                        continue;
                    };
                    let Some(provider) = artifact.find_chunk(target.target_chunk_id) else {
                        continue;
                    };
                    if target.target_file != provider.js.entry_file {
                        continue;
                    }
                    artifact
                        .chunk_table
                        .name(target.target_chunk_id)
                        .to_string()
                };
                if target_chunk == chunk_name {
                    continue;
                }
                let Some(target_exports) = legacy_to_readable_by_chunk.get(&target_chunk) else {
                    continue;
                };
                for (specifier_index, specifier) in import.specifiers.iter().enumerate() {
                    let ImportSpecifier::Named(named) = specifier else {
                        continue;
                    };
                    let imported = named
                        .imported
                        .as_ref()
                        .map(module_export_name)
                        .unwrap_or_else(|| named.local.sym.to_string());
                    let Some(readable) = target_exports.get(&imported) else {
                        continue;
                    };
                    if named.local.sym.as_ref() == readable {
                        pending.push(PendingImportRename {
                            chunk_id: chunk_name.clone(),
                            file: file.path.clone(),
                            item_index,
                            specifier_index,
                            local: named.local.sym.to_string(),
                            imported: imported.clone(),
                            readable: readable.clone(),
                            already_readable: true,
                        });
                    } else {
                        pending.push(PendingImportRename {
                            chunk_id: chunk_name.clone(),
                            file: file.path.clone(),
                            item_index,
                            specifier_index,
                            local: named.local.sym.to_string(),
                            imported: imported.clone(),
                            readable: readable.clone(),
                            already_readable: false,
                        });
                    }
                }
            }
        }
    }
    pending.sort_by(|left, right| {
        (
            &left.chunk_id,
            &left.file,
            &left.readable,
            &left.imported,
            &left.local,
        )
            .cmp(&(
                &right.chunk_id,
                &right.file,
                &right.readable,
                &right.imported,
                &right.local,
            ))
    });

    // Group by importer file. The ledger seals each file's candidate locals
    // together, so two imported bindings competing for one readable name are
    // both rejected by the heuristic target-collision rule instead of
    // depending on source traversal order.
    let mut by_file = BTreeMap::<(String, String), Vec<PendingImportRename>>::new();
    for candidate in pending {
        by_file
            .entry((candidate.chunk_id.clone(), candidate.file.clone()))
            .or_default()
            .push(candidate);
    }
    for ((chunk_name, file_path), candidates) in by_file {
        let chunk_id = artifact
            .chunk_table
            .get(&chunk_name)
            .with_context(|| format!("unknown importer chunk {chunk_name}"))?;
        let file = artifact
            .js_chunk_mut(chunk_id)?
            .files
            .iter_mut()
            .find(|file| file.path == file_path)
            .with_context(|| format!("missing importer file {chunk_name}/{file_path}"))?;
        let Some(parsed) = file.ast_mut() else {
            continue;
        };
        let root_names = collect_occupied_local_names(&parsed.module.body);
        let nested_names = collect_nested_binding_names(&parsed.module.body);
        let mut ledger = RenameLedger::default();
        for candidate in &candidates {
            if candidate.already_readable {
                continue;
            }
            if !is_binding_identifier(&candidate.readable) {
                continue;
            }
            // The desired spelling comes from an explicit spec name, but
            // changing the consumer's local is still cosmetic: on any
            // collision the original import remains valid. Heuristic
            // priority is therefore intentional; using Explicit would make
            // a cosmetic importer collision reject an otherwise valid spec.
            ledger.submit(RenameIntent {
                scope: RenameScope::Chunk,
                from: top_level_id(&candidate.local, parsed.top_level_mark),
                to: candidate.readable.as_str().into(),
                origin: RenameOrigin::Heuristic {
                    contributor: CROSS_CHUNK_IMPORT_RENAME_CONTRIBUTOR,
                },
            });
        }
        let pending_renames = ledger.pending_renames(&RenameScope::Chunk);
        let unresolved_context = SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
        let mut capture_probe = RenameCaptureProbe::new(&pending_renames, unresolved_context);
        for item in &parsed.module.body {
            item.visit_with(&mut capture_probe);
        }
        let captured = capture_probe.captured;
        let sealed = match ledger.seal(&SealValidation {
            occupancy: BTreeMap::from([(
                RenameScope::Chunk,
                ScopeOccupancy::Body {
                    label: format!("{chunk_name}/{file_path}"),
                    root: root_names,
                    nested: nested_names,
                    captured,
                },
            )]),
            reserved: BTreeSet::new(),
        }) {
            Ok(sealed) => sealed,
            Err(_) => continue,
        };
        let renames = sealed
            .scope_renames(&RenameScope::Chunk)
            .unwrap_or_default();
        let approved: BTreeMap<(usize, usize), String> = candidates
            .iter()
            .filter(|candidate| {
                candidate.already_readable
                    || renames.get(&top_level_id(&candidate.local, parsed.top_level_mark))
                        == Some(&candidate.readable.as_str().into())
            })
            .map(|candidate| {
                (
                    (candidate.item_index, candidate.specifier_index),
                    candidate.readable.clone(),
                )
            })
            .collect();
        for candidate in &candidates {
            if let Some(to_imported_name) =
                approved.get(&(candidate.item_index, candidate.specifier_index))
            {
                let from_local = top_level_id(&candidate.local, parsed.top_level_mark);
                let item = parsed.module.body.get(candidate.item_index);
                let item_span = item.map(Spanned::span).unwrap_or(DUMMY_SP);
                let specifier_span = item
                    .and_then(|item| match item {
                        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => import
                            .specifiers
                            .get(candidate.specifier_index)
                            .map(Spanned::span),
                        _ => None,
                    })
                    .unwrap_or(DUMMY_SP);
                let mut use_spans = IdentSpanCollector {
                    wanted: from_local.clone(),
                    spans: Vec::new(),
                };
                parsed.module.visit_with(&mut use_spans);
                receipts.push(CandidateImportNaturalizationReceipt {
                    chunk: chunk_id,
                    file: file_path.clone(),
                    item_index: candidate.item_index,
                    item_span,
                    specifier_index: candidate.specifier_index,
                    specifier_span,
                    from_local,
                    to_local: top_level_id(to_imported_name, parsed.top_level_mark),
                    from_imported_name: candidate.imported.clone(),
                    to_imported_name: to_imported_name.clone(),
                    rewritten_use_spans: use_spans.spans,
                });
            }
        }
        if !renames.is_empty() {
            let mut renamer = IdentifierRenamer::new(&renames, unresolved_context);
            for item in &mut parsed.module.body {
                item.visit_mut_with(&mut renamer);
            }
            debug_assert!(renamer.captured.is_empty());
        }
        for ((item_index, specifier_index), readable) in approved {
            let Some(ModuleItem::ModuleDecl(ModuleDecl::Import(import))) =
                parsed.module.body.get_mut(item_index)
            else {
                continue;
            };
            let Some(ImportSpecifier::Named(named)) = import.specifiers.get_mut(specifier_index)
            else {
                continue;
            };
            named.imported = if named.local.sym.as_ref() == readable {
                None
            } else {
                Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                    readable.into(),
                    DUMMY_SP,
                )))
            };
        }
    }
    Ok(())
}

struct IdentSpanCollector {
    wanted: Id,
    spans: Vec<Span>,
}

impl Visit for IdentSpanCollector {
    fn visit_ident(&mut self, ident: &Ident) {
        if ident.to_id() == self.wanted {
            self.spans.push(ident.span);
        }
    }
}

#[derive(Debug)]
struct PendingImportRename {
    chunk_id: String,
    file: String,
    item_index: usize,
    specifier_index: usize,
    local: String,
    imported: String,
    readable: String,
    already_readable: bool,
}
