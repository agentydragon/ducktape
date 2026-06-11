//! Plan what each lowered module needs to import from sibling modules,
//! the source chunk, or the residual entry. `plan_module_reference_needs`
//! walks the post-naturalize body facts and resolves each referenced Id
//! to its provider. Includes the PR #1633 reverse-lookup bridge that
//! pairs a body Id with the heuristic-rename pre-sym to find the
//! runtime-import entry.

use super::*;

#[derive(Debug)]
pub(super) struct ImportedReexport {
    pub(super) local: String,
    pub(super) imported_name: String,
    pub(super) imported_from: String,
    pub(super) public_name: String,
}

#[derive(Debug, Clone)]
pub(super) struct EntryExport {
    pub(super) local_name: String,
    pub(super) exported_name: String,
}

#[derive(Default)]
pub(super) struct ModuleReferenceNeeds<'a> {
    pub(super) cross_module_imports_by_provider: BTreeMap<usize, BTreeMap<String, String>>,
    pub(super) residual_entry_imports: BTreeMap<String, EntryExport>,
    pub(super) missing_residual_exports: BTreeSet<String>,
    pub(super) runtime_reimports: BTreeMap<Id, &'a RuntimeImportInfo>,
    /// Side-effect-only providers: modules this module has a constraining
    /// edge to (via at-init call promotion) but whose bindings aren't
    /// directly referenced in this module's body. Without an explicit
    /// ESM `import "./<provider>.js";` here, ESM DFS wouldn't see the
    /// dependency and would evaluate this module's body before the
    /// provider's body — a runtime TDZ on any of the provider's
    /// declared bindings the call chain transitively reads. See
    /// `accepted_spec_runs_under_node_test::early_entry_importer_…`.
    pub(super) phantom_side_effect_providers: BTreeSet<usize>,
}

pub(super) type SourceImportResolutionKey = (String, String, String);
pub(super) type SourceImportResolution = Option<(String, String, String)>;

pub(super) struct ArtifactSourceImportResolutionCache<'a> {
    artifact: &'a ChunkBundle,
    indexes: &'a ArtifactIndexes,
    resolutions: BTreeMap<SourceImportResolutionKey, SourceImportResolution>,
    resolver: Option<ArtifactSourceImportResolver<'a>>,
}

impl<'a> ArtifactSourceImportResolutionCache<'a> {
    pub(super) fn new(artifact: &'a ChunkBundle, indexes: &'a ArtifactIndexes) -> Self {
        Self {
            artifact,
            indexes,
            resolutions: BTreeMap::new(),
            resolver: None,
        }
    }

    pub(super) fn resolve(
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
            .resolve(source, caller_chunk_id_interned, caller_file)?;
        self.resolutions.insert(key, resolved.clone());
        Ok(resolved)
    }
}

pub(super) fn collect_imported_reexports_by_module(
    factorization: &ChunkFactorization,
    module_count: usize,
) -> Vec<Vec<ImportedReexport>> {
    let mut by_module: Vec<Vec<ImportedReexport>> = (0..module_count).map(|_| Vec::new()).collect();
    // Stable iteration order on `factorization.analysis.bindings` (HashMap): the
    // recorded sequence determines the emit order of
    // `import { ... }` statements per module body and we want that
    // source-level shape pinned.
    let mut sorted_bindings: Vec<(&Id, &BindingKind)> =
        factorization.analysis.bindings.iter().collect();
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
        let ModuleId(LogicalModuleIndex(index)) = re_exporter;
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

/// `naturalize_module_body`) provides the reverse lookup when the
/// body has been renamed. The long-term fix is the
/// collect→validate→execute-once rename pipeline tracked in
/// <devinfra/js/debundle/TODO.md> ("Rename pipeline").
pub(super) struct RuntimeImportLookup<'a> {
    pub(super) imports: &'a RuntimeImportFacts,
    pub(super) heuristic_renames: &'a BTreeMap<String, String>,
}

pub(super) fn plan_module_reference_needs<'a>(
    module_index: usize,
    body_facts: &ModuleBodyFacts,
    factorization: &ChunkFactorization,
    declaration_by_name: &HashMap<Id, usize>,
    binding_assignment: &HashMap<Id, usize>,
    entry_exports_by_original_local: &HashMap<Id, EntryExport>,
    runtime_imports: RuntimeImportLookup<'a>,
) -> ModuleReferenceNeeds<'a> {
    let mut needs = ModuleReferenceNeeds::default();
    for body_id in &body_facts.referenced_idents {
        // body_id is the hygiene-aware (sym, ctxt) of one referenced ident.
        // Some spec-derived lookup tables are still String-keyed by sym
        // (`declaration_by_name`, `binding_assignment`,
        // `entry_exports_by_original_local`), while owner lookup and
        // `runtime_imports.imports` are Id-keyed.
        let name_str = body_id.0.as_ref();
        if let Some(ModuleId(LogicalModuleIndex(provider_index))) =
            factorization.analysis.owner_of(body_id)
        {
            // provider.rename_map is now Id-keyed; reconstruct the
            // provider's Id from the body ident's sym + ctxt. Within
            // a chunk all top-level bindings share the chunk's
            // top_level_mark, so body_id.1 matches the provider's
            // binding ctxt.
            let provider_key: Id = (body_id.0.clone(), body_id.1);
            if provider_index != module_index
                && let Some(provider) = factorization
                    .analysis
                    .logical_module(LogicalModuleIndex(provider_index))
                && let Some(exported_name) = provider.rename_map.get(&provider_key)
            {
                needs
                    .cross_module_imports_by_provider
                    .entry(provider_index)
                    .or_default()
                    .insert(name_str.to_string(), exported_name.to_string());
            }
            continue;
        }

        if !body_facts.provided_locals.contains(body_id)
            && !binding_assignment.contains_key(body_id)
            && declaration_by_name.contains_key(body_id)
        {
            if let Some(entry_export) = entry_exports_by_original_local.get(body_id) {
                needs
                    .residual_entry_imports
                    .insert(name_str.to_string(), entry_export.clone());
            } else {
                needs.missing_residual_exports.insert(name_str.to_string());
            }
            continue;
        }

        if body_facts.imported_locals.contains(body_id) {
            continue;
        }
        // Direct hit when the body still uses the original local. Fall back to
        // a reverse lookup through `heuristic_renames` when a naturalizer pass
        // renamed the binding (e.g. `sA` → `propKeyA` from a return-object
        // alias collapse): the body now references `(propKeyA, ctxt=X)`, but
        // the source chunk's import map is keyed by `(sA, ctxt=X)`. The
        // recovered `RuntimeImportInfo` keeps `imported = "sA"`, so emit
        // produces `import { sA as propKeyA } from "<src>"` via
        // `runtime_reimport_specifier`'s local-vs-imported branch. The
        // naturalizer's in-place sym mutation preserves `ctxt`, so we
        // reconstruct the pre-rename Id by pairing the pre-rename sym
        // (looked up in heuristic_renames as the entry whose value
        // matches the body sym) with the body Id's own ctxt.
        let info = runtime_imports.imports.imports.get(body_id).or_else(|| {
            runtime_imports
                .heuristic_renames
                .iter()
                .find(|(_, post)| post.as_str() == name_str)
                .and_then(|(pre, _)| {
                    let pre_id: Id = (pre.as_str().into(), body_id.1);
                    runtime_imports.imports.imports.get(&pre_id)
                })
        });
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
    factorization: &ChunkFactorization,
    cross_module_imports_by_provider: &BTreeMap<usize, BTreeMap<String, String>>,
    phantom_side_effect_providers: &mut BTreeSet<usize>,
) {
    let module_id = ModuleId(LogicalModuleIndex(module_index));
    let residual = factorization.partition.residual();
    let owner_graph = &factorization.analysis.owner_graph;
    for (owner_id, owner_module) in factorization.partition.iter() {
        if owner_module != module_id {
            continue;
        }
        for &edge_id in owner_graph.out_edges_of(owner_id) {
            let edge = owner_graph.edge(edge_id);
            if !edge.reason.constrains_init_order() {
                continue;
            }
            let target_module = factorization.partition.of(edge.to);
            if target_module == module_id || target_module == residual {
                continue;
            }
            let ModuleId(LogicalModuleIndex(target_index)) = target_module;
            if cross_module_imports_by_provider.contains_key(&target_index) {
                continue;
            }
            phantom_side_effect_providers.insert(target_index);
        }
    }
}
