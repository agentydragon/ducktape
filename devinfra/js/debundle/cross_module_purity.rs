//! Program-level cross-module function-purity oracle.
//!
//! The per-chunk purity classifier ([`crate::purity::ChunkCodeGraph`])
//! treats a call to an imported binding as `unknown_call` — it cannot see
//! into the other module's body. On a real app bundle that is the dominant
//! impurity (every `memo(fn)` / `forwardRef(fn)` factory wrap), and via the
//! dataflow-aware S-chain it over-merges atomic factor groups.
//!
//! This oracle closes that gap. Given every module's entry body plus its
//! resolved imports and exports, it computes, per module, the purity verdict
//! for each imported function binding — the map fed to that module's
//! [`ChunkCodeGraph::build_full`] (`imported_purities`).
//!
//! ## Algorithm — greatest fixpoint
//!
//! A verdict moves from optimistic `Pure` toward `NotPure`; pure verdicts may
//! also gain a finite set of author-input dependencies. Each round rebuilds
//! every module's `ChunkCodeGraph` with imported purities from the current
//! export summaries. It demotes newly impure exports and unions dependencies
//! into still-pure exports, so both changes are monotone and the iteration
//! converges. Starting optimistic is what lets a function that is pure except
//! for calling a same-cycle peer stay `Pure`.
//!
//! Soundness is conservative by construction: an import whose target module or
//! export is absent (external/vendor module not analyzed, a non-function
//! export, an unresolved re-export) simply gets no entry, so the call stays
//! `unknown_call` exactly as before. The oracle only ever *removes*
//! false-impurity; it never asserts purity it did not derive from a body.

use crate::inference::{IterativeContext, IterativeInference, IterativeRules};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use anyhow::{Result, bail};
use js_ast::SourceLineIndex;
use swc_common::{BytePos, Span};
use swc_ecma_ast::ModuleItem;

use crate::SourceLocation;
use crate::analysis_inputs::{
    CallableSummary, CallbackFactoryEvidence, CallbackObjectArguments, DeferredRecordMember,
    NamespaceWrapperSummary, NativeIntegrity, ResolvedBindingValueInput, ReturnedRecordSummary,
    SelectedBindingValueInput,
};
use crate::facts::{
    ChunkValueFlow, TopLevelItemView, compute_shadowed_globals, top_level_item_views,
};
use crate::purity::{
    ChunkCodeGraph, Purity, collect_plain_array_bindings, collect_plain_data_bindings,
};

/// Stable key identifying a module in the program (the chunk name).
pub type ModuleKey = String;

/// An import of a single named/default binding, already resolved to the
/// module that defines it and the export read. Namespace imports
/// (`import * as ns`) are intentionally not represented: their call sites are
/// member calls (`ns.foo()`), not the bare-`Ident` callee the imported-purity
/// arm handles.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedImport {
    /// The module that defines the imported binding.
    pub module: ModuleKey,
    /// The export name read from that module.
    pub export: String,
}

/// One module's purity-relevant surface for the oracle.
pub struct ModulePurityFacts<'a> {
    /// The module entry's top-level items (caller-owned parsed AST).
    pub body: &'a [ModuleItem],
    /// Local import binding name → the export it resolves to.
    pub imports: BTreeMap<String, ResolvedImport>,
    /// Export name → the local binding this module re-exports under it.
    pub exports: BTreeMap<String, String>,
    /// Origin of direct function-body reasons, if the caller retained the
    /// source line index. Imports reuse these locations unchanged.
    pub source: Option<(String, SourceLineIndex)>,
}

struct PreparedModulePurityFacts<'a> {
    views: Vec<TopLevelItemView<'a>>,
    shadowed: BTreeSet<&'static str>,
    set_iterable_proof: crate::facts::SetIterableProof,
    local_class_construction_proof: crate::facts::LocalClassConstructionProof,
    plain_data_bindings: Vec<String>,
    plain_array_bindings: BTreeSet<String>,
}

impl<'a> ModulePurityFacts<'a> {
    fn prepare(
        &self,
        inputs: &crate::analysis_inputs::AnalysisInputs,
    ) -> PreparedModulePurityFacts<'a> {
        let views = top_level_item_views(self.body);
        let shadowed = compute_shadowed_globals(&views);
        let value_flow = ChunkValueFlow::analyze(&views);
        let plain_data_bindings = collect_plain_data_bindings(&views, &shadowed, &value_flow);
        let plain_array_bindings = collect_plain_array_bindings(&views, &shadowed);
        let dynamic_scope = crate::facts::DynamicScopeFact::analyze(&views);
        let set_iterable_proof = crate::facts::SetIterableShapes::analyze(
            &views,
            &shadowed,
            &value_flow,
            &plain_data_bindings,
            dynamic_scope,
        );
        let local_class_construction_proof = crate::facts::LocalClassConstructionShapes::analyze(
            &views,
            &shadowed,
            &value_flow,
            dynamic_scope,
        );
        let prepared_sync_index =
            crate::facts::sync_effects::execution_index::interpreter::prepare_synchronous_call_index(
                &views,
                &value_flow,
                inputs,
                &plain_array_bindings,
                &local_class_construction_proof,
                set_iterable_proof,
                &shadowed,
            );
        let local_class_construction_proof =
            prepared_sync_index.local_class_construction_proof().clone();
        let set_iterable_proof = prepared_sync_index.set_iterable_proof().clone();
        PreparedModulePurityFacts {
            views,
            shadowed,
            set_iterable_proof,
            local_class_construction_proof,
            plain_data_bindings,
            plain_array_bindings,
        }
    }

    /// Build this module's `ChunkCodeGraph` with the supplied imported-binding
    /// purities. Only the cross-module map varies across fixpoint rounds; the
    /// declared-purity inputs are not consulted here (the oracle reasons about
    /// inferred body purity, and declared annotations only ever make a binding
    /// *more* pure, so omitting them keeps the verdict conservative).
    fn build_graph(
        &self,
        prepared: &PreparedModulePurityFacts<'_>,
        imported: &BTreeMap<String, Arc<Purity>>,
    ) -> ChunkCodeGraph {
        let mut locate = |span: Span| {
            let (source_path, index) = self.source.as_ref()?;
            let (start_line, end_line, start_column) =
                index.line_range_and_start_column_for_span(span)?;
            Some(SourceLocation {
                source_path: source_path.clone(),
                start_line,
                end_line,
                start_column: Some(start_column),
            })
        };
        ChunkCodeGraph::build_full_with_prepared_plain_data(
            &prepared.views,
            &prepared.shadowed,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &BTreeMap::new(),
            imported,
            &BTreeSet::new(),
            &prepared.set_iterable_proof,
            &prepared.local_class_construction_proof,
            &prepared.plain_data_bindings,
            &prepared.plain_array_bindings,
            Some(&mut locate),
        )
    }

    /// The imported-binding purities for this module under the current
    /// program-wide export verdicts: each local import bound to a *function*
    /// export of an analyzed module gets that export's verdict; anything
    /// unresolved is omitted (stays `unknown_call`).
    fn imported_purities(
        &self,
        export_purity: &BTreeMap<(ModuleKey, String), Arc<Purity>>,
    ) -> BTreeMap<String, Arc<Purity>> {
        self.imports
            .iter()
            .filter_map(|(local, target)| {
                let key = (target.module.clone(), target.export.clone());
                export_purity
                    .get(&key)
                    .map(|purity| (local.clone(), purity.clone()))
            })
            .collect()
    }
}

/// Merge one inferred function-body verdict into an optimistic export summary.
/// Still-pure summaries retain every consumed proof dependency; a newly
/// impure body demotes the summary and terminates further updates.
fn merge_inferred_export_purity(current: &Arc<Purity>, inferred: &Purity) -> Option<Purity> {
    if !current.is_pure() {
        return None;
    }
    let updated = current.as_ref().clone().worst(inferred.clone());
    (updated != **current).then_some(updated)
}

/// Compute, per module, the imported-binding purity map to feed that module's
/// `ChunkCodeGraph::build_full`. See the module docs for the fixpoint.
///
/// `asserted_pure` carries author-asserted pure exports keyed by defining
/// module (`TransformSpec::chunk_export_purity`). Assertions are trusted
/// axioms: they enter the verdict map as `Pure` even when the export's
/// binding is not a classifiable chunk-top function (interop wrappers,
/// re-exported callables), and the fixpoint never demotes them. An assertion
/// naming a module or export the program doesn't have is reported to stderr
/// as a dangling assertion (likely a typo or a stale entry) and ignored.
pub fn resolve_imported_purities(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted_pure: &BTreeMap<ModuleKey, BTreeSet<String>>,
    callable_summaries: &ResolvedCallableSummaries,
) -> BTreeMap<ModuleKey, BTreeMap<String, Arc<Purity>>> {
    // Only exported verdicts read by a named/default import can affect the
    // returned maps or another module's fixpoint inputs. Namespace member
    // calls are intentionally outside this oracle. Avoid value-flow analysis
    // of modules whose bodies cannot contribute an inferred verdict.
    let required: BTreeSet<_> = modules
        .values()
        .flat_map(|facts| facts.imports.values())
        .map(|target| (target.module.clone(), target.export.clone()))
        .collect();
    let mut export_purity: BTreeMap<(ModuleKey, String), Arc<Purity>> = BTreeMap::new();

    // Author-asserted axioms: pinned Pure, exempt from demotion below.
    let mut pinned: BTreeSet<(ModuleKey, String)> = BTreeSet::new();
    for (module, export_names) in asserted_pure {
        for export_name in export_names {
            let known_export = modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export_name));
            if !known_export {
                eprintln!(
                    "cross_module_purity: dangling pure_exports assertion \
                     {module}:{export_name} — no such analyzed module export; ignoring"
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

    // Asserted exports need no inference, but their validation above must run
    // even when no import consumes them. Keep structural facts only for the
    // remaining providers; they do not change across fixpoint rounds.
    let progress = std::env::var_os("DUCKTAPE_DEBUNDLE_PROGRESS").is_some();
    let mut prepared = BTreeMap::new();
    for (key, facts) in modules {
        let inferred_exports: Vec<_> = facts
            .exports
            .iter()
            .filter(|(export, _)| {
                let slot = (key.clone(), (*export).clone());
                required.contains(&slot) && !pinned.contains(&slot)
            })
            .collect();
        if inferred_exports.is_empty() {
            continue;
        }
        if progress {
            eprintln!(
                "[debundle progress] chunk={key} phase=prepare_cross_module_purity state=start"
            );
        }
        // Rebuilding a provider's execution index must retain the same captured
        // receiver and imported-call facts that established the program census.
        // A native-integrity result alone cannot refine the provider's local
        // mutation census, which would otherwise reintroduce unknown aliases.
        let inputs = crate::analysis_inputs::AnalysisInputs {
            native_integrity: callable_summaries.native_integrity.clone(),
            assumptions: crate::analysis_inputs::AnalysisAssumptions {
                captured_value_inputs: callable_summaries
                    .value_inputs
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
        let facts_prepared = facts.prepare(&inputs);
        // Discover function exports before optimistically seeding them. Other
        // exports stay unresolved, unless explicitly asserted above.
        let graph = facts.build_graph(&facts_prepared, &BTreeMap::new());
        let mut has_function_exports = false;
        for (export, local) in inferred_exports {
            if graph.function_purity(local).is_some() {
                export_purity.insert((key.clone(), export.clone()), Arc::new(Purity::pure()));
                has_function_exports = true;
            }
        }
        if has_function_exports {
            prepared.insert(key, facts_prepared);
        }
        if progress {
            eprintln!(
                "[debundle progress] chunk={key} phase=prepare_cross_module_purity state=end"
            );
        }
    }

    struct ExportRules<'a, 'ast> {
        modules: &'a BTreeMap<ModuleKey, ModulePurityFacts<'ast>>,
        prepared: &'a BTreeMap<&'a ModuleKey, PreparedModulePurityFacts<'ast>>,
        slots: BTreeSet<(ModuleKey, String)>,
        pinned: &'a BTreeSet<(ModuleKey, String)>,
    }
    impl IterativeRules for ExportRules<'_, '_> {
        type Key = (ModuleKey, String);
        type Fact = Arc<Purity>;
        type Rule = ModuleKey;

        fn evaluate(&self, module: &ModuleKey, context: &mut IterativeContext<'_, Self>) {
            let facts = &self.modules[module];
            let imported = facts
                .imports
                .iter()
                .filter_map(|(local, target)| {
                    context
                        .read((target.module.clone(), target.export.clone()))
                        .cloned()
                        .map(|purity| (local.clone(), purity))
                })
                .collect();
            let graph = facts.build_graph(&self.prepared[&module], &imported);
            for (export, local) in &facts.exports {
                let key = (module.clone(), export.clone());
                if !self.slots.contains(&key) || self.pinned.contains(&key) {
                    continue;
                }
                if let Some(purity) = graph.function_purity(local) {
                    context.propose(key, purity.clone());
                }
            }
        }

        fn merge(_: &Self::Key, current: &mut Arc<Purity>, contribution: Arc<Purity>) -> bool {
            if let Some(next) = merge_inferred_export_purity(current, &contribution) {
                *current = Arc::new(next);
                true
            } else {
                false
            }
        }
    }
    let producers = prepared
        .keys()
        .map(|key| (*key).clone())
        .collect::<Vec<_>>();
    let rules = ExportRules {
        modules,
        prepared: &prepared,
        slots: export_purity.keys().cloned().collect(),
        pinned: &pinned,
    };
    let mut inference = IterativeInference::new(rules);
    for (key, purity) in export_purity {
        inference.seed(key, purity);
    }
    let export_purity = inference.solve(producers).into_values();

    modules
        .iter()
        .map(|(key, facts)| (key.clone(), facts.imported_purities(&export_purity)))
        .collect()
}

/// Callback transfer is independent of purity, including asserted purity.
/// Compose checked source call timing and returned capabilities separately
/// from author assertions. Missing exports and unsupported bodies stay opaque.
#[derive(Default)]
pub struct ResolvedCallableSummaries {
    pub imported_callables: BTreeMap<ModuleKey, BTreeMap<String, CallableSummary>>,
    pub native_integrity: Option<NativeIntegrity>,
    pub value_inputs: BTreeMap<ModuleKey, Vec<ResolvedBindingValueInput>>,
}

pub fn resolve_imported_callable_summaries(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<ModuleKey, BTreeSet<String>>,
    selected_value_inputs: &BTreeMap<ModuleKey, Vec<SelectedBindingValueInput>>,
) -> Result<ResolvedCallableSummaries> {
    let required: BTreeSet<_> = modules
        .values()
        .flat_map(|facts| facts.imports.values())
        .map(|target| (target.module.clone(), target.export.clone()))
        .collect();
    let mut factories: BTreeMap<(ModuleKey, String), CallableSummary> = BTreeMap::new();
    for (module, exports) in asserted {
        for export in exports {
            if modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export))
            {
                factories.insert(
                    (module.clone(), export.clone()),
                    CallableSummary::callback_factory(CallbackFactoryEvidence::AuthorAssertion {
                        defining_module: module.clone(),
                        export: export.clone(),
                    }),
                );
            } else {
                eprintln!(
                    "cross_module_purity: dangling callback_factory_exports assertion {module}:{export} — no such analyzed module export; ignoring"
                );
            }
        }
    }
    let mut prepared = BTreeMap::new();
    let mut resolved_value_inputs = BTreeMap::new();
    for (module, facts) in modules {
        let locals = facts
            .exports
            .iter()
            .filter(|(export, _)| required.contains(&(module.clone(), (*export).clone())))
            .map(|(_, local)| local.clone())
            .collect();
        let views = top_level_item_views(facts.body);
        let mut prepared_module =
            crate::facts::sync_effects::execution_index::prepare_callable_capabilities(
                &views,
                locals,
                selected_value_inputs
                    .get(module)
                    .cloned()
                    .unwrap_or_default(),
            );
        if !prepared_module.value_input_errors().is_empty() {
            bail!(
                "invalid captured value input for provider {module}: {}",
                prepared_module.value_input_errors().join("; ")
            );
        }
        prepared_module.locate_value_inputs(facts.source.as_ref());
        let module_inputs = prepared_module.resolved_value_inputs();
        if !module_inputs.is_empty() && facts.source.is_none() {
            bail!(
                "captured value input for provider {module} has no retained parser source locations"
            );
        }
        if !module_inputs.is_empty() {
            resolved_value_inputs.insert(module.clone(), module_inputs);
        }
        prepared_module.locate_native_writes(module, facts.source.as_ref());
        prepared.insert(module, prepared_module);
    }
    // Argument/entry coverage is structural and independent of the native
    // integrity that it can refine. Author timing assertions cannot supply it.
    for (module, facts) in modules {
        let views = top_level_item_views(facts.body);
        let arguments = prepared[module].callback_object_arguments(&views);
        for (export, local) in &facts.exports {
            let Some((start, objects)) = arguments.get(local) else {
                continue;
            };
            let evidence = CallbackFactoryEvidence::InferredImmutableWrapper {
                defining_module: module.clone(),
                export: export.clone(),
                local: local.clone(),
                source_location: facts.source.as_ref().and_then(|(path, index)| {
                    let (line, column) = index.line_and_column_for_pos(BytePos(*start))?;
                    Some(SourceLocation {
                        source_path: path.clone(),
                        start_line: line,
                        end_line: line,
                        start_column: Some(column),
                    })
                }),
            };
            let mut summary = CallableSummary::callback_factory(evidence.clone());
            summary.callback_object_arguments = Some(CallbackObjectArguments {
                wrapper: evidence,
                object_arguments: objects.clone(),
            });
            factories
                .entry((module.clone(), export.clone()))
                .or_default()
                .merge(summary);
        }
    }
    for (module, facts) in &mut prepared {
        facts.refine_native_branches(&project_callable_summaries(&modules[*module], &factories));
        facts.locate_native_writes(module, modules[*module].source.as_ref());
    }
    let native_integrity = crate::facts::sync_effects::execution_index::resolve_native_integrity(
        prepared.values().map(|facts| facts.native_writes()),
    );
    let inferred: BTreeMap<_, _> = prepared
        .into_iter()
        .map(|(module, prepared)| {
            let views = top_level_item_views(modules[module].body);
            (
                module,
                crate::facts::sync_effects::execution_index::infer_callable_capabilities(
                    &views,
                    prepared,
                    &native_integrity,
                ),
            )
        })
        .collect();
    for (module, facts) in modules {
        let Some(capabilities) = inferred.get(module) else {
            continue;
        };
        let callback_factories = &capabilities.callback_factories;
        for (export, local) in &facts.exports {
            let key = (module.clone(), export.clone());
            if !required.contains(&key) || factories.contains_key(&key) {
                continue;
            }
            let Some(&function_start) = callback_factories.get(local) else {
                continue;
            };
            factories
                .entry(key)
                .or_default()
                .merge(CallableSummary::callback_factory(
                    CallbackFactoryEvidence::InferredImmutableWrapper {
                        defining_module: module.clone(),
                        export: export.clone(),
                        local: local.clone(),
                        source_location: facts.source.as_ref().and_then(|(path, index)| {
                            let (line, column) =
                                index.line_and_column_for_pos(BytePos(function_start))?;
                            Some(SourceLocation {
                                source_path: path.clone(),
                                start_line: line,
                                end_line: line,
                                start_column: Some(column),
                            })
                        }),
                    },
                ));
        }
    }
    let needs_map_integrity = inferred
        .values()
        .any(|summary| !summary.returned_records.is_empty());
    // A source-composed native premise must survive every analyzed chunk's
    // canonical intrinsic-write census, including the importing chunk.
    if needs_map_integrity
        && inferred
            .values()
            .all(|capabilities| capabilities.map_integrity)
    {
        for (module, facts) in modules {
            let records = &inferred[module].returned_records;
            let locate = |start| {
                facts.source.as_ref().and_then(|(path, index)| {
                    let (line, column) = index.line_and_column_for_pos(BytePos(start))?;
                    Some(SourceLocation {
                        source_path: path.clone(),
                        start_line: line,
                        end_line: line,
                        start_column: Some(column),
                    })
                })
            };
            for (export, local) in &facts.exports {
                let key = (module.clone(), export.clone());
                if !required.contains(&key) {
                    continue;
                }
                let Some(record) = records.get(local) else {
                    continue;
                };
                factories
                    .entry(key)
                    .or_default()
                    .merge(CallableSummary::returned_record(ReturnedRecordSummary {
                        defining_module: module.clone(),
                        export: export.clone(),
                        local: local.clone(),
                        source_location: locate(record.facts.function_start),
                        members: record
                            .facts
                            .members
                            .iter()
                            .map(|(name, start, captures)| DeferredRecordMember {
                                name: name.clone(),
                                source_location: locate(*start),
                                captures: captures.clone(),
                            })
                            .collect(),
                        inference: record.inference.clone(),
                    }));
            }
        }
    }
    for (module, facts) in modules {
        for (export, local) in &facts.exports {
            let key = (module.clone(), export.clone());
            if !required.contains(&key) {
                continue;
            }
            let Some(wrapper) = inferred[module].namespace_wrappers.get(local) else {
                continue;
            };
            let source_location = facts.source.as_ref().and_then(|(path, index)| {
                let (line, column) =
                    index.line_and_column_for_pos(BytePos(wrapper.function_start))?;
                Some(SourceLocation {
                    source_path: path.clone(),
                    start_line: line,
                    end_line: line,
                    start_column: Some(column),
                })
            });
            factories
                .entry(key)
                .or_default()
                .merge(CallableSummary::namespace_wrapper(
                    NamespaceWrapperSummary {
                        defining_module: module.clone(),
                        export: export.clone(),
                        local: local.clone(),
                        source_location,
                        inference: wrapper.inference.clone(),
                    },
                ));
        }
    }
    let imported_callables = modules
        .iter()
        .map(|(module, facts)| {
            let bindings = project_callable_summaries(facts, &factories);
            (module.clone(), bindings)
        })
        .collect();
    Ok(ResolvedCallableSummaries {
        imported_callables,
        native_integrity: Some(native_integrity),
        value_inputs: resolved_value_inputs,
    })
}

fn project_callable_summaries(
    facts: &ModulePurityFacts<'_>,
    summaries: &BTreeMap<(ModuleKey, String), CallableSummary>,
) -> BTreeMap<String, CallableSummary> {
    facts
        .imports
        .iter()
        .filter_map(|(local, target)| {
            summaries
                .get(&(target.module.clone(), target.export.clone()))
                .map(|summary| (local.clone(), summary.clone()))
        })
        .collect()
}

/// Resolve author-asserted inline-only callback factories through the same
/// import identities as the broader callback-factory contract. No local
/// wrapper inference grants this narrower assertion implicitly.
pub fn resolve_asserted_inline_callback_factories(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<ModuleKey, BTreeSet<String>>,
) -> BTreeMap<ModuleKey, BTreeSet<String>> {
    let mut factories = BTreeSet::new();
    for (module, exports) in asserted {
        for export in exports {
            if modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export))
            {
                factories.insert((module.clone(), export.clone()));
            } else {
                eprintln!(
                    "cross_module_purity: dangling inline_callback_factory_exports assertion {module}:{export} — no such analyzed module export; ignoring"
                );
            }
        }
    }
    modules
        .iter()
        .map(|(module, facts)| {
            let bindings = facts
                .imports
                .iter()
                .filter(|(_, target)| {
                    factories.contains(&(target.module.clone(), target.export.clone()))
                })
                .map(|(local, _)| local.clone())
                .collect();
            (module.clone(), bindings)
        })
        .collect()
}

/// Project author-asserted pure members of exported namespace-like objects
/// (`chunk_export_purity.<chunk>.pure_members`) onto each importing module's
/// local binding names. Pure axiom projection — no fixpoint: for every import
/// `local → (module, export)` with an assertion on `(module, export)`, the
/// importing module receives `local → member set`, which feeds the classifier's
/// member-call trust arm (`declared_pure_members`). Assertions naming a module
/// or export the program doesn't have warn to stderr and are ignored.
pub fn resolve_asserted_member_purities(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted_members: &BTreeMap<ModuleKey, BTreeMap<String, BTreeSet<String>>>,
) -> BTreeMap<ModuleKey, BTreeMap<String, BTreeSet<String>>> {
    resolve_asserted_members(modules, asserted_members, "pure_members")
}

pub fn resolve_asserted_callback_factory_members(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted_members: &BTreeMap<ModuleKey, BTreeMap<String, BTreeSet<String>>>,
) -> BTreeMap<ModuleKey, BTreeMap<String, BTreeSet<String>>> {
    resolve_asserted_members(modules, asserted_members, "callback_factory_members")
}

pub fn resolve_asserted_no_reentry_members(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted_members: &BTreeMap<ModuleKey, BTreeMap<String, BTreeSet<String>>>,
) -> BTreeMap<ModuleKey, BTreeMap<String, BTreeSet<String>>> {
    resolve_asserted_members(modules, asserted_members, "no_reentry_members")
}

pub fn resolve_asserted_no_reentry_record_members(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted_members: &BTreeMap<ModuleKey, BTreeMap<String, BTreeSet<String>>>,
) -> BTreeMap<ModuleKey, BTreeMap<String, BTreeSet<String>>> {
    resolve_asserted_members(modules, asserted_members, "no_reentry_record_members")
}

/// Project direct-export call-timing assertions through resolved import
/// identities, without inferring purity or wrapper behavior.
pub fn resolve_asserted_no_reentry_bindings(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<ModuleKey, BTreeSet<String>>,
) -> BTreeMap<ModuleKey, BTreeSet<String>> {
    resolve_asserted_export_bindings(modules, asserted, "no_reentry_exports")
}

pub fn resolve_asserted_namespace_independent_members(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted_members: &BTreeMap<ModuleKey, BTreeMap<String, BTreeSet<String>>>,
) -> BTreeMap<ModuleKey, BTreeMap<String, BTreeSet<String>>> {
    resolve_asserted_members(modules, asserted_members, "namespace_independent_members")
}

pub fn resolve_asserted_retained_callback_factories(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<ModuleKey, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
) -> BTreeMap<ModuleKey, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>> {
    resolve_asserted_members(modules, asserted, "retained_callback_factory_exports")
}

pub fn resolve_asserted_retained_callback_constructors(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<ModuleKey, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
) -> BTreeMap<ModuleKey, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>> {
    resolve_asserted_members(modules, asserted, "retained_callback_constructor_exports")
}

pub fn resolve_asserted_no_reentry_callbacks(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<ModuleKey, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
) -> BTreeMap<ModuleKey, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>> {
    resolve_asserted_members(modules, asserted, "no_reentry_callback_exports")
}

pub fn resolve_asserted_retained_callback_factory_members(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<
        ModuleKey,
        BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
    >,
) -> BTreeMap<ModuleKey, BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>> {
    resolve_asserted_members(modules, asserted, "retained_callback_factory_members")
}

fn resolve_asserted_members<T: Clone>(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted_members: &BTreeMap<ModuleKey, BTreeMap<String, T>>,
    contract: &str,
) -> BTreeMap<ModuleKey, BTreeMap<String, T>> {
    for (module, by_export) in asserted_members {
        for export_name in by_export.keys() {
            let known_export = modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export_name));
            if !known_export {
                eprintln!(
                    "cross_module_purity: dangling {contract} assertion \
                     {module}:{export_name} — no such analyzed module export; ignoring"
                );
            }
        }
    }
    modules
        .iter()
        .map(|(key, facts)| {
            let members: BTreeMap<String, T> = facts
                .imports
                .iter()
                .filter_map(|(local, target)| {
                    let asserted = asserted_members
                        .get(&target.module)?
                        .get(&target.export)?
                        .clone();
                    // Only project assertions whose export actually exists in
                    // the defining module (dangling ones warned above).
                    modules
                        .get(&target.module)?
                        .exports
                        .contains_key(&target.export)
                        .then(|| (local.clone(), asserted))
                })
                .collect();
            (key.clone(), members)
        })
        .collect()
}

/// Project author-asserted fluent exports
/// (`chunk_export_purity.<chunk>.fluent_exports`) onto each importing
/// module's local binding names. Pure axiom projection — no fixpoint,
/// same shape as `resolve_asserted_member_purities`: for every import
/// `local → (module, export)` with a fluent assertion on
/// `(module, export)`, the importing module receives `local` in its
/// fluent set, which seeds the classifier's deep-purity chain arm
/// (`ChunkCodeGraph::fluent_bindings`). Assertions naming a module or
/// export the program doesn't have warn to stderr and are ignored.
pub fn resolve_asserted_fluent_bindings(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted_fluent: &BTreeMap<ModuleKey, BTreeSet<String>>,
) -> BTreeMap<ModuleKey, BTreeSet<String>> {
    resolve_asserted_export_bindings(modules, asserted_fluent, "fluent_exports")
}

/// Project author-asserted constructor purity onto importing local bindings.
/// The result feeds `declared_pure_new`, so only `new local(...)` is admitted;
/// plain calls and argument expressions retain their normal classification.
pub fn resolve_asserted_pure_new_bindings(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted_pure_new: &BTreeMap<ModuleKey, BTreeSet<String>>,
) -> BTreeMap<ModuleKey, BTreeSet<String>> {
    resolve_asserted_export_bindings(modules, asserted_pure_new, "pure_new_exports")
}

fn resolve_asserted_export_bindings(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<ModuleKey, BTreeSet<String>>,
    field: &str,
) -> BTreeMap<ModuleKey, BTreeSet<String>> {
    for (module, export_names) in asserted {
        for export_name in export_names {
            let known_export = modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export_name));
            if !known_export {
                eprintln!(
                    "cross_module_purity: dangling {field} assertion \
                     {module}:{export_name} — no such analyzed module export; ignoring"
                );
            }
        }
    }
    modules
        .iter()
        .map(|(key, facts)| {
            let bindings: BTreeSet<String> = facts
                .imports
                .iter()
                .filter_map(|(local, target)| {
                    let is_asserted = asserted.get(&target.module)?.contains(&target.export);
                    // Only project assertions whose export actually exists in
                    // the defining module (dangling ones warned above).
                    (is_asserted
                        && modules
                            .get(&target.module)?
                            .exports
                            .contains_key(&target.export))
                    .then(|| local.clone())
                })
                .collect();
            (key.clone(), bindings)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::{ProofDependencies, ValueInputRef};
    use swc_common::{FileName, Span, sync::Lrc};
    use swc_ecma_ast::{Expr, Module, NewExpr};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};
    use swc_ecma_visit::{Visit, VisitWith};

    fn parse(source: &str) -> Module {
        let cm: Lrc<swc_common::SourceMap> = Default::default();
        let fm = cm.new_source_file(FileName::Custom("m.js".into()).into(), source.to_string());
        let lexer = Lexer::new(
            Syntax::Es(Default::default()),
            Default::default(),
            StringInput::from(&*fm),
            None,
        );
        Parser::new_from(lexer).parse_module().unwrap()
    }

    fn facts<'a>(
        module: &'a Module,
        imports: &[(&str, &str, &str)],
        exports: &[(&str, &str)],
    ) -> ModulePurityFacts<'a> {
        ModulePurityFacts {
            body: &module.body,
            imports: imports
                .iter()
                .map(|(local, source, export)| {
                    (
                        (*local).to_string(),
                        ResolvedImport {
                            module: (*source).to_string(),
                            export: (*export).to_string(),
                        },
                    )
                })
                .collect(),
            exports: exports
                .iter()
                .map(|(name, local)| ((*name).to_string(), (*local).to_string()))
                .collect(),
            source: None,
        }
    }

    fn function_purity_with_imports(
        source: &str,
        local: &str,
        imported: &BTreeMap<String, Arc<Purity>>,
    ) -> Purity {
        let module = parse(source);
        let facts = facts(&module, &[], &[("out", local)]);
        let prepared = facts.prepare(&crate::analysis_inputs::AnalysisInputs::default());
        facts
            .build_graph(&prepared, imported)
            .function_purity(local)
            .expect("function is indexed")
            .as_ref()
            .clone()
    }

    fn value_input_dependencies(provider: &str, input_index: usize) -> ProofDependencies {
        ProofDependencies {
            value_inputs: BTreeSet::from([ValueInputRef {
                defining_chunk: provider.to_string(),
                input_index,
            }]),
        }
    }

    #[test]
    fn impurity_propagates_back_across_a_module_chain() {
        // c.base (impure) demotes b.wrap, which demotes a's `wrap` import.
        let a = parse("const W = wrap();");
        let b = parse("import { base } from \"c\";\nexport function wrap() { return base(); }");
        let c = parse("export function base() { globalSink(); }");
        let modules = BTreeMap::from([
            ("a".to_string(), facts(&a, &[("wrap", "b", "wrap")], &[])),
            (
                "b".to_string(),
                facts(&b, &[("base", "c", "base")], &[("wrap", "wrap")]),
            ),
            ("c".to_string(), facts(&c, &[], &[("base", "base")])),
        ]);
        let resolved = resolve_imported_purities(
            &modules,
            &BTreeMap::new(),
            &ResolvedCallableSummaries::default(),
        );
        assert!(!resolved["a"]["wrap"].is_pure());
    }

    #[test]
    fn import_of_an_unanalyzed_module_stays_unknown() {
        // `vendor` is not in the module set (external/opaque) → no entry.
        let app = parse("const C = ext(x);");
        let modules = BTreeMap::from([(
            "app".to_string(),
            facts(&app, &[("ext", "vendor", "ext")], &[]),
        )]);
        let resolved = resolve_imported_purities(
            &modules,
            &BTreeMap::new(),
            &ResolvedCallableSummaries::default(),
        );
        assert!(!resolved["app"].contains_key("ext"));
    }

    #[test]
    fn import_of_a_non_function_export_stays_unknown() {
        // A data export is not callable → no purity entry, stays unknown_call.
        let app = parse("const C = table(x);");
        let lib = parse("export const table = 42;");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("table", "lib", "table")], &[]),
            ),
            ("lib".to_string(), facts(&lib, &[], &[("table", "table")])),
        ]);
        let resolved = resolve_imported_purities(
            &modules,
            &BTreeMap::new(),
            &ResolvedCallableSummaries::default(),
        );
        assert!(!resolved["app"].contains_key("table"));
    }

    #[test]
    fn cross_module_graph_preparation_gates_mutated_set_iterable_proof() {
        let module = parse(
            "const values = [1]; Set.prototype.add = function add(value) {}; new Set(values);",
        );
        let prepared =
            facts(&module, &[], &[]).prepare(&crate::analysis_inputs::AnalysisInputs::default());

        struct SetConstructors(Vec<Span>);
        impl Visit for SetConstructors {
            fn visit_new_expr(&mut self, node: &NewExpr) {
                if matches!(node.callee.unwrap_parens(), Expr::Ident(ident) if ident.sym.as_ref() == "Set")
                {
                    self.0.push(node.span);
                }
                node.visit_children_with(self);
            }
        }
        let mut constructors = SetConstructors(Vec::new());
        module.visit_with(&mut constructors);
        assert_eq!(constructors.0.len(), 1, "fixture needs one Set constructor");
        assert!(
            prepared
                .set_iterable_proof
                .dependencies_for_constructor(constructors.0[0])
                .is_none(),
            "cross-module purity preparation must honor the canonical intrinsic census"
        );
    }

    #[test]
    fn asserted_pure_export_overrides_inferred_impurity() {
        // `observer` is genuinely impure to the classifier (writes a global),
        // so inference demotes it. An author assertion pins it Pure, and the
        // verdict propagates to the importing chunk.
        let app = parse("const C = observer(x);");
        let lib = parse("export function observer(c) { globalThis.__warned = 1; return c; }");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("observer", "lib", "observer")], &[]),
            ),
            (
                "lib".to_string(),
                facts(&lib, &[], &[("observer", "observer")]),
            ),
        ]);
        // Without the assertion: impure.
        assert!(
            !resolve_imported_purities(
                &modules,
                &BTreeMap::new(),
                &ResolvedCallableSummaries::default()
            )["app"]["observer"]
                .is_pure()
        );
        // With the assertion on the defining chunk: pure, at every importer.
        let asserted =
            BTreeMap::from([("lib".to_string(), BTreeSet::from(["observer".to_string()]))]);
        let resolved =
            resolve_imported_purities(&modules, &asserted, &ResolvedCallableSummaries::default());
        assert!(resolved["app"]["observer"].is_pure());
        assert_eq!(
            resolved["app"]["observer"].dependencies(),
            Some(&ProofDependencies::default()),
            "the separate pure_exports axiom must not acquire inferred captured-value dependencies"
        );
    }

    #[test]
    fn pure_export_fixpoint_unions_dependencies_and_importers_retain_them() {
        let first = value_input_dependencies("static/provider-a.js", 1);
        let second = value_input_dependencies("static/provider-b.js", 3);
        let current = Arc::new(Purity::pure().with_dependencies(&first));
        let newly_inferred = Purity::pure().with_dependencies(&second);
        let merged = merge_inferred_export_purity(&current, &newly_inferred)
            .expect("new proof dependency must update a still-pure export");
        assert_eq!(
            merged.dependencies(),
            Some(&ProofDependencies {
                value_inputs: BTreeSet::from([
                    ValueInputRef {
                        defining_chunk: "static/provider-a.js".to_string(),
                        input_index: 1,
                    },
                    ValueInputRef {
                        defining_chunk: "static/provider-b.js".to_string(),
                        input_index: 3,
                    },
                ]),
            })
        );
        assert!(
            merge_inferred_export_purity(&Arc::new(merged.clone()), &newly_inferred).is_none(),
            "an unchanged summary must converge"
        );

        let first_import = BTreeMap::from([("upstream".to_string(), Arc::new(merged))]);
        let middle = function_purity_with_imports(
            "export function forward() { return upstream(); }",
            "forward",
            &first_import,
        );
        assert_eq!(
            middle.dependencies(),
            first_import["upstream"].dependencies()
        );
        let second_import = BTreeMap::from([("middle".to_string(), Arc::new(middle))]);
        let leaf = function_purity_with_imports(
            "export function forwardAgain() { return middle(); }",
            "forwardAgain",
            &second_import,
        );
        assert_eq!(leaf.dependencies(), second_import["middle"].dependencies());
    }

    #[test]
    fn asserted_pure_export_applies_to_a_non_function_callable() {
        // The export is a re-exported interop value the classifier does not see
        // as a chunk-top function, so inference yields no verdict. The
        // assertion still admits it (author trust), reaching the importer.
        let app = parse("const C = forwardRef(x);");
        let lib = parse("const inner = makeRef();\nexport { inner as forwardRef };");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("forwardRef", "lib", "forwardRef")], &[]),
            ),
            (
                "lib".to_string(),
                facts(&lib, &[], &[("forwardRef", "inner")]),
            ),
        ]);
        assert!(
            !resolve_imported_purities(
                &modules,
                &BTreeMap::new(),
                &ResolvedCallableSummaries::default()
            )["app"]
                .contains_key("forwardRef")
        );
        let asserted = BTreeMap::from([(
            "lib".to_string(),
            BTreeSet::from(["forwardRef".to_string()]),
        )]);
        assert!(
            resolve_imported_purities(&modules, &asserted, &ResolvedCallableSummaries::default())["app"]["forwardRef"].is_pure()
        );
    }

    #[test]
    fn callback_factory_summaries_keep_source_and_assertion_provenance() {
        let app = parse(
            "import { wrap as localWrap, audited as localAudited } from 'lib';\n\
             import { wrap as missingWrap } from 'vendor';",
        );
        let lib = parse(
            "export function wrap(callback) { return () => callback(); }\n\
             export const audited = makeOpaque();",
        );
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(
                    &app,
                    &[
                        ("localWrap", "lib", "wrap"),
                        ("localAudited", "lib", "audited"),
                        ("missingWrap", "vendor", "wrap"),
                    ],
                    &[],
                ),
            ),
            (
                "lib".to_string(),
                facts(&lib, &[], &[("wrap", "wrap"), ("audited", "audited")]),
            ),
        ]);
        let asserted = BTreeMap::from([
            ("lib".to_string(), BTreeSet::from(["audited".to_string()])),
            ("vendor".to_string(), BTreeSet::from(["wrap".to_string()])),
        ]);
        let resolved = resolve_imported_callable_summaries(&modules, &asserted, &BTreeMap::new())
            .unwrap()
            .imported_callables;
        assert_eq!(
            resolved["app"]["localWrap"].callback_factory_evidence(),
            &BTreeSet::from([CallbackFactoryEvidence::InferredImmutableWrapper {
                defining_module: "lib".to_string(),
                export: "wrap".to_string(),
                local: "wrap".to_string(),
                source_location: None,
            }]),
        );
        assert_eq!(
            resolved["app"]["localAudited"].callback_factory_evidence(),
            &BTreeSet::from([CallbackFactoryEvidence::AuthorAssertion {
                defining_module: "lib".to_string(),
                export: "audited".to_string(),
            }]),
        );
        assert!(!resolved["app"].contains_key("missingWrap"));
    }

    #[test]
    fn dangling_member_assertion_is_ignored() {
        let app = parse("const C = b.forwardRef(x);");
        let modules = BTreeMap::from([(
            "app".to_string(),
            facts(&app, &[("b", "vendor", "reactns")], &[]),
        )]);
        // `vendor` is not analyzed at all — the assertion must not project.
        let asserted = BTreeMap::from([(
            "vendor".to_string(),
            BTreeMap::from([(
                "reactns".to_string(),
                BTreeSet::from(["forwardRef".to_string()]),
            )]),
        )]);
        let resolved = resolve_asserted_member_purities(&modules, &asserted);
        assert!(resolved["app"].is_empty());
    }

    #[test]
    fn dangling_fluent_assertion_is_ignored() {
        let app = parse("const S = k.object({});");
        let modules = BTreeMap::from([(
            "app".to_string(),
            facts(&app, &[("k", "vendor", "e4")], &[]),
        )]);
        // `vendor` is not analyzed at all — the assertion must not project.
        let asserted = BTreeMap::from([("vendor".to_string(), BTreeSet::from(["e4".to_string()]))]);
        let resolved = resolve_asserted_fluent_bindings(&modules, &asserted);
        assert!(resolved["app"].is_empty());
    }
}
