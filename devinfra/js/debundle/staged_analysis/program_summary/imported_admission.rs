//! Imported values, constructors and provider closures admitted at importers.

use super::static_dependencies::{ResolvedStaticDependencies, resolved_closure_precedes_importer};
use analysis_projection::AnalysisProjection;
use analysis_vocabulary::inputs::{ImportedPrimitiveRead, ResolvedInitializerCallEffectInput};
use analysis_vocabulary::value::Value;
use anyhow::{Result, ensure};
use cross_module_purity::callable_summaries::ResolvedCallableSummaries;
use cross_module_purity::export_primitives::ExportPrimitiveValue;
use cross_module_purity::module_facts::ModulePurityFacts;
use std::collections::{BTreeMap, BTreeSet};

pub(super) fn imported_primitive_reads(
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    export_primitives: &BTreeMap<(String, String), ExportPrimitiveValue>,
    static_dependencies: &ResolvedStaticDependencies,
) -> BTreeMap<String, BTreeMap<String, ImportedPrimitiveRead>> {
    modules
        .iter()
        .map(|(importer, module)| {
            let reads = module
                .imports
                .iter()
                .filter_map(|(local, target)| {
                    let primitive =
                        export_primitives.get(&(target.module.clone(), target.export.clone()))?;
                    if primitive.kinds.is_empty() || primitive.source_cells.is_empty() {
                        return None;
                    }
                    let dependency_closure = resolved_closure_precedes_importer(
                        importer,
                        &target.module,
                        &target.source_specifier,
                        static_dependencies,
                    )?;
                    if !primitive.source_cells.iter().all(|cell| {
                        dependency_closure.contains(&cell.module)
                            && export_primitives
                                .get(&(cell.module.clone(), cell.export.clone()))
                                .is_some_and(|source| !source.kinds.is_empty())
                    }) {
                        return None;
                    }
                    Some((
                        local.clone(),
                        ImportedPrimitiveRead {
                            kinds: primitive.kinds,
                            provider_module: target.module.clone(),
                            dependency_closure,
                            source_cells: primitive.source_cells.clone(),
                        },
                    ))
                })
                .collect();
            (importer.clone(), reads)
        })
        .collect()
}

/// Raw stable export kinds are useful while resolving copy and re-export
/// chains, but they do not prove that an imported binding's GetValue is
/// available at its consumer. Keep the existing dependency-closure read
/// capability as the single join at the candidate-input boundary.
pub(super) fn admitted_imported_values(
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    primitive_reads: &BTreeMap<String, BTreeMap<String, ImportedPrimitiveRead>>,
    export_values: &BTreeMap<(String, String), Value>,
) -> BTreeMap<String, BTreeMap<String, Value>> {
    modules
        .iter()
        .map(|(name, module)| {
            let proved_reads = primitive_reads.get(name);
            let values = module
                .imported_values(export_values)
                .into_iter()
                .filter(|(local, value)| {
                    proved_reads
                        .and_then(|reads| reads.get(local))
                        .is_some_and(|read| *value == Value::Prim(read.kinds))
                })
                .collect();
            (name.clone(), values)
        })
        .collect()
}
/// A structurally selected member's trusted constructor contract follows
/// every export of that resolved binding. The importer sees its local
/// binding name; neither spec nor analysis needs the minified export name.
pub(super) fn imported_pure_new_constructors(
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    declared_pure_new_bindings: &BTreeMap<String, BTreeSet<String>>,
) -> BTreeMap<String, BTreeSet<String>> {
    modules
        .iter()
        .map(|(importer, facts)| {
            let locals = facts
                .imports
                .iter()
                .filter_map(|(local, target)| {
                    let binding = modules.get(&target.module)?.exports.get(&target.export)?;
                    declared_pure_new_bindings
                        .get(&target.module)?
                        .contains(binding)
                        .then(|| local.clone())
                })
                .collect();
            (importer.clone(), locals)
        })
        .collect()
}

/// A fresh result cannot be allocated at an importer AST site when all
/// provider code is in this complete static closure, which excludes that
/// importer. Dynamic scope cannot supply the missing identity proof.
pub(super) fn closed_factory_imports(
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    projection: &AnalysisProjection<'_>,
    lexically_closed_modules: &BTreeSet<String>,
    static_dependencies: &ResolvedStaticDependencies,
) -> BTreeMap<String, BTreeSet<String>> {
    let chunk_export_purity = projection.chunk_export_purity;
    modules
        .iter()
        .map(|(module, facts)| {
            let mut closures = BTreeMap::new();
            let imports = facts
                .imports
                .iter()
                .filter_map(|(local, target)| {
                    if !lexically_closed_modules.contains(module)
                        || !chunk_export_purity
                            .get(&target.module)
                            .is_some_and(|policy| {
                                policy.callback_factory_exports.contains(&target.export)
                                    || policy.callback_factory_members.contains_key(&target.export)
                            })
                    {
                        return None;
                    }
                    let closed = *closures
                        .entry((&target.module, &target.source_specifier))
                        .or_insert_with(|| {
                            resolved_closure_precedes_importer(
                                module,
                                &target.module,
                                &target.source_specifier,
                                static_dependencies,
                            )
                            .is_some_and(|closure| {
                                closure
                                    .iter()
                                    .all(|provider| lexically_closed_modules.contains(provider))
                            })
                        });
                    closed.then(|| local.clone())
                })
                .collect();
            (module.clone(), imports)
        })
        .collect()
}

/// Rejects initializer-call-effect inputs that a source-known original
/// provider write contradicts, and imported primitive array spreads whose
/// providers lack a complete static initialization closure before the
/// consumer.
pub(super) fn check_provider_closures(
    initializer_call_effect_inputs: &BTreeMap<String, Vec<ResolvedInitializerCallEffectInput>>,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    callable_summaries: &ResolvedCallableSummaries,
    static_dependencies: &ResolvedStaticDependencies,
) -> Result<()> {
    for inputs in initializer_call_effect_inputs.values() {
        for input in inputs {
            if let Some((module, export, reason)) = known_original_provider_write(
                &input.provider_chunk,
                &input.provider_export,
                modules,
                callable_summaries,
                &mut BTreeSet::new(),
            ) {
                anyhow::bail!(
                    "initializer_call_effect_inputs {}:{} is contradicted by source-known original provider write {module}:{export}: {reason}",
                    input.reference.defining_chunk,
                    input.reference.input_index,
                );
            }
        }
    }
    for (consumer, inputs) in &callable_summaries.imported_primitive_array_spread_inputs {
        for input in inputs {
            for operand in &input.evidence.operands {
                ensure!(
                    resolved_closure_precedes_importer(
                        consumer,
                        &operand.provider_module,
                        &operand.source_specifier,
                        static_dependencies,
                    )
                    .is_some(),
                    "{} requires direct provider {} to have a complete acyclic ordinary static initialization closure before consumer {}",
                    input.evidence.spec_location,
                    operand.provider_module,
                    consumer
                );
            }
        }
    }
    Ok(())
}

fn known_original_provider_write(
    module: &str,
    export: &str,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    known: &ResolvedCallableSummaries,
    visited: &mut BTreeSet<(String, String)>,
) -> Option<(String, String, String)> {
    if !visited.insert((module.to_owned(), export.to_owned())) {
        return None;
    }
    if let Some(reason) = known.known_original_external_write(module, export) {
        return Some((module.to_owned(), export.to_owned(), reason.to_owned()));
    }
    let local = modules.get(module)?.exports.get(export)?;
    let target = modules.get(module)?.imports.get(local)?;
    known_original_provider_write(&target.module, &target.export, modules, known, visited)
}
