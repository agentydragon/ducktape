//! Plan what each lowered module needs to import from sibling modules,
//! the source chunk, or the residual entry. `plan_module_reference_needs`
//! walks binding uses that retain both the source and emitted Id.

use crate::body_facts::NaturalizedBodyFacts;
use crate::imports::{RuntimeImportFacts, RuntimeImportInfo};
use analysis::{BindingKind, ModuleId};
use anyhow::{Context, Result};
use artifact::{ArtifactIndexes, ArtifactSourceImportResolver, ChunkBundle};
use gate::AssignmentAnalysis;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use swc_ecma_ast::*;

#[derive(Debug)]
pub(crate) struct ImportedReexport {
    pub(crate) local: String,
    pub(crate) imported_name: String,
    pub(crate) imported_from: String,
    pub(crate) public_name: String,
}

#[derive(Debug, Clone)]
pub(crate) struct EntryExport {
    pub(crate) local_name: String,
    pub(crate) exported_name: String,
}

#[derive(Default)]
pub(crate) struct ModuleReferenceNeeds<'a> {
    pub(crate) cross_module_imports_by_provider: BTreeMap<usize, BTreeMap<String, String>>,
    pub(crate) residual_entry_imports: BTreeMap<String, EntryExport>,
    pub(crate) missing_residual_exports: BTreeSet<String>,
    pub(crate) runtime_reimports: BTreeMap<Id, &'a RuntimeImportInfo>,
    /// Side-effect-only providers: modules this module has a constraining
    /// edge to (via at-init call promotion) but whose bindings aren't
    /// directly referenced in this module's body. Without an explicit
    /// ESM `import "./<provider>.js";` here, ESM DFS wouldn't see the
    /// dependency and would evaluate this module's body before the
    /// provider's body — a runtime TDZ on any of the provider's
    /// declared bindings the call chain transitively reads. See
    /// `accepted_spec_runs_under_node_test::early_entry_importer_…`.
    pub(crate) phantom_side_effect_providers: BTreeSet<usize>,
}

pub(crate) type SourceImportResolutionKey = (String, String, String);
/// A resolved source-chunk import. Keep the artifact resolver's tuple at this
/// boundary; downstream import planning should name the fields it uses.
#[derive(Clone)]
pub(crate) struct SourceImportTarget {
    pub(crate) chunk_id: String,
    pub(crate) entry_file: String,
    pub(crate) path: String,
}

pub(crate) type SourceImportResolution = Option<SourceImportTarget>;

pub(crate) struct ArtifactSourceImportResolutionCache<'a> {
    artifact: &'a ChunkBundle,
    indexes: &'a ArtifactIndexes,
    resolutions: BTreeMap<SourceImportResolutionKey, SourceImportResolution>,
    resolver: Option<ArtifactSourceImportResolver<'a>>,
}

impl<'a> ArtifactSourceImportResolutionCache<'a> {
    pub(crate) fn new(artifact: &'a ChunkBundle, indexes: &'a ArtifactIndexes) -> Self {
        Self {
            artifact,
            indexes,
            resolutions: BTreeMap::new(),
            resolver: None,
        }
    }

    pub(crate) fn resolve(
        &mut self,
        source: &str,
        caller_chunk_id: &str,
        caller_file: &str,
    ) -> Result<SourceImportResolution> {
        if source.is_empty() || (!source.starts_with('.') && !source.starts_with('/')) {
            return Ok(None);
        }
        let key = (
            source.to_string(),
            caller_chunk_id.to_string(),
            caller_file.to_string(),
        );
        if let Some(resolved) = self.resolutions.get(&key) {
            return Ok(resolved.clone());
        }
        if self.resolver.is_none() {
            self.resolver = Some(self.artifact.source_import_resolver(self.indexes));
        }
        let caller_chunk_id_interned = self
            .artifact
            .chunk_table
            .get(caller_chunk_id)
            .with_context(|| format!("unknown caller chunk: {caller_chunk_id}"))?;
        let resolved = self
            .resolver
            .as_ref()
            .expect("resolver initialized")
            .resolve(source, caller_chunk_id_interned, caller_file)
            .map(|(chunk_id, entry_file, path)| SourceImportTarget {
                chunk_id,
                entry_file,
                path,
            });
        self.resolutions.insert(key, resolved.clone());
        Ok(resolved)
    }
}

pub(crate) fn collect_imported_reexports_by_module(
    factorization: &AssignmentAnalysis,
    module_count: usize,
) -> Vec<Vec<ImportedReexport>> {
    let mut by_module: Vec<Vec<ImportedReexport>> = (0..module_count).map(|_| Vec::new()).collect();
    // Stable iteration order on `factorization.analysis().bindings()` (HashMap): the
    // recorded sequence determines the emit order of
    // `import { ... }` statements per module body and we want that
    // source-level shape pinned.
    let mut sorted_bindings: Vec<(&Id, &BindingKind)> =
        factorization.analysis().bindings().iter().collect();
    sorted_bindings.sort_by(|a, b| a.0.0.cmp(&b.0.0));
    for (id, kind) in sorted_bindings {
        // The body of this loop refers to the sym-typed `local` name
        // for ImportedReexport.local (a BindingName/String); pull it
        // out so the existing String-typed downstream works.
        let local = &id.0;
        let BindingKind::Imported {
            imported_name,
            imported_from,
            re_exporter,
            public_name,
        } = kind
        else {
            continue;
        };
        let ModuleId(index) = re_exporter;
        let Some(reexports) = by_module.get_mut(*index) else {
            continue;
        };
        reexports.push(ImportedReexport {
            local: local.to_string(),
            imported_name: imported_name.to_string(),
            imported_from: imported_from.clone(),
            public_name: public_name.to_string(),
        });
    }
    by_module
}

pub(crate) fn plan_module_reference_needs<'a>(
    module_index: usize,
    body_facts: &NaturalizedBodyFacts,
    factorization: &AssignmentAnalysis,
    declaration_by_name: &HashMap<Id, Vec<usize>>,
    binding_assignment: &HashMap<Id, usize>,
    entry_exports_by_original_local: &HashMap<Id, EntryExport>,
    runtime_imports: &'a RuntimeImportFacts,
) -> ModuleReferenceNeeds<'a> {
    let mut needs = ModuleReferenceNeeds::default();
    for reference in body_facts.binding_uses() {
        let Some(original_id) = reference.source_id() else {
            continue;
        };
        let body_id = reference.emitted_id();
        let name_str = body_id.0.as_ref();
        if let Some(ModuleId(provider_index)) = factorization.analysis().owner_of(original_id) {
            if provider_index != module_index
                && let Some(provider) = factorization
                    .analysis()
                    .logical_module(ModuleId(provider_index))
                && let Some(exported_name) = provider.rename_map.get(original_id)
            {
                needs
                    .cross_module_imports_by_provider
                    .entry(provider_index)
                    .or_default()
                    .insert(name_str.to_string(), exported_name.to_string());
            }
            continue;
        }

        if !body_facts.provides(body_id)
            && !binding_assignment.contains_key(original_id)
            && declaration_by_name.contains_key(original_id)
        {
            if let Some(entry_export) = entry_exports_by_original_local.get(original_id) {
                needs
                    .residual_entry_imports
                    .insert(name_str.to_string(), entry_export.clone());
            } else {
                needs.missing_residual_exports.insert(name_str.to_string());
            }
            continue;
        }

        if body_facts.imports(body_id) {
            continue;
        }
        let info = runtime_imports.imports.get(original_id);
        if let Some(info) = info {
            needs.runtime_reimports.insert(body_id.clone(), info);
        }
    }

    collect_phantom_side_effect_providers(
        module_index,
        factorization,
        &needs.cross_module_imports_by_provider,
        &mut needs.phantom_side_effect_providers,
    );

    needs
}

/// Phantom side-effect providers (Lemma 2's emit-side fix):
///
/// Walk this module's owners' outgoing constraining edges in the
/// owner graph. Any target module not already in
/// `cross_module_imports_by_provider` (and not residual) needs a
/// side-effect-only ESM import so the linker visits it as a
/// dependency. Without this, at-init promotion records the
/// constraint at the ducktape level but ESM doesn't see it (the
/// actual `import` for the read target lives in the residual
/// function decl's home, not in this module), so DFS might evaluate
/// this module's body before the provider's.
///
/// Skip residual: it's entry, the root of every chunk's import
/// tree. Adding a side-effect import of entry from a peeled module
/// is redundant — entry is always reachable.
fn collect_phantom_side_effect_providers(
    module_index: usize,
    factorization: &AssignmentAnalysis,
    cross_module_imports_by_provider: &BTreeMap<usize, BTreeMap<String, String>>,
    phantom_side_effect_providers: &mut BTreeSet<usize>,
) {
    let module_id = ModuleId(module_index);
    let residual = factorization.assignment().residual();
    let owner_graph = factorization.analysis().owner_graph();
    for (owner_id, owner_module) in factorization.assignment().iter() {
        if owner_module != module_id {
            continue;
        }
        for &edge_id in owner_graph.out_edges_of(owner_id) {
            let edge = owner_graph.edge(edge_id);
            if !edge.reason.constrains_init_order() {
                continue;
            }
            let target_module = factorization.assignment().of(edge.to);
            if target_module == module_id || target_module == residual {
                continue;
            }
            let ModuleId(target_index) = target_module;
            if cross_module_imports_by_provider.contains_key(&target_index) {
                continue;
            }
            phantom_side_effect_providers.insert(target_index);
        }
    }
}
