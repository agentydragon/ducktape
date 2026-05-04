//! Typed deserialisation surface for `js.ast_transform_spec` JSONC files.
//!
//! Three declarative top-level maps describe what the spec wants applied:
//!
//! - `vendor` keyed by chunk path (`"static/lib.js"` → [`VendorMark`]).
//! - `logicalModules` keyed by chunk id, then target path
//!   (`"static/app"` → `"foo/bar/baz.js"` → [`LogicalModule`]).
//! - `residualModules` keyed by chunk id (`"static/app"` →
//!   [`ResidualModule`]). At most one residual per chunk — encoded by the
//!   map shape.
//!
//! Each pipeline stage that needs configuration gets its own optional
//! top-level field ([`TransformSpec::materialize_logical_modules`],
//! [`TransformSpec::write_js_tree`], [`TransformSpec::emit_browser_harness`],
//! [`TransformSpec::swap_vendor_chunks`]). Stages run in a fixed canonical
//! order, gated by the presence of their config (or — for the vendor
//! stages and `rewriteChunkEntrySpecifiers` — by an explicit boolean
//! toggle and the contents of the declarative maps). There is no
//! user-supplied pipeline list.
//!
//! All consumers see typed structs; nothing here returns
//! `serde_json::Value` for a known field.

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TransformSpec {
    pub kind: String,
    pub inputs: LoadJsChunksArgs,

    // --- declarative data sections ---
    #[serde(default)]
    pub vendor: BTreeMap<String, VendorMark>,
    #[serde(default)]
    pub logical_modules: BTreeMap<String, BTreeMap<String, LogicalModule>>,
    #[serde(default)]
    pub residual_modules: BTreeMap<String, ResidualModule>,
    /// Per-chunk in-place renames for bindings staying in entry's
    /// body (i.e. *not* assigned to a logical module and not pulled
    /// into the explicit residual). The materializer collects these
    /// into a `binding_name -> export_name` map; the lowerer rewrites
    /// identifiers in entry's source AST during chunk lowering. No
    /// `Logical(R)` module is created for these bindings, no separate
    /// residual file is emitted, and the orphan-statement node
    /// (`ModuleId::ResidualEntry`) keeps owning the bindings — which
    /// avoids the 2-module SCC the residual-member-rename path would
    /// otherwise create when orphan stmts and residual decls
    /// interleave with side-effecting initializers.
    ///
    /// Bindings claimed by a logical module take their rename from
    /// the module plan; the `chunk_renames` entry (if any) is dropped
    /// for those.
    #[serde(default)]
    pub chunk_renames: BTreeMap<String, ChunkRenames>,

    // --- per-stage configuration (presence gates the stage) ---
    /// When `true`, run `rewrite_chunk_entry_specifiers` after the
    /// always-on startup steps. The stage takes no arguments.
    #[serde(default)]
    pub rewrite_chunk_entry_specifiers: bool,
    /// Optional output configuration for `swap_vendor_chunks`. The stage
    /// itself runs whenever `vendor` contains any `level: swap` entries;
    /// this field only adds output paths and a `write` toggle.
    #[serde(default)]
    pub swap_vendor_chunks: Option<SwapVendorChunksConfig>,
    /// When set, run `materialize_logical_modules`. `chunkIds` is
    /// required.
    #[serde(default)]
    pub materialize_logical_modules: Option<MaterializeLogicalModulesConfig>,
    /// When set, persist the artifact tree to `outDir`.
    #[serde(default)]
    pub write_js_tree: Option<WriteJsTreeConfig>,
    /// When set, emit a browser-runtime harness alongside the artifact.
    #[serde(default)]
    pub emit_browser_harness: Option<EmitBrowserHarnessConfig>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LoadJsChunksArgs {
    pub input_root: PathBuf,
    pub js_list_path: PathBuf,
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SwapVendorChunksConfig {
    #[serde(default)]
    pub output_manifest_path: Option<PathBuf>,
    #[serde(default)]
    pub output_wrapper_dir: Option<PathBuf>,
    /// Defaults to `true` — actually write the manifest / wrapper files
    /// to disk. Set `false` for dry-run.
    #[serde(default = "default_true")]
    pub write: bool,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct MaterializeLogicalModulesConfig {
    pub chunk_ids: Vec<String>,
    #[serde(default)]
    pub file: Option<String>,
    /// Defaults to `true` — drop chunks not in `chunkIds` before
    /// materialising. Set `false` to keep them.
    #[serde(default = "default_true")]
    pub prune_other_chunks: bool,
    #[serde(default)]
    pub force: bool,
    #[serde(default)]
    pub report_out_dir: Option<PathBuf>,
    #[serde(default)]
    pub report_summary_path: Option<PathBuf>,
    #[serde(default)]
    pub target_dir: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WriteJsTreeConfig {
    pub out_dir: PathBuf,
    #[serde(default)]
    pub force: bool,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EmitBrowserHarnessConfig {
    pub asset_summary_path: PathBuf,
    pub out_dir: PathBuf,
    pub snapshot_root: PathBuf,
    #[serde(default)]
    pub force: bool,
}

/// Container for per-chunk in-place renames; see
/// [`TransformSpec::chunk_renames`].
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ChunkRenames {
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub members: Vec<Member>,
}

fn default_true() -> bool {
    true
}

// --- Vendor ---------------------------------------------------------------

/// One vendor annotation, keyed in the spec by chunk path
/// (e.g. `"static/lib.js"`). The `level` discriminator selects between
/// `suppress` / `boundary-rename` / `swap`; only `swap` requires the
/// `package`/`version`/`subpath` triple, encoded as the
/// [`VendorLevel::Swap`] variant carrying those fields.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct VendorMark {
    pub id: String,
    pub identity: String,
    pub evidence: Vec<Evidence>,
    #[serde(default)]
    pub role: VendorRole,
    #[serde(default)]
    pub upstream_family: Option<String>,
    #[serde(default)]
    pub confidence: Option<String>,
    #[serde(default)]
    pub notes: Option<String>,
    #[serde(default)]
    pub export_shape: Option<serde_json::Map<String, serde_json::Value>>,
    #[serde(default)]
    pub fingerprint: Option<Fingerprint>,
    #[serde(flatten)]
    pub level: VendorLevel,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "level", rename_all = "kebab-case")]
pub enum VendorLevel {
    Suppress,
    BoundaryRename,
    Swap(SwapMark),
}

impl VendorLevel {
    pub fn as_str(&self) -> &'static str {
        match self {
            VendorLevel::Suppress => "suppress",
            VendorLevel::BoundaryRename => "boundary-rename",
            VendorLevel::Swap(_) => "swap",
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SwapMark {
    pub package: String,
    pub version: String,
    pub subpath: String,
    #[serde(default)]
    pub wrapper_shape: Option<WrapperShape>,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Default, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum VendorRole {
    #[default]
    Module,
    Worker,
}

impl VendorRole {
    pub fn as_str(&self) -> &'static str {
        match self {
            VendorRole::Module => "module",
            VendorRole::Worker => "worker",
        }
    }
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "kebab-case")]
pub enum WrapperShape {
    NamedFromDefault,
    NamedFromJsonDefault,
    NamedFromModuleDefault,
}

impl WrapperShape {
    pub fn as_str(&self) -> &'static str {
        match self {
            WrapperShape::NamedFromDefault => "named-from-default",
            WrapperShape::NamedFromJsonDefault => "named-from-json-default",
            WrapperShape::NamedFromModuleDefault => "named-from-module-default",
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Evidence {
    pub path: String,
    pub line: u64,
    pub text: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Fingerprint {
    pub algorithm: String,
    pub hash: String,
}

// --- Logical / Residual modules ------------------------------------------

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LogicalModule {
    /// Optional human-readable id used in cycle reports and the per-chunk
    /// requested-modules summary. Defaults to a stable derived value when
    /// absent.
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub members: Vec<Member>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ResidualModule {
    #[serde(default)]
    pub id: Option<String>,
    /// Logical-module path the residual catch-all writes to. Defaults to
    /// `"residual/unhandled"` when absent.
    #[serde(default)]
    pub target: Option<String>,
    #[serde(default)]
    pub members: Vec<Member>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Member {
    /// Public export name. Defaults to the bound `selector.binding.name`.
    #[serde(default)]
    pub name: Option<String>,
    pub selector: MemberSelector,
    #[serde(default)]
    pub purity: MemberPurity,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemberSelector {
    pub binding: BindingSelector,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BindingSelector {
    pub name: String,
    #[serde(default)]
    pub kind: Option<BindingSourceKind>,
}

#[derive(Debug, Clone, Copy, Deserialize, Eq, PartialEq)]
pub enum BindingSourceKind {
    /// The bound name comes from an `import` specifier in the source
    /// chunk, not a top-level decl. The materializer rewrites the import
    /// statement to a re-import in the destination module.
    ImportSpecifier,
    /// Top-level `var` / `let` / `const` declaration in the source chunk.
    /// Carried for documentation; no special materializer path.
    VariableDeclarator,
    /// Top-level `function` declaration in the source chunk.
    FunctionDeclaration,
    /// Top-level `class` declaration in the source chunk.
    ClassDeclaration,
}

#[derive(Debug, Clone, Copy, Deserialize, Default, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum MemberPurity {
    #[default]
    Default,
    /// Author asserts that calls to the bound function have no observable
    /// side effects. Validator drops `S` edges for `<binding>(...)` call
    /// sites. See AGENTS.md "Declared purity" + DESIGN.md A9.
    Pure,
}
