use super::module_facts::ModulePurityFacts;
use super::stable_imports::resolve_stable_imports;
use analysis::diagnostics::{ChunkDiagnostic, Diagnostics, ExportContract};
use analysis_domain::item_views::{TopLevelItemView, top_level_item_views};
use analysis_vocabulary::inputs::{
    BundleGlobalNames, CallableSummary, CallbackFactoryEvidence, CallbackObjectArguments,
    DeferredRecordMember, DescriptorCopyFactorySummary, ForeignCallSummary,
    NamespaceWrapperSummary, NativeInputKind, NativeIntegrity, NativeIntrinsic, ProofDependencies,
    ResolvedBindingValueInput, ResolvedNativeOperationInput, ReturnedRecordSummary,
    SelectedBindingValueInput, SelectedNativeOperationInput,
};
use anyhow::{Result, bail};
use execution_index::callable_capabilities::{
    InferredCallableCapabilities, PreparedCallableCapabilities,
};
use inference::{FactContext, IterativeInference, IterativeRules};
use std::collections::{BTreeMap, BTreeSet};
use swc_common::BytePos;
use value_flow::program_bindings::{BindingId, ExportId, ProgramBindings, SourceModuleKey};

/// Callback transfer is independent of purity, including asserted purity.
/// Compose checked source call timing and returned capabilities separately
/// from author assertions. Missing exports and unsupported bodies stay opaque.
#[derive(Default)]
pub struct ResolvedCallableSummaries {
    pub imported_callables: BTreeMap<SourceModuleKey, BTreeMap<String, CallableSummary>>,
    pub native_integrity: Option<NativeIntegrity>,
    pub value_inputs: BTreeMap<SourceModuleKey, Vec<ResolvedBindingValueInput>>,
    pub native_operation_inputs: BTreeMap<SourceModuleKey, Vec<ResolvedNativeOperationInput>>,
    pub imported_primitive_array_spread_inputs: BTreeMap<
        SourceModuleKey,
        Vec<analysis_vocabulary::inputs::ResolvedImportedPrimitiveArraySpreadInput>,
    >,
    pub(crate) selected_imported_primitive_array_spread_inputs: BTreeMap<
        SourceModuleKey,
        Vec<analysis_vocabulary::inputs::AnalysisImportedPrimitiveArraySpreadInput>,
    >,
    pub inferred_set_constructions: BTreeMap<
        SourceModuleKey,
        BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>,
    >,
    pub argument_inputs:
        BTreeMap<SourceModuleKey, Vec<analysis_vocabulary::inputs::ResolvedArgumentInput>>,
    /// The whole-program bundle-owned globals the census used.
    pub bundle_globals: BundleGlobalNames,
    /// Structural value flows already solved while deriving summaries, which
    /// the purity oracle's census of the same body takes instead of solving
    /// again.
    pub structural_flows: value_flow::ProgramValueFlows,
    /// Negative facts from the already prepared original provider callables.
    /// Missing entries are unknown, not proof that a provider is effect free.
    known_original_external_writes: BTreeMap<SourceModuleKey, BTreeMap<String, String>>,
}

impl ResolvedCallableSummaries {
    /// A source-known contradiction for one requested original export. No
    /// result means this bounded negative check found no contradiction.
    pub fn known_original_external_write(&self, module: &str, export: &str) -> Option<&str> {
        self.known_original_external_writes
            .get(module)?
            .get(export)
            .map(String::as_str)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) enum CallableFactKey {
    Export(ExportId),
    Import(BindingId),
}

#[derive(Debug, Clone, Default)]
pub(super) struct CallableExportProof {
    summary: CallableSummary,
    dependencies: ProofDependencies,
}

impl CallableExportProof {
    pub(super) fn add(&mut self, summary: CallableSummary, dependencies: ProofDependencies) {
        self.summary.merge(summary);
        self.dependencies.extend(&dependencies);
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) enum CallableProjectionRule {
    Export(ExportId),
    Import(BindingId),
}

pub(super) struct CallableProjectionRules<'a> {
    pub(super) bindings: &'a ProgramBindings,
    pub(super) exports: BTreeMap<ExportId, CallableExportProof>,
}

impl CallableProjectionRules<'_> {
    pub(super) fn producers(&self) -> BTreeSet<CallableProjectionRule> {
        self.exports
            .keys()
            .copied()
            .map(CallableProjectionRule::Export)
            .chain(
                self.bindings
                    .modules()
                    .flat_map(|module| self.bindings.imports(module))
                    .map(|(local, _)| CallableProjectionRule::Import(local)),
            )
            .collect()
    }
}

impl IterativeRules for CallableProjectionRules<'_> {
    type Key = CallableFactKey;
    type Fact = CallableSummary;
    type Rule = CallableProjectionRule;

    type MergeScratch = ();
    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        match rule {
            CallableProjectionRule::Export(key) => {
                let Some(proof) = self.exports.get(key) else {
                    return;
                };
                context.assume(&proof.dependencies);
                context.propose(CallableFactKey::Export(*key), proof.summary.clone());
            }
            CallableProjectionRule::Import(local) => {
                let provider = self
                    .bindings
                    .import_target(*local)
                    .expect("prepared import");
                let Some(summary) = context.read(CallableFactKey::Export(provider)).cloned() else {
                    return;
                };
                context.propose(CallableFactKey::Import(*local), summary);
            }
        }
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        current.merge(contribution)
    }
}

fn solve_callable_import_projections(
    bindings: &ProgramBindings,
    exports: BTreeMap<ExportId, CallableExportProof>,
) -> BTreeMap<SourceModuleKey, BTreeMap<String, CallableSummary>> {
    let rules = CallableProjectionRules { bindings, exports };
    let producers = rules.producers();
    let solved = IterativeInference::new(rules).solve(producers);
    bindings
        .modules()
        .map(|module| {
            let imports = bindings
                .imports(module)
                .filter_map(|(local, _)| {
                    let summary = solved.get(&CallableFactKey::Import(local))?;
                    Some((
                        bindings.binding_name(local).1.clone(),
                        summary.value().clone(),
                    ))
                })
                .collect();
            (module.clone(), imports)
        })
        .collect()
}

/// The author inputs and host claims `resolve_imported_callable_summaries`
/// checks the providers against.
pub struct CallableSummaryInputs<'a> {
    /// Ordinary imports with a complete, dynamically closed provider dependency
    /// closure excluding the importer. This proves allocation ownership only.
    pub closed_factory_imports: &'a BTreeMap<SourceModuleKey, BTreeSet<String>>,
    /// Full importer-local namespace family; empty member sets retain roots
    /// configured by other namespace contracts without asserting independence.
    pub namespace_privacy_members:
        &'a BTreeMap<SourceModuleKey, BTreeMap<String, BTreeSet<String>>>,
    pub callback_factory_members: &'a BTreeMap<SourceModuleKey, BTreeMap<String, BTreeSet<String>>>,
    /// Exact original exports selected by audited initializer call inputs.
    pub requested_original_provider_exports: &'a BTreeSet<(SourceModuleKey, String)>,
    pub selected_value_inputs: &'a BTreeMap<SourceModuleKey, Vec<SelectedBindingValueInput>>,
    pub selected_native_operation_inputs:
        &'a BTreeMap<SourceModuleKey, Vec<SelectedNativeOperationInput>>,
    pub selected_imported_primitive_array_spread_inputs: &'a BTreeMap<
        SourceModuleKey,
        Vec<analysis_vocabulary::inputs::AnalysisImportedPrimitiveArraySpreadInput>,
    >,
    pub argument_inputs:
        &'a BTreeMap<SourceModuleKey, Vec<analysis_vocabulary::inputs::ResolvedArgumentInput>>,
    pub predefined_globals: spec::PredefinedGlobals,
    pub no_accessor_install_callables:
        &'a BTreeMap<SourceModuleKey, Vec<analysis_vocabulary::inputs::ResolvedNoAccessorInstall>>,
    /// `program_premises.intrinsics_never_written` families.
    pub intrinsics_never_written: BTreeSet<NativeIntrinsic>,
}

pub fn resolve_imported_callable_summaries(
    bindings: &ProgramBindings,
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
    descriptor_copy_asserted: &BTreeMap<
        SourceModuleKey,
        BTreeMap<String, Vec<spec::DescriptorCopyFactorySignature>>,
    >,
    inputs: CallableSummaryInputs<'_>,
    diagnostics: &Diagnostics,
) -> Result<ResolvedCallableSummaries> {
    let CallableSummaryInputs {
        closed_factory_imports,
        namespace_privacy_members,
        callback_factory_members,
        requested_original_provider_exports,
        selected_value_inputs,
        selected_native_operation_inputs,
        selected_imported_primitive_array_spread_inputs,
        argument_inputs,
        predefined_globals,
        no_accessor_install_callables,
        intrinsics_never_written,
    } = inputs;
    let required: BTreeSet<_> = bindings
        .modules()
        .flat_map(|module| bindings.imports(module).map(|(_, export)| export))
        .collect();
    let mut export_summaries =
        asserted_callback_factory_proofs(bindings, modules, asserted, diagnostics);
    // The bundle-owned globals are a whole-program greatest fixed point over
    // every module's writes; a second preparation reads them.
    let preparation = ProviderPreparation {
        bindings,
        modules,
        required: &required,
        selected_value_inputs,
        selected_native_operation_inputs,
        predefined_globals,
        namespace_privacy_members,
    };
    let PreparedProviders {
        mut prepared,
        mut resolved_value_inputs,
        mut resolved_native_operation_inputs,
    } = preparation.prepare(&BundleGlobalNames::default())?;
    let mut bundle_globals = BundleGlobalNames::default();
    if predefined_globals != spec::PredefinedGlobals::Unknown {
        bundle_globals =
            execution_index::bundle_globals::resolve(prepared.iter().map(|(module, facts)| {
                (
                    facts,
                    no_accessor_install_callables
                        .get(*module)
                        .map_or(&[][..], Vec::as_slice),
                )
            }));
        if bundle_globals != BundleGlobalNames::default() {
            // The first census has supplied its global-name result. Retire
            // its complete source indexes before constructing their replacements.
            drop(prepared);
            PreparedProviders {
                prepared,
                resolved_value_inputs,
                resolved_native_operation_inputs,
            } = preparation.prepare(&bundle_globals)?;
        }
    }
    let known_original_external_writes = resolve_known_original_external_writes(
        modules,
        &prepared,
        requested_original_provider_exports,
    );
    reject_contradicted_native_operation_inputs(selected_native_operation_inputs, &prepared)?;
    let inferred_set_constructions =
        preflow_set_constructions(&prepared, &intrinsics_never_written);

    // Author declarations are available before flow. Inferred wrapper proofs
    // depend on the completed flow and must never feed this solve back into itself.
    // Stability comes from raw source declarations, not purity assertions.
    let source_stable = resolve_stable_imports(bindings, modules, &BTreeMap::new());
    let declared_imports = solve_callable_import_projections(bindings, export_summaries.clone());
    let fresh_factory_imports = collect_fresh_factory_imports(
        modules,
        &declared_imports,
        &source_stable,
        closed_factory_imports,
    );
    let fresh_factory_members = collect_fresh_factory_members(
        modules,
        &prepared,
        &source_stable,
        closed_factory_imports,
        callback_factory_members,
    );
    // All source identities are qualified once, then the existing flow rules
    // close imports, calls and object carriers in one program-wide store.
    // These are possible values, not imported execution/availability proofs.
    diagnostics.progress(
        "*",
        progress::Phase::ProgramValueFlow,
        progress::State::Start,
    );
    let flow_views = collect_flow_views(modules, diagnostics);
    let mut structural_flows = solve_program_value_flows(
        bindings,
        &flow_views,
        &fresh_factory_imports,
        &fresh_factory_members,
        &resolved_native_operation_inputs,
        &inferred_set_constructions,
        diagnostics,
    );
    diagnostics.progress("*", progress::Phase::ProgramValueFlow, progress::State::End);
    add_inferred_wrapper_summaries(
        bindings,
        modules,
        &prepared,
        &flow_views,
        &structural_flows,
        &mut export_summaries,
    );
    let native_integrity = refine_native_integrity(
        bindings,
        modules,
        &mut prepared,
        &export_summaries,
        &intrinsics_never_written,
    )?;
    validate_descriptor_copy_assertions(
        bindings,
        modules,
        descriptor_copy_asserted,
        &required,
        &prepared,
    )?;
    reject_contradicted_array_spread_inputs(
        selected_imported_primitive_array_spread_inputs,
        &prepared,
    )?;
    let inferred = preparation.infer_capabilities(
        prepared,
        descriptor_copy_asserted,
        &flow_views,
        &native_integrity,
        &structural_flows,
    );
    add_descriptor_copy_summaries(
        bindings,
        modules,
        descriptor_copy_asserted,
        &required,
        &inferred,
        &mut export_summaries,
    )?;
    add_inferred_capability_summaries(
        bindings,
        modules,
        &required,
        &inferred,
        &mut export_summaries,
    );
    let mut imported_callables = solve_callable_import_projections(bindings, export_summaries);
    add_importer_cell_reads(bindings, &mut structural_flows, &mut imported_callables);
    Ok(ResolvedCallableSummaries {
        bundle_globals,
        imported_callables,
        native_integrity: Some(native_integrity),
        value_inputs: resolved_value_inputs,
        native_operation_inputs: resolved_native_operation_inputs,
        imported_primitive_array_spread_inputs: BTreeMap::new(),
        selected_imported_primitive_array_spread_inputs:
            selected_imported_primitive_array_spread_inputs.clone(),
        inferred_set_constructions,
        argument_inputs: argument_inputs.clone(),
        structural_flows,
        known_original_external_writes,
    })
}
/// The provider inputs `resolve_imported_callable_summaries` prepares once
/// per bundle-globals census and later infers capabilities from.
struct ProviderPreparation<'a> {
    bindings: &'a ProgramBindings,
    modules: &'a BTreeMap<SourceModuleKey, ModulePurityFacts<'a>>,
    required: &'a BTreeSet<ExportId>,
    selected_value_inputs: &'a BTreeMap<SourceModuleKey, Vec<SelectedBindingValueInput>>,
    selected_native_operation_inputs:
        &'a BTreeMap<SourceModuleKey, Vec<SelectedNativeOperationInput>>,
    predefined_globals: spec::PredefinedGlobals,
    namespace_privacy_members: &'a BTreeMap<SourceModuleKey, BTreeMap<String, BTreeSet<String>>>,
}

/// One bundle-globals census of every provider.
struct PreparedProviders<'a> {
    prepared: BTreeMap<&'a SourceModuleKey, PreparedCallableCapabilities>,
    resolved_value_inputs: BTreeMap<SourceModuleKey, Vec<ResolvedBindingValueInput>>,
    resolved_native_operation_inputs: BTreeMap<SourceModuleKey, Vec<ResolvedNativeOperationInput>>,
}

impl<'a> ProviderPreparation<'a> {
    fn prepare(&self, bundle_globals: &BundleGlobalNames) -> Result<PreparedProviders<'a>> {
        let ProviderPreparation {
            bindings,
            modules,
            required,
            selected_value_inputs,
            selected_native_operation_inputs,
            predefined_globals,
            namespace_privacy_members,
        } = *self;
        let mut prepared = BTreeMap::new();
        let mut resolved_value_inputs = BTreeMap::new();
        let mut resolved_native_operation_inputs = BTreeMap::new();
        for (module, facts) in modules {
            let locals = facts
                .exports
                .iter()
                .filter(|(export, _)| {
                    required.contains(&prepared_export_id(bindings, module, export))
                })
                .map(|(_, local)| local.clone())
                .collect();
            let views = top_level_item_views(facts.body);
            let mut prepared_module =
                execution_index::callable_capabilities::prepare_callable_capabilities(
                    &views,
                    locals,
                    selected_value_inputs
                        .get(module)
                        .cloned()
                        .unwrap_or_default(),
                    selected_native_operation_inputs
                        .get(module)
                        .cloned()
                        .unwrap_or_default(),
                    predefined_globals,
                    bundle_globals.clone(),
                    namespace_privacy_members
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
            prepared_module.locate_value_inputs(facts.locator());
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
            prepared_module.locate_native_operation_inputs(facts.locator());
            let module_native_inputs = prepared_module.resolved_native_operation_inputs();
            if !module_native_inputs.is_empty() && facts.source.is_none() {
                bail!(
                    "native operation input for provider {module} has no retained parser source locations"
                );
            }
            if !module_native_inputs.is_empty() {
                resolved_native_operation_inputs.insert(module.clone(), module_native_inputs);
            }
            prepared_module.locate_native_writes(module, facts.locator());
            prepared.insert(module, prepared_module);
        }
        Ok(PreparedProviders {
            prepared,
            resolved_value_inputs,
            resolved_native_operation_inputs,
        })
    }

    fn infer_capabilities(
        &self,
        prepared: BTreeMap<&'a SourceModuleKey, PreparedCallableCapabilities>,
        descriptor_copy_asserted: &BTreeMap<
            SourceModuleKey,
            BTreeMap<String, Vec<spec::DescriptorCopyFactorySignature>>,
        >,
        flow_views: &BTreeMap<&SourceModuleKey, Vec<TopLevelItemView<'_>>>,
        native_integrity: &NativeIntegrity,
        structural_flows: &value_flow::ProgramValueFlows,
    ) -> BTreeMap<&'a SourceModuleKey, InferredCallableCapabilities> {
        let ProviderPreparation {
            bindings,
            modules,
            required,
            ..
        } = *self;
        prepared
            .into_iter()
            .map(|(module, prepared)| {
                let views = &flow_views[module];
                let required_stable_providers = descriptor_copy_asserted
                    .get(module)
                    .into_iter()
                    .flat_map(|exports| exports.keys())
                    .filter_map(|export| modules[module].exports.get(export.as_str()))
                    .cloned()
                    .collect();
                (
                    module,
                    execution_index::callable_capabilities::infer_callable_capabilities(
                        views,
                        prepared,
                        native_integrity,
                        &required_stable_providers,
                        modules[module].exports.keys().any(|export| {
                            required.contains(&prepared_export_id(bindings, module, export))
                        }),
                        || structural_flows.get(module).expect("prepared program flow"),
                    ),
                )
            })
            .collect()
    }
}

fn collect_fresh_factory_imports(
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    declared_imports: &BTreeMap<SourceModuleKey, BTreeMap<String, CallableSummary>>,
    source_stable: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
    closed_factory_imports: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
) -> BTreeMap<SourceModuleKey, BTreeSet<String>> {
    modules
        .keys()
        .map(|module| {
            let eligible = declared_imports[module]
                .iter()
                .filter(|(local, summary)| {
                    source_stable[module].contains(*local)
                        && closed_factory_imports
                            .get(module)
                            .is_some_and(|imports| imports.contains(*local))
                        && summary.callback_factory_evidence().iter().any(|evidence| {
                            matches!(evidence, CallbackFactoryEvidence::AuthorAssertion { .. })
                        })
                })
                .map(|(local, _)| local.clone())
                .collect();
            (module.clone(), eligible)
        })
        .collect()
}

fn collect_fresh_factory_members(
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    prepared: &BTreeMap<&SourceModuleKey, PreparedCallableCapabilities>,
    source_stable: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
    closed_factory_imports: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
    callback_factory_members: &BTreeMap<SourceModuleKey, BTreeMap<String, BTreeSet<String>>>,
) -> BTreeMap<SourceModuleKey, BTreeMap<String, BTreeSet<String>>> {
    modules
        .keys()
        .map(|module| {
            let mut eligible = BTreeMap::new();
            if let Some(family) = prepared[module].source_private_namespace_family()
                && let Some(declared) = callback_factory_members.get(module)
            {
                for (root, independent) in family {
                    let name = root.0.as_ref();
                    if source_stable[module].contains(name)
                        && closed_factory_imports
                            .get(module)
                            .is_some_and(|imports| imports.contains(name))
                        && let Some(members) = declared.get(name)
                    {
                        eligible.insert(
                            name.to_owned(),
                            members.intersection(independent).cloned().collect(),
                        );
                    }
                }
            }
            (module.clone(), eligible)
        })
        .collect()
}

fn collect_flow_views<'a>(
    modules: &'a BTreeMap<SourceModuleKey, ModulePurityFacts<'a>>,
    diagnostics: &Diagnostics,
) -> BTreeMap<&'a SourceModuleKey, Vec<TopLevelItemView<'a>>> {
    modules
        .iter()
        .map(|(module, facts)| {
            let views = top_level_item_views(facts.body);
            if diagnostics.metrics().enabled() {
                let owned = views
                    .iter()
                    .filter(|view| {
                        matches!(
                            view,
                            analysis_domain::item_views::TopLevelItemView::Owned { .. }
                        )
                    })
                    .count();
                diagnostics.metrics().statement_views(views.len(), owned);
            }
            (module, views)
        })
        .collect()
}

fn solve_program_value_flows(
    bindings: &ProgramBindings,
    flow_views: &BTreeMap<&SourceModuleKey, Vec<TopLevelItemView<'_>>>,
    fresh_factory_imports: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
    fresh_factory_members: &BTreeMap<SourceModuleKey, BTreeMap<String, BTreeSet<String>>>,
    resolved_native_operation_inputs: &BTreeMap<SourceModuleKey, Vec<ResolvedNativeOperationInput>>,
    inferred_set_constructions: &BTreeMap<
        SourceModuleKey,
        BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>,
    >,
    diagnostics: &Diagnostics,
) -> value_flow::ProgramValueFlows {
    let empty_sets = BTreeMap::new();
    value_flow::program_value_flows(
        bindings,
        flow_views
            .iter()
            .map(|(module, views)| value_flow::ProgramFlowModule {
                module,
                fresh_factory_imports: &fresh_factory_imports[*module],
                fresh_factory_members: &fresh_factory_members[*module],
                body: views,
                native_operation_inputs: resolved_native_operation_inputs
                    .get(*module)
                    .map_or(&[][..], Vec::as_slice),
                inferred_sets: inferred_set_constructions
                    .get(*module)
                    .unwrap_or(&empty_sets),
            }),
        diagnostics.metrics(),
        diagnostics.progress_observer(),
    )
}

fn refine_native_integrity(
    bindings: &ProgramBindings,
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    prepared: &mut BTreeMap<&SourceModuleKey, PreparedCallableCapabilities>,
    export_summaries: &BTreeMap<ExportId, CallableExportProof>,
    intrinsics_never_written: &BTreeSet<NativeIntrinsic>,
) -> Result<NativeIntegrity> {
    // This projection is the source-only input to native branch refinement.
    // Generic inferred callback factories are deliberately added after the
    // completed all-module native census and cannot flow back into it.
    let source_only_imports = solve_callable_import_projections(bindings, export_summaries.clone());
    for (module, facts) in prepared.iter_mut() {
        facts.refine_native_branches(
            source_only_imports
                .get(module.as_str())
                .expect("every prepared module has an import projection"),
        );
        facts.locate_native_writes(module, modules[*module].locator());
    }
    let mut native_integrity = execution_index::resolve_native_integrity(
        prepared.values().map(|facts| facts.native_writes()),
    );
    if !intrinsics_never_written.is_empty() {
        execution_index::apply_intrinsics_never_written(
            &mut native_integrity,
            intrinsics_never_written,
        )
        .map_err(|refutations| {
            anyhow::anyhow!(
                "program_premises.intrinsics_never_written is refuted by census writes on the exact objects of named families: {}",
                refutations.join("; ")
            )
        })?;
    }
    Ok(native_integrity)
}

fn add_importer_cell_reads(
    bindings: &ProgramBindings,
    structural_flows: &mut value_flow::ProgramValueFlows,
    imported_callables: &mut BTreeMap<SourceModuleKey, BTreeMap<String, CallableSummary>>,
) {
    for (module, calls) in structural_flows.take_importer_cell_reads(bindings) {
        for (callee, cells) in calls {
            imported_callables
                .entry(module.clone())
                .or_default()
                .entry(callee)
                .or_default()
                .may_read_importer_cells
                .extend(cells);
        }
    }
}

/// The export key for one module export. The callable-export table is
/// prepared before these summaries are built, so a missing entry means the
/// preparation was skipped - not that this module lacks the export.
fn prepared_export_id(bindings: &ProgramBindings, module: &str, export: &str) -> ExportId {
    bindings
        .export_id(module, export)
        .expect("prepared callable export")
}

/// Callback-factory proofs for the author-asserted exports that exist.
fn asserted_callback_factory_proofs(
    bindings: &ProgramBindings,
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
    diagnostics: &Diagnostics,
) -> BTreeMap<ExportId, CallableExportProof> {
    let mut export_summaries: BTreeMap<ExportId, CallableExportProof> = BTreeMap::new();
    for (module, exports) in asserted {
        for export in exports {
            if modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export))
            {
                export_summaries
                    .entry(prepared_export_id(bindings, module, export))
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
                diagnostics.record_chunk(
                    module,
                    ChunkDiagnostic::DanglingExportContract {
                        contract: ExportContract::CallbackFactoryExports,
                        export: export.clone(),
                    },
                );
            }
        }
    }
    export_summaries
}

/// Source-known external writes of the requested original exports and the
/// exports they re-export.
fn resolve_known_original_external_writes(
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    prepared: &BTreeMap<&SourceModuleKey, PreparedCallableCapabilities>,
    requested_original_provider_exports: &BTreeSet<(SourceModuleKey, String)>,
) -> BTreeMap<SourceModuleKey, BTreeMap<String, String>> {
    let mut requested_original_exports = (*requested_original_provider_exports).clone();
    let mut pending = requested_original_exports
        .iter()
        .cloned()
        .collect::<Vec<_>>();
    while let Some((module, export)) = pending.pop() {
        let Some(local) = modules
            .get(&module)
            .and_then(|facts| facts.exports.get(&export))
        else {
            continue;
        };
        let Some(target) = modules[&module].imports.get(local) else {
            continue;
        };
        let next = (target.module.clone(), target.export.clone());
        if requested_original_exports.insert(next.clone()) {
            pending.push(next);
        }
    }
    let mut known_original_external_writes = BTreeMap::new();
    for (module, export) in requested_original_exports {
        let Some(index) = prepared.get(&module) else {
            continue;
        };
        let Some(local) = modules[&module].exports.get(&export) else {
            continue;
        };
        if let Some(reason) = index.known_external_write_for_local(local) {
            known_original_external_writes
                .entry(module)
                .or_insert_with(BTreeMap::new)
                .insert(export, reason);
        }
    }
    known_original_external_writes
}

/// Rejects native-operation inputs a positively identified source mutation
/// contradicts.
fn reject_contradicted_native_operation_inputs(
    selected_native_operation_inputs: &BTreeMap<SourceModuleKey, Vec<SelectedNativeOperationInput>>,
    prepared: &BTreeMap<&SourceModuleKey, PreparedCallableCapabilities>,
) -> Result<()> {
    for input in selected_native_operation_inputs.values().flatten() {
        let name = match input.evidence.operation {
            NativeInputKind::Operation(spec::NativeOperationKind::Symbol) => Some("Symbol"),
            operation => operation.empty_constructor_name(),
        };
        let Some(name) = name else {
            continue;
        };
        if let Some((module, owner)) = prepared.iter().find_map(|(module, facts)| {
            facts
                .known_global_native_operation_mutation_owner(input.evidence.operation)
                .map(|owner| (module, owner))
        }) {
            bail!(
                "{} is contradicted by a positively identified global {name} replacement or operation mutation in provider {module}, source unit {}",
                input.evidence.spec_location,
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
    Ok(())
}

/// `Set` constructions inferred against the raw pre-flow census, or none
/// when the program premises fail on it.
fn preflow_set_constructions(
    prepared: &BTreeMap<&SourceModuleKey, PreparedCallableCapabilities>,
    intrinsics_never_written: &BTreeSet<NativeIntrinsic>,
) -> BTreeMap<SourceModuleKey, BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>>
{
    // Source inference must finish before any root can seed structural flow.
    // The completed raw census is conservative; later branch refinement may
    // not retroactively introduce a root into an already solved flow.
    let mut preflow_native_integrity = execution_index::resolve_native_integrity(
        prepared.values().map(|facts| facts.native_writes()),
    );
    // Validate the existing program premise against the conservative raw
    // census too. Failure only declines this early inference; the established
    // refined-census check below remains authoritative for the author input.
    let preflow_premises_hold = intrinsics_never_written.is_empty()
        || execution_index::apply_intrinsics_never_written(
            &mut preflow_native_integrity,
            intrinsics_never_written,
        )
        .is_ok();
    let inferred_set_constructions: BTreeMap<_, _> = prepared
        .iter()
        .filter_map(|(module, facts)| {
            if !preflow_premises_hold {
                return None;
            }
            let proofs = facts.inferred_set_constructions(&preflow_native_integrity);
            (!proofs.is_empty()).then_some(((*module).clone(), proofs))
        })
        .collect();
    inferred_set_constructions
}

/// Adds the structurally inferred immutable-wrapper callback factories.
fn add_inferred_wrapper_summaries(
    bindings: &ProgramBindings,
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    prepared: &BTreeMap<&SourceModuleKey, PreparedCallableCapabilities>,
    flow_views: &BTreeMap<&SourceModuleKey, Vec<TopLevelItemView<'_>>>,
    structural_flows: &value_flow::ProgramValueFlows,
    export_summaries: &mut BTreeMap<ExportId, CallableExportProof>,
) {
    // Argument/entry coverage is structural and independent of the native
    // integrity that it can refine. Author timing assertions cannot supply it.
    for (module, facts) in modules {
        let views = &flow_views[module];
        let arguments = prepared[module].callback_object_arguments(views, || {
            structural_flows.get(module).expect("prepared program flow")
        });
        for (export, local) in &facts.exports {
            let Some((start, objects)) = arguments.get(local) else {
                continue;
            };
            let evidence = CallbackFactoryEvidence::InferredImmutableWrapper {
                defining_module: module.clone(),
                export: export.clone(),
                local: local.clone(),
                source_location: facts
                    .locator()
                    .and_then(|locator| locator.pos(BytePos(*start))),
            };
            let mut summary = CallableSummary::callback_factory(evidence.clone());
            summary.callback_object_arguments = Some(CallbackObjectArguments {
                wrapper: evidence,
                object_arguments: objects.clone(),
            });
            export_summaries
                .entry(prepared_export_id(bindings, module, export))
                .or_default()
                .add(summary, ProofDependencies::default());
        }
    }
}

/// Rejects malformed or source-contradicted descriptor-copy factory
/// assertions on required exports.
fn validate_descriptor_copy_assertions(
    bindings: &ProgramBindings,
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    descriptor_copy_asserted: &BTreeMap<
        SourceModuleKey,
        BTreeMap<String, Vec<spec::DescriptorCopyFactorySignature>>,
    >,
    required: &BTreeSet<ExportId>,
    prepared: &BTreeMap<&SourceModuleKey, PreparedCallableCapabilities>,
) -> Result<()> {
    for (module, exports) in descriptor_copy_asserted {
        for (export, signatures) in exports {
            if !required.contains(&prepared_export_id(bindings, module, export))
                || !modules
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
    Ok(())
}

/// Rejects imported primitive-array spread inputs when the all-source
/// census writes the Array iterator protocol.
fn reject_contradicted_array_spread_inputs(
    selected_imported_primitive_array_spread_inputs: &BTreeMap<
        SourceModuleKey,
        Vec<analysis_vocabulary::inputs::AnalysisImportedPrimitiveArraySpreadInput>,
    >,
    prepared: &BTreeMap<&SourceModuleKey, PreparedCallableCapabilities>,
) -> Result<()> {
    // Imported spread premises use a premise-relative view of the all-source
    // census while preserving each candidate's Array effect and fallback.
    let array_spread_request = selected_imported_primitive_array_spread_inputs
        .iter()
        .find_map(|(consumer, inputs)| {
            (!inputs.is_empty()).then(|| {
                format!(
                    "chunk_analysis_options.{consumer}.imported_primitive_array_spread_inputs[0]"
                )
            })
        });
    if let Some(request) = array_spread_request {
        let protocol_integrity = execution_index::resolve_array_spread_protocol_integrity(
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
    Ok(())
}

/// Adds descriptor-copy factory summaries for asserted required exports
/// that are stable direct function providers.
fn add_descriptor_copy_summaries(
    bindings: &ProgramBindings,
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    descriptor_copy_asserted: &BTreeMap<
        SourceModuleKey,
        BTreeMap<String, Vec<spec::DescriptorCopyFactorySignature>>,
    >,
    required: &BTreeSet<ExportId>,
    inferred: &BTreeMap<&SourceModuleKey, InferredCallableCapabilities>,
    export_summaries: &mut BTreeMap<ExportId, CallableExportProof>,
) -> Result<()> {
    for (module, exports) in descriptor_copy_asserted {
        for (export, signatures) in exports {
            let key = prepared_export_id(bindings, module, export);
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
    Ok(())
}

/// Adds the inferred callback-factory, returned-record, foreign-call and
/// namespace-wrapper summaries of required exports.
fn add_inferred_capability_summaries(
    bindings: &ProgramBindings,
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    required: &BTreeSet<ExportId>,
    inferred: &BTreeMap<&SourceModuleKey, InferredCallableCapabilities>,
    export_summaries: &mut BTreeMap<ExportId, CallableExportProof>,
) {
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
            let key = prepared_export_id(bindings, module, export);
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
                        source_location: facts
                            .locator()
                            .and_then(|locator| locator.pos(BytePos(factory.declaration_start))),
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
            let key = prepared_export_id(bindings, module, export);
            // Summary eligibility does not establish readiness at an importer's
            // execution point. Passive providers prove that their emitted
            // owners cannot reenter an importer during initialization; class
            // and captured-cell TDZs retain the ordered/gate obligations.
            let Some(call) = capabilities
                .foreign_calls
                .get(local)
                .filter(|call| call.permits_foreign_summary())
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
                facts
                    .locator()
                    .and_then(|locator| locator.pos(BytePos(start)))
            };
            if let Some(record) = &call.record {
                export_summaries.entry(key).or_default().add(
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
                        replacement_package: facts.replacement_packages.get(export).cloned(),
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
                    retained_provider_captures: call.derived.retained_provider_captures.clone(),
                    provider_cell_reads: call.derived.provider_cell_reads.clone(),
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
            let key = prepared_export_id(bindings, module, export);
            if !required.contains(&key) {
                continue;
            }
            let Some(wrapper) = inferred[module].namespace_wrappers.get(local) else {
                continue;
            };
            let source_location = facts
                .locator()
                .and_then(|locator| locator.pos(BytePos(wrapper.function_start)));
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
}
