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
    /// See [`UnassignedMode`]. Every chunk that appears in
    /// `logical_modules` must also appear here — there is no implicit
    /// default; spec authors must state the policy explicitly. The
    /// outer map itself may be omitted (`#[serde(default)]`) only when
    /// the spec processes no chunks at all (e.g. vendor-only specs).
    #[serde(default)]
    pub unassigned_mode: BTreeMap<String, UnassignedMode>,
    /// Per-chunk analysis options. Opt-in flags for conditionally-correct
    /// inferences that hold only when the input satisfies a checkable
    /// precondition (see `devinfra/js/debundle/AGENTS.md` →
    /// "Conditionally-correct optimizations" and
    /// `devinfra/js/debundle/README.md` →
    /// "Conditionally-correct optimizations"). Default-empty: every
    /// chunk uses the strictly-conservative analysis paths unless the
    /// spec explicitly opts in.
    #[serde(default)]
    pub chunk_analysis_options: BTreeMap<String, OwnerGraphOptions>,

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

/// Per-chunk owner-graph build options. Each field defaults to the
/// strictly-conservative behavior; opt-ins enable conditionally-correct
/// inferences that hold only when the input satisfies a checkable
/// precondition (see `devinfra/js/debundle/AGENTS.md` →
/// "Conditionally-correct optimizations").
///
/// Serves two roles with one type: it's the YAML surface for
/// `TransformSpec::chunk_analysis_options` (each per-chunk entry
/// deserializes into this), and it's the input to
/// `analysis::build_owner_graph_with`. The materializer threads it
/// straight through — no per-chunk copy.
#[derive(Debug, Clone, Copy, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OwnerGraphOptions {
    /// Emit the side-effect ordering chain using per-statement
    /// (writes, reads) summaries instead of the adjacent-impure
    /// transitive reduction. See the S-chain block in
    /// `build_owner_graph_with` and `README.md` →
    /// "Conditionally-correct optimizations". Only sound when the
    /// chunk is free of dynamic dispatch shapes (direct `eval`,
    /// `with`, `Function(...)` constructor, computed
    /// `globalThis[<expr>]` access, `Object.defineProperty` on
    /// globals, `Proxy` on globals). Individual statements that
    /// contain a non-summarizable shape fall back to the
    /// conservative path automatically; this flag only enables the
    /// dataflow path for statements that pass the per-statement
    /// check.
    #[serde(default)]
    pub dataflow_aware_s_chain: bool,
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
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub report_out_dir: Option<PathBuf>,
    #[serde(skip_serializing_if = "String::is_empty")]
    #[serde(default)]
    pub target_dir: String,
}

impl Default for MaterializeLogicalModulesConfig {
    fn default() -> Self {
        Self {
            file: None,
            prune_other_chunks: true,
            report_out_dir: None,
            target_dir: String::new(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WriteJsTreeConfig {
    pub out_dir: PathBuf,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EmitBrowserHarnessConfig {
    pub asset_summary_path: PathBuf,
    pub out_dir: PathBuf,
    pub snapshot_root: PathBuf,
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

fn is_default_member_effect(effect: &MemberEffect) -> bool {
    matches!(effect, MemberEffect::Default)
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
    PartialSwap(PartialSwapMark),
    BundledPartialSwap(BundledPartialSwapMark),
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
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case", tag = "kind")]
pub enum UnassignedMode {
    /// Unclaimed bindings stay inline in the chunk's entry file
    /// (owned by `ModuleId::ResidualEntry`); no separate residual
    /// module is emitted. Renames against unclaimed bindings come
    /// from [`TransformSpec::chunk_renames`] and are applied in-place
    /// by the lowerer.
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
    /// `docs/design.md` §"Layered mental model" + §"Two classes of atom".
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

/// Per-symbol vendor swap on a mixed chunk. The chunk stays on disk
/// (residual exports keep working). Each listed export is rewritten
/// according to its [`PartialSwapKind`] — see the variants for the
/// three supported shapes (`member` for per-symbol member-access
/// rewrites à la zod; `namespace` and `default` for libraries the
/// chunk re-exports as a whole namespace or as a default value).
/// Multiple upstream packages can share one chunk.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PartialSwapMark {
    /// package_name → upstream coordinates.
    pub packages: BTreeMap<String, PartialSwapPackage>,
    /// chunk_export_name → which package + how to rewrite it.
    pub symbols: BTreeMap<String, PartialSwapSymbol>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PartialSwapPackage {
    pub version: String,
    pub subpath: String,
    /// Local identifier used in the emitted
    /// `import * as <namespace> from "<package>"` for symbols with
    /// `kind: member`. Ignored for `kind: namespace` and
    /// `kind: default`, which use the caller-side local binding name
    /// for the import alias instead.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub namespace: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PartialSwapSymbol {
    /// Key into the enclosing [`PartialSwapMark::packages`] map.
    pub package: String,
    /// How to rewrite references to the caller's local binding. See
    /// [`PartialSwapKind`].
    #[serde(default, skip_serializing_if = "is_default_partial_swap_kind")]
    pub kind: PartialSwapKind,
    /// `kind: member` and `kind: named` only: upstream package's
    /// export name. For `member` it becomes the member accessed off
    /// the namespace import; for `named` it becomes the imported name
    /// in `import { <upstream_export> as <local_binding> } from "<pkg>"`.
    /// Forbidden for `kind: namespace` / `kind: default`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub upstream_export: Option<String>,
    /// Optional chunk-local binding that implements this swapped symbol.
    ///
    /// Normally the debundler discovers the implementation binding from
    /// the chunk's `export { local as <symbol-key> }` specifier. Some
    /// bundled vendors also have reachable internal helpers that are not
    /// exported by the chunk but still need to be replaced when residual
    /// in-chunk code calls them. In that case, set `local` to the
    /// top-level binding name to rewrite/strip.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub local: Option<String>,
}

/// Per-symbol vendor swap backed by a caller-supplied ESM bundle.
/// The input package may be CJS or otherwise browser-hostile; the
/// debundler does not run a bundler itself. Instead, `bundle.path`
/// names a prebuilt ESM blob whose named exports are projected into
/// per-package facades under `vendors/generated`, and consumer imports
/// are rewritten to those facades.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BundledPartialSwapMark {
    pub bundle: BundledPartialSwapBundle,
    /// package_name -> upstream metadata and bundle projection.
    pub packages: BTreeMap<String, BundledPartialSwapPackage>,
    /// chunk_export_name -> which package + how to rewrite it.
    pub symbols: BTreeMap<String, PartialSwapSymbol>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BundledPartialSwapBundle {
    pub path: PathBuf,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BundledPartialSwapPackage {
    pub version: String,
    pub subpath: String,
    /// Named export in [`BundledPartialSwapMark::bundle`] that is the
    /// package object/default value for this package coordinate.
    pub bundle_export: String,
    /// Local identifier used for member/named rewrites that need a
    /// stable imported object. Ignored for `kind: namespace` and
    /// `kind: default`, which use the caller-side local binding.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub namespace: Option<String>,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Default, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum PartialSwapKind {
    /// Per-symbol member-access rewrite (zod-style). Emits one
    /// `import * as <namespace> from "<package>"` per file that uses
    /// the package and rewrites each `<local_binding>(...)` reference
    /// to `<namespace>.<upstream_export>(...)`.
    #[default]
    Member,
    /// Whole-namespace import. The chunk's export is itself the
    /// package's namespace object (e.g. `import { a as React } from
    /// "../chunk"` where `a` is React's default-namespace export and
    /// callers use `React.useState(...)` etc.). Rewrites the import
    /// to `import * as <local_binding> from "<package>"`; leaves
    /// every `<local_binding>.xxx` reference alone.
    Namespace,
    /// Default import. The chunk's export is the package's default
    /// (e.g. `import { aQ as z } from "../chunk"` where `z` is
    /// clsx's default function). Rewrites the import to
    /// `import <local_binding> from "<package>"`; leaves every
    /// `<local_binding>(...)` reference alone.
    Default,
    /// Named import. The chunk's export is a single named export of
    /// the package (e.g. `import { o as mobxObserver } from
    /// "../chunk"` where `o` is `mobx-react-lite`'s `observer`).
    /// Rewrites the import to
    /// `import { <upstream_export> as <local_binding> } from "<package>"`
    /// (or `import { <name> } from "<package>"` when the local
    /// binding name matches the upstream export); leaves every
    /// `<local_binding>(...)` reference alone.
    Named,
}

fn is_default_partial_swap_kind(kind: &PartialSwapKind) -> bool {
    matches!(kind, PartialSwapKind::Member)
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
    /// Optional human-readable comment emitted at the top of the
    /// generated module file, before any imports. Per-line literal
    /// `// ` prefix; empty input lines emit as `//`. See
    /// `CLI.md` § "per-member and module-level `comment:` fields".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comment: Option<String>,
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
    /// Optional YAML-only note. Use this for provenance,
    /// uncertainty, and other scratch reverse-engineering notes that
    /// should survive spec edits without appearing in generated JS.
    /// Ignored by the materializer.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    /// Optional human-readable comment. Preserved on round-trip and
    /// emitted immediately above the matched anonymous statement in
    /// generated JS. Accepted for symmetry with the top-level
    /// [`LogicalModule::comment`] and per-[`Member::comment`] fields.
    /// Prefer `note:` for scratch text that should stay YAML-only.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comment: Option<String>,
}

/// Default value the [`UnassignedMode::CatchallFile`] target path
/// falls back to when the spec author omits it. SSOT consumed by
/// the materializer (`logical_modules` residual synthesis) and by
/// analysis tools that want to match the canonical residual
/// catch-all path.
pub const DEFAULT_RESIDUAL_MODULE_PATH: &str = "residual/unhandled";

/// The single, canonical identity of a logical module.
///
/// One spec module had, historically, several stringly-typed
/// spellings that all denoted the same thing: the clean spec path
/// (`domains/system/ids`, derived from the `*.yaml` file location),
/// the chunk-prefixed `LogicalModule.id`
/// (`static/index-DI2GynTv::domains/system/ids`, minted in
/// `lowering/plans.rs`), the `<chunk>::` form surfaced as a report
/// `destination.label`, and the generated `target_file`. Comparing
/// two spellings of one module with `==` produced false positives —
/// most visibly a `merge_into` self-merge proposal in the peel
/// factorizer.
///
/// `ModulePath` collapses all of those to one normalized value at
/// construction. The canonical spelling is **relative, slash-
/// separated, lowercase** and equals the module's destination path
/// (id == path; the dest file is `path + extension`). [`parse`] is
/// the only way to build one from an untrusted string, so the
/// chunk-id prefix is stripped exactly once, at the boundary, and
/// `==` can no longer disagree about identity.
///
/// [`parse`]: ModulePath::parse
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ModulePath(String);

impl ModulePath {
    /// Normalize an untrusted module identifier into canonical form.
    ///
    /// `chunk_id` is the owning chunk's name (e.g.
    /// `static/index-DI2GynTv`); a leading `"<chunk_id>::"` is the
    /// production `LogicalModule.id` spelling and is stripped so it
    /// collapses onto the clean path. Pass `""` when no chunk context
    /// applies (the value is already a clean path).
    ///
    /// Rejects values that cannot be a relative module path:
    /// backslashes (Windows separators), absolute paths, empty
    /// segments, and `.`/`..` traversal.
    pub fn parse(raw: &str, chunk_id: &str) -> Result<Self, ModulePathError> {
        let stripped = match chunk_id.is_empty() {
            true => raw,
            false => raw
                .strip_prefix(chunk_id)
                .and_then(|rest| rest.strip_prefix("::"))
                .unwrap_or(raw),
        };
        if stripped.contains('\\') {
            return Err(ModulePathError::Backslash(stripped.to_string()));
        }
        let lower = stripped.to_ascii_lowercase();
        let trimmed = lower.trim_matches('/');
        if trimmed.is_empty() {
            return Err(ModulePathError::Empty(raw.to_string()));
        }
        for segment in trimmed.split('/') {
            if segment.is_empty() || segment == "." || segment == ".." {
                return Err(ModulePathError::Segment {
                    raw: raw.to_string(),
                    segment: segment.to_string(),
                });
            }
        }
        Ok(Self(trimmed.to_string()))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// The chunk-relative file this module emits to, e.g.
    /// `domains/system/ids` + `"js"` → `domains/system/ids.js`.
    pub fn dest_file(&self, extension: &str) -> String {
        format!("{}.{extension}", self.0)
    }

    /// True for the residual catch-all subtree (`residual` or
    /// `residual/...`). Folds in the prefix rule
    /// `spec_modules::is_residual_module_path` enforced.
    pub fn is_residual(&self) -> bool {
        self.0 == "residual" || self.0.starts_with("residual/")
    }
}

impl std::fmt::Display for ModulePath {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModulePathError {
    Empty(String),
    Backslash(String),
    Segment { raw: String, segment: String },
}

impl std::fmt::Display for ModulePathError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Empty(raw) => write!(f, "module path is empty: {raw:?}"),
            Self::Backslash(raw) => {
                write!(f, "module path must use '/' separators, got {raw:?}")
            }
            Self::Segment { raw, segment } => {
                write!(f, "invalid module path segment {segment:?} in {raw:?}")
            }
        }
    }
}

impl std::error::Error for ModulePathError {}

#[cfg(test)]
mod module_path_tests {
    use super::ModulePath;

    #[test]
    fn chunk_prefixed_and_clean_spellings_parse_equal() {
        // The two-spelling bug: production reports spell a module
        // `<chunk>::<path>` while active claims spell it `<path>`.
        // Both must normalize to the same identity so `==` is honest.
        let chunk = "static/index-DI2GynTv";
        let prefixed =
            ModulePath::parse("static/index-DI2GynTv::domains/system/ids", chunk).unwrap();
        let clean = ModulePath::parse("domains/system/ids", chunk).unwrap();
        assert_eq!(prefixed, clean);
        assert_eq!(clean.as_str(), "domains/system/ids");
    }

    #[test]
    fn normalizes_case_and_surrounding_slashes() {
        let p = ModulePath::parse("/Domains/System/IDs/", "").unwrap();
        assert_eq!(p.as_str(), "domains/system/ids");
    }

    #[test]
    fn dest_file_appends_extension() {
        let p = ModulePath::parse("domains/system/ids", "").unwrap();
        assert_eq!(p.dest_file("js"), "domains/system/ids.js");
    }

    #[test]
    fn residual_subtree_detected() {
        assert!(ModulePath::parse("residual", "").unwrap().is_residual());
        assert!(
            ModulePath::parse("residual/unhandled", "")
                .unwrap()
                .is_residual()
        );
        assert!(!ModulePath::parse("ui/residual", "").unwrap().is_residual());
    }

    #[test]
    fn rejects_traversal_and_backslashes() {
        assert!(ModulePath::parse("a/../b", "").is_err());
        assert!(ModulePath::parse("a\\b", "").is_err());
        assert!(ModulePath::parse("", "").is_err());
        assert!(ModulePath::parse("///", "").is_err());
    }
}

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
    #[serde(skip_serializing_if = "is_default_member_effect")]
    #[serde(default)]
    pub effect: MemberEffect,
    /// Property names on the bound value whose calls (`<binding>.<name>(args)`)
    /// the author asserts have no observable side effects when their arguments
    /// classify pure. Targets the vendor-namespace shape — a star-import or
    /// renamed binding standing in for a vendor module like React, where
    /// member calls (`React.forwardRef`, `React.memo`, `React.lazy`,
    /// `React.createContext`) are pure under the same author-trust contract
    /// as `purity: pure` extends to direct calls of the bound Ident.
    ///
    /// Static identifier-property access only — `<binding>.<name>(...)` and
    /// `<binding>?.<name>(...)`. Computed access (`<binding>[expr](...)`),
    /// chained property access (`<binding>.x.y(...)`), shadowed bindings, and
    /// non-Ident receivers fall through to the regular classifier path.
    ///
    /// See AGENTS.md "Declared purity" for the soundness contract — the
    /// validator does not re-verify; soundness shifts to the spec author.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    #[serde(default)]
    pub pure_members: Vec<String>,
    /// Optional human-readable comment emitted as a `// ...` block
    /// immediately above the binding's owner statement in the
    /// generated JS. Per-line literal `// ` prefix; empty input
    /// lines emit as `//`. See `CLI.md` § "per-member and
    /// module-level `comment:` fields".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comment: Option<String>,
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
    /// sites. See AGENTS.md "Declared purity" + docs/design.md A9.
    Pure,
    /// Author asserts that `new <binding>(...)` has no observable side
    /// effects beyond evaluating its constructor arguments. The analyzer
    /// still requires every argument expression to classify pure; this
    /// annotation does not affect ordinary `<binding>(...)` calls.
    PureNew,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Default, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum MemberEffect {
    #[default]
    Default,
    /// Author asserts that recognized calls to the bound helper have a
    /// TypeScript `__decorate`-style target-local mutation effect.
    /// The analyzer shape-checks the call and models it as a local
    /// effect on the target class/prototype instead of as a global
    /// side-effect-order edge. See docs/design.md A10.
    TypescriptDecorateHelper,
}
