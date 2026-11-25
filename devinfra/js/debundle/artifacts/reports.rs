//! Serialized report models for artifact output.

use std::collections::BTreeMap;
use std::sync::Arc;

use analysis_model::DepKind;
use analysis_model::StableExportValue;
use js_ast::ParsedGlobalUses;
use serde::{Deserialize, Serialize};
use swc_common::Span;

use crate::FileRole;
use crate::metrics::{DecompositionMetrics, OutputMetrics};

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

/// Per-chunk view serialized to `reports/tree/<chunk>/chunk.json`.
#[derive(Serialize)]
pub struct ChunkManifest<'a> {
    #[serde(flatten)]
    pub analysis: &'a ChunkAnalysisReport,
    pub validation: &'a ChunkValidationSummary,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub logical_modules: Option<&'a ChunkLogicalModulesSummary>,
    pub selected_module_lowerings: &'a [SelectedModuleLowering],
    pub output_metrics: OutputMetrics,
}

#[derive(Serialize)]
pub struct ArtifactManifest<'a> {
    pub counts: &'a ArtifactCounts,
    pub chunks: &'a [ArtifactChunkRecord],
    pub logical_modules: RootLogicalModulesSummary,
    pub selected_module_lowerings: &'a [SelectedModuleLowering],
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
    /// Unresolved identifier sites of the prepared parse. `None` means the
    /// inventory was not computed (no prepared parse for this chunk).
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

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize, strum::IntoStaticStr,
)]
#[cfg_attr(test, derive(strum::EnumIter))]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
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

impl InteropUseKind {
    /// Stable snake_case identifier used in machine-facing output.
    pub fn machine_code(self) -> &'static str {
        let code: &'static str = self.into();
        code
    }
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
}

#[derive(Debug, Clone, Serialize)]
pub struct KeptTopLevelDeclarationRecord {
    pub line: Option<usize>,
    pub names: Vec<String>,
    pub kind: TopLevelDeclarationKind,
}

/// The three top-level declaration variants we anchor extraction on.
/// Mirrors the SWC `Decl` arms where `analyze_program_shallow` produces
/// a `KeptTopLevelDeclarationRecord`.
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
    use strum::IntoEnumIterator;

    #[test]
    fn interop_use_machine_codes_match_serde() {
        for kind in InteropUseKind::iter() {
            assert_eq!(
                serde_json::to_string(&kind).unwrap(),
                format!("\"{}\"", kind.machine_code())
            );
        }
    }
}
