//! Serialized report models and the shared JSON writer for artifact output.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{BufWriter, Write};
use std::path::Path;
use std::sync::Arc;

use analysis_model::DepKind;
use analysis_model::StableExportValue;
use anyhow::{Context, Result};
use js_ast::ParsedGlobalUses;
use serde::{Deserialize, Serialize};
use source_identity::{ChunkId, ModuleCellId, SourceUnitOrdinal, StatementId};
use swc_common::Span;
use swc_ecma_ast::Id;

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
    let writer = BufWriter::new(
        fs::File::create(path).with_context(|| format!("creating {}", path.display()))?,
    );
    write_json_to(writer, path, data)
}

fn write_json_to(mut writer: impl Write, path: &Path, data: &impl Serialize) -> Result<()> {
    serde_json::to_writer(&mut writer, data)
        .with_context(|| format!("writing {}", path.display()))?;
    writer
        .flush()
        .with_context(|| format!("flushing {}", path.display()))
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
    /// All exact source resolver-unbound identifier sites from the canonical
    /// parse. `None` means no parsed-source inventory exists; `Some` with an
    /// empty map is a complete empty inventory. The inventory grants no host
    /// value or effect authority on its own.
    pub global_uses: Option<Arc<ParsedGlobalUses>>,
    pub resources: Vec<crate::runtime_resources::RuntimeResourceUse>,
    /// Direct ESM exports whose complete local binding history is primitive
    /// once their copied imports are. Resolved by the program-level
    /// cross-module analysis.
    pub stable_export_values: BTreeMap<String, StableExportValue>,
    pub module_kind: ChunkModuleKind,
    pub has_top_level_await: bool,
    pub has_cjs_interop: bool,
    /// True when source uses the CommonJS export namespace (`module`,
    /// `exports`, or TypeScript `export =`). Kept distinct from request-only
    /// host loading so direct ESM export bindings can retain value-type facts.
    pub has_own_cjs_export_surface: bool,
    pub static_requests: Vec<StaticModuleRequest>,
    /// Source-only host interop sites from the canonical shallow SWC walk.
    pub interop_uses: Vec<InteropUse>,
    pub loader_bindings: Vec<LoaderBindingUses>,
    pub namespace_requests: Vec<NamespaceRequest>,
    /// Complete source import-binding inventory from the original canonical
    /// parse. `None` means no parse inventory was retained; `Some(empty)` is a
    /// complete source file with no import bindings.
    pub parsed_source_imports: Option<ParsedSourceImports>,
}

/// Source import binding captured from the canonical parsed AST. This is
/// runtime-only evidence and is deliberately not part of any report schema.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedSourceImports {
    pub source_file: String,
    /// Every source-level static import/re-export request, including
    /// side-effect-only imports with no binding rows.
    pub requests: Vec<ParsedSourceRequest>,
    pub bindings: Vec<ParsedSourceImportBinding>,
    pub reexports: Vec<ParsedSourceReExport>,
    pub exports: Vec<ParsedSourceExport>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceStaticRequestKind {
    Import,
    NamedReExport,
    ExportAll,
}

/// Canonical source request identity, captured before any output rewriting.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedSourceRequest {
    pub statement: StatementId,
    pub declaration_span: Span,
    pub request: String,
    pub mode: StaticModuleRequestMode,
    pub kind: SourceStaticRequestKind,
    /// Import/export specifier spans, empty for a side-effect-only import.
    pub specifier_spans: Vec<Span>,
    pub type_only: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedSourceImportBinding {
    pub statement: StatementId,
    pub specifier_index: usize,
    pub local: Id,
    pub local_span: Span,
    pub declaration_span: Span,
    pub request: String,
    pub imported_name: String,
    pub kind: ImportSpecifierKind,
    pub type_only: bool,
    /// Every non-import-specifier occurrence of this exact source Id in the
    /// canonical parsed AST. The import declaration itself is represented by
    /// `local_span` and its prepared request binding receipt.
    pub use_spans: Vec<Span>,
    /// Containing top-level source statement for each use, retained so an
    /// exact raw pass-through can preserve source-to-output use positions
    /// without reparsing rendered bytes or guessing from identifiers.
    pub use_sites: Vec<ParsedSourceImportUseSite>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ParsedSourceImportUseSite {
    pub statement: StatementId,
    pub span: Span,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceReExportKind {
    Named,
    Namespace,
    All,
    Default,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedSourceReExport {
    pub statement: StatementId,
    pub specifier: Option<usize>,
    pub specifier_span: Option<Span>,
    pub declaration_span: Span,
    pub request: String,
    pub imported_name: Option<String>,
    pub exported_name: Option<String>,
    pub kind: SourceReExportKind,
    pub type_only: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedSourceExport {
    pub statement: StatementId,
    pub specifier_span: Option<Span>,
    pub declaration_span: Span,
    pub local: Option<Id>,
    pub public_name: String,
    pub type_only: bool,
}

/// Candidate output-file address used by typed source correspondence.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct OutputFileAddress {
    pub chunk: ChunkId,
    pub file: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceCorrespondenceStatus {
    Complete,
    MissingSourceFacts,
    MissingCandidateRoute,
    UnsupportedRewrite,
    Ambiguous,
}

#[derive(Debug, Clone)]
pub struct CandidateSourceUnitRoute {
    pub source_unit: SourceUnitOrdinal,
    pub source_statement: StatementId,
    pub source_span: Span,
    pub source_declarator_span: Option<Span>,
    pub outputs: Vec<OutputFileAddress>,
    pub positions: Vec<CandidateSourcePosition>,
    pub status: SourceCorrespondenceStatus,
}

#[derive(Debug, Clone)]
pub struct CandidateSourcePosition {
    pub output: OutputFileAddress,
    /// Zero-based emitted module-item position, taken from the prepared AST.
    pub item_index: usize,
    /// Exact candidate module-item span used to re-find this row after item
    /// insertions/removals.
    pub item_span: Span,
    /// Exact candidate declarator span when the source statement was split.
    pub declarator_span: Option<Span>,
    /// Original post-comma unit index when the source statement was split.
    pub declarator_offset: Option<usize>,
}

#[derive(Debug, Clone)]
pub struct CandidateImportRoute {
    /// Output file that contains this emitted import binding.
    pub output: OutputFileAddress,
    /// Exact candidate module-item span used to survive item-index shifts.
    pub item_span: Span,
    /// Exact hygiene-aware local Id in that emitted file.
    pub candidate_local: Id,
    pub specifier_index: usize,
    pub specifier_span: Span,
    pub emitted_request: String,
    pub imported_name: String,
    pub kind: ImportSpecifierKind,
    pub target: CandidateImportTarget,
    /// The source binding's checked module-cell identity, when one exists.
    pub source_cell: Option<ModuleCellId>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CandidateImportTarget {
    OriginalSource {
        request: String,
    },
    /// Exact external/package request identity; its export surface remains a
    /// separate host/module capability premise.
    OriginalExternal {
        request: String,
    },
    OriginalSourceUnknownExports {
        request: String,
    },
    CandidateOutput {
        chunk: ChunkId,
        file: String,
    },
    /// Exact bare package request produced by an admitted vendor plan.
    VendorPackage {
        request: String,
        package: String,
    },
    /// Exact generated facade request and its planned application path.
    VendorFacade {
        request: String,
        app_path: String,
    },
    Unsupported,
}

#[derive(Debug, Clone)]
pub struct CandidateSourceImportRoutes {
    pub source: ParsedSourceImportBinding,
    pub routes: Vec<CandidateImportRoute>,
    pub uses: Vec<CandidateSourceUseRoute>,
    pub status: SourceCorrespondenceStatus,
}

#[derive(Debug, Clone)]
pub struct CandidateSourceUseRoute {
    pub source_span: Span,
    pub routes: Vec<CandidateSourceUseLocation>,
    pub status: SourceCorrespondenceStatus,
}

#[derive(Debug, Clone)]
pub struct CandidateSourceUseLocation {
    pub output: OutputFileAddress,
    pub item_index: usize,
    pub candidate_span: Span,
    pub candidate_local: Id,
}

/// One static request in the exact prepared emitter AST, before later
/// whole-artifact cosmetic rewrites. This inventory includes synthesized
/// imports as well as retained source imports, so a final AST audit can check
/// both missing planned requests and unplanned additions.
#[derive(Debug, Clone)]
pub struct CandidatePreparedRequestRoute {
    pub output: OutputFileAddress,
    /// Zero-based candidate module-item position in the prepared AST.
    pub item_index: usize,
    /// Exact span used to re-find this request after item insertions/removals.
    pub item_span: Span,
    pub request_kind: CandidateOutputRequestKind,
    /// Original source statement when the emitted request retained an exact
    /// source span. Synthesized requests have no source statement.
    pub source_statement: Option<StatementId>,
    pub emitted_request: String,
    pub mode: StaticModuleRequestMode,
    pub targets: Vec<CandidateImportTarget>,
    pub bindings: Vec<CandidatePreparedRequestBinding>,
    pub status: SourceCorrespondenceStatus,
}

#[derive(Debug, Clone)]
pub struct CandidatePreparedRequestBinding {
    /// Source binding carried by the canonical planner, including generated
    /// source identities where no source import can be matched.
    /// Exact source-local identity when the request specifier imports a local
    /// binding. Re-export specifiers intentionally have no local Id.
    pub source_binding: Option<Id>,
    /// Exact hygiene-aware local Id in the prepared candidate AST.
    /// Re-export specifiers carry no local binding in the output AST.
    pub candidate_local: Option<Id>,
    pub specifier_index: usize,
    pub specifier_span: Span,
    pub imported_name: Option<String>,
    pub kind: ImportSpecifierKind,
    pub exported_name: Option<String>,
    pub source_cell: Option<ModuleCellId>,
    pub target: CandidateImportTarget,
}

#[derive(Debug, Clone)]
pub struct CandidateSourceRequestRoute {
    pub output: OutputFileAddress,
    pub item_index: usize,
    pub item_span: Span,
    pub source_statement: Option<StatementId>,
    pub emitted_request: String,
    pub mode: StaticModuleRequestMode,
    pub targets: Vec<CandidateImportTarget>,
    pub bindings: Vec<CandidatePreparedRequestBinding>,
    pub status: SourceCorrespondenceStatus,
}

#[derive(Debug, Clone)]
pub struct CandidateImportNaturalizationReceipt {
    pub chunk: ChunkId,
    pub file: String,
    pub item_index: usize,
    pub item_span: Span,
    pub specifier_index: usize,
    pub specifier_span: Span,
    pub from_local: Id,
    pub to_local: Id,
    pub from_imported_name: String,
    pub to_imported_name: String,
    /// Candidate output identifier spans changed by the same sealed rename.
    pub rewritten_use_spans: Vec<Span>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CandidateOutputRequestKind {
    Import,
    NamedReExport,
    ExportAll,
    LocalExport,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CandidateRewriteStage {
    CrossChunkNaturalization,
    EmissionVendorRewrite,
    DeadImportPruning,
    DeadExportPruning,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CandidateRewriteDisposition {
    Preserved,
    Rewritten,
    /// The source request or binding was intentionally redirected by a
    /// checked vendor plan. The payload records the authored replacement
    /// premise, exact emitted target, and every rewritten source use.
    Replaced(CandidateVendorReplacement),
    Elided,
    Unsupported,
}

/// Typed source-to-vendor replacement evidence. This is an in-memory
/// correspondence relation, not a claim that an arbitrary package is
/// semantically equivalent to its source. The checked vendor plan and its
/// authored mark remain explicit premises for the final candidate gate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateVendorReplacement {
    pub source_request: CandidateVendorSourceRequestIdentity,
    pub bindings: Vec<CandidateVendorBindingReplacement>,
}

/// Exact original static request identity. The candidate's `before` request
/// is carried separately by [`CandidateRewriteReceipt`], since lowered
/// runtime imports can have no one-to-one source request in the output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateVendorSourceRequestIdentity {
    pub source_chunk: ChunkId,
    pub source_file: String,
    pub statement: StatementId,
    pub declaration_span: Span,
    pub request: String,
    pub mode: StaticModuleRequestMode,
    pub kind: SourceStaticRequestKind,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateVendorBindingReplacement {
    pub source_specifier_index: usize,
    pub source_specifier_span: Span,
    pub source_binding: Option<Id>,
    pub source_cell: Option<ModuleCellId>,
    pub source_export: String,
    pub source_exported_name: Option<String>,
    pub source_kind: ImportSpecifierKind,
    /// Complete source-level use census for this imported local. Every row
    /// must have a corresponding `uses` transition before replacement is
    /// considered complete.
    pub source_uses: Vec<CandidateVendorSourceUse>,
    pub action: CandidateVendorPlanAction,
    pub destination: CandidateVendorDestination,
    /// Exact request ASTs produced by this plan action. Shared namespace
    /// imports may appear in several source-binding rows, recording a
    /// deliberate many-to-one relation.
    pub emitted_requests: Vec<CandidateVendorEmittedRequest>,
    /// Complete source-use mapping for an imported local. Re-exports have no
    /// local binding and therefore an empty use list.
    pub uses: Vec<CandidateVendorUseRewrite>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateVendorSourceUse {
    pub statement: StatementId,
    pub span: Span,
}

/// Action selected by the checked vendor plan, including the original
/// provider coordinate and authored swap shape.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateVendorEmittedRequest {
    pub output: OutputFileAddress,
    pub identity: CandidateStaticRequestIdentity,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CandidateVendorPlanAction {
    BoundaryRename {
        provider_chunk: ChunkId,
        source_export: String,
        public_export: String,
    },
    PartialPackageMember {
        provider_chunk: ChunkId,
        source_export: String,
        package: String,
        version: String,
        subpath: String,
        namespace: String,
        upstream_export: String,
    },
    PartialPackageNamespace {
        provider_chunk: ChunkId,
        source_export: String,
        package: String,
        version: String,
        subpath: String,
    },
    PartialPackageDefault {
        provider_chunk: ChunkId,
        source_export: String,
        package: String,
        version: String,
        subpath: String,
    },
    PartialPackageNamed {
        provider_chunk: ChunkId,
        source_export: String,
        package: String,
        version: String,
        subpath: String,
        upstream_export: String,
    },
    BundledFacadeMember {
        provider_chunk: ChunkId,
        source_export: String,
        symbol_kind: spec::PartialSwapKind,
        package: String,
        version: String,
        subpath: String,
        bundle_path: String,
        bundle_export: String,
        facade_app_path: String,
        namespace: String,
        property_path: Vec<String>,
    },
    BundledFacadeDefault {
        provider_chunk: ChunkId,
        source_export: String,
        symbol_kind: spec::PartialSwapKind,
        package: String,
        version: String,
        subpath: String,
        bundle_path: String,
        bundle_export: String,
        facade_app_path: String,
    },
}

/// Exact destination selected by the checked vendor plan. `emitted_request`
/// stores the actual specifier spelling at the output site; the plan target
/// is retained separately so the final gate can validate its resolution.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CandidateVendorDestination {
    BoundaryOutput {
        output: OutputFileAddress,
        export: String,
    },
    Package {
        request: String,
        package: String,
    },
    Facade {
        app_path: String,
        emitted_request: String,
    },
}

impl CandidateVendorDestination {
    /// Candidate import target represented by an external vendor destination.
    /// Boundary output targets additionally retain the named export in the
    /// destination and are checked as ordinary candidate-output edges.
    pub fn import_target(&self) -> Option<CandidateImportTarget> {
        match self {
            Self::BoundaryOutput { .. } => None,
            Self::Package { request, package } => Some(CandidateImportTarget::VendorPackage {
                request: request.clone(),
                package: package.clone(),
            }),
            Self::Facade {
                app_path,
                emitted_request,
            } => Some(CandidateImportTarget::VendorFacade {
                request: emitted_request.clone(),
                app_path: app_path.clone(),
            }),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateVendorUseRewrite {
    pub source_statement: StatementId,
    pub source_span: Span,
    pub before_output: OutputFileAddress,
    pub candidate_before_span: Span,
    pub candidate_before_local: Id,
    pub after_output: OutputFileAddress,
    pub candidate_after_span: Span,
    pub result: CandidateVendorUseResult,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CandidateVendorUseResult {
    LocalIdentifier { local: Id },
    MemberPath { namespace: Id, path: Vec<String> },
}

#[derive(Debug, Clone)]
pub enum CandidateRewriteElision {
    DeadNamedImport {
        local: Id,
        request_retained_as_bare: bool,
        duplicate_request_dropped_after_earlier_load: bool,
    },
    DeadLocalExport {
        local: Id,
        exported_name: String,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CandidateRewriteIssue {
    MissingExactInputIdentity,
    MissingExactOutputIdentity,
    UnmappedGeneratedRequest,
    AmbiguousTransition,
    UnprovedTarget,
    UnprovedUseRewrite,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateRewriteBindingIdentity {
    pub specifier_index: usize,
    pub specifier_span: Span,
    pub local: Option<Id>,
    pub imported_name: Option<String>,
    pub exported_name: Option<String>,
    pub kind: Option<ImportSpecifierKind>,
    pub source_binding: Option<Id>,
    pub source_cell: Option<ModuleCellId>,
    pub target: Option<CandidateImportTarget>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateStaticRequestIdentity {
    pub item_index: usize,
    pub item_span: Span,
    pub kind: CandidateOutputRequestKind,
    /// Canonical source request owner when this exact prepared request came
    /// from one source statement. Generated requests have no source owner.
    pub source_statement: Option<StatementId>,
    pub request: Option<String>,
    pub mode: Option<StaticModuleRequestMode>,
    pub targets: Vec<CandidateImportTarget>,
    pub bindings: Vec<CandidateRewriteBindingIdentity>,
}

/// Exact one-pass transition for a static request, including bare requests.
/// Receipts are kept in application order on the source correspondence row.
#[derive(Debug, Clone)]
pub struct CandidateRewriteReceipt {
    pub source_chunk: ChunkId,
    pub output: OutputFileAddress,
    pub stage: CandidateRewriteStage,
    pub before: Option<CandidateStaticRequestIdentity>,
    pub after: Option<CandidateStaticRequestIdentity>,
    pub disposition: CandidateRewriteDisposition,
    pub elisions: Vec<CandidateRewriteElision>,
    pub issue: Option<CandidateRewriteIssue>,
}

#[derive(Debug, Clone)]
pub struct CandidateSourceStaticRequestRoutes {
    pub source: ParsedSourceRequest,
    pub routes: Vec<CandidateSourceRequestRoute>,
    pub status: SourceCorrespondenceStatus,
}

#[derive(Debug, Clone)]
pub struct CandidateSourceReExportRoutes {
    pub source: ParsedSourceReExport,
    pub routes: Vec<CandidateSourceRequestRoute>,
    pub status: SourceCorrespondenceStatus,
}

#[derive(Debug, Clone)]
pub struct CandidateSourceExportRoutes {
    pub source: ParsedSourceExport,
    pub routes: Vec<CandidateSourceBindingRoute>,
    pub status: SourceCorrespondenceStatus,
}

#[derive(Debug, Clone)]
pub struct CandidateSourceBindingRoute {
    pub output: OutputFileAddress,
    /// Exact emitted local identity when preserved by a typed lowering map.
    pub candidate_local: Option<Id>,
    /// Exact candidate declarator/declaration span, when present in prepared AST.
    pub declaration_span: Option<Span>,
    /// Exact candidate binding identifier span, when independently located.
    pub binding_span: Option<Span>,
    /// Exact candidate export specifier span, when this binding is exported.
    pub exported_specifier_span: Option<Span>,
    /// Public export name when a typed plan supplies it.
    pub exported_name: Option<String>,
}

#[derive(Debug, Clone)]
pub struct CandidateModuleCellDeclaration {
    pub source_cell: ModuleCellId,
    pub source_binding: Option<Id>,
    pub source_owner: Option<SourceUnitOrdinal>,
    pub declaration_span: Option<Span>,
    pub routes: Vec<CandidateSourceBindingRoute>,
    pub status: SourceCorrespondenceStatus,
}

/// Typed source-to-candidate routing evidence produced while candidate ASTs
/// and prepared lowering maps are both available. Missing routes are retained
/// as rows with an explicit status; they are never inferred from report names.
#[derive(Debug, Clone)]
pub struct CandidateSourceCorrespondence {
    pub source_chunk: ChunkId,
    pub source_file: String,
    pub source_units: BTreeMap<SourceUnitOrdinal, CandidateSourceUnitRoute>,
    /// Source statement/declarator placement is tracked separately from
    /// import binding and static request identity. A raw pass-through can
    /// prove request identity while lacking the split-aware ordinal layout.
    pub source_unit_status: SourceCorrespondenceStatus,
    pub source_requests: Vec<CandidateSourceStaticRequestRoutes>,
    pub imports: Vec<CandidateSourceImportRoutes>,
    pub reexports: Vec<CandidateSourceReExportRoutes>,
    pub exports: Vec<CandidateSourceExportRoutes>,
    pub prepared_requests: Vec<CandidatePreparedRequestRoute>,
    pub module_cell_declarations: Vec<CandidateModuleCellDeclaration>,
    pub source_module_cells: Option<BTreeMap<Id, ModuleCellId>>,
    pub source_initialized_module_cells: Option<BTreeSet<ModuleCellId>>,
    /// Ordered receipts for post-materialization AST transitions.
    pub rewrite_receipts: Vec<CandidateRewriteReceipt>,
    pub status: SourceCorrespondenceStatus,
}

pub fn prepared_request_identity(
    route: &CandidatePreparedRequestRoute,
) -> CandidateStaticRequestIdentity {
    CandidateStaticRequestIdentity {
        item_index: route.item_index,
        item_span: route.item_span,
        kind: route.request_kind,
        source_statement: route.source_statement,
        request: Some(route.emitted_request.clone()),
        mode: Some(route.mode),
        targets: route.targets.clone(),
        bindings: route
            .bindings
            .iter()
            .map(|binding| CandidateRewriteBindingIdentity {
                specifier_index: binding.specifier_index,
                specifier_span: binding.specifier_span,
                local: binding.candidate_local.clone(),
                imported_name: binding.imported_name.clone(),
                exported_name: binding.exported_name.clone(),
                kind: Some(binding.kind),
                source_binding: binding.source_binding.clone(),
                source_cell: binding.source_cell,
                target: Some(binding.target.clone()),
            })
            .collect(),
    }
}

impl CandidateSourceCorrespondence {
    /// Apply the emission vendor/canonicalization transition against the
    /// current prepared-request identities. Receipts are checked before
    /// mutating route coordinates so a missing or ambiguous input cannot be
    /// repaired by matching a later spelling.
    pub fn apply_emission_rewrite_receipts(&mut self, receipts: &mut [CandidateRewriteReceipt]) {
        let stage_input = self.prepared_requests.clone();
        let source_chunk = self.source_chunk;
        for receipt in receipts.iter_mut().filter(|receipt| {
            receipt.source_chunk == source_chunk
                && !matches!(
                    receipt.disposition,
                    CandidateRewriteDisposition::Replaced(_)
                )
        }) {
            let Some(before) = receipt.before.as_ref() else {
                receipt
                    .issue
                    .get_or_insert(CandidateRewriteIssue::MissingExactInputIdentity);
                receipt.disposition = CandidateRewriteDisposition::Unsupported;
                continue;
            };
            let matching = self
                .prepared_requests
                .iter()
                .enumerate()
                .filter(|(_, route)| {
                    route.output == receipt.output && prepared_request_identity(route) == *before
                })
                .map(|(index, _)| index)
                .collect::<Vec<_>>();
            let [route_index] = matching.as_slice() else {
                receipt.issue.get_or_insert(if matching.is_empty() {
                    CandidateRewriteIssue::MissingExactInputIdentity
                } else {
                    CandidateRewriteIssue::AmbiguousTransition
                });
                receipt.disposition = CandidateRewriteDisposition::Unsupported;
                continue;
            };

            let old_route = self.prepared_requests[*route_index].clone();
            let mut replacement = old_route.clone();
            let replacement_ok = receipt
                .after
                .as_ref()
                .is_some_and(|after| apply_static_request_identity(&mut replacement, after));
            if receipt.after.is_some() && !replacement_ok {
                receipt
                    .issue
                    .get_or_insert(CandidateRewriteIssue::MissingExactOutputIdentity);
                receipt.disposition = CandidateRewriteDisposition::Unsupported;
                self.prepared_requests[*route_index].status =
                    SourceCorrespondenceStatus::UnsupportedRewrite;
                mark_source_request_routes_unsupported(self, &old_route);
                continue;
            }

            if let Some(after) = receipt.after.as_ref()
                && replacement_ok
            {
                if receipt.disposition == CandidateRewriteDisposition::Unsupported
                    || receipt.issue.is_some()
                {
                    replacement.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                }
                self.prepared_requests[*route_index] = replacement.clone();
                for request in &mut self.source_requests {
                    for route in &mut request.routes {
                        if route.output == old_route.output
                            && route.item_span == old_route.item_span
                            && route.source_statement == old_route.source_statement
                        {
                            apply_source_request_identity(route, after, &replacement);
                        }
                    }
                }
                for reexport in &mut self.reexports {
                    for route in &mut reexport.routes {
                        if route.output == old_route.output
                            && route.item_span == old_route.item_span
                            && route.source_statement == old_route.source_statement
                        {
                            apply_source_request_identity(route, after, &replacement);
                        }
                    }
                }
                for import in &mut self.imports {
                    for route in &mut import.routes {
                        if route.output != old_route.output
                            || route.item_span != old_route.item_span
                        {
                            continue;
                        }
                        if let Some(binding) = after.bindings.iter().find(|binding| {
                            binding.source_binding.as_ref() == Some(&import.source.local)
                                && binding.specifier_span == route.specifier_span
                        }) {
                            if let (Some(local), Some(kind), Some(target), Some(imported_name)) = (
                                binding.local.as_ref(),
                                binding.kind,
                                binding.target.as_ref(),
                                binding.imported_name.as_ref(),
                            ) {
                                route.candidate_local = local.clone();
                                route.specifier_index = binding.specifier_index;
                                route.emitted_request = replacement.emitted_request.clone();
                                route.imported_name = imported_name.clone();
                                route.kind = kind;
                                route.target = target.clone();
                                if receipt.disposition == CandidateRewriteDisposition::Unsupported
                                    || receipt.issue.is_some()
                                {
                                    import.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                                }
                            } else {
                                import.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                            }
                        } else {
                            import.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                        }
                    }
                }
            } else {
                self.prepared_requests[*route_index].status =
                    SourceCorrespondenceStatus::UnsupportedRewrite;
                mark_source_request_routes_unsupported(self, &old_route);
            }
        }
        crate::vendor_replacements::apply_replaced_requests(self, receipts, &stage_input);
        self.record_rewrite_receipts(receipts.to_vec());
        crate::vendor_replacements::refresh_replacement_status(self);
    }

    pub fn apply_import_naturalization_receipts(
        &mut self,
        receipts: &[CandidateImportNaturalizationReceipt],
    ) {
        let mut transitions = Vec::new();
        for receipt in receipts
            .iter()
            .filter(|receipt| receipt.chunk == self.source_chunk)
        {
            let output = OutputFileAddress {
                chunk: receipt.chunk,
                file: receipt.file.clone(),
            };
            let request_index = self.prepared_requests.iter().position(|request| {
                request.output == output
                    && request.item_span == receipt.item_span
                    && request.request_kind == CandidateOutputRequestKind::Import
            });
            let Some(request_index) = request_index else {
                transitions.push(CandidateRewriteReceipt {
                    source_chunk: self.source_chunk,
                    output,
                    stage: CandidateRewriteStage::CrossChunkNaturalization,
                    before: None,
                    after: None,
                    disposition: CandidateRewriteDisposition::Unsupported,
                    elisions: Vec::new(),
                    issue: Some(CandidateRewriteIssue::MissingExactInputIdentity),
                });
                continue;
            };
            let before = prepared_request_identity(&self.prepared_requests[request_index]);
            let matches_binding = |binding: &CandidatePreparedRequestBinding| {
                binding.specifier_index == receipt.specifier_index
                    && binding.specifier_span == receipt.specifier_span
                    && binding.candidate_local.as_ref() == Some(&receipt.from_local)
            };
            let mut found = false;
            for binding in &mut self.prepared_requests[request_index].bindings {
                if matches_binding(binding) {
                    binding.candidate_local = Some(receipt.to_local.clone());
                    binding.imported_name = Some(receipt.to_imported_name.clone());
                    binding.exported_name = Some(receipt.to_imported_name.clone());
                    found = true;
                }
            }
            for request in &mut self.source_requests {
                for route in &mut request.routes {
                    if route.output == output && route.item_span == receipt.item_span {
                        for binding in &mut route.bindings {
                            if matches_binding(binding) {
                                binding.candidate_local = Some(receipt.to_local.clone());
                                binding.imported_name = Some(receipt.to_imported_name.clone());
                                binding.exported_name = Some(receipt.to_imported_name.clone());
                            }
                        }
                    }
                }
            }
            for import in &mut self.imports {
                for route in &mut import.routes {
                    if route.output == output
                        && route.item_span == receipt.item_span
                        && route.specifier_index == receipt.specifier_index
                        && route.specifier_span == receipt.specifier_span
                        && route.candidate_local == receipt.from_local
                    {
                        route.candidate_local = receipt.to_local.clone();
                        route.imported_name = receipt.to_imported_name.clone();
                        found = true;
                    }
                }
                for use_route in &mut import.uses {
                    for location in &mut use_route.routes {
                        if location.output == output
                            && receipt
                                .rewritten_use_spans
                                .contains(&location.candidate_span)
                            && location.candidate_local == receipt.from_local
                        {
                            location.candidate_local = receipt.to_local.clone();
                        }
                    }
                }
            }
            for reexport in &mut self.reexports {
                for route in &mut reexport.routes {
                    if route.output == output && route.item_span == receipt.item_span {
                        for binding in &mut route.bindings {
                            if matches_binding(binding) {
                                binding.candidate_local = Some(receipt.to_local.clone());
                                binding.imported_name = Some(receipt.to_imported_name.clone());
                                binding.exported_name = Some(receipt.to_imported_name.clone());
                            }
                        }
                    }
                }
            }
            let after = prepared_request_identity(&self.prepared_requests[request_index]);
            let disposition = if found {
                CandidateRewriteDisposition::Rewritten
            } else {
                CandidateRewriteDisposition::Unsupported
            };
            transitions.push(CandidateRewriteReceipt {
                source_chunk: self.source_chunk,
                output,
                stage: CandidateRewriteStage::CrossChunkNaturalization,
                before: Some(before),
                after: Some(after),
                disposition,
                elisions: Vec::new(),
                issue: (!found).then_some(CandidateRewriteIssue::UnprovedUseRewrite),
            });
        }
        self.record_rewrite_receipts(transitions);
    }

    pub fn record_rewrite_receipts(&mut self, receipts: Vec<CandidateRewriteReceipt>) {
        for receipt in &receipts {
            if receipt.source_chunk != self.source_chunk {
                continue;
            }
            if receipt.disposition == CandidateRewriteDisposition::Unsupported
                || receipt.issue.is_some()
            {
                self.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                if let Some(before) = &receipt.before {
                    for request in &mut self.prepared_requests {
                        if request.output == receipt.output && request.item_span == before.item_span
                        {
                            request.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                        }
                    }
                }
            }
        }
        self.rewrite_receipts.extend(receipts);
    }

    pub fn unavailable(
        source_chunk: ChunkId,
        source_file: String,
        parsed: Option<&ParsedSourceImports>,
    ) -> Self {
        let source_requests = parsed
            .map(|parsed| {
                parsed
                    .requests
                    .iter()
                    .cloned()
                    .map(|source| CandidateSourceStaticRequestRoutes {
                        source,
                        routes: Vec::new(),
                        status: SourceCorrespondenceStatus::MissingCandidateRoute,
                    })
                    .collect()
            })
            .unwrap_or_default();
        let imports = parsed
            .map(|parsed| {
                parsed
                    .bindings
                    .iter()
                    .cloned()
                    .map(|source| CandidateSourceImportRoutes {
                        uses: source
                            .use_spans
                            .iter()
                            .copied()
                            .map(|source_span| CandidateSourceUseRoute {
                                source_span,
                                routes: Vec::new(),
                                status: SourceCorrespondenceStatus::MissingCandidateRoute,
                            })
                            .collect(),
                        source,
                        routes: Vec::new(),
                        status: SourceCorrespondenceStatus::MissingCandidateRoute,
                    })
                    .collect()
            })
            .unwrap_or_default();
        let reexports = parsed
            .map(|parsed| {
                parsed
                    .reexports
                    .iter()
                    .cloned()
                    .map(|source| CandidateSourceReExportRoutes {
                        source,
                        routes: Vec::new(),
                        status: SourceCorrespondenceStatus::MissingCandidateRoute,
                    })
                    .collect()
            })
            .unwrap_or_default();
        let exports = parsed
            .map(|parsed| {
                parsed
                    .exports
                    .iter()
                    .cloned()
                    .map(|source| CandidateSourceExportRoutes {
                        source,
                        routes: Vec::new(),
                        status: SourceCorrespondenceStatus::MissingCandidateRoute,
                    })
                    .collect()
            })
            .unwrap_or_default();
        Self {
            source_chunk,
            source_file,
            source_units: BTreeMap::new(),
            source_unit_status: if parsed.is_some() {
                SourceCorrespondenceStatus::MissingCandidateRoute
            } else {
                SourceCorrespondenceStatus::MissingSourceFacts
            },
            source_requests,
            imports,
            reexports,
            exports,
            prepared_requests: Vec::new(),
            module_cell_declarations: Vec::new(),
            source_module_cells: None,
            source_initialized_module_cells: None,
            rewrite_receipts: Vec::new(),
            status: if parsed.is_some() {
                SourceCorrespondenceStatus::MissingCandidateRoute
            } else {
                SourceCorrespondenceStatus::MissingSourceFacts
            },
        }
    }
}

fn apply_static_request_identity(
    route: &mut CandidatePreparedRequestRoute,
    identity: &CandidateStaticRequestIdentity,
) -> bool {
    if route.request_kind != identity.kind || route.source_statement != identity.source_statement {
        return false;
    }
    let (Some(request), Some(mode)) = (identity.request.as_ref(), identity.mode) else {
        return false;
    };
    let mut bindings = Vec::with_capacity(identity.bindings.len());
    for binding in &identity.bindings {
        let (Some(kind), Some(target)) = (binding.kind, binding.target.as_ref()) else {
            return false;
        };
        bindings.push(CandidatePreparedRequestBinding {
            source_binding: binding.source_binding.clone(),
            candidate_local: binding.local.clone(),
            specifier_index: binding.specifier_index,
            specifier_span: binding.specifier_span,
            imported_name: binding.imported_name.clone(),
            kind,
            exported_name: binding.exported_name.clone(),
            source_cell: binding.source_cell,
            target: target.clone(),
        });
    }
    route.item_index = identity.item_index;
    route.item_span = identity.item_span;
    route.emitted_request = request.clone();
    route.mode = mode;
    route.targets = identity.targets.clone();
    route.bindings = bindings;
    true
}

fn apply_source_request_identity(
    route: &mut CandidateSourceRequestRoute,
    identity: &CandidateStaticRequestIdentity,
    prepared: &CandidatePreparedRequestRoute,
) {
    route.item_index = identity.item_index;
    route.item_span = identity.item_span;
    route.source_statement = identity.source_statement;
    route.emitted_request = prepared.emitted_request.clone();
    route.mode = prepared.mode;
    route.targets = prepared.targets.clone();
    route.bindings = prepared.bindings.clone();
    if prepared.status != SourceCorrespondenceStatus::Complete {
        route.status = SourceCorrespondenceStatus::UnsupportedRewrite;
    }
}

fn mark_source_request_routes_unsupported(
    correspondence: &mut CandidateSourceCorrespondence,
    prepared: &CandidatePreparedRequestRoute,
) {
    for request in &mut correspondence.source_requests {
        for route in &mut request.routes {
            if route.output == prepared.output && route.item_span == prepared.item_span {
                route.status = SourceCorrespondenceStatus::UnsupportedRewrite;
            }
        }
    }
    for reexport in &mut correspondence.reexports {
        for route in &mut reexport.routes {
            if route.output == prepared.output && route.item_span == prepared.item_span {
                route.status = SourceCorrespondenceStatus::UnsupportedRewrite;
            }
        }
    }
    for import in &mut correspondence.imports {
        if import
            .routes
            .iter()
            .any(|route| route.output == prepared.output && route.item_span == prepared.item_span)
        {
            import.status = SourceCorrespondenceStatus::UnsupportedRewrite;
        }
    }
}

#[derive(Debug, Clone)]
pub struct InteropUse {
    pub item_index: usize,
    pub line: Option<usize>,
    /// Exact original SWC site, retained for future phase/call joins.
    pub span: Span,
    pub kind: InteropUseKind,
    pub specifier: Option<String>,
    pub phase: SitePhase,
}

/// When a source site can run relative to the stack that enters its function
/// (SPEC.md § Loader-closed derivation).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SitePhase {
    Synchronous,
    /// Synchronous, in top-level code outside every function: it runs only
    /// while its own module's body evaluates.
    ModuleBody,
    /// The site follows an `await` on every path of its innermost async
    /// function, so it never runs on the stack that invoked that function.
    MandatorilySuspended,
    /// No reachable code can enter the site's innermost function.
    Unreachable,
}

/// Uses of a binding that may hold the host loader: an import binding, or a
/// top-level binding initialized with exactly `import.meta.require`.
#[derive(Debug, Clone)]
pub struct LoaderBindingUses {
    pub binding: LoaderBinding,
    /// Uses in reachable code; unreachable uses are dropped.
    pub uses: Vec<LoaderBindingUse>,
}

#[derive(Debug, Clone)]
pub enum LoaderBinding {
    Import {
        source: String,
        imported: String,
    },
    LoaderAlias {
        /// The `import.meta.require` census site that initializes it.
        definition: Span,
        /// Names a local export publishes it under.
        exported_as: Vec<String>,
    },
    /// A binding initialized with a direct call of a `createRequire` named
    /// import from the `module` builtin.
    CreatedLoader,
}

/// What immediately consumes a use: the value a call returns, or the binding
/// itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoaderUseConsumer {
    /// `use.name`, read and not called.
    MemberRead(String),
    /// `use.name(...)`.
    MemberCall(String),
    /// The initializer of a `var`/`let`/`const` binding the walk tracks.
    BindingInitializer,
    Other,
}

#[derive(Debug, Clone)]
pub struct LoaderBindingUse {
    pub item_index: usize,
    pub line: Option<usize>,
    /// `Some` for a direct call whose only argument is a string literal.
    pub literal: Option<String>,
    /// A direct call, literal or not.
    pub call: bool,
    /// For a call, what consumes its result; otherwise what consumes the
    /// binding's value.
    pub consumer: LoaderUseConsumer,
    pub phase: SitePhase,
}

/// A request exposing its target's whole export surface: `import * as`,
/// `export * from`, `export { … } from`.
#[derive(Debug, Clone)]
pub struct NamespaceRequest {
    pub item_index: usize,
    pub specifier: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InteropUseKind {
    ImportMetaRequireLiteralCall,
    ImportMetaRequireDynamicCall,
    ImportMetaRequireReference,
    GlobalThisRequire,
    UnboundRequire,
    /// A call of a direct `eval` whose code folds to exactly `require`:
    /// `eval("require")("x")` with one string literal.
    EvaluatedRequireLiteralCall,
    /// The same with any other argument list.
    EvaluatedRequireDynamicCall,
    UnboundModule,
    /// `module.exports`: the export object, which cannot load a module.
    UnboundModuleExports,
    UnboundExports,
    ImportMetaDynamicProperty,
    ImportMetaOtherProperty,
    ImportMetaValue,
    TsImportEquals,
    TsExportAssignment,
    /// `import(...)`; `specifier` is set for a string-literal argument.
    DynamicImport,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum ChunkModuleKind {
    #[default]
    Unknown,
    Esm,
    /// No AST items. When statically imported, it cannot run code or reenter
    /// an importer, even though there is no ESM declaration in its source.
    EmptySource,
}

impl ChunkModuleKind {
    pub fn supports_static_esm_closure(self) -> bool {
        matches!(self, Self::Esm | Self::EmptySource)
    }
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
    /// Present for a named re-export; `local` then names its provider export,
    /// not a binding declared in this module.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::io;

    struct FlushFailure;

    impl Write for FlushFailure {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            Ok(bytes.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::other("flush failed"))
        }
    }

    #[test]
    fn compact_report_propagates_final_flush_failure() {
        let error =
            write_json_to(FlushFailure, Path::new("full.json"), &Vec::<u8>::new()).unwrap_err();
        assert!(error.to_string().contains("flushing full.json"));
    }
}
