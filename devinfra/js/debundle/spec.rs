//! Typed deserialisation surface for `js.ast_transform_spec` YAML files.
//!
//! Two declarative top-level maps describe what the spec wants applied:
//!
//! - `vendor` keyed by chunk path (`"static/lib.js"` → [`VendorMark`]).
//! - `logical_modules` keyed by chunk id, then target path
//!   (`"static/app"` → `"foo/bar/baz.js"` → [`LogicalModule`]).
//!
//! A third per-chunk map, `unassigned_mode`, decides what happens to
//! top-level statements that no `logical_modules` entry explicitly
//! claims (catch-all to entry, catch-all to a separate file, or one
//! synthetic mini-factor per atomic unit). See [`UnassignedMode`].
//!
//! Pipeline stages run in a fixed canonical order; each stage is either
//! always-on or gated by the contents of those maps / by the presence of a
//! per-stage config field ([`TransformSpec::write_js_tree`],
//! [`TransformSpec::emit_browser_harness`]). There is no user-supplied
//! pipeline list.
//!
//! All consumers see typed structs; nothing here returns
//! `serde_json::Value` for a known field.

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TransformSpec {
    pub inputs: LoadJsChunksArgs,

    // --- declarative data sections ---
    #[serde(default)]
    pub vendor: BTreeMap<String, VendorMark>,
    #[serde(default)]
    pub logical_modules: BTreeMap<String, BTreeMap<String, LogicalModule>>,
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
    /// Per-chunk control over what happens to top-level statements
    /// the spec doesn't explicitly claim for any logical module.
    /// See [`UnassignedMode`]; absent entries default to
    /// [`UnassignedMode::InlineInEntry`].
    #[serde(default)]
    pub unassigned_mode: BTreeMap<String, UnassignedMode>,

    // --- per-stage configuration ---
    /// Output configuration for `swap_vendor_chunks`. The stage runs
    /// whenever `vendor` contains any `level: swap` entries; this field
    /// only adds output paths and a `write` toggle. All inner fields
    /// have defaults, so omitting `swap_vendor_chunks` is identical to
    /// supplying an empty object.
    #[serde(default)]
    pub swap_vendor_chunks: SwapVendorChunksConfig,
    /// Configuration for `materialize_logical_modules`. The stage runs
    /// whenever `logical_modules ∪ unassigned_mode ∪ chunk_renames`
    /// is non-empty; the chunk ids it processes are the union of
    /// those maps' keys. This field only carries auxiliary options.
    #[serde(default)]
    pub materialize_logical_modules: MaterializeLogicalModulesConfig,
    /// When set, persist the artifact tree to `out_dir`.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub write_js_tree: Option<WriteJsTreeConfig>,
    /// When set, emit a browser-runtime harness alongside the artifact.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub emit_browser_harness: Option<EmitBrowserHarnessConfig>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LoadJsChunksArgs {
    pub input_root: PathBuf,
    pub js_list_path: PathBuf,
}

#[derive(Debug, Clone, Deserialize, Serialize, Default)]
#[serde(deny_unknown_fields)]
pub struct SwapVendorChunksConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub output_manifest_path: Option<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub output_wrapper_dir: Option<PathBuf>,
    /// Defaults to `true` — actually write the manifest / wrapper files
    /// to disk. Set `false` for dry-run.
    #[serde(skip_serializing_if = "is_true")]
    #[serde(default = "default_true")]
    pub write: bool,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MaterializeLogicalModulesConfig {
    /// Optional override for the entry-file path to read per chunk.
    /// Absent means "use the chunk's recorded entry file".
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub file: Option<String>,
    /// Defaults to `true` — drop chunks outside the materialised set
    /// (the union of `logical_modules`, `unassigned_mode`, and
    /// `chunk_renames` keys) before materialising. Set `false` to
    /// keep them.
    #[serde(default = "default_true")]
    pub prune_other_chunks: bool,
    #[serde(default)]
    pub force: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub report_out_dir: Option<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub report_summary_path: Option<PathBuf>,
    #[serde(skip_serializing_if = "String::is_empty")]
    #[serde(default)]
    pub target_dir: String,
}

impl Default for MaterializeLogicalModulesConfig {
    fn default() -> Self {
        Self {
            file: None,
            prune_other_chunks: true,
            force: false,
            report_out_dir: None,
            report_summary_path: None,
            target_dir: String::new(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WriteJsTreeConfig {
    pub out_dir: PathBuf,
    #[serde(default)]
    pub force: bool,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EmitBrowserHarnessConfig {
    pub asset_summary_path: PathBuf,
    pub out_dir: PathBuf,
    pub snapshot_root: PathBuf,
    #[serde(default)]
    pub force: bool,
}

/// Container for per-chunk in-place renames; see
/// [`TransformSpec::chunk_renames`].
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ChunkRenames {
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub members: Vec<Member>,
}

fn default_true() -> bool {
    true
}

fn is_true(value: &bool) -> bool {
    *value
}

fn is_default_member_purity(purity: &MemberPurity) -> bool {
    matches!(purity, MemberPurity::Default)
}

fn is_default_vendor_role(role: &VendorRole) -> bool {
    matches!(role, VendorRole::Module)
}

// --- Vendor ---------------------------------------------------------------

/// One vendor annotation, keyed in the spec by chunk path
/// (e.g. `"static/lib.js"`). The `level` discriminator selects between
/// `suppress` / `boundary-rename` / `swap`; only `swap` requires the
/// `package`/`version`/`subpath` triple, encoded as the
/// [`VendorLevel::Swap`] variant carrying those fields.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct VendorMark {
    pub identity: String,
    #[serde(skip_serializing_if = "is_default_vendor_role")]
    #[serde(default)]
    pub role: VendorRole,
    #[serde(flatten)]
    pub level: VendorLevel,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "level", rename_all = "snake_case")]
pub enum VendorLevel {
    Suppress,
    BoundaryRename,
    Swap(SwapMark),
}

/// What [`TransformSpec::unassigned_mode`] means for a chunk's
/// top-level statements that the YAML doesn't explicitly claim for
/// any logical module. The atomic-factor-unit primitive in
/// `analysis::atomic_units` partitions the chunk's owners into
/// minimal co-location groups; this enum decides what destination
/// each *unclaimed* unit lands in.
///
/// The three variants are mutually exclusive — every chunk picks
/// exactly one destination policy. Subsuming the previous
/// `residual_modules` map: today's `CatchallFile` variant covers
/// the case the standalone map used to express ("emit unclaimed
/// code to a separate file at `target`").
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Default)]
#[serde(rename_all = "snake_case", tag = "kind")]
pub enum UnassignedMode {
    /// Default. Unclaimed bindings stay inline in the chunk's entry
    /// file (owned by `ModuleId::ResidualEntry`); no separate
    /// residual module is emitted. Renames against unclaimed
    /// bindings come from [`TransformSpec::chunk_renames`] and are
    /// applied in-place by the lowerer.
    #[default]
    InlineInEntry,
    /// Unclaimed bindings emit to a separate logical module at
    /// `target` (defaults to [`DEFAULT_RESIDUAL_MODULE_PATH`]). The
    /// module behaves like any other logical module — it can be a
    /// peel destination for factorize proposals — but structurally
    /// is the catch-all for unclaimed code. Renames for bindings
    /// that land in this catch-all should be expressed by listing
    /// them as members of a regular `logical_modules` entry at the
    /// same `target` path; the materializer joins explicit member
    /// claims with unclaimed overflow on a per-binding basis.
    CatchallFile {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        target: Option<String>,
    },
    /// One synthetic mini-factor per unclaimed atomic factor unit.
    /// The residual catch-all collapses to whatever truly cannot
    /// be peeled (typically empty for clean chunks). See
    /// FACTORIZE.md.
    MiniFactors,
}

impl UnassignedMode {
    /// Convenience accessor for [`CatchallFile::target`]: returns
    /// the configured target path (or [`DEFAULT_RESIDUAL_MODULE_PATH`]
    /// when none) iff `self` is [`CatchallFile`], else `None`.
    pub fn catchall_file_target(&self) -> Option<&str> {
        match self {
            UnassignedMode::CatchallFile { target } => {
                Some(target.as_deref().unwrap_or(DEFAULT_RESIDUAL_MODULE_PATH))
            }
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct SwapMark {
    pub package: String,
    pub version: String,
    pub subpath: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub wrapper_shape: Option<WrapperShape>,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Default, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum VendorRole {
    #[default]
    Module,
    Worker,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum WrapperShape {
    NamedFromDefault,
    NamedFromJsonDefault,
    NamedFromModuleDefault,
}

// --- Logical modules -----------------------------------------------------

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LogicalModule {
    #[serde(default)]
    pub members: Vec<Member>,
    /// Anonymous (empty-`declared_bindings`) top-level statements
    /// the materializer must co-move into this module's body.
    /// Required when a peel proposal's closure includes side-effect
    /// statements that have no name to address as `members`
    /// (decorator applications, IIFE preludes, runtime init calls).
    /// Each entry is matched by AST shape against the chunk's
    /// top-level statements; the resolver requires exactly one
    /// match per entry.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub anonymous_statements: Vec<AnonymousStatement>,
}

/// Co-mover spec for a top-level anonymous side-effect statement.
/// See [`LogicalModule::anonymous_statements`].
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AnonymousStatement {
    /// JS source of the target top-level statement, verbatim.
    /// Parsed as a single `Stmt` and compared structurally
    /// (modulo spans) against the chunk's top-level statements.
    /// Must match exactly one — zero matches and ambiguous matches
    /// are spec errors.
    #[serde(rename = "match")]
    pub match_source: String,
    /// Optional human-readable note (e.g. "MobX-style decorator on
    /// $g.prototype.invites"). Ignored by the resolver.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// Default value the [`UnassignedMode::CatchallFile`] target path
/// falls back to when the spec author omits it. SSOT consumed by
/// the materializer (`logical_modules` residual synthesis) and by
/// analysis tools that want to match the canonical residual
/// catch-all path.
pub const DEFAULT_RESIDUAL_MODULE_PATH: &str = "residual/unhandled";

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Member {
    /// Public export name. Defaults to the bound `selector.binding.name`.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub name: Option<String>,
    pub selector: MemberSelector,
    #[serde(skip_serializing_if = "is_default_member_purity")]
    #[serde(default)]
    pub purity: MemberPurity,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MemberSelector {
    pub binding: BindingSelector,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BindingSelector {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub kind: Option<BindingSourceKind>,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
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

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Default, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum MemberPurity {
    #[default]
    Default,
    /// Author asserts that calls to the bound function have no observable
    /// side effects. Validator drops `S` edges for `<binding>(...)` call
    /// sites. See AGENTS.md "Declared purity" + DESIGN.md A9.
    Pure,
}
