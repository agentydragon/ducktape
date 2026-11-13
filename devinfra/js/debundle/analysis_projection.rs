//! The source-analysis inputs projected from the authored spec. The cross-module
//! pass and each chunk's fact analysis take those inputs only from an
//! [`AnalysisProjection`], built once the selector solve has run. Its
//! assignment-derived fields hold what the solve resolved premises to
//! (binding names, declarations); module destinations and the logical-module
//! layout do not reach analysis through it.
//!
//! [`AnalysisSpec`] is the same content owned and serializable: what
//! `debundle spec project-analysis` writes, `debundle analyze` reads, and an
//! analysis artifact's header records. Its canonical encoding is the key a
//! run checks before it uses an artifact.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use analysis::AnalysisAssumptions;
pub use analysis::analysis_inputs::{
    AnalysisImportedPrimitiveArrayOperand, AnalysisImportedPrimitiveArraySpreadInput,
};
use analysis::analysis_inputs::{NativeOperationSelectorProvenance, SourceDeclaration};
use analysis::{GraphOptions, LocalEffectPolicy};
use anyhow::{Context, Result, ensure};
use artifact::{ArtifactIndexes, ChunkBundle};
use serde::{Deserialize, Serialize};
use source_identity::ChunkId;
use spec::{
    AdmissionOverrides, ArgumentInputContract, BuiltinGlobalRoot, ChunkExportPurity,
    CommutingStatementsContract, DependencyGraphOptions, HostBehavior,
    InitializerCallEffectInputContract, IntrinsicsNeverWritten, LoadJsChunksArgs,
    NativeCallIdentityInputContract, NativeOperationInputContract, NativeOperationKind,
    NoAccessorInstallCallableContract, ProgramPremises, TransformSpec, ValueInputContract,
    VendorLevel, VendorMark,
};

/// Native premises after resolving layout-relative references at the selector boundary.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnalysisNativeOperationInput {
    pub declaration: analysis::analysis_inputs::SourceDeclaration,
    pub selector: NativeOperationSelectorProvenance,
    pub operation: NativeOperationKind,
    pub audited_source_sha256: String,
}

/// Exact source statements selected by frontend elaboration for analysis
/// premises. Each vector is parallel to its authored contract array in
/// [`ChunkAnalysisInputs`]. Analyze consumes these stable body indices and
/// retains source-hash and semantic-contract validation.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AnalysisSourceSites {
    /// Parser-input digest for each provider chunk with a resolved premise.
    /// It prevents body indices from being reused with another source snapshot.
    pub source_sha256: BTreeMap<String, String>,
    pub value_inputs: BTreeMap<String, Vec<SourceDeclaration>>,
    pub native_call_identity_inputs: BTreeMap<String, Vec<SourceDeclaration>>,
    pub initializer_call_effect_inputs: BTreeMap<String, Vec<SourceDeclaration>>,
    pub argument_inputs: BTreeMap<String, Vec<SourceDeclaration>>,
    pub no_accessor_install_callables: BTreeMap<String, Vec<SourceDeclaration>>,
    /// Exact consumer/provider declarations and captures selected by the
    /// canonical source selector. Digests for every named chunk are in
    /// `source_sha256`.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub imported_primitive_array_spread_inputs:
        BTreeMap<String, Vec<AnalysisImportedPrimitiveArraySpreadInput>>,
}

/// Author inputs consumed while facts are analyzed for one chunk.
/// Graph construction and authored report context have separate projections.
#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize, Deserialize)]
pub struct ChunkAnalysisInputs {
    pub trusted_dataflow_summaries: bool,
    pub deferred_global_publications: BTreeMap<BuiltinGlobalRoot, BTreeSet<String>>,
    pub non_reentrant_global_membership: BTreeMap<BuiltinGlobalRoot, BTreeSet<String>>,
    pub local_effect_policy: LocalEffectPolicy,
    pub admission_overrides: AdmissionOverrides,
    pub value_inputs: Vec<ValueInputContract>,
    pub native_call_identity_inputs: Vec<NativeCallIdentityInputContract>,
    pub initializer_call_effect_inputs: Vec<InitializerCallEffectInputContract>,
    pub argument_inputs: Vec<ArgumentInputContract>,
    pub no_accessor_install_callables: Vec<NoAccessorInstallCallableContract>,
}

impl ChunkAnalysisInputs {
    fn is_default(&self) -> bool {
        self == &Self::default()
    }
}

/// Inputs used only while building a chunk's dependency graph. The commuting
/// contracts retain their authored notes for the graph's report evidence.
#[derive(Debug, Clone, Default)]
pub struct ChunkGraphInputs {
    pub options: GraphOptions,
    pub commuting_statements: Vec<CommutingStatementsContract>,
}

/// The authored options after an exhaustive split at the stage boundary.
/// This is transient; `AnalysisSpec` stores only `analysis` inputs.
#[derive(Debug, Clone, Default)]
pub struct ProjectedChunkOptions {
    pub analysis: ChunkAnalysisInputs,
    pub graph: ChunkGraphInputs,
    pub native_operation_inputs: Vec<NativeOperationInputContract>,
    pub imported_primitive_array_spread_inputs:
        Vec<spec::ImportedPrimitiveArraySpreadInputContract>,
}

/// Split the mixed YAML authoring type into the inputs read by each stage.
/// Every field is named here so adding an option requires an explicit owner.
pub fn project_chunk_options(
    options: &BTreeMap<String, DependencyGraphOptions>,
) -> BTreeMap<String, ProjectedChunkOptions> {
    options
        .iter()
        .map(|(chunk, authored)| {
            let DependencyGraphOptions {
                value_inputs,
                native_operation_inputs,
                initializer_call_effect_inputs,
                imported_primitive_array_spread_inputs,
                commuting_statements,
                native_call_identity_inputs,
                argument_inputs,
                no_accessor_install_callables,
                deferred_global_publications,
                non_reentrant_global_membership,
                dataflow_aware_s_chain,
                trusted_dataflow_summaries,
                admission_overrides,
                local_property_effects,
            } = authored.clone();
            let graph = ChunkGraphInputs {
                options: GraphOptions {
                    dataflow_aware_s_chain,
                },
                commuting_statements,
            };
            (
                chunk.clone(),
                ProjectedChunkOptions {
                    analysis: ChunkAnalysisInputs {
                        trusted_dataflow_summaries,
                        deferred_global_publications,
                        non_reentrant_global_membership,
                        local_effect_policy: if local_property_effects {
                            LocalEffectPolicy::LocalPropertyWrites
                        } else {
                            LocalEffectPolicy::KnownEffectsOnly
                        },
                        admission_overrides,
                        value_inputs: value_inputs.into_iter().map(project_value_input).collect(),
                        native_call_identity_inputs: native_call_identity_inputs
                            .into_iter()
                            .map(project_native_call_identity_input)
                            .collect(),
                        initializer_call_effect_inputs: initializer_call_effect_inputs
                            .into_iter()
                            .map(project_initializer_call_effect_input)
                            .collect(),
                        argument_inputs: argument_inputs
                            .into_iter()
                            .map(project_argument_input)
                            .collect(),
                        no_accessor_install_callables: no_accessor_install_callables
                            .into_iter()
                            .map(project_no_accessor_install_callable)
                            .collect(),
                    },
                    graph,
                    native_operation_inputs: native_operation_inputs
                        .into_iter()
                        .map(project_native_operation_input)
                        .collect(),
                    imported_primitive_array_spread_inputs: imported_primitive_array_spread_inputs
                        .into_iter()
                        .map(project_imported_primitive_array_spread_input)
                        .collect(),
                },
            )
        })
        .collect()
}

fn project_value_input(input: ValueInputContract) -> ValueInputContract {
    let ValueInputContract {
        source_match,
        value,
        audited_source_sha256,
        expected_export,
        note: _,
    } = input;
    ValueInputContract {
        source_match: source_match.without_author_note(),
        value,
        audited_source_sha256,
        expected_export,
        note: None,
    }
}

fn project_native_operation_input(
    input: NativeOperationInputContract,
) -> NativeOperationInputContract {
    let NativeOperationInputContract {
        selector,
        operation,
        audited_source_sha256,
        note: _,
    } = input;
    NativeOperationInputContract {
        selector: project_source_unit_selector(selector),
        operation,
        audited_source_sha256,
        note: None,
    }
}

fn project_source_unit_selector(selector: spec::SourceUnitSelector) -> spec::SourceUnitSelector {
    match selector {
        spec::SourceUnitSelector::SourceMatch(source_match) => {
            spec::SourceUnitSelector::SourceMatch(source_match.without_author_note())
        }
        spec::SourceUnitSelector::ResolvedMember(member) => {
            spec::SourceUnitSelector::ResolvedMember(member)
        }
    }
}

fn project_initializer_call_effect_input(
    input: InitializerCallEffectInputContract,
) -> InitializerCallEffectInputContract {
    let InitializerCallEffectInputContract {
        resolved_member,
        audited_source_sha256,
        provider_chunk,
        provider_export,
        audited_provider_sha256,
        returns_opaque_function_from_const_primitive,
        effective_replacement,
        note: _,
    } = input;
    InitializerCallEffectInputContract {
        resolved_member,
        audited_source_sha256,
        provider_chunk,
        provider_export,
        audited_provider_sha256,
        returns_opaque_function_from_const_primitive,
        effective_replacement,
        note: None,
    }
}

fn project_imported_primitive_array_spread_input(
    input: spec::ImportedPrimitiveArraySpreadInputContract,
) -> spec::ImportedPrimitiveArraySpreadInputContract {
    let spec::ImportedPrimitiveArraySpreadInputContract {
        source_match,
        operands,
        audited_source_sha256,
        lifetime,
        note: _,
    } = input;
    spec::ImportedPrimitiveArraySpreadInputContract {
        source_match: source_match.without_author_note(),
        operands: operands
            .into_iter()
            .map(|operand| {
                let spec::ImportedPrimitiveArrayOperandContract {
                    selector_local,
                    provider_chunk,
                    provider_source_match,
                    audited_provider_source_sha256,
                } = operand;
                spec::ImportedPrimitiveArrayOperandContract {
                    selector_local,
                    provider_chunk,
                    provider_source_match: provider_source_match.without_author_note(),
                    audited_provider_source_sha256,
                }
            })
            .collect(),
        audited_source_sha256,
        lifetime,
        note: None,
    }
}

fn project_native_call_identity_input(
    input: NativeCallIdentityInputContract,
) -> NativeCallIdentityInputContract {
    let NativeCallIdentityInputContract {
        source_match,
        callee,
        audited_source_sha256,
        note: _,
    } = input;
    NativeCallIdentityInputContract {
        source_match: source_match.without_author_note(),
        callee,
        audited_source_sha256,
        note: None,
    }
}

fn project_argument_input(input: ArgumentInputContract) -> ArgumentInputContract {
    let ArgumentInputContract {
        source_match,
        parameter_index,
        exact_arity,
        audited_source_sha256,
        note: _,
    } = input;
    ArgumentInputContract {
        source_match: source_match.without_author_note(),
        parameter_index,
        exact_arity,
        audited_source_sha256,
        note: None,
    }
}

fn project_no_accessor_install_callable(
    input: NoAccessorInstallCallableContract,
) -> NoAccessorInstallCallableContract {
    let NoAccessorInstallCallableContract {
        source_match,
        note: _,
    } = input;
    NoAccessorInstallCallableContract {
        source_match: source_match.without_author_note(),
        note: None,
    }
}

fn project_host_behavior(host: &HostBehavior) -> HostBehavior {
    let HostBehavior {
        synchronous_module_loader,
        loader_result_for_evaluating_module,
        synchronous_load_runs_app_callbacks,
        synchronous_load_drains_microtasks,
        reentry_free_builtin_modules,
        reentry_free_bun_modules,
        reentry_free_embedded_loaders,
        reentry_free_operations,
        predefined_globals,
        note: _,
    } = host;
    HostBehavior {
        synchronous_module_loader: *synchronous_module_loader,
        loader_result_for_evaluating_module: *loader_result_for_evaluating_module,
        synchronous_load_runs_app_callbacks: *synchronous_load_runs_app_callbacks,
        synchronous_load_drains_microtasks: *synchronous_load_drains_microtasks,
        reentry_free_builtin_modules: reentry_free_builtin_modules.clone(),
        reentry_free_bun_modules: reentry_free_bun_modules.clone(),
        reentry_free_embedded_loaders: reentry_free_embedded_loaders.clone(),
        reentry_free_operations: reentry_free_operations.clone(),
        predefined_globals: *predefined_globals,
        note: None,
    }
}

fn project_program_premises(premises: &ProgramPremises) -> ProgramPremises {
    let ProgramPremises {
        process_prototype_chain_accessors,
        non_literal_loader_calls,
        intrinsics_never_written,
        note: _,
    } = premises;
    ProgramPremises {
        process_prototype_chain_accessors: *process_prototype_chain_accessors,
        non_literal_loader_calls: *non_literal_loader_calls,
        intrinsics_never_written: intrinsics_never_written
            .as_ref()
            .map(project_intrinsics_never_written),
        note: None,
    }
}

fn project_intrinsics_never_written(premises: &IntrinsicsNeverWritten) -> IntrinsicsNeverWritten {
    let IntrinsicsNeverWritten { families, note: _ } = premises;
    IntrinsicsNeverWritten {
        families: families.clone(),
        note: None,
    }
}

/// Static-request provider closure shared by frontend premise resolution and
/// staged analysis. Canonical requests include export-from dependencies, even
/// in modules with no import declarations. Including a request here grants no
/// availability proof; unsupported request modes retain their later refusals.
pub fn selected_static_import_closure(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    selected_chunk_ids: &[String],
) -> Result<BTreeSet<ChunkId>> {
    let mut closure = BTreeSet::new();
    let mut pending = Vec::new();
    for chunk_id in selected_chunk_ids {
        let interned = artifact
            .chunk_table
            .get(chunk_id)
            .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
        if closure.insert(interned) {
            pending.push(interned);
        }
    }

    while let Some(chunk_id) = pending.pop() {
        let Some(chunk_artifact) = artifact.find_chunk(chunk_id) else {
            continue;
        };
        let entry_file = &chunk_artifact.analysis.entry_file;
        for request in &chunk_artifact.analysis.runtime_facts.static_requests {
            let Some(target) = indexes.resolve_runtime_import_reference(
                &request.specifier,
                chunk_id,
                entry_file,
                &artifact.chunk_table,
            ) else {
                continue;
            };
            if closure.insert(target.target_chunk_id) {
                pending.push(target.target_chunk_id);
            }
        }
    }

    Ok(closure)
}

pub struct AnalysisProjection<'a> {
    pub chunk_analysis_inputs: Cow<'a, BTreeMap<String, ChunkAnalysisInputs>>,
    /// With source-marker export policies already resolved.
    pub chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
    pub host_behavior: &'a HostBehavior,
    /// Narrows the native census (SPEC.md § Intrinsics never written).
    pub intrinsics_never_written: Option<&'a IntrinsicsNeverWritten>,
    /// Per analysed chunk: member and chunk-rename annotations and
    /// `no_reentry_call` premises, keyed by the bindings the selector solve
    /// resolved them to.
    pub declared_assumptions: BTreeMap<String, AnalysisAssumptions>,
    pub source_sites: Cow<'a, AnalysisSourceSites>,
    pub native_operation_inputs: BTreeMap<String, Vec<AnalysisNativeOperationInput>>,
}

impl<'a> AnalysisProjection<'a> {
    pub fn new(
        chunk_options: &BTreeMap<String, ProjectedChunkOptions>,
        chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
        host_behavior: &'a HostBehavior,
        intrinsics_never_written: Option<&'a IntrinsicsNeverWritten>,
        declared_assumptions: BTreeMap<String, AnalysisAssumptions>,
        source_sites: AnalysisSourceSites,
        native_operation_inputs: BTreeMap<String, Vec<AnalysisNativeOperationInput>>,
    ) -> Result<Self> {
        Ok(Self {
            chunk_analysis_inputs: Cow::Owned(
                chunk_options
                    .iter()
                    .filter(|(_, options)| !options.analysis.is_default())
                    .map(|(chunk, options)| (chunk.clone(), options.analysis.clone()))
                    .collect(),
            ),
            chunk_export_purity,
            host_behavior,
            intrinsics_never_written,
            declared_assumptions,
            source_sites: Cow::Owned(source_sites),
            native_operation_inputs,
        })
    }

    /// `purity: pure_new` annotations, the only declared assumptions the
    /// cross-module pass reads.
    pub fn declared_pure_new(&self) -> BTreeMap<String, BTreeSet<String>> {
        self.declared_assumptions
            .iter()
            .map(|(chunk, assumptions)| (chunk.clone(), assumptions.declared_pure_new.clone()))
            .collect()
    }
}

/// Everything source analysis reads from a run's spec, with premises resolved
/// by the selector solve. Source paths are relative to the source root; author
/// notes and graph-construction inputs are kept in their owning contexts.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnalysisSpec {
    pub inputs: LoadJsChunksArgs,
    pub host_behavior: HostBehavior,
    pub program_premises: ProgramPremises,
    pub vendor: BTreeMap<String, VendorMark>,
    /// As authored; source-marker policies are resolved against the parsed
    /// program.
    pub chunk_export_purity: BTreeMap<String, ChunkExportPurity>,
    pub chunk_analysis_inputs: BTreeMap<String, ChunkAnalysisInputs>,
    /// `TransformSpec::materialize_chunk_ids`, in order.
    pub analysed_chunks: Vec<String>,
    /// `materialize_logical_modules.file`.
    pub entry_file: Option<String>,
    /// `materialize_logical_modules.prune_other_chunks`.
    pub prune_other_chunks: bool,
    pub declared_assumptions: BTreeMap<String, AnalysisAssumptions>,
    /// Canonical source sites selected by frontend elaboration. Analyze uses
    /// these instead of resolving author selectors in the later stage.
    pub source_sites: AnalysisSourceSites,
    pub native_operation_inputs: BTreeMap<String, Vec<AnalysisNativeOperationInput>>,
}

impl AnalysisSpec {
    /// The spec half of the projection; `declared_assumptions` and
    /// `native_operation_inputs` come from the selector solve.
    pub fn from_transform(spec: &TransformSpec, source_root: &Path) -> Result<Self> {
        let relative = |path: &Path| -> PathBuf {
            path.strip_prefix(source_root)
                .map_or_else(|_| path.to_path_buf(), Path::to_path_buf)
        };
        // Vendor markings are all semantic data. Preserve arbitrary chunk,
        // package, and symbol keys literally named "note".
        let mut vendor = spec.vendor.clone();
        for mark in vendor.values_mut() {
            if let VendorLevel::BundledPartialSwap(swap) = &mut mark.level {
                swap.bundle.path = relative(&swap.bundle.path);
            }
        }
        let projected_options = project_chunk_options(&spec.chunk_analysis_options);
        // Graph-only and empty authored entries have no source-analysis identity.
        let chunk_analysis_inputs = projected_options
            .into_iter()
            .filter(|(_, options)| !options.analysis.is_default())
            .map(|(chunk, options)| (chunk, options.analysis))
            .collect();
        Ok(Self {
            inputs: LoadJsChunksArgs {
                input_root: relative(&spec.inputs.input_root),
                js_list_path: relative(&spec.inputs.js_list_path),
                embedded_loaders_path: spec.inputs.embedded_loaders_path.as_deref().map(relative),
            },
            host_behavior: project_host_behavior(&spec.host_behavior),
            program_premises: project_program_premises(&spec.program_premises),
            vendor,
            // Purity maps are all semantic data, including export keys named "note".
            chunk_export_purity: spec.chunk_export_purity.clone(),
            chunk_analysis_inputs,
            analysed_chunks: spec.materialize_chunk_ids().into_iter().collect(),
            entry_file: spec.materialize_logical_modules.file.clone(),
            prune_other_chunks: spec.materialize_logical_modules.prune_other_chunks,
            declared_assumptions: BTreeMap::new(),
            source_sites: AnalysisSourceSites::default(),
            native_operation_inputs: BTreeMap::new(),
        })
    }

    /// Source paths joined back onto `source_root`.
    pub fn inputs_under(&self, source_root: &Path) -> LoadJsChunksArgs {
        LoadJsChunksArgs {
            input_root: source_root.join(&self.inputs.input_root),
            js_list_path: source_root.join(&self.inputs.js_list_path),
            embedded_loaders_path: self
                .inputs
                .embedded_loaders_path
                .as_ref()
                .map(|path| source_root.join(path)),
        }
    }

    /// Vendor marks with source paths joined back onto `source_root`.
    pub fn vendor_under(&self, source_root: &Path) -> BTreeMap<String, VendorMark> {
        let mut vendor = self.vendor.clone();
        for mark in vendor.values_mut() {
            if let VendorLevel::BundledPartialSwap(swap) = &mut mark.level {
                swap.bundle.path = source_root.join(&swap.bundle.path);
            }
        }
        vendor
    }

    pub fn analysed_chunks(&self) -> &[String] {
        &self.analysed_chunks
    }

    pub fn with_premises(
        mut self,
        declared_assumptions: BTreeMap<String, AnalysisAssumptions>,
        source_sites: AnalysisSourceSites,
        native_operation_inputs: BTreeMap<String, Vec<AnalysisNativeOperationInput>>,
    ) -> Result<Self> {
        // The canonical encoding carries no hygiene contexts: declared
        // assumptions are binding names, never `Id`s resolved by S2.
        ensure!(
            declared_assumptions.values().all(|assumptions| {
                assumptions.captured_value_inputs.is_empty()
                    && assumptions.native_operation_inputs.is_empty()
                    && assumptions.initializer_call_effect_inputs.is_empty()
                    && assumptions
                        .imported_primitive_array_spread_inputs
                        .is_empty()
                    && assumptions.argument_inputs.is_empty()
            }),
            "declared assumptions carry source-resolved inputs"
        );
        self.declared_assumptions = declared_assumptions;
        self.source_sites = source_sites;
        self.native_operation_inputs = native_operation_inputs;
        Ok(self)
    }

    pub fn projection<'a>(
        &'a self,
        chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
    ) -> AnalysisProjection<'a> {
        AnalysisProjection {
            chunk_analysis_inputs: Cow::Borrowed(&self.chunk_analysis_inputs),
            chunk_export_purity,
            host_behavior: &self.host_behavior,
            intrinsics_never_written: self.program_premises.intrinsics_never_written.as_ref(),
            declared_assumptions: self.declared_assumptions.clone(),
            source_sites: Cow::Borrowed(&self.source_sites),
            native_operation_inputs: self.native_operation_inputs.clone(),
        }
    }

    /// Canonical bytes: named MessagePack over ordered maps.
    pub fn encode(&self) -> Result<Vec<u8>> {
        Ok(rmp_serde::to_vec_named(self)?)
    }

    pub fn decode(bytes: &[u8]) -> Result<Self> {
        rmp_serde::from_slice(bytes).context("decode analysis projection")
    }

    /// The fields, and per-chunk entries, in which `self` and `other` differ.
    pub fn differences(&self, other: &Self) -> Result<Vec<String>> {
        let mut differing = Vec::new();
        let mut field = |name: &str, a: Vec<u8>, b: Vec<u8>| {
            if a != b {
                differing.push(name.to_owned());
            }
        };
        field(
            "inputs",
            rmp_serde::to_vec_named(&self.inputs)?,
            rmp_serde::to_vec_named(&other.inputs)?,
        );
        field(
            "host_behavior",
            rmp_serde::to_vec_named(&self.host_behavior)?,
            rmp_serde::to_vec_named(&other.host_behavior)?,
        );
        field(
            "program_premises",
            rmp_serde::to_vec_named(&self.program_premises)?,
            rmp_serde::to_vec_named(&other.program_premises)?,
        );
        field(
            "analysed_chunks",
            rmp_serde::to_vec_named(&self.analysed_chunks)?,
            rmp_serde::to_vec_named(&other.analysed_chunks)?,
        );
        field(
            "entry_file",
            rmp_serde::to_vec_named(&self.entry_file)?,
            rmp_serde::to_vec_named(&other.entry_file)?,
        );
        field(
            "prune_other_chunks",
            rmp_serde::to_vec_named(&self.prune_other_chunks)?,
            rmp_serde::to_vec_named(&other.prune_other_chunks)?,
        );
        field(
            "native_operation_inputs",
            rmp_serde::to_vec_named(&self.native_operation_inputs)?,
            rmp_serde::to_vec_named(&other.native_operation_inputs)?,
        );
        field(
            "source_sites",
            rmp_serde::to_vec_named(&self.source_sites)?,
            rmp_serde::to_vec_named(&other.source_sites)?,
        );
        differing.extend(keyed_differences("vendor", &self.vendor, &other.vendor)?);
        differing.extend(keyed_differences(
            "chunk_export_purity",
            &self.chunk_export_purity,
            &other.chunk_export_purity,
        )?);
        differing.extend(keyed_differences(
            "chunk_analysis_inputs",
            &self.chunk_analysis_inputs,
            &other.chunk_analysis_inputs,
        )?);
        differing.extend(keyed_differences(
            "declared assumptions",
            &self.declared_assumptions,
            &other.declared_assumptions,
        )?);
        Ok(differing)
    }
}

fn keyed_differences<T: Serialize>(
    field: &str,
    a: &BTreeMap<String, T>,
    b: &BTreeMap<String, T>,
) -> Result<Vec<String>> {
    let keys: BTreeSet<&String> = a.keys().chain(b.keys()).collect();
    let mut differing = Vec::new();
    for key in keys {
        let left = a.get(key).map(rmp_serde::to_vec_named).transpose()?;
        let right = b.get(key).map(rmp_serde::to_vec_named).transpose()?;
        if left != right {
            differing.push(format!("{field}.{key}"));
        }
    }
    Ok(differing)
}
