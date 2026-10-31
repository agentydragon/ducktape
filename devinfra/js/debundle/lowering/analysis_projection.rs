//! The spec as the analysis stages read it. The cross-module pass and each
//! chunk's fact analysis take their spec-derived inputs only from an
//! [`AnalysisProjection`], built once the selector solve has run. Its
//! assignment-derived fields hold what the solve resolved premises to
//! (binding names, declarations); module destinations and the logical-module
//! layout do not reach analysis through it.
//!
//! [`AnalysisSpec`] is the same content owned and serializable: what
//! `debundle spec project-analysis` writes, `debundle analyze` reads, and an
//! analysis artifact's header records. Its canonical encoding is the key a
//! run checks before it uses an artifact.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use analysis::AnalysisAssumptions;
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use spec::{
    ChunkExportPurity, DependencyGraphOptions, HostBehavior, IntrinsicsNeverWritten,
    LoadJsChunksArgs, ProgramPremises, SourceUnitSelector, TransformSpec, VendorLevel, VendorMark,
};

use super::materialize::ResolvedMemberSite;

pub(super) struct AnalysisProjection<'a> {
    /// Analysis reads every field but `commuting_statements` and
    /// `dataflow_aware_s_chain`, which only the chunk graph build reads.
    pub(super) chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
    /// With source-marker export policies already resolved.
    pub(super) chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
    pub(super) host_behavior: &'a HostBehavior,
    /// Narrows the native census (SPEC.md § Intrinsics never written).
    pub(super) intrinsics_never_written: Option<&'a IntrinsicsNeverWritten>,
    /// Per analysed chunk: member and chunk-rename annotations and
    /// `no_reentry_call` premises, keyed by the bindings the selector solve
    /// resolved them to.
    pub(super) declared_assumptions: BTreeMap<String, AnalysisAssumptions>,
    /// The logical-module members that `native_operation_inputs` name
    /// through `resolved_member`, keyed by `(module, name)`, with the
    /// declaration each resolved to. No other member reaches analysis.
    pub(super) premise_members: BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
}

impl<'a> AnalysisProjection<'a> {
    pub(super) fn new(
        chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
        chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
        host_behavior: &'a HostBehavior,
        intrinsics_never_written: Option<&'a IntrinsicsNeverWritten>,
        declared_assumptions: BTreeMap<String, AnalysisAssumptions>,
        resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
    ) -> Self {
        let premise_members = chunk_analysis_options
            .values()
            .flat_map(|options| &options.native_operation_inputs)
            .filter_map(|input| match &input.selector {
                SourceUnitSelector::ResolvedMember(member) => {
                    let key = (member.module.clone(), member.name.clone());
                    let sites = resolved_member_sites.get(&key)?.clone();
                    Some((key, sites))
                }
                SourceUnitSelector::SourceMatch(_) => None,
            })
            .collect();
        Self {
            chunk_analysis_options,
            chunk_export_purity,
            host_behavior,
            intrinsics_never_written,
            declared_assumptions,
            premise_members,
        }
    }

    /// `purity: pure_new` annotations, the only declared assumptions the
    /// cross-module pass reads.
    pub(super) fn declared_pure_new(&self) -> BTreeMap<String, BTreeSet<String>> {
        self.declared_assumptions
            .iter()
            .map(|(chunk, assumptions)| (chunk.clone(), assumptions.declared_pure_new.clone()))
            .collect()
    }
}

/// Everything analysis reads from a run's spec, with premises resolved by the
/// selector solve. Source paths are relative to the source root; author notes
/// and the options only the chunk graph build reads are left out, so editing
/// them leaves the encoding unchanged.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnalysisSpec {
    pub(super) inputs: LoadJsChunksArgs,
    pub(super) host_behavior: HostBehavior,
    pub(super) program_premises: ProgramPremises,
    pub(super) vendor: BTreeMap<String, VendorMark>,
    /// As authored; source-marker policies are resolved against the parsed
    /// program.
    pub(super) chunk_export_purity: BTreeMap<String, ChunkExportPurity>,
    pub(super) chunk_analysis_options: BTreeMap<String, DependencyGraphOptions>,
    /// `TransformSpec::materialize_chunk_ids`, in order.
    pub(super) analysed_chunks: Vec<String>,
    /// `materialize_logical_modules.file`.
    pub(super) entry_file: Option<String>,
    /// `materialize_logical_modules.prune_other_chunks`.
    pub(super) prune_other_chunks: bool,
    pub(super) declared_assumptions: BTreeMap<String, AnalysisAssumptions>,
    pub(super) premise_members: BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
}

impl AnalysisSpec {
    /// The spec half of the projection; `declared_assumptions` and
    /// `premise_members` come from the selector solve.
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
        let mut chunk_analysis_options = without_notes(&spec.chunk_analysis_options)?;
        for options in chunk_analysis_options.values_mut() {
            options.commuting_statements.clear();
            options.dataflow_aware_s_chain = false;
        }
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
            chunk_analysis_options,
            analysed_chunks: spec.materialize_chunk_ids().into_iter().collect(),
            entry_file: spec.materialize_logical_modules.file.clone(),
            prune_other_chunks: spec.materialize_logical_modules.prune_other_chunks,
            declared_assumptions: BTreeMap::new(),
            premise_members: BTreeMap::new(),
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

    pub(super) fn with_premises(
        mut self,
        declared_assumptions: BTreeMap<String, AnalysisAssumptions>,
        premise_members: BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
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
        self.premise_members = premise_members;
        Ok(self)
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
            "premise_members",
            rmp_serde::to_vec_named(&self.premise_members)?,
            rmp_serde::to_vec_named(&other.premise_members)?,
        );
        differing.extend(keyed_differences("vendor", &self.vendor, &other.vendor)?);
        differing.extend(keyed_differences(
            "chunk_export_purity",
            &self.chunk_export_purity,
            &other.chunk_export_purity,
        )?);
        differing.extend(keyed_differences(
            "chunk_analysis_options",
            &self.chunk_analysis_options,
            &other.chunk_analysis_options,
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
