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
//! ## Algorithm — bottom-up effect closure
//!
//! Canonical body classification emits direct effects, proof dependencies,
//! and exact obligations to inferred local/imported functions. A single typed
//! worklist across all participating modules propagates finite effects through
//! those obligations. Pure is projected only after closure, so recursive pure
//! components remain pure without optimistic verdicts.
//!
//! Soundness is conservative by construction: an import whose target module or
//! export is absent (external/vendor module not analyzed, a non-function
//! export, an unresolved re-export) simply gets no entry, so the call stays
//! `unknown_call` exactly as before. Pinned assertions remain trusted axioms.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Arc;

use anyhow::{Result, bail};
use js_ast::SourceLineIndex;
use swc_common::{BytePos, Span};
use swc_ecma_ast::{Id, ModuleItem};

use crate::SourceLocation;
use crate::analysis_inputs::{
    CallableSummary, CallbackFactoryEvidence, CallbackObjectArguments, DeferredRecordMember,
    DescriptorCopyFactorySummary, ForeignCallSummary, NamespaceWrapperSummary, NativeInputKind,
    NativeIntegrity, ProofDependencies, ResolvedBindingValueInput, ResolvedNativeOperationInput,
    ReturnedRecordSummary, SelectedBindingValueInput, SelectedNativeOperationInput,
};
use crate::facts::abstract_domain::value::{PrimKinds, Value};
use crate::facts::{TopLevelItemView, top_level_item_views};
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use crate::purity::{ChunkCodeGraph, Purity, PurityEffectRules, PurityMode};

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

/// A stable direct ESM export's primitive value before import resolution.
/// Canonical shallow-scan facts; never serialized or author asserted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StableExportValue {
    /// The types the exported binding's history computes itself, joined with
    /// the values of the named/default import bindings it copies.
    Local {
        kinds: PrimKinds,
        imports: BTreeSet<String>,
    },
    /// `export { export as name } from source`.
    ReExport { source: String, export: String },
}

/// A stable export after its imports resolved: its own types and the exports
/// it copies.
pub struct ExportValueSource {
    pub kinds: PrimKinds,
    pub copies: Vec<ResolvedImport>,
}

/// Close export values over resolved imports and re-exports. An export's
/// value is known once every export it copies is; unsupported cycles have no
/// seed and stay unknown. The closure neither interprets values nor certifies
/// that evaluating an initializer is pure.
pub fn resolve_export_values(
    sources: BTreeMap<(ModuleKey, String), ExportValueSource>,
) -> BTreeMap<(ModuleKey, String), Value> {
    let rules = ExportValueRules(sources.into_iter().collect());
    let count = rules.0.len();
    IterativeInference::new(rules)
        .solve_values(0..count)
        .into_iter()
        .map(|(export, kinds)| (export, Value::Prim(kinds)))
        .collect()
}

struct ExportValueRules(Vec<((ModuleKey, String), ExportValueSource)>);

impl IterativeRules for ExportValueRules {
    type Key = (ModuleKey, String);
    type Fact = PrimKinds;
    type Rule = usize;

    fn evaluate(&self, rule: &usize, context: &mut impl FactContext<Self::Key, PrimKinds>) {
        let (export, source) = &self.0[*rule];
        let mut kinds = source.kinds;
        for copy in &source.copies {
            let Some(copied) = context.read((copy.module.clone(), copy.export.clone())) else {
                return;
            };
            kinds = kinds.union(*copied);
        }
        context.propose(export.clone(), kinds);
    }

    fn merge(_: &Self::Key, current: &mut PrimKinds, contribution: PrimKinds) -> bool {
        let joined = current.union(contribution);
        std::mem::replace(current, joined) != joined
    }
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
    /// Export name → the package a vendor swap replaces it with. Facts derived
    /// from this body still hold for those exports under the swap's
    /// equivalence promise (SPEC.md § Vendor swap equivalence); summaries
    /// name the package so an author can audit them against it.
    pub replacement_packages: BTreeMap<String, String>,
}

struct PreparedModulePurityFacts<'a> {
    views: Vec<TopLevelItemView<'a>>,
    shadowed: BTreeSet<&'static str>,
    set_iterable_proof: crate::facts::SetIterableProof,
    local_class_construction_proof: crate::facts::LocalClassConstructionProof,
    plain_data_bindings: Vec<String>,
    plain_data_dependencies: BTreeMap<String, crate::analysis_inputs::ProofDependencies>,
    plain_data_inference_uses: BTreeMap<String, BTreeSet<crate::reports::schema::InferenceUse>>,
    plain_array_bindings: BTreeSet<String>,
}

impl<'a> ModulePurityFacts<'a> {
    fn prepare(
        &self,
        inputs: &crate::analysis_inputs::AnalysisInputs,
    ) -> PreparedModulePurityFacts<'a> {
        let structural = crate::facts::complete_chunk_structural_with_inputs(
            crate::facts::collect_chunk_source_facts_from_items(self.body, None, |_| None),
            &inputs.assumptions.native_operation_inputs,
            &inputs.stable_set_spreads,
            &inputs.assumptions.argument_inputs,
        );
        let views = structural.body;
        let shadowed = structural.shadowed;
        let value_flow = structural.value_flow;
        let plain_data_bindings = structural.plain_data_bindings;
        let plain_data_dependencies = structural.plain_data_dependencies;
        let plain_data_inference_uses = structural.plain_data_inference_uses;
        let source_semantic_facts = structural.source_semantic_facts;
        let stable_array_spread_shapes = structural.stable_array_spread_shapes;
        let plain_array_bindings =
            crate::facts::abstract_domain::heap::plain_array_bindings(&views, &shadowed);
        let set_iterable_proof = structural.set_iterable_shapes;
        let local_class_construction_proof = structural.local_class_shapes;
        let prepared_sync_index =
            crate::facts::sync_effects::execution_index::interpreter::prepare_synchronous_call_index_with_source_facts(
                &views,
                &value_flow,
                inputs,
                &plain_array_bindings,
                crate::facts::sync_effects::execution_index::interpreter::PreparedSyncShapeInputs {
                    local_class_construction: &local_class_construction_proof,
                    set_iterable_shapes: set_iterable_proof,
                    stable_array_spread_shapes,
                    shadowed: &shadowed,
                    source_facts: source_semantic_facts.as_deref(),
                },
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
            plain_data_dependencies,
            plain_data_inference_uses,
            plain_array_bindings,
        }
    }

    /// Test helper for checking a module graph with pre-resolved imports.
    #[cfg(test)]
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
            &prepared.plain_data_dependencies,
            &prepared.plain_data_inference_uses,
            &prepared.plain_array_bindings,
            &BTreeSet::new(),
            None,
            Some(&mut locate),
        )
    }

    /// The imported-binding purities for this module under the current
    /// program-wide export verdicts: each local import bound to a *function*
    /// export of an analyzed module gets that export's verdict; anything
    /// unresolved is omitted (stays `unknown_call`).
    /// Each local import's value under the program-wide export values.
    pub fn imported_values(
        &self,
        export_values: &BTreeMap<(ModuleKey, String), Value>,
    ) -> BTreeMap<String, Value> {
        self.imports
            .iter()
            .filter_map(|(local, target)| {
                let value = export_values.get(&(target.module.clone(), target.export.clone()))?;
                Some((local.clone(), *value))
            })
            .collect()
    }

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

/// Compute, per module, the imported-binding purity map to feed that module's
/// callers. See the module docs for the shared effects closure.
///
/// `asserted_pure` carries author-asserted pure exports keyed by defining
/// module (`TransformSpec::chunk_export_purity`). Assertions are trusted
/// axioms: they enter the verdict map as `Pure` even when the export's
/// binding is not a classifiable chunk-top function (interop wrappers,
/// re-exported callables), and the effects closure never demotes them. An assertion
/// naming a module or export the program doesn't have is reported to stderr
/// as a dangling assertion (likely a typo or a stale entry) and ignored.
pub fn resolve_imported_purities(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted_pure: &BTreeMap<ModuleKey, BTreeSet<String>>,
    callable_summaries: &ResolvedCallableSummaries,
) -> BTreeMap<ModuleKey, BTreeMap<String, Arc<Purity>>> {
    // Only exported verdicts read by a named/default import can affect the
    // returned maps or another module's closure. Namespace member calls are
    // intentionally outside this oracle.
    let required: BTreeSet<_> = modules
        .values()
        .flat_map(|facts| facts.imports.values())
        .map(|target| (target.module.clone(), target.export.clone()))
        .collect();
    // Author-asserted axioms stay pinned Pure, even when no source body exists.
    let mut pinned: BTreeSet<(ModuleKey, String)> = BTreeSet::new();
    let mut export_purity: BTreeMap<(ModuleKey, String), Arc<Purity>> = BTreeMap::new();
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

    // Authoritative source/native/captured-input facts are fixed before this
    // one effects closure starts. Declared chunk-local purity hints remain
    // absent from export mode, as before.
    let progress = std::env::var_os("DUCKTAPE_DEBUNDLE_PROGRESS").is_some();
    let mut prepared = BTreeMap::new();
    for (key, facts) in modules {
        if !inferred_export_keys.keys().any(|(module, _)| module == key) {
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
            stable_set_spreads: callable_summaries
                .stable_set_spreads
                .get(key)
                .cloned()
                .unwrap_or_default(),
            assumptions: crate::analysis_inputs::AnalysisAssumptions {
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
        let facts_prepared = facts.prepare(&inputs);
        prepared.insert(key.clone(), facts_prepared);
        if progress {
            eprintln!(
                "[debundle progress] chunk={key} phase=prepare_cross_module_purity state=end"
            );
        }
    }

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
        let mut locate = |span: Span| {
            let (source_path, index) = facts.source.as_ref()?;
            let (start_line, end_line, start_column) =
                index.line_range_and_start_column_for_span(span)?;
            Some(SourceLocation {
                source_path: source_path.clone(),
                start_line,
                end_line,
                start_column: Some(start_column),
            })
        };
        let shape_inputs = crate::purity::PurityShapeInputs {
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
            structural_facts: None,
        };
        let call_context = crate::purity::PurityCallContext {
            module,
            mode: PurityMode::Export,
            declared_pure: &no_declared_pure,
            imported_targets: &imported_targets,
        };
        let transfers = ChunkCodeGraph::effect_transfers(
            &prepared_facts.views,
            &shape_inputs,
            &call_context,
            Some(&mut locate),
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
    pub native_operation_inputs: BTreeMap<ModuleKey, Vec<ResolvedNativeOperationInput>>,
    pub stable_set_spreads:
        BTreeMap<ModuleKey, BTreeMap<u32, crate::analysis_inputs::StableSetIterableUse>>,
    pub argument_inputs: BTreeMap<ModuleKey, Vec<crate::analysis_inputs::ResolvedArgumentInput>>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct CallableExportKey {
    module: ModuleKey,
    export: String,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct CallableImportKey {
    module: ModuleKey,
    local: String,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum CallableFactKey {
    Export(CallableExportKey),
    Import(CallableImportKey),
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct CallableImportProjection {
    imported: CallableImportKey,
    provider: CallableExportKey,
}

#[derive(Debug, Clone, Default)]
struct CallableExportProof {
    summary: CallableSummary,
    dependencies: ProofDependencies,
}

impl CallableExportProof {
    fn add(&mut self, summary: CallableSummary, dependencies: ProofDependencies) {
        self.summary.merge(summary);
        self.dependencies.extend(&dependencies);
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum CallableProjectionRule {
    Export(CallableExportKey),
    Import(CallableImportProjection),
}

struct CallableProjectionRules {
    exports: BTreeMap<CallableExportKey, CallableExportProof>,
    imports: Vec<CallableImportProjection>,
}

impl CallableProjectionRules {
    fn new(
        exports: BTreeMap<CallableExportKey, CallableExportProof>,
        modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    ) -> Self {
        let imports = modules
            .iter()
            .flat_map(|(module, facts)| {
                facts
                    .imports
                    .iter()
                    .map(|(local, target)| CallableImportProjection {
                        imported: CallableImportKey {
                            module: module.clone(),
                            local: local.clone(),
                        },
                        provider: CallableExportKey {
                            module: target.module.clone(),
                            export: target.export.clone(),
                        },
                    })
            })
            .collect();
        Self { exports, imports }
    }

    fn producers(&self) -> BTreeSet<CallableProjectionRule> {
        self.exports
            .keys()
            .cloned()
            .map(CallableProjectionRule::Export)
            .chain(
                self.imports
                    .iter()
                    .cloned()
                    .map(CallableProjectionRule::Import),
            )
            .collect()
    }
}

impl IterativeRules for CallableProjectionRules {
    type Key = CallableFactKey;
    type Fact = CallableSummary;
    type Rule = CallableProjectionRule;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        match rule {
            CallableProjectionRule::Export(key) => {
                let Some(proof) = self.exports.get(key) else {
                    return;
                };
                context.assume(&proof.dependencies);
                context.propose(CallableFactKey::Export(key.clone()), proof.summary.clone());
            }
            CallableProjectionRule::Import(projection) => {
                let provider = CallableFactKey::Export(projection.provider.clone());
                let Some(summary) = context.read(provider).cloned() else {
                    return;
                };
                context.propose(
                    CallableFactKey::Import(projection.imported.clone()),
                    summary,
                );
            }
        }
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        let previous = current.clone();
        current.merge(contribution);
        *current != previous
    }
}

fn solve_callable_import_projections(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    exports: BTreeMap<CallableExportKey, CallableExportProof>,
) -> BTreeMap<ModuleKey, BTreeMap<String, CallableSummary>> {
    let rules = CallableProjectionRules::new(exports, modules);
    let producers = rules.producers();
    let solved = IterativeInference::new(rules).solve(producers);
    modules
        .iter()
        .map(|(module, facts)| {
            let bindings = facts
                .imports
                .keys()
                .filter_map(|local| {
                    let key = CallableFactKey::Import(CallableImportKey {
                        module: module.clone(),
                        local: local.clone(),
                    });
                    solved
                        .get(&key)
                        .map(|judgment| (local.clone(), judgment.value().clone()))
                })
                .collect();
            (module.clone(), bindings)
        })
        .collect()
}

pub fn resolve_imported_callable_summaries(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<ModuleKey, BTreeSet<String>>,
    descriptor_copy_asserted: &BTreeMap<
        ModuleKey,
        BTreeMap<String, Vec<spec::DescriptorCopyFactorySignature>>,
    >,
    selected_value_inputs: &BTreeMap<ModuleKey, Vec<SelectedBindingValueInput>>,
    selected_native_operation_inputs: &BTreeMap<ModuleKey, Vec<SelectedNativeOperationInput>>,
    explicit_binding_destinations: &BTreeMap<ModuleKey, HashMap<Id, usize>>,
    argument_inputs: &BTreeMap<ModuleKey, Vec<crate::analysis_inputs::ResolvedArgumentInput>>,
) -> Result<ResolvedCallableSummaries> {
    let required: BTreeSet<_> = modules
        .values()
        .flat_map(|facts| facts.imports.values())
        .map(|target| CallableExportKey {
            module: target.module.clone(),
            export: target.export.clone(),
        })
        .collect();
    let mut export_summaries: BTreeMap<CallableExportKey, CallableExportProof> = BTreeMap::new();
    for (module, exports) in asserted {
        for export in exports {
            if modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export))
            {
                export_summaries
                    .entry(CallableExportKey {
                        module: module.clone(),
                        export: export.clone(),
                    })
                    .or_default()
                    .add(
                        CallableSummary::callback_factory(
                            CallbackFactoryEvidence::AuthorAssertion {
                                defining_module: module.clone(),
                                export: export.clone(),
                            },
                        ),
                        ProofDependencies::default(),
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
    let mut resolved_native_operation_inputs = BTreeMap::new();
    for (module, facts) in modules {
        let locals = facts
            .exports
            .iter()
            .filter(|(export, _)| {
                required.contains(&CallableExportKey {
                    module: module.clone(),
                    export: (*export).clone(),
                })
            })
            .map(|(_, local)| local.clone())
            .collect();
        let views = top_level_item_views(facts.body);
        let mut prepared_module =
            crate::facts::sync_effects::execution_index::prepare_callable_capabilities(
                &views,
                crate::facts::StableArraySpreadShapes::collect(facts.body, &views),
                locals,
                selected_value_inputs
                    .get(module)
                    .cloned()
                    .unwrap_or_default(),
                selected_native_operation_inputs
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
        if !prepared_module.native_operation_input_errors().is_empty() {
            bail!(
                "invalid native operation input for provider {module}: {}",
                prepared_module.native_operation_input_errors().join("; ")
            );
        }
        prepared_module.locate_native_operation_inputs(facts.source.as_ref());
        let module_native_inputs = prepared_module.resolved_native_operation_inputs();
        if !module_native_inputs.is_empty() && facts.source.is_none() {
            bail!(
                "native operation input for provider {module} has no retained parser source locations"
            );
        }
        if !module_native_inputs.is_empty() {
            resolved_native_operation_inputs.insert(module.clone(), module_native_inputs);
        }
        prepared_module.locate_native_writes(module, facts.source.as_ref());
        prepared.insert(module, prepared_module);
    }
    for input in selected_native_operation_inputs
        .values()
        .flatten()
        .filter(|input| {
            matches!(
                input.evidence.operation,
                NativeInputKind::Operation(
                    spec::NativeOperationKind::Symbol
                        | spec::NativeOperationKind::SymbolFor
                        | spec::NativeOperationKind::PromiseResolve
                )
            )
        })
    {
        if let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_global_native_operation_mutation_owner(input.evidence.operation)
                .map(|owner| (module, owner))
        }) {
            bail!(
                "{} is contradicted by a positively identified global {} replacement or operation mutation in provider {module}, source unit {}",
                input.evidence.spec_location,
                match input.evidence.operation {
                    NativeInputKind::Operation(
                        spec::NativeOperationKind::Symbol | spec::NativeOperationKind::SymbolFor,
                    ) => "Symbol",
                    _ => "Promise",
                },
                owner.0
            );
        }
    }
    if selected_native_operation_inputs
        .values()
        .flatten()
        .any(|input| {
            input.evidence.operation
                == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze)
        })
        && let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_object_freeze_mutation_owner()
                .map(|owner| (module, owner))
        })
    {
        let request = selected_native_operation_inputs
            .values()
            .flatten()
            .find(|input| {
                input.evidence.operation
                    == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze)
            })
            .map(|input| input.evidence.spec_location.as_str())
            .unwrap_or("native_call_identity_inputs");
        bail!(
            "{request} is contradicted by a positively identified required Object.freeze slot write in provider {module}, source unit {}",
            owner.0
        );
    }
    if selected_native_operation_inputs
        .values()
        .flatten()
        .any(|input| {
            input.evidence.operation
                == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::SetEmptyPrivateField)
        })
        && let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_global_set_mutation_owner()
                .map(|owner| (module, owner))
        })
    {
        let request = selected_native_operation_inputs
            .values()
            .flatten()
            .find(|input| {
                input.evidence.operation
                    == NativeInputKind::CallIdentity(
                        spec::NativeCallIdentityKind::SetEmptyPrivateField,
                    )
            })
            .map(|input| input.evidence.spec_location.as_str())
            .unwrap_or("native_call_identity_inputs");
        bail!(
            "{request} is contradicted by a positively identified global Set slot write in provider {module}, source unit {}",
            owner.0
        );
    }
    if let Some(input) = selected_native_operation_inputs
        .values()
        .flatten()
        .find(|input| {
            input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SymbolFor)
        })
        && let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_symbol_for_mutation_owner()
                .map(|owner| (module, owner))
        })
    {
        bail!(
            "{} is contradicted by a positively identified Symbol.for replacement or accessor installation in provider {module}, source unit {}",
            input.evidence.spec_location,
            owner.0
        );
    }
    let mut stable_set_spreads = BTreeMap::new();
    let empty_destinations = HashMap::new();
    for (module, inputs) in &resolved_native_operation_inputs {
        let destinations = explicit_binding_destinations
            .get(module)
            .unwrap_or(&empty_destinations);
        let uses = crate::facts::certify_stable_set_spreads(inputs, destinations)?;
        if !uses.is_empty() {
            stable_set_spreads.insert(module.clone(), uses);
        }
    }
    if selected_native_operation_inputs
        .values()
        .flatten()
        .any(|input| {
            input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray)
        })
        && let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_set_protocol_mutation_owner()
                .map(|owner| (module, owner))
        })
    {
        let request = selected_native_operation_inputs
            .values()
            .flatten()
            .find(|input| {
                input.evidence.operation
                    == NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray)
            })
            .map(|input| input.evidence.spec_location.as_str())
            .unwrap_or("native_operation_inputs");
        bail!(
            "{request} is contradicted by a positively identified required Set construction protocol slot write in provider {module}, source unit {}",
            owner.0
        );
    }
    if selected_native_operation_inputs
        .values()
        .flatten()
        .any(|input| {
            input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SetFromSetSpread)
        })
        && let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_set_spread_protocol_mutation_owner()
                .map(|owner| (module, owner))
        })
    {
        let request = selected_native_operation_inputs
            .values()
            .flatten()
            .find(|input| {
                input.evidence.operation
                    == NativeInputKind::Operation(spec::NativeOperationKind::SetFromSetSpread)
            })
            .map(|input| input.evidence.spec_location.as_str())
            .unwrap_or("native_operation_inputs");
        bail!(
            "{request} is contradicted by a positively identified required Set spread protocol slot write in provider {module}, source unit {}",
            owner.0
        );
    }
    for (provider, inputs) in &resolved_native_operation_inputs {
        for input in inputs {
            if input.evidence.operation
                != NativeInputKind::Operation(spec::NativeOperationKind::ObjectOwnDataFunctionRead)
            {
                continue;
            }
            let property = prepared[provider]
                .object_read_property(input)
                .ok_or_else(|| {
                    anyhow::anyhow!(
                        "{} lost its exact Object own-data read during preparation",
                        input.evidence.spec_location
                    )
                })?;
            if let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
                facts
                    .known_object_read_contradiction_owner(&property)
                    .map(|owner| (module, owner))
            }) {
                bail!(
                    "{} is contradicted by a known Object binding or Object.{property} write in provider {module}, source unit {}",
                    input.evidence.spec_location,
                    owner.0
                );
            }
        }
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
            export_summaries
                .entry(CallableExportKey {
                    module: module.clone(),
                    export: export.clone(),
                })
                .or_default()
                .add(summary, ProofDependencies::default());
        }
    }
    // This projection is the source-only input to native branch refinement.
    // Generic inferred callback factories are deliberately added after the
    // completed all-module native census and cannot flow back into it.
    let source_only_imports = solve_callable_import_projections(modules, export_summaries.clone());
    for (module, facts) in &mut prepared {
        facts.refine_native_branches(
            source_only_imports
                .get(module.as_str())
                .expect("every prepared module has an import projection"),
        );
        facts.locate_native_writes(module, modules[*module].source.as_ref());
    }
    let native_integrity = crate::facts::sync_effects::execution_index::resolve_native_integrity(
        prepared.values().map(|facts| facts.native_writes()),
    );
    for (module, exports) in descriptor_copy_asserted {
        for (export, signatures) in exports {
            if !required.contains(&CallableExportKey {
                module: module.clone(),
                export: export.clone(),
            }) || !modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export))
            {
                continue;
            }
            if signatures.len() != 1 {
                bail!(
                    "descriptor_copy_factory_exports {module}:{export} requires exactly one audited signature"
                );
            }
            for signature in signatures {
                if signature.argument_count != 1
                    || signature.object_argument != 0
                    || signature.eager_own_data_keys.is_empty()
                    || signature.assigned_data_keys.is_empty()
                    || signature
                        .eager_own_data_keys
                        .iter()
                        .any(|key| key == "__proto__")
                    || signature
                        .assigned_data_keys
                        .iter()
                        .any(|key| key == "__proto__")
                {
                    bail!(
                        "descriptor_copy_factory_exports {module}:{export} requires one object argument and nonempty safe key sets"
                    );
                }
                if let Some((write_module, owner)) =
                    prepared.iter().find_map(|(write_module, facts)| {
                        facts
                            .known_descriptor_copy_mutation_owner(&signature.assigned_data_keys)
                            .map(|owner| (write_module, owner))
                    })
                {
                    bail!(
                        "descriptor_copy_factory_exports {module}:{export} is contradicted by required Object slot/prototype write in {write_module}, source unit {}",
                        owner.0
                    );
                }
            }
        }
    }
    // Array-protocol contradictions use a premise-relative copy of the
    // all-source census. It preserves each candidate's Array effect and its
    // conservative fallback, but the selected Array-spread premise may
    // identify the iterator receiver while Object/Symbol remain independent.
    let array_spread_request = selected_native_operation_inputs
        .values()
        .flatten()
        .find_map(|input| {
            (input.evidence.operation
                == NativeInputKind::Operation(
                    spec::NativeOperationKind::ArraySpreadFromPrimitiveArrays,
                ))
            .then_some(input.evidence.spec_location.as_str())
        })
        .or_else(|| {
            resolved_native_operation_inputs
                .values()
                .flatten()
                .find_map(|input| {
                    (input.evidence.operation
                        == NativeInputKind::Operation(
                            spec::NativeOperationKind::ArraySpreadFromPrimitiveArrays,
                        ))
                    .then_some(input.evidence.spec_location.as_str())
                })
        });
    if let Some(request) = array_spread_request {
        let protocol_integrity =
            crate::facts::sync_effects::execution_index::resolve_array_spread_protocol_integrity(
                prepared.values().map(|facts| facts.native_writes()),
            );
        if let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_array_iterator_protocol_mutation_owner_in(&protocol_integrity)
                .map(|owner| (module, owner))
        }) {
            bail!(
                "{request} is contradicted by a known Array iterator protocol write in provider {module}, source unit {}",
                owner.0
            );
        }
    }
    let inferred: BTreeMap<_, _> = prepared
        .into_iter()
        .map(|(module, prepared)| {
            let views = top_level_item_views(modules[module].body);
            let required_stable_providers = descriptor_copy_asserted
                .get(module)
                .into_iter()
                .flat_map(|exports| exports.keys())
                .filter_map(|export| modules[module].exports.get(export.as_str()))
                .cloned()
                .collect();
            (
                module,
                crate::facts::sync_effects::execution_index::infer_callable_capabilities(
                    &views,
                    prepared,
                    &native_integrity,
                    &required_stable_providers,
                    modules[module].exports.keys().any(|export| {
                        required.contains(&CallableExportKey {
                            module: module.clone(),
                            export: export.clone(),
                        })
                    }),
                ),
            )
        })
        .collect();
    for (module, exports) in descriptor_copy_asserted {
        for (export, signatures) in exports {
            let key = CallableExportKey {
                module: module.clone(),
                export: export.clone(),
            };
            if !required.contains(&key) {
                continue;
            }
            let Some(local) = modules
                .get(module)
                .and_then(|facts| facts.exports.get(export))
            else {
                continue;
            };
            if !inferred[module].stable_providers.contains(local) {
                bail!(
                    "descriptor_copy_factory_exports {module}:{export} requires a stable, unrebound direct function export"
                );
            }
            export_summaries.entry(key).or_default().add(
                CallableSummary::descriptor_copy_factory(DescriptorCopyFactorySummary {
                    defining_module: module.clone(),
                    export: export.clone(),
                    author_location: format!(
                        "chunk_export_purity.{module}.descriptor_copy_factory_exports.{export}"
                    ),
                    signatures: signatures.clone(),
                }),
                ProofDependencies::default(),
            );
        }
    }
    // A construction premise must hold in every analyzed chunk's census,
    // including the importer's.
    let intact_collections = inferred
        .values()
        .map(|capabilities| &capabilities.intact_collections)
        .fold(None::<BTreeSet<_>>, |all, module| {
            Some(match all {
                None => module.clone(),
                Some(all) => all.intersection(module).copied().collect(),
            })
        })
        .unwrap_or_default();
    for (module, facts) in modules {
        let Some(capabilities) = inferred.get(module) else {
            continue;
        };
        for (export, local) in &facts.exports {
            let key = CallableExportKey {
                module: module.clone(),
                export: export.clone(),
            };
            if !required.contains(&key) || export_summaries.contains_key(&key) {
                continue;
            }
            let Some(factory) = capabilities.foreign_calls.get(local).filter(|call| {
                call.callback_factory
                    && call
                        .derived
                        .native_constructions
                        .is_subset(&intact_collections)
            }) else {
                continue;
            };
            export_summaries.entry(key).or_default().add(
                CallableSummary::callback_factory(
                    CallbackFactoryEvidence::InferredImmutableWrapper {
                        defining_module: module.clone(),
                        export: export.clone(),
                        local: local.clone(),
                        source_location: facts.source.as_ref().and_then(|(path, index)| {
                            let (line, column) = index
                                .line_and_column_for_pos(BytePos(factory.declaration_start))?;
                            Some(SourceLocation {
                                source_path: path.clone(),
                                start_line: line,
                                end_line: line,
                                start_column: Some(column),
                            })
                        }),
                    },
                ),
                factory.dependencies.clone(),
            );
        }
    }
    for (module, facts) in modules {
        let Some(capabilities) = inferred.get(module) else {
            continue;
        };
        for (export, local) in &facts.exports {
            let key = CallableExportKey {
                module: module.clone(),
                export: export.clone(),
            };
            // An initializer may not have run when the importer calls it.
            let Some(call) = capabilities
                .foreign_calls
                .get(local)
                .filter(|call| call.hoisted)
            else {
                continue;
            };
            if !required.contains(&key)
                || !call
                    .derived
                    .native_constructions
                    .is_subset(&intact_collections)
            {
                continue;
            }
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
            if let Some(record) = &call.record {
                export_summaries.entry(key.clone()).or_default().add(
                    CallableSummary::returned_record(ReturnedRecordSummary {
                        defining_module: module.clone(),
                        export: export.clone(),
                        local: local.clone(),
                        source_location: locate(call.declaration_start),
                        members: record
                            .members
                            .iter()
                            .map(|(name, start, captures)| DeferredRecordMember {
                                name: name.clone(),
                                source_location: locate(*start),
                                captures: captures.clone(),
                            })
                            .collect(),
                        inference: record.inference.clone(),
                    }),
                    call.dependencies.clone(),
                );
            }
            export_summaries.entry(key).or_default().add(
                CallableSummary::foreign_call(ForeignCallSummary {
                    defining_module: module.clone(),
                    export: export.clone(),
                    local: local.clone(),
                    source_location: locate(call.declaration_start),
                    invocation: call.derived.invocation,
                    returns: call.derived.returns.clone(),
                    retained_arguments: call.derived.retained_arguments.clone(),
                    native_constructions: call.derived.native_constructions.clone(),
                    replacement_package: facts.replacement_packages.get(export).cloned(),
                    dependencies: call.dependencies.clone(),
                }),
                call.dependencies.clone(),
            );
        }
    }
    for (module, facts) in modules {
        for (export, local) in &facts.exports {
            let key = CallableExportKey {
                module: module.clone(),
                export: export.clone(),
            };
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
            export_summaries.entry(key).or_default().add(
                CallableSummary::namespace_wrapper(NamespaceWrapperSummary {
                    defining_module: module.clone(),
                    export: export.clone(),
                    local: local.clone(),
                    source_location,
                    inference: wrapper.inference.clone(),
                    dependencies: wrapper.dependencies.clone(),
                }),
                wrapper.dependencies.clone(),
            );
        }
    }
    let imported_callables = solve_callable_import_projections(modules, export_summaries);
    Ok(ResolvedCallableSummaries {
        imported_callables,
        native_integrity: Some(native_integrity),
        value_inputs: resolved_value_inputs,
        native_operation_inputs: resolved_native_operation_inputs,
        stable_set_spreads,
        argument_inputs: argument_inputs.clone(),
    })
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
            replacement_packages: BTreeMap::new(),
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
            ..Default::default()
        }
    }

    #[test]
    fn callable_import_projection_preserves_typed_judgment_dependencies() {
        let app = parse("import { build } from 'provider';");
        let provider = parse("export function build() { return {}; }");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("build", "provider", "build")], &[]),
            ),
            (
                "provider".to_string(),
                facts(&provider, &[], &[("build", "build")]),
            ),
        ]);
        let reported_dependencies = ValueInputRef {
            defining_chunk: "report-only.js".to_string(),
            input_index: 99,
        };
        let summary = CallableSummary::returned_record(ReturnedRecordSummary {
            defining_module: "provider".to_string(),
            export: "build".to_string(),
            local: "build".to_string(),
            source_location: None,
            members: Vec::new(),
            inference: crate::reports::schema::InferenceUse {
                conclusion: crate::reports::schema::InferenceRule::ReturnedRecord,
                derivation_rules: BTreeSet::from([
                    crate::reports::schema::InferenceRule::ReturnedRecord,
                ]),
                value_input_refs: BTreeSet::from([reported_dependencies]),
                descriptor_copy_author_refs: Default::default(),
                argument_input_refs: BTreeSet::new(),
                argument_input_spec_locations: BTreeSet::new(),
                native_operation_input_refs: BTreeSet::new(),
            },
        });
        let trusted_dependencies = value_input_dependencies("provider-values.js", 0);
        let mut export = CallableExportProof::default();
        export.add(summary.clone(), trusted_dependencies.clone());
        let exports = BTreeMap::from([(
            CallableExportKey {
                module: "provider".to_string(),
                export: "build".to_string(),
            },
            export,
        )]);
        let rules = CallableProjectionRules::new(exports, &modules);
        let producers = rules.producers();
        let solved = IterativeInference::new(rules).solve(producers);

        let import_key = CallableFactKey::Import(CallableImportKey {
            module: "app".to_string(),
            local: "build".to_string(),
        });
        let judgment = solved
            .get(&import_key)
            .expect("provider summary projects onto the resolved importer");
        assert_eq!(judgment.value(), &summary);
        assert_eq!(
            judgment.dependencies(),
            &trusted_dependencies,
            "typed proof dependencies flow through the import; report references are projection-only"
        );
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
    fn mutually_recursive_imported_functions_close_without_optimistic_seeds() {
        let a = parse("import { right } from 'b'; export function left() { return right(); }");
        let b = parse("import { left } from 'a'; export function right() { return left(); }");
        let modules = BTreeMap::from([
            (
                "a".to_string(),
                facts(&a, &[("right", "b", "right")], &[("left", "left")]),
            ),
            (
                "b".to_string(),
                facts(&b, &[("left", "a", "left")], &[("right", "right")]),
            ),
        ]);

        let resolved = resolve_imported_purities(
            &modules,
            &BTreeMap::new(),
            &ResolvedCallableSummaries::default(),
        );

        assert!(resolved["a"]["right"].is_pure());
        assert!(resolved["b"]["left"].is_pure());
    }

    #[test]
    fn impurity_in_mutually_recursive_imports_has_finite_cause_chains() {
        let a = parse("import { right } from 'b'; export function left() { return right(); }");
        let b =
            parse("import { left } from 'a'; export function right() { left(); globalSink(); }");
        let modules = BTreeMap::from([
            (
                "a".to_string(),
                facts(&a, &[("right", "b", "right")], &[("left", "left")]),
            ),
            (
                "b".to_string(),
                facts(&b, &[("left", "a", "left")], &[("right", "right")]),
            ),
        ]);

        let resolved = resolve_imported_purities(
            &modules,
            &BTreeMap::new(),
            &ResolvedCallableSummaries::default(),
        );

        let right = &resolved["a"]["right"];
        let left = &resolved["b"]["left"];
        assert!(!right.is_pure());
        assert!(!left.is_pure());
        assert_finite_causes(right);
        assert_finite_causes(left);
    }

    fn assert_finite_causes(root: &Arc<Purity>) {
        fn visit(
            purity: &Arc<Purity>,
            active: &mut BTreeSet<usize>,
            completed: &mut BTreeSet<usize>,
        ) {
            let identity = Arc::as_ptr(purity) as usize;
            assert!(
                !active.contains(&identity),
                "purity cause graph contains a cycle"
            );
            if !completed.insert(identity) {
                return;
            }
            active.insert(identity);
            if let Purity::NotPure { reasons } = purity.as_ref() {
                for reason in reasons {
                    if let Some(cause) = &reason.cause {
                        visit(cause, active, completed);
                    }
                }
            }
            active.remove(&identity);
        }

        visit(root, &mut BTreeSet::new(), &mut BTreeSet::new());
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
    fn imported_pure_dependencies_are_retained_by_local_wrappers() {
        let dependencies = value_input_dependencies("static/provider.js", 1);
        let imported = BTreeMap::from([(
            "upstream".to_string(),
            Arc::new(Purity::pure().with_dependencies(&dependencies)),
        )]);
        let wrapper = function_purity_with_imports(
            "export function forward() { return upstream(); }",
            "forward",
            &imported,
        );
        assert_eq!(wrapper.dependencies(), Some(&dependencies));
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
        let resolved = resolve_imported_callable_summaries(
            &modules,
            &asserted,
            &BTreeMap::new(),
            &BTreeMap::new(),
            &BTreeMap::new(),
            &BTreeMap::new(),
            &BTreeMap::new(),
        )
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
