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
use analysis::analysis_inputs::{NativeOperationSelectorProvenance, SourceDeclaration};
use analysis::{GraphOptions, LocalEffectPolicy};
use anyhow::{Context, Result, ensure};
use artifact::{ArtifactIndexes, ChunkBundle};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use source_identity::ChunkId;
use spec::{
    AdmissionOverrides, ArgumentInputContract, BuiltinGlobalRoot, ChunkExportPurity,
    CommutingStatementsContract, DependencyGraphOptions, HostBehavior, IntrinsicsNeverWritten,
    LoadJsChunksArgs, NativeCallIdentityInputContract, NativeOperationInputContract,
    NativeOperationKind, NoAccessorInstallCallableContract, ProgramPremises, TransformSpec,
    ValueInputContract, VendorLevel, VendorMark,
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
    pub argument_inputs: BTreeMap<String, Vec<SourceDeclaration>>,
    pub no_accessor_install_callables: BTreeMap<String, Vec<SourceDeclaration>>,
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
}

/// Split the mixed YAML authoring type into the inputs read by each stage.
/// Every field is named here so adding an option requires an explicit owner.
pub fn project_chunk_options(
    options: &BTreeMap<String, DependencyGraphOptions>,
) -> Result<BTreeMap<String, ProjectedChunkOptions>> {
    options
        .iter()
        .map(|(chunk, authored)| {
            // Graph report evidence retains the authored rationale. Analysis
            // identities strip notes from the source contracts below.
            let commuting_statements = authored.commuting_statements.clone();
            let DependencyGraphOptions {
                value_inputs,
                native_operation_inputs,
                commuting_statements: _,
                native_call_identity_inputs,
                argument_inputs,
                no_accessor_install_callables,
                deferred_global_publications,
                non_reentrant_global_membership,
                dataflow_aware_s_chain,
                trusted_dataflow_summaries,
                admission_overrides,
                local_property_effects,
            } = without_notes(authored)?;
            Ok((
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
                        value_inputs,
                        native_call_identity_inputs,
                        argument_inputs,
                        no_accessor_install_callables,
                    },
                    graph: ChunkGraphInputs {
                        options: GraphOptions {
                            dataflow_aware_s_chain,
                        },
                        commuting_statements,
                    },
                    native_operation_inputs,
                },
            ))
        })
        .collect()
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
        let mut vendor = without_notes(&spec.vendor)?;
        for mark in vendor.values_mut() {
            if let VendorLevel::BundledPartialSwap(swap) = &mut mark.level {
                swap.bundle.path = relative(&swap.bundle.path);
            }
        }
        let projected_options = project_chunk_options(&spec.chunk_analysis_options)?;
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
            host_behavior: without_notes(&spec.host_behavior)?,
            program_premises: without_notes(&spec.program_premises)?,
            vendor,
            chunk_export_purity: without_notes(&spec.chunk_export_purity)?,
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

/// `value` with every `note` key removed. A `note` is author rationale that
/// no engine stage reads.
fn without_notes<T: Serialize + DeserializeOwned>(value: &T) -> Result<T> {
    fn strip(value: &mut serde_json::Value) {
        match value {
            serde_json::Value::Object(map) => {
                map.remove("note");
                map.values_mut().for_each(strip);
            }
            serde_json::Value::Array(items) => items.iter_mut().for_each(strip),
            _ => {}
        }
    }
    let mut json = serde_json::to_value(value)?;
    strip(&mut json);
    Ok(serde_json::from_value(json)?)
}
