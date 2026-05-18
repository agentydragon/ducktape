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
    pub(super) runtime_reimports: BTreeMap<String, &'a RuntimeImportInfo>,
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
    schedule: &ChunkFactorization,
    module_count: usize,
) -> Vec<Vec<ImportedReexport>> {
    let mut by_module: Vec<Vec<ImportedReexport>> = (0..module_count).map(|_| Vec::new()).collect();
    // Stable iteration order on `schedule.analysis.bindings` (HashMap): the
    // recorded sequence determines the emit order of
    // `import { ... }` statements per module body and we want that
    // source-level shape pinned.
    let mut sorted_bindings: Vec<(&Id, &BindingKind)> = schedule.analysis.bindings.iter().collect();
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
            imported_name: imported_name.clone(),
            imported_from: imported_from.clone(),
            public_name: public_name.clone(),
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
    schedule: &ChunkFactorization,
    declaration_by_name: &HashMap<Id, usize>,
    binding_assignment: &HashMap<Id, usize>,
    entry_exports_by_original_local: &HashMap<Id, EntryExport>,
    runtime_imports: RuntimeImportLookup<'a>,
) -> ModuleReferenceNeeds<'a> {
    let mut needs = ModuleReferenceNeeds::default();
    for body_id in &body_facts.referenced_idents {
        // body_id is the hygiene-aware (sym, ctxt) of one referenced ident.
        // Spec-derived lookup tables (schedule.{owner_of, logical_module},
        // declaration_by_name, binding_assignment, entry_exports_by_original_local,
        // LogicalModule.rename_map) are still String-keyed by sym; convert at
        // each call. `runtime_imports.imports` IS Id-keyed.
        let name_str = body_id.0.as_ref();
        if let Some(ModuleId(LogicalModuleIndex(provider_index))) =
            schedule.analysis.owner_of(name_str)
        {
            // provider.rename_map is now Id-keyed; reconstruct the
            // provider's Id from the body ident's sym + ctxt. Within
            // a chunk all top-level bindings share the chunk's
            // top_level_mark, so body_id.1 matches the provider's
            // binding ctxt.
            let provider_key: Id = (body_id.0.clone(), body_id.1);
            if provider_index != module_index
                && let Some(provider) = schedule
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
            needs.runtime_reimports.insert(name_str.to_string(), info);
        }
    }
    needs
}
