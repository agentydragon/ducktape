//! Plan what each lowered module needs to import from sibling modules,
//! the source chunk, or the residual entry. `plan_module_reference_needs`
//! walks binding uses that retain both the source and emitted Id.

use crate::body_facts::NaturalizedBodyFacts;
use analysis::{BindingId, BindingKind, ImportedSourceTarget, ModuleId};
use anyhow::{Context, Result};
use artifact::runtime_resources::{
    RuntimeResourceDestination, RuntimeResourceDestinations, RuntimeResourceKind,
    UnresolvedResourceReason, local_resource_path,
};
use artifact::{ArtifactIndexes, ArtifactSourceImportResolver, ChunkBundle};
use frontend_elaboration::source_ast::{RuntimeImportFacts, RuntimeImportInfo};
use gate::AssignmentAnalysis;
use source_identity::ChunkId;
use source_identity::StatementId;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Arc;
use swc_ecma_ast::*;

#[derive(Debug)]
pub(crate) struct ImportedReexport {
    pub(crate) local: String,
    pub(crate) imported_name: String,
    pub(crate) imported_source: ImportedSourceTarget,
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
    /// Canonical initialization or deferred import targets with no direct
    /// binding import in this body. Emit them as side-effect imports so actual
    /// ESM traversal has exactly the topology validated by the schedule.
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
    resource_destinations: BTreeMap<SourceImportResolutionKey, Arc<RuntimeResourceDestinations>>,
}

impl<'a> ArtifactSourceImportResolutionCache<'a> {
    pub(crate) fn new(artifact: &'a ChunkBundle, indexes: &'a ArtifactIndexes) -> Self {
        Self {
            artifact,
            indexes,
            resolutions: BTreeMap::new(),
            resolver: None,
            resource_destinations: BTreeMap::new(),
        }
    }

    pub(crate) fn runtime_resource_destinations(
        &mut self,
        caller_chunk: &str,
        caller_file: &str,
        source_path: &str,
    ) -> Result<Arc<RuntimeResourceDestinations>> {
        let key = (
            source_path.to_string(),
            caller_chunk.to_string(),
            caller_file.to_string(),
        );
        if let Some(destinations) = self.resource_destinations.get(&key) {
            return Ok(Arc::clone(destinations));
        }
        let id = self
            .artifact
            .chunk_table
            .get(caller_chunk)
            .with_context(|| format!("unknown caller chunk: {caller_chunk}"))?;
        let resources = self
            .artifact
            .chunk(id)?
            .analysis
            .runtime_facts
            .resources
            .clone();
        let destinations = resources
            .into_iter()
            .filter_map(|resource| {
                resource
                    .specifier
                    .map(|specifier| (resource.kind, specifier))
            })
            .map(|(kind, source)| {
                let destination = if kind == RuntimeResourceKind::Worker && !source.starts_with('.')
                {
                    RuntimeResourceDestination::External {
                        url: source.clone(),
                    }
                } else if kind == RuntimeResourceKind::Worker {
                    // Preserve the existing bare-Worker transform. Its browser
                    // document base is outside this module-URL proof.
                    match local_resource_path(
                        &artifact::paths::join_module_path(&[caller_chunk, caller_file]),
                        &source,
                        kind,
                    ) {
                        Some(path) => RuntimeResourceDestination::Unresolved {
                            reason: UnresolvedResourceReason::UnverifiedPath { path },
                        },
                        None => RuntimeResourceDestination::External {
                            url: source.clone(),
                        },
                    }
                } else if let Some(target) = self.resolve(&source, caller_chunk, caller_file)? {
                    RuntimeResourceDestination::Emitted { path: target.path }
                } else if let Some(path) = local_resource_path(source_path, &source, kind) {
                    RuntimeResourceDestination::Unresolved {
                        reason: UnresolvedResourceReason::UnverifiedPath { path },
                    }
                } else {
                    RuntimeResourceDestination::External {
                        url: source.clone(),
                    }
                };
                Ok(((kind, source), destination))
            })
            .collect::<Result<RuntimeResourceDestinations>>()?;
        let destinations = Arc::new(destinations);
        self.resource_destinations
            .insert(key, Arc::clone(&destinations));
        Ok(destinations)
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
    assignment_analysis: &AssignmentAnalysis,
    chunk_identity: ChunkId,
    module_count: usize,
) -> Result<Vec<Vec<ImportedReexport>>> {
    let mut by_module: Vec<Vec<ImportedReexport>> = (0..module_count).map(|_| Vec::new()).collect();
    let local_bindings = assignment_analysis
        .inputs()
        .local_bindings(chunk_identity)?;
    // Stable iteration order on the checked per-chunk catalogue (HashMap): the
    // recorded sequence determines the emit order of
    // `import { ... }` statements per module body and we want that
    // source-level shape pinned.
    let mut sorted_bindings: Vec<(&Id, &BindingKind)> = local_bindings.as_map().iter().collect();
    sorted_bindings.sort_by(|a, b| a.0.0.cmp(&b.0.0));
    for (id, kind) in sorted_bindings {
        // The body of this loop refers to the sym-typed `local` name
        // for ImportedReexport.local (a BindingName/String); pull it
        // out so the existing String-typed downstream works.
        let local = &id.0;
        let BindingKind::Imported {
            imported_name,
            imported_source,
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
            imported_source: imported_source.clone(),
            public_name: public_name.to_string(),
        });
    }
    Ok(by_module)
}

pub(crate) fn plan_module_reference_needs<'a>(
    module_index: usize,
    body_facts: &NaturalizedBodyFacts,
    assignment_analysis: &AssignmentAnalysis,
    declaration_by_name: &HashMap<Id, Vec<StatementId>>,
    binding_assignment: &HashMap<Id, usize>,
    entry_exports_by_original_local: &HashMap<Id, EntryExport>,
    runtime_imports: &'a RuntimeImportFacts,
) -> Result<ModuleReferenceNeeds<'a>> {
    let mut needs = ModuleReferenceNeeds::default();
    for reference in body_facts.binding_uses() {
        let Some(original_id) = reference.source_id() else {
            continue;
        };
        let body_id = reference.emitted_id();
        let name_str = body_id.0.as_ref();
        let binding_id = BindingId::new(
            assignment_analysis.inputs().chunk_identity(),
            original_id.clone(),
        );
        if let Some(ModuleId(provider_index)) =
            assignment_analysis.inputs().owner_of(&binding_id)?
        {
            if provider_index != module_index
                && let Some(provider) = assignment_analysis
                    .inputs()
                    .planned_module(ModuleId(provider_index))
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
        assignment_analysis,
        &needs.cross_module_imports_by_provider,
        !needs.residual_entry_imports.is_empty(),
        &mut needs.phantom_side_effect_providers,
    );

    Ok(needs)
}

/// Materialize every canonical import target that has no binding import.
/// Besides initialization promotion, deferred read closures can introduce lazy
/// import edges without a directly referenced local in the naturalized body.
/// Reading the retained target lists keeps phantom routing in the same model as
/// validation, including imports to true entry.
fn collect_phantom_side_effect_providers(
    module_index: usize,
    assignment_analysis: &AssignmentAnalysis,
    cross_module_imports_by_provider: &BTreeMap<usize, BTreeMap<String, String>>,
    has_entry_binding_import: bool,
    phantom_side_effect_providers: &mut BTreeSet<usize>,
) {
    let entry = assignment_analysis
        .assignment()
        .entry_destination()
        .module_id();
    for &target in assignment_analysis
        .import_schedule()
        .imports(ModuleId(module_index))
    {
        if !cross_module_imports_by_provider.contains_key(&target.0)
            && !(target == entry && has_entry_binding_import)
        {
            phantom_side_effect_providers.insert(target.0);
        }
    }
}
