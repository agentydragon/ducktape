use super::callable_summaries::ResolvedCallableSummaries;
use super::module_facts::{
    ModulePurityFacts, PreparationOptions, resolve_imported_primitive_array_spread_inputs,
};
use analysis::diagnostics::{ChunkDiagnostic, Diagnostics, ExportContract};
use analysis_domain::item_views::top_level_item_views;
use analysis_vocabulary::purity::Purity;
use anyhow::Result;
use purity::{ChunkCodeGraph, PurityEffectRules, PurityMode};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use value_flow::program_bindings::SourceModuleKey;

/// Compute, per module, the imported-binding purity map to feed that module's
/// callers. See the module docs for the shared effects closure.
///
/// `asserted_pure` carries author-asserted pure exports keyed by defining
/// module (`TransformSpec::chunk_export_purity`). Assertions are trusted
/// axioms: they enter the verdict map as `Pure` even when the export's
/// binding is not a classifiable chunk-top function (interop wrappers,
/// re-exported callables), and the effects closure never demotes them. An assertion
/// naming a module or export the program does not have records a diagnostic
/// as a dangling assertion (likely a typo or a stale entry) and ignored.
pub fn resolve_imported_purities(
    stable_imports: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    asserted_pure: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
    callable_summaries: &mut ResolvedCallableSummaries,
    diagnostics: &Diagnostics,
) -> Result<BTreeMap<SourceModuleKey, BTreeMap<String, Arc<Purity>>>> {
    // Only exported verdicts read by a named/default import can affect the
    // returned maps or another module's closure. Namespace member calls are
    // intentionally outside this oracle.
    let required: BTreeSet<_> = modules
        .values()
        .flat_map(|facts| facts.imports.values())
        .map(|target| (target.module.clone(), target.export.clone()))
        .collect();
    // Author-asserted axioms stay pinned Pure, even when no source body exists.
    let mut pinned: BTreeSet<(SourceModuleKey, String)> = BTreeSet::new();
    let mut export_purity: BTreeMap<(SourceModuleKey, String), Arc<Purity>> = BTreeMap::new();
    for (module, export_names) in asserted_pure {
        for export_name in export_names {
            let known_export = modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export_name));
            if !known_export {
                diagnostics.record_chunk(
                    module,
                    ChunkDiagnostic::DanglingExportContract {
                        contract: ExportContract::PureExports,
                        export: export_name.clone(),
                    },
                );
                continue;
            }
            export_purity.insert(
                (module.clone(), export_name.clone()),
                Arc::new(Purity::pure()),
            );
            pinned.insert((module.clone(), export_name.clone()));
        }
    }

    // Establish module-qualified function identities before extracting any
    // bodies, so imports become exact edges in one program-wide effect graph.
    let function_keys_by_module: BTreeMap<_, _> = modules
        .iter()
        .map(|(module, facts)| {
            let views = top_level_item_views(facts.body);
            (
                module.clone(),
                ChunkCodeGraph::inferred_function_keys(&views, module, PurityMode::Export),
            )
        })
        .collect();
    let inferred_export_keys: BTreeMap<_, _> = required
        .iter()
        .filter(|slot| !pinned.contains(*slot))
        .filter_map(|(module, export)| {
            let local = modules.get(module)?.exports.get(export)?;
            let function = function_keys_by_module.get(module)?.get(local)?;
            Some(((module.clone(), export.clone()), function.clone()))
        })
        .collect();
    let imported_array_write_fact_chunks: BTreeSet<_> = callable_summaries
        .selected_imported_primitive_array_spread_inputs
        .iter()
        .flat_map(|(consumer, inputs)| {
            std::iter::once(consumer.clone()).chain(inputs.iter().flat_map(|input| {
                input
                    .operands
                    .iter()
                    .map(|operand| operand.provider_chunk.clone())
            }))
        })
        .collect();

    // Authoritative source/native/captured-input facts are fixed before this
    // one effects closure starts. Declared chunk-local purity hints remain
    // absent from export mode, as before.
    let jobs: Vec<_> = modules
        .iter()
        .filter(|(key, _)| {
            imported_array_write_fact_chunks.contains(*key)
                || inferred_export_keys
                    .keys()
                    .any(|(module, _)| module == *key)
        })
        .map(|(key, facts)| {
            // Rebuilding a provider's execution index must retain the same captured
            // receiver and imported-call facts that established the program census.
            // A native-integrity result alone cannot refine the provider's local
            // mutation census, which would otherwise reintroduce unknown aliases.
            let inputs = analysis_vocabulary::inputs::AnalysisInputs {
                native_integrity: callable_summaries.native_integrity.clone(),
                inferred_set_constructions: callable_summaries
                    .inferred_set_constructions
                    .get(key)
                    .cloned()
                    .unwrap_or_default(),
                assumptions: analysis_vocabulary::inputs::AnalysisAssumptions {
                    captured_value_inputs: callable_summaries
                        .value_inputs
                        .get(key)
                        .cloned()
                        .unwrap_or_default(),
                    native_operation_inputs: callable_summaries
                        .native_operation_inputs
                        .get(key)
                        .cloned()
                        .unwrap_or_default(),
                    argument_inputs: callable_summaries
                        .argument_inputs
                        .get(key)
                        .cloned()
                        .unwrap_or_default(),
                    ..Default::default()
                },
                imported_callables: callable_summaries
                    .imported_callables
                    .get(key)
                    .cloned()
                    .unwrap_or_default(),
                ..Default::default()
            };
            (
                key,
                facts,
                inputs,
                callable_summaries.structural_flows.get(key).cloned(),
                imported_array_write_fact_chunks.contains(key),
            )
        })
        .collect();
    // Each provider prepares from its own body and inputs alone.
    let prepared: BTreeMap<_, _> = crate::swc_parallel::map_with_swc_globals(
        jobs,
        |(key, facts, inputs, flow, collect_write_sites)| {
            diagnostics.progress(
                key,
                progress::Phase::PrepareCrossModulePurity,
                progress::State::Start,
            );
            let facts_prepared = facts.prepare(
                &inputs,
                flow,
                PreparationOptions {
                    collect_imported_array_write_sites: collect_write_sites,
                },
            );
            diagnostics.progress(
                key,
                progress::Phase::PrepareCrossModulePurity,
                progress::State::End,
            );
            (key.clone(), facts_prepared)
        },
    )
    .into_iter()
    .collect();

    callable_summaries.imported_primitive_array_spread_inputs =
        resolve_imported_primitive_array_spread_inputs(
            modules,
            &prepared,
            &callable_summaries.selected_imported_primitive_array_spread_inputs,
        )?;

    let mut bodies = BTreeMap::new();
    let no_declared_pure = BTreeSet::new();
    let no_declared_pure_members = BTreeMap::new();
    for (module, prepared_facts) in &prepared {
        let facts = &modules[module];
        let imported_targets: BTreeMap<_, _> = facts
            .imports
            .iter()
            .filter_map(|(local, target)| {
                inferred_export_keys
                    .get(&(target.module.clone(), target.export.clone()))
                    .map(|key| (local.clone(), key.clone()))
            })
            .collect();
        let imported_purities: BTreeMap<_, _> = facts
            .imports
            .iter()
            .filter(|(_, target)| pinned.contains(&(target.module.clone(), target.export.clone())))
            .map(|(local, _)| (local.clone(), Arc::new(Purity::pure())))
            .collect();
        let shape_inputs = purity::PurityShapeInputs {
            shadowed: &prepared_facts.shadowed,
            declared_pure_new: &no_declared_pure,
            declared_pure_members: &no_declared_pure_members,
            imported_purities: &imported_purities,
            fluent_bindings: &no_declared_pure,
            set_iterable_proof: &prepared_facts.set_iterable_proof,
            local_class_construction_proof: &prepared_facts.local_class_construction_proof,
            plain_data_bindings: &prepared_facts.plain_data_bindings,
            plain_data_dependencies: &prepared_facts.plain_data_dependencies,
            plain_data_inference_uses: &prepared_facts.plain_data_inference_uses,
            plain_array_bindings: &prepared_facts.plain_array_bindings,
            stable_var_number_candidates: &BTreeSet::new(),
            stable_imports: stable_imports.get(module).unwrap_or(&no_declared_pure),
        };
        let call_context = purity::PurityCallContext {
            module,
            mode: PurityMode::Export,
            declared_pure: &no_declared_pure,
            imported_targets: &imported_targets,
        };
        let transfers = ChunkCodeGraph::effect_transfers(
            &prepared_facts.views,
            &shape_inputs,
            &call_context,
            facts.locator(),
        );
        bodies.extend(transfers);
    }
    // Every module-qualified function body shares this one finite effects
    // store/worklist, including mutually recursive imports across modules.
    let solved = PurityEffectRules::new(bodies).solve_to_purities();
    for (slot, function) in inferred_export_keys {
        if let Some(purity) = solved.get(&function) {
            export_purity.insert(slot, purity.clone());
        }
    }

    Ok(modules
        .iter()
        .map(|(key, facts)| (key.clone(), facts.imported_purities(&export_purity)))
        .collect())
}
