//! Serialized report models and the shared JSON writer for artifact output.

use std::collections::BTreeSet;
use std::fs;
use std::io::BufWriter;
use std::path::Path;

use analysis::DepKind;
use anyhow::{Context, Result};
use serde::Serialize;

use crate::FileRole;
use crate::metrics::{DecompositionMetrics, OutputMetrics};

/// Emit compact JSON for pipeline side outputs, creating parent directories.
///
/// These reports are usually consumed by tooling or `jq`; pretty printing is a
/// measurable cost on large bundles.
pub fn write_json(path: impl AsRef<Path>, data: &impl Serialize) -> Result<()> {
    let path = path.as_ref();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
    }
    let mut writer = BufWriter::new(
        fs::File::create(path).with_context(|| format!("creating {}", path.display()))?,
    );
    serde_json::to_writer(&mut writer, data)
        .with_context(|| format!("writing {}", path.display()))?;
    Ok(())
}

/// Emission-set chunk records, serialized as `{ "chunks": [...] }` to the
/// chunks report. Shared by `write_tree` and `emit_harness`.
#[derive(Serialize)]
pub struct ChunksReport<'a> {
    pub chunks: &'a [ArtifactChunkRecord],
}

/// `package.json` manifest written into the materialized app root to mark it
/// as an ESM tree. Shared by `write_tree` and `emit_harness`.
#[derive(Serialize)]
pub struct PackageManifest {
    #[serde(rename = "type")]
    pub module_type: &'static str,
}

/// Per-chunk report serialized to `reports/tree/<chunk>/chunk.json` at write time.
/// The analysis report is flattened so the serialized shape is the
/// analysis fields followed by the decomposition/metrics fields —
/// identical to the previous field-by-field copy.
#[derive(Debug, Clone, Serialize)]
pub struct ChunkManifest {
    #[serde(flatten)]
    pub analysis: ChunkAnalysisReport,
    pub validation: ChunkValidationSummary,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub logical_modules: Option<ChunkLogicalModulesSummary>,
    pub selected_module_lowerings: Vec<SelectedModuleLowering>,
    pub output_metrics: OutputMetrics,
}

impl ChunkManifest {
    pub fn from_analysis(
        analysis: &ChunkAnalysisReport,
        decomposition: Option<&ChunkDecompositionOutput>,
        output_metrics: OutputMetrics,
    ) -> Self {
        Self {
            analysis: analysis.clone(),
            validation: decomposition.map_or_else(
                || ChunkValidationSummary {
                    linker_order: Vec::new(),
                },
                |d| d.validation.clone(),
            ),
            logical_modules: decomposition.map(|d| d.logical_modules.clone()),
            selected_module_lowerings: decomposition
                .map(|d| d.selected_module_lowerings.clone())
                .unwrap_or_default(),
            output_metrics,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactManifest {
    pub counts: ArtifactCounts,
    pub chunks: Vec<ArtifactChunkRecord>,
    pub logical_modules: RootLogicalModulesSummary,
    pub selected_module_lowerings: Vec<SelectedModuleLowering>,
    pub output_metrics: OutputMetrics,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub decomposition_metrics: Option<DecompositionMetrics>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactCounts {
    pub kept_top_level_declarations: usize,
    pub top_level_side_effects: usize,
    pub export_aliases: usize,
    pub unresolved_exports: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct RootLogicalModulesSummary {
    pub module_count: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkLogicalModulesSummary {
    /// Canonical [`spec::ModulePath`] per materialized module (the
    /// chunk is implicit — this summary is per-chunk).
    pub module_paths: Vec<spec::ModulePath>,
    pub target_dir: String,
}

/// Chunk-qualified canonical module reference for tree-level
/// (cross-chunk) reports. Per-chunk reports denote a module by bare
/// [`spec::ModulePath`] (their chunk is implicit); tree-wide reports
/// qualify the same canonical path with its chunk id. There is no
/// third spelling — the in-process `"<chunk_id>::<path>"` id never
/// hits the wire.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub struct ModuleRef {
    pub chunk_id: String,
    pub path: spec::ModulePath,
}

#[derive(Debug, Clone, Serialize)]
pub struct SelectedModuleLowering {
    pub binding_names: Vec<String>,
    pub chunk_id: String,
    pub exported_names: Vec<String>,
    /// Public names explicitly assigned by the spec. Kept in-memory for
    /// cross-chunk import naturalization; omitted from serialized reports.
    #[serde(skip)]
    pub spec_named_export_names: Vec<String>,
    /// Original entry export names that actually received a readable alias,
    /// paired with that alias. Kept in-memory for import rewriting only.
    #[serde(skip)]
    pub cross_chunk_import_aliases: Vec<(String, String)>,
    pub file: String,
    pub source_unit_ids: Vec<String>,
    /// Planned output with `ModuleRole::Unassigned`; the physical entry is
    /// represented separately by `FileRole::Entry`.
    pub residual: bool,
    pub target_file: String,
    pub target_path: String,
}

impl SelectedModuleLowering {
    /// The module this lowering materialized, as a [`ModuleRef`].
    pub fn module_ref(&self) -> ModuleRef {
        ModuleRef {
            chunk_id: self.chunk_id.clone(),
            path: spec::ModulePath::parse(&self.target_path, "")
                .expect("lowering target_path is a canonical module path"),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactChunkRecord {
    pub chunk_id: String,
    pub source_path: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkAnalysisReport {
    pub chunk_id: String,
    pub source_path: String,
    pub entry_file: String,
    pub counts: ChunkCounts,
    pub files: Vec<ChunkFileRecord>,
    pub imports: Vec<ImportRecord>,
    pub export_aliases: Vec<ExportAliasRecord>,
    pub unresolved_exports: Vec<ExportAliasRecord>,
    pub kept_top_level_declarations: Vec<KeptTopLevelDeclarationRecord>,
    /// Syntax-derived facts captured from the original SWC parse. These are
    /// consumed by the conservative export-reentry proof and are intentionally
    /// omitted from the public report format.
    #[serde(skip)]
    pub runtime_facts: ChunkRuntimeFacts,
}

#[derive(Debug, Clone, Default)]
pub struct ChunkRuntimeFacts {
    /// Direct ESM exports whose complete local binding history is primitive.
    /// Canonical shallow-scan facts; never serialized or author asserted.
    pub stable_primitive_exports: BTreeSet<String>,
    pub module_kind: ChunkModuleKind,
    pub has_top_level_await: bool,
    pub has_cjs_interop: bool,
    pub static_requests: Vec<StaticModuleRequest>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum ChunkModuleKind {
    #[default]
    Unknown,
    Esm,
}

#[derive(Debug, Clone)]
pub struct StaticModuleRequest {
    pub specifier: String,
    pub mode: StaticModuleRequestMode,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StaticModuleRequestMode {
    OrdinaryEsm,
    Unsupported,
}

/// Decomposition result for a single chunk — set during `materialize_logical_modules`.
pub struct ChunkDecompositionOutput {
    pub logical_modules: ChunkLogicalModulesSummary,
    pub selected_module_lowerings: Vec<SelectedModuleLowering>,
    pub directory_dependency_facts: Vec<DirectoryDependencyFact>,
    pub validation: ChunkValidationSummary,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkValidationSummary {
    /// Linker evaluation order, by canonical [`spec::ModulePath`].
    pub linker_order: Vec<spec::ModulePath>,
}

/// One semantic dependency-edge fact projected onto emitted module files.
#[derive(Debug, Clone)]
pub struct DirectoryDependencyFact {
    pub source_file: String,
    pub target_file: String,
    pub edge_kind: DepKind,
    /// The target/provider symbol, rendered as `<target_file>#<export>`.
    pub symbol: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct ChunkCounts {
    pub dynamic_imports: usize,
    pub export_aliases: usize,
    pub import_declarations: usize,
    pub kept_top_level_declarations: usize,
    pub top_level_bindings: usize,
    pub top_level_declarations: usize,
    pub top_level_side_effects: usize,
    pub unresolved_exports: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkFileRecord {
    pub file: String,
    pub role: FileRole,
}

#[derive(Debug, Clone, Serialize)]
pub struct ImportRecord {
    pub line: Option<usize>,
    pub source: String,
    pub specifiers: Vec<ImportSpecifierRecord>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ImportSpecifierRecord {
    pub kind: ImportSpecifierKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub imported: Option<String>,
    pub local: String,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ImportSpecifierKind {
    Named,
    Default,
    Namespace,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExportAliasRecord {
    pub exported: String,
    pub line: Option<usize>,
    pub local: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct KeptTopLevelDeclarationRecord {
    pub line: Option<usize>,
    pub names: Vec<String>,
    pub kind: TopLevelDeclarationKind,
}

/// The three top-level declaration variants we anchor extraction on.
/// Mirrors the SWC `Decl` arms where `analyze_program_shallow` produces
/// an `DeclarationRecord`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TopLevelDeclarationKind {
    Function,
    Class,
    Variable,
}
