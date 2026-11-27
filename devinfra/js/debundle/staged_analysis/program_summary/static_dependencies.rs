//! Resolved static-import dependency graph and dependency-closure ordering.

use artifact::reports::{StaticModuleRequest, StaticModuleRequestMode};
use artifact::{ArtifactIndexes, ChunkBundle};
use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet};

pub(super) fn resolved_target_is_entry(
    artifact: &ChunkBundle,
    target: &artifact::ResolvedImportReference,
) -> bool {
    artifact
        .find_chunk(target.target_chunk_id)
        .is_some_and(|chunk| chunk.analysis.entry_file == target.target_file)
}

pub(super) struct ResolvedStaticDependencies {
    pub(super) targets: BTreeMap<String, BTreeSet<String>>,
    pub(super) ordinary_requests: BTreeMap<String, BTreeSet<(String, String)>>,
    pub(super) complete_modules: BTreeSet<String>,
}

pub(super) fn resolved_static_dependency_graph(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    requests: &BTreeMap<String, (ChunkId, String, Vec<StaticModuleRequest>)>,
    analyzed_modules: &BTreeSet<String>,
    host_behavior: &spec::HostBehavior,
) -> ResolvedStaticDependencies {
    let mut dependencies = BTreeMap::new();
    let mut ordinary_dependencies = BTreeMap::new();
    let mut complete_modules = BTreeSet::new();
    for (module, (chunk, entry_file, module_requests)) in requests {
        let mut module_dependencies = BTreeSet::new();
        let mut module_ordinary_dependencies = BTreeSet::new();
        let mut is_complete = true;
        for request in module_requests {
            if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                is_complete = false;
            }
            let Some(target) = indexes.resolve_runtime_import_reference(
                &request.specifier,
                *chunk,
                entry_file,
                &artifact.chunk_table,
            ) else {
                if request.mode == StaticModuleRequestMode::OrdinaryEsm
                    && host_behavior
                        .reentry_free_module(&request.specifier)
                        .is_some()
                {
                    continue;
                }
                is_complete = false;
                continue;
            };
            if !resolved_target_is_entry(artifact, &target) {
                is_complete = false;
                continue;
            }
            let target_module = artifact
                .chunk_table
                .name(target.target_chunk_id)
                .to_string();
            module_dependencies.insert(target_module.clone());
            if request.mode == StaticModuleRequestMode::OrdinaryEsm {
                module_ordinary_dependencies
                    .insert((request.specifier.clone(), target_module.clone()));
            }
            if artifact.find_chunk(target.target_chunk_id).is_none()
                || !analyzed_modules.contains(&target_module)
            {
                is_complete = false;
            }
        }
        dependencies.insert(module.clone(), module_dependencies);
        ordinary_dependencies.insert(module.clone(), module_ordinary_dependencies);
        if is_complete {
            complete_modules.insert(module.clone());
        }
    }
    ResolvedStaticDependencies {
        targets: dependencies,
        ordinary_requests: ordinary_dependencies,
        complete_modules,
    }
}

pub(super) fn resolved_closure_precedes_importer(
    importer: &str,
    provider: &str,
    source_specifier: &str,
    dependencies: &ResolvedStaticDependencies,
) -> Option<BTreeSet<String>> {
    if !dependencies
        .ordinary_requests
        .get(importer)
        .is_some_and(|requests| {
            requests.contains(&(source_specifier.to_string(), provider.to_string()))
        })
    {
        return None;
    }
    let mut pending = vec![provider.to_string()];
    let mut visited = BTreeSet::new();
    while let Some(module) = pending.pop() {
        if module == importer {
            return None;
        }
        if !visited.insert(module.clone()) {
            continue;
        }
        if !dependencies.complete_modules.contains(&module) {
            return None;
        }
        pending.extend(dependencies.targets.get(&module)?.iter().cloned());
    }
    Some(visited)
}
