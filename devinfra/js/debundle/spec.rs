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

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
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
    /// Author-asserted purity of a chunk's exports, keyed by the
    /// *defining* chunk. The cross-module purity oracle seeds these as
    /// trusted axioms (pinned `Pure`, exempt from fixpoint demotion) and
    /// propagates them to every importing chunk — the assertion is made
    /// once, where the audited code lives, not per consumer. Same trust
    /// contract as member-level `purity: pure` (docs/design.md A9):
    /// an incorrect assertion can produce a buggy debundle; soundness
    /// shifts to the spec author.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub chunk_export_purity: BTreeMap<String, ChunkExportPurity>,

    // --- per-stage configuration ---
    /// Output configuration for vendor swap emission outputs (wrappers,
    /// facade bundles, the combined manifest); this field only adds
    /// output paths and a `write` toggle. All inner fields have
    /// defaults, so omitting `swap_vendor_chunks` is identical to
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

/// Author-asserted purity for one defining chunk's exports. See
/// [`TransformSpec::chunk_export_purity`].
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ChunkExportPurity {
    /// Export names whose *calls* the author asserts have no observable
    /// side effects. Applies to the export regardless of how the binding
    /// is produced (function declaration, function-valued const, interop
    /// wrapper), so it also covers callables the static classifier cannot
    /// see into.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub pure_exports: BTreeSet<String>,
    /// Member names on an exported namespace-like object whose *calls*
    /// the author asserts have no observable side effects, keyed by the
    /// export name. Covers CJS-interop namespace exports whose factories
    /// are reached as member calls at the importer (`ns.forwardRef(...)`):
    /// each importing chunk's local binding for the export receives the
    /// member set, feeding the same member-call trust arm as a spec
    /// member's `purity: pure` companion `declared_pure_members`.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub pure_members: BTreeMap<String, BTreeSet<String>>,
    /// Export names the author asserts are **deeply** pure fluent-API
    /// roots: member reads and calls on the export, *and on every value
    /// transitively derived from it* through static member reads and
    /// calls, have no observable side effects (call arguments are still
    /// classified normally). This is the only surface that reaches
    /// builder-style chains whose receivers are call results rather
    /// than bindings — `z.object({...}).optional().describe(...)` —
    /// which `pure_exports`/`pure_members` (binding-keyed) cannot.
    ///
    /// The assertion covers the API's **entire** transitive fluent
    /// surface, including methods like a schema's `.parse(...)` that
    /// run author-registered callbacks — assert only exports whose
    /// derived-value methods are all side-effect-free when invoked at
    /// module top level, and whose values the program does not
    /// monkey-patch. Same author-trust contract as `pure_exports`
    /// (docs/design.md A9).
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub fluent_exports: BTreeSet<String>,
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
    /// Author-trusted refinement for chunks using
    /// `dataflow_aware_s_chain`: let statements whose ordinary
    /// dataflow summary is conservative-but-present use that syntactic
    /// read/write summary instead of becoming opaque S-chain barriers.
    ///
    /// This preserves the conservative default added for generic JS.
    /// Enabling it shifts the same review burden as other
    /// conditionally-correct options to the spec author: every such
    /// top-level statement must be audited either to have no observable
    /// order dependency with unrelated top-level effects, or to expose
    /// all ordering-relevant state through the analyzer's binding/global
    /// property summaries. Shapes that defeat write-cell extraction
    /// outright (`eval`, `with`, `Function`, computed global-object
    /// keys, global `defineProperty`, global `Proxy`) still fall back to
    /// conservative barriers regardless of this flag.
    #[serde(default)]
    pub trusted_dataflow_summaries: bool,
    /// Input-chunk admission checks (docs/design.md A1/A3/A5) the
    /// spec author has audited and explicitly disabled for this
    /// chunk. YAML surface is a list of check names
    /// (`admission_overrides: [a1_eval, a5_import_meta]`). Every configured
    /// override prints a one-line notice when it suppresses a
    /// violation, and a redundant-override warning when it no longer
    /// suppresses anything.
    #[serde(default, skip_serializing_if = "AdmissionOverrides::is_empty")]
    pub admission_overrides: AdmissionOverrides,
    /// Classify whole-statement local property writes —
    /// `X.prop = <pure-rhs>;` (or a comma-sequence of such) where `X`
    /// is a chunk-top declared binding — as a *local effect on `X`*
    /// instead of a globally-ordered side effect. The statement leaves
    /// the S-chain and instead gets a bidirectional `LocalEffect` edge
    /// to `X`'s declaring statement, forcing co-location with the
    /// declaration (so the write still cannot be split away from `X`).
    /// This is the React annotation idiom — `C.displayName = "…"`,
    /// `C.defaultProps = {…}` — which is otherwise an
    /// `assign_or_update` impurity that chains otherwise-unrelated
    /// statements into one atomic unit.
    ///
    /// **Soundness precondition (author-audited):** all writes to `X`
    /// co-locate with `X`'s declaration, and a cross-module reader of
    /// `X` observes them only after `X`'s module fully initializes
    /// (ESM import ordering) — i.e. the reader sees the *post-write*
    /// value. That is behavior-preserving exactly when no top-level
    /// statement destined to a different module reads the written
    /// property *textually before* the write in the original chunk.
    /// Annotation writes placed directly after the declaration they
    /// annotate (the idiom this targets) satisfy this by construction;
    /// a chunk that read-then-writes an annotated binding's property
    /// across destinations does not, and must not enable this flag.
    /// Statements containing anything beyond such writes (compound
    /// assignment, computed non-literal keys, `__proto__` segments,
    /// impure RHS, non-chunk-top targets) keep the conservative
    /// classification regardless of this flag.
    #[serde(default)]
    pub local_property_effects: bool,
}

/// One named input-chunk admission check, identified by the
/// docs/design.md assumption it enforces. Used both as the
/// spec-override list-element type and as the violation tag in
/// admission diagnostics.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AdmissionCheck {
    /// A1: no `eval(...)` / `(0, eval)(...)` at module top level.
    A1Eval,
    /// A3: no dynamic `import()` of a debundled internal module
    /// (same-chunk literal target, or non-literal specifier at
    /// module top level).
    A3DynamicImport,
    /// A5 (partial): no `import.meta` reflection beyond
    /// `import.meta.url` at module top level.
    A5ImportMeta,
}

impl AdmissionCheck {
    /// The spec-facing snake_case name (the `admission_overrides`
    /// list element spelling), for diagnostics.
    pub fn spec_name(self) -> &'static str {
        match self {
            Self::A1Eval => "a1_eval",
            Self::A3DynamicImport => "a3_dynamic_import",
            Self::A5ImportMeta => "a5_import_meta",
        }
    }
}

impl fmt::Display for AdmissionCheck {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.spec_name())
    }
}

/// Per-chunk escape hatch for the input-chunk admission scan: the set
/// of checks disabled for an audited chunk. Serialized as a list of
/// [`AdmissionCheck`] names so the YAML reads as
/// `admission_overrides: [a1_eval, ...]`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(from = "Vec<AdmissionCheck>", into = "Vec<AdmissionCheck>")]
pub struct AdmissionOverrides {
    pub a1_eval: bool,
    pub a3_dynamic_import: bool,
    pub a5_import_meta: bool,
}

impl AdmissionOverrides {
    pub fn is_empty(&self) -> bool {
        *self == Self::default()
    }

    pub fn contains(&self, check: AdmissionCheck) -> bool {
        match check {
            AdmissionCheck::A1Eval => self.a1_eval,
            AdmissionCheck::A3DynamicImport => self.a3_dynamic_import,
            AdmissionCheck::A5ImportMeta => self.a5_import_meta,
        }
    }

    pub fn iter(&self) -> impl Iterator<Item = AdmissionCheck> + '_ {
        [
            AdmissionCheck::A1Eval,
            AdmissionCheck::A3DynamicImport,
            AdmissionCheck::A5ImportMeta,
        ]
        .into_iter()
        .filter(|check| self.contains(*check))
    }
}

impl From<Vec<AdmissionCheck>> for AdmissionOverrides {
    fn from(checks: Vec<AdmissionCheck>) -> Self {
        let mut overrides = Self::default();
        for check in checks {
            match check {
                AdmissionCheck::A1Eval => overrides.a1_eval = true,
                AdmissionCheck::A3DynamicImport => overrides.a3_dynamic_import = true,
                AdmissionCheck::A5ImportMeta => overrides.a5_import_meta = true,
            }
        }
        overrides
    }
}

impl From<AdmissionOverrides> for Vec<AdmissionCheck> {
    fn from(overrides: AdmissionOverrides) -> Self {
        overrides.iter().collect()
    }
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
    /// Upstream named exports the author asserts are aliases of the
    /// swapped package's default export. The `named_from_module_default`
    /// soundness check normally verifies, from the upstream chunk alone,
    /// that each named export shares a local binding with the chunk's own
    /// `default` export. A chunk that re-exports the package default under
    /// a single minified name without also exporting it as `default`
    /// (`export { Ft as c }`) gives the static check nothing to anchor on,
    /// so it cannot prove the alias even when it holds. Listing the export
    /// name here records the author's verified assertion (e.g. confirmed by
    /// the binding's runtime identity/version) and admits it as a default
    /// alias. Only consulted for `wrapper_shape: named_from_module_default`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub default_export_aliases: Vec<String>,
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

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LogicalModule {
    #[serde(default)]
    pub members: Vec<Member>,
    /// Compact form for several bindings selected from the same source
    /// context, usually one multi-declarator statement. Each `exports` key is
    /// a selector-local binding name inside `source_match.match`; each value
    /// is the public export name to give the matched runtime binding.
    ///
    /// This is sugar for several `members[].selector.source_match` entries
    /// with identical `match` and different `target_binding` values.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub binding_groups: Vec<BindingGroup>,
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
    /// Optional YAML-only note: module-level provenance / honest-debt
    /// rationale (e.g. `merged from: …` provenance written by
    /// `modules merge`) that survives spec edits without appearing in
    /// generated JS. Ignored by the lowering pass — unlike `comment:`,
    /// which emits a `//` block. The module-level counterpart of
    /// `Member.note` / `BindingGroup.note`; same non-emitting contract
    /// and STYLE.md local exemption (see AGENTS.md "Spec `note:` field").
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
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
    #[serde(rename = "match", default, skip_serializing_if = "Option::is_none")]
    pub match_source: Option<String>,
    /// Readable structural selector for the target top-level statement.
    /// `source_match` treats binding/value identifiers in `match` as
    /// alpha-renamable placeholders while keeping literals, operators, member
    /// property names, and overall AST structure significant.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_match: Option<SourceMatch>,
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

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct BindingGroup {
    pub source_match: SourceMatch,
    /// Selector-local names from `source_match.match` to export under the
    /// same readable names. This is only sugar for filling `exports`; it does
    /// not adopt transitive ownership or co-move unnamed statements.
    #[serde(default, skip_serializing_if = "BindingGroupAdoptNames::is_none")]
    pub adopt_names: BindingGroupAdoptNames,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub exports: BTreeMap<String, String>,
    /// Optional per-binding comments keyed by selector-local binding name.
    /// These are equivalent to `members[].comment` after the binding group is
    /// expanded, but stay attached to the structural binding selected by the
    /// group even when `exports` renames it.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub comments: BTreeMap<String, String>,
    /// Optional YAML-only note: provenance / honest-debt rationale (e.g. why a
    /// binding group's selector has no forward-stable anchor yet) that survives
    /// spec edits without appearing in generated JS. Ignored by the materializer
    /// — unlike `comments`, which emit. Same non-emitting contract and STYLE.md
    /// local exemption as `Member.note`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Default)]
#[serde(untagged)]
pub enum BindingGroupAdoptNames {
    #[default]
    None,
    All(bool),
    Names(Vec<String>),
}

impl BindingGroupAdoptNames {
    pub fn is_none(&self) -> bool {
        matches!(
            self,
            BindingGroupAdoptNames::None | BindingGroupAdoptNames::All(false)
        )
    }
}

impl AnonymousStatement {
    pub fn selector(
        &self,
    ) -> std::result::Result<AnonymousStatementSelector, AnonymousStatementSelectorError> {
        match (&self.match_source, &self.source_match) {
            (Some(match_source), None) => Ok(AnonymousStatementSelector {
                match_source: match_source.clone(),
                identifiers: SourceMatchIdentifierMode::Exact,
                target_binding: None,
                wildcard_string_literals: BTreeSet::new(),
            }),
            (None, Some(source_match)) if source_match.target_binding.is_some() => {
                Err(AnonymousStatementSelectorError {
                    message: "anonymous_statements source_match cannot include `target_binding`",
                })
            }
            (None, Some(source_match)) => Ok(source_match.selector()),
            (Some(_), Some(_)) => Err(AnonymousStatementSelectorError {
                message: "anonymous_statements entry must use either `match` or `source_match`, not both",
            }),
            (None, None) => Err(AnonymousStatementSelectorError {
                message: "anonymous_statements entry must include either `match` or `source_match.match`",
            }),
        }
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct AnonymousStatementSelectorError {
    message: &'static str,
}

impl fmt::Display for AnonymousStatementSelectorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.message)
    }
}

impl std::error::Error for AnonymousStatementSelectorError {}

#[derive(Debug, Clone, Serialize, Eq, PartialEq, Ord, PartialOrd)]
pub struct SourceMatch {
    #[serde(
        default = "default_source_match_identifier_mode",
        skip_serializing_if = "is_default_source_match_identifier_mode"
    )]
    pub identifiers: SourceMatchIdentifierMode,
    /// Selector-local binding name to export when this source match is used as
    /// a `members[].selector.source_match`. This lets a selector use a whole
    /// multi-declarator statement as readable context while choosing one
    /// binding from it. Invalid on `anonymous_statements[].source_match`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_binding: Option<String>,
    /// Internal-only string-literal placeholder values used by legacy matcher
    /// tests. The public YAML surface no longer accepts or emits this field.
    #[serde(skip)]
    pub wildcard_string_literals: BTreeSet<String>,
    #[serde(rename = "match")]
    pub match_source: String,
}

#[derive(Deserialize)]
struct SourceMatchWire {
    #[serde(default)]
    identifiers: Option<SourceMatchIdentifierMode>,
    #[serde(default)]
    target_binding: Option<String>,
    #[serde(rename = "match")]
    match_source: String,
    #[serde(flatten)]
    unsupported_fields: BTreeMap<String, serde::de::IgnoredAny>,
}

impl<'de> Deserialize<'de> for SourceMatch {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let wire = SourceMatchWire::deserialize(deserializer)?;
        if !wire.unsupported_fields.is_empty() {
            let fields = wire
                .unsupported_fields
                .keys()
                .map(|field| format!("`{field}`"))
                .collect::<Vec<_>>()
                .join(", ");
            return Err(serde::de::Error::custom(format!(
                "unsupported selector capability: source_match field(s) {fields} are not \
                 supported by this debundler; upgrade the debundler or remove the field(s)"
            )));
        }
        let identifiers = match wire.identifiers {
            None | Some(SourceMatchIdentifierMode::AlphaAll) => SourceMatchIdentifierMode::AlphaAll,
            Some(SourceMatchIdentifierMode::Exact) => {
                return Err(serde::de::Error::custom(
                    "source_match identifiers: exact is no longer supported; omit `identifiers` \
                     or use `alpha_all`",
                ));
            }
        };
        Ok(Self {
            identifiers,
            target_binding: wire.target_binding,
            wildcard_string_literals: BTreeSet::new(),
            match_source: wire.match_source,
        })
    }
}

impl SourceMatch {
    pub fn selector(&self) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            match_source: self.match_source.clone(),
            identifiers: self.identifiers,
            target_binding: self.target_binding.clone(),
            wildcard_string_literals: self.wildcard_string_literals.clone(),
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub struct AnonymousStatementSelector {
    pub match_source: String,
    pub identifiers: SourceMatchIdentifierMode,
    pub target_binding: Option<String>,
    pub wildcard_string_literals: BTreeSet<String>,
}

impl AnonymousStatementSelector {
    pub fn exact(match_source: impl Into<String>) -> Self {
        Self {
            match_source: match_source.into(),
            identifiers: SourceMatchIdentifierMode::Exact,
            target_binding: None,
            wildcard_string_literals: BTreeSet::new(),
        }
    }
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Default, Eq, PartialEq, Ord, PartialOrd)]
#[serde(rename_all = "snake_case")]
pub enum SourceMatchIdentifierMode {
    /// Internal exact identifier constraints for native lowering / solver tests.
    /// Public `SourceMatch` deserialization rejects this spelling.
    Exact,
    /// The public `source_match` identifier policy.
    #[default]
    AlphaAll,
}

fn default_source_match_identifier_mode() -> SourceMatchIdentifierMode {
    SourceMatchIdentifierMode::AlphaAll
}

fn is_default_source_match_identifier_mode(mode: &SourceMatchIdentifierMode) -> bool {
    *mode == default_source_match_identifier_mode()
}

/// Default value the [`UnassignedMode::CatchallFile`] target path
/// falls back to when the spec author omits it. SSOT consumed by
/// the materializer (`logical_modules` residual synthesis) and by
/// analysis tools that want to match the canonical residual
/// catch-all path.
pub const DEFAULT_RESIDUAL_MODULE_PATH: &str = "residual/unhandled";

/// The single, canonical identity of a logical module: **relative,
/// slash-separated, lowercase** (e.g. `domains/system/ids`), equal to
/// the module's destination path.
///
/// [`parse`] is the only constructor, so the several historical
/// spellings of one module (clean spec path, chunk-prefixed
/// `LogicalModule.id`, report label) collapse to one value and `==` is
/// an honest identity test.
///
/// [`parse`]: ModulePath::parse
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
#[serde(transparent)]
pub struct ModulePath(String);

/// Deserialization routes through [`ModulePath::parse`] (with no
/// chunk-id prefix to strip) so wire data can't construct a
/// non-canonical value — `parse` stays the only constructor.
impl<'de> serde::Deserialize<'de> for ModulePath {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let raw = String::deserialize(deserializer)?;
        ModulePath::parse(&raw, "").map_err(serde::de::Error::custom)
    }
}

impl ModulePath {
    /// Normalize an untrusted module identifier into canonical form.
    ///
    /// A leading `"<chunk_id>::"` (the production `LogicalModule.id`
    /// spelling) is stripped so it collapses onto the clean path; pass
    /// `""` for `chunk_id` when the value is already a clean path.
    /// Rejects what can't be a relative module path: backslashes,
    /// absolute paths, empty segments, and `.`/`..` traversal.
    pub fn parse(raw: &str, chunk_id: &str) -> Result<Self, ModulePathError> {
        let stripped = if chunk_id.is_empty() {
            raw
        } else {
            raw.strip_prefix(chunk_id)
                .and_then(|rest| rest.strip_prefix("::"))
                .unwrap_or(raw)
        };
        let err = || ModulePathError(raw.to_string());
        if stripped.contains('\\') {
            return Err(err());
        }
        let trimmed = stripped.to_ascii_lowercase().trim_matches('/').to_string();
        if trimmed.is_empty() {
            return Err(err());
        }
        if trimmed.split('/').any(|s| matches!(s, "" | "." | "..")) {
            return Err(err());
        }
        Ok(Self(trimmed))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// True for the residual catch-all subtree (`residual` or
    /// `residual/...`); matches `spec_modules::is_residual_module_path`.
    pub fn is_residual(&self) -> bool {
        self.0 == "residual" || self.0.starts_with("residual/")
    }
}

impl std::fmt::Display for ModulePath {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Carries the offending raw input; module paths only fail validation
/// in one way from the caller's view (the value isn't a valid relative
/// path), so no caller branches on a reason.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModulePathError(String);

impl std::fmt::Display for ModulePathError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "not a valid relative module path: {:?}", self.0)
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
    /// Property names on the bound value whose calls may receive callback-like
    /// arguments but do not synchronously invoke them. The call remains an
    /// opaque side-effecting member call for purity / S-chain purposes; this
    /// only narrows at-init call promotion by not treating inline functions,
    /// object literals containing functions, or first-order argument callbacks
    /// as synchronously reachable fallback roots for audited callback-storing
    /// APIs.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    #[serde(default)]
    pub no_sync_callback_members: Vec<String>,
    /// Optional YAML-only note: provenance / honest-debt rationale (e.g. why a
    /// name pin has no forward-stable anchor yet) that survives spec edits
    /// without appearing in generated JS. Ignored by the materializer — unlike
    /// `comment:`, which emits. Use for annotations that must NOT change
    /// byte-identical output. (Ratified STYLE.md exemption — see AGENTS.md
    /// "Spec `note:` field".)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding: Option<BindingSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_match: Option<SourceMatch>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cross_ref: Option<CrossRefSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reads_member: Option<ReadsMemberSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub member_of_module: Option<MemberOfModuleSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub passed_to_call: Option<PassedToCallSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub makes_decorate_call: Option<MakesDecorateCallSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub intrinsic_alias: Option<IntrinsicAliasSelector>,
}

/// Pin a member by a **cross-reference** to another spec member instead of by
/// this member's own (re-minify-fragile) minified name. The anchor names another
/// member by its readable name; the target is resolved through the owner graph as
/// the entity standing in the named relation to the anchor's resolved binding —
/// e.g. a shapeless delegator `function T(x){ return Anchor(x) }` pinned as "the
/// function that references @Anchor". Exactly one of `references` / `aliases`.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct CrossRefSelector {
    /// The target references the anchor member (a delegator / consumer body).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub references: Option<String>,
    /// The target aliases the anchor member (`const T = Anchor`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub aliases: Option<String>,
    /// Optional statement-kind constraint disambiguating when several owners
    /// stand in the relation to the anchor (e.g. `function_declaration`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<BindingSourceKind>,
}

/// The validated cross-reference target (`MemberSelector::selected` resolves the
/// `references`/`aliases` one-of into this).
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub struct CrossRefTarget {
    pub relation: CrossRefRelation,
    pub anchor: String,
    pub kind: Option<BindingSourceKind>,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd)]
pub enum CrossRefRelation {
    References,
    Aliases,
}

impl CrossRefSelector {
    fn target(&self) -> std::result::Result<CrossRefTarget, MemberSelectorError> {
        let (relation, anchor) = match (&self.references, &self.aliases) {
            (Some(anchor), None) => (CrossRefRelation::References, anchor.clone()),
            (None, Some(anchor)) => (CrossRefRelation::Aliases, anchor.clone()),
            (None, None) => {
                return Err(MemberSelectorError {
                    message: "members[].selector.cross_ref must include `references` or `aliases`",
                });
            }
            (Some(_), Some(_)) => {
                return Err(MemberSelectorError {
                    message: "members[].selector.cross_ref must use either `references` or \
                              `aliases`, not both",
                });
            }
        };
        Ok(CrossRefTarget {
            relation,
            anchor,
            kind: self.kind,
        })
    }
}

/// Pin a member by the **member it reads** off an object, instead of by this
/// member's own (re-minify-fragile) minified name. The canonical shape is a TS
/// codegen helper `function ls(c){ return c.uniqueId }` whose stable identity is
/// "the function that reads `.uniqueId` off the codegen context" — pinned by the
/// invariant property name `.uniqueId` (and optionally the object it reads off),
/// never by the minified `ls`. Resolved through the owner graph's `reads_member`
/// EDB: the unique declaring owner whose body reads the named member.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct ReadsMemberSelector {
    /// The property name `X` the target reads (`obj.X`). Required — the relation
    /// is "reads member `.member`".
    pub member: String,
    /// Optional object constraint: the readable `name:` of another member the
    /// property is read off (`@object.member`). Narrows "the owner that reads
    /// `.X`" to "the owner that reads `.X` **off `@object`**" — the codegen
    /// context being the canonical object. Resolved like a `cross_ref` anchor:
    /// the object's already-resolved minified binding rides the relational edge.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub object: Option<String>,
    /// Optional statement-kind constraint disambiguating when several owners read
    /// the member (e.g. `function_declaration`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<BindingSourceKind>,
}

/// The validated `reads_member` target (`MemberSelector::selected` resolves the
/// selector into this). `member` is always present; `object`/`kind` narrow it.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub struct ReadsMemberTarget {
    pub member: String,
    pub object: Option<String>,
    pub kind: Option<BindingSourceKind>,
}

impl ReadsMemberSelector {
    fn target(&self) -> ReadsMemberTarget {
        ReadsMemberTarget {
            member: self.member.clone(),
            object: self.object.clone(),
            kind: self.kind,
        }
    }
}

/// Pin a member by **how it is consumed at a use site** — the export consumed as
/// `mod.member`, where `mod` is a binding imported from `module` — instead of by
/// this member's own (re-minify-fragile) minified name. This is the first
/// *use-site* selector: it rides the import/use graph rather than the target's
/// own body. The canonical shapes are the empty-class/superclass cluster
/// (`class Uee extends Ye {}`, several byte-identical empty subclasses
/// distinguished only by *how each is consumed*) and shapeless delegators with no
/// internal anchor. Both `module` (an import specifier) and `member` (an export
/// name) are re-minify-invariant, so the whole edge survives a bundle rebuild.
/// Resolved through the owner graph's `member_of_module` EDB: the unique declaring
/// owner whose body consumes `<module>.<member>`.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct MemberOfModuleSelector {
    /// The import **source specifier** the consumed binding is imported from
    /// (`"./codegen"`, `"react"`). Required — half of the invariant "consumed as
    /// `module.member`" identity.
    pub module: String,
    /// The export **name** consumed off the imported binding (`mod.member`).
    /// Required — the other half of the identity.
    pub member: String,
    /// Optional statement-kind constraint disambiguating when several owners
    /// consume the module member (e.g. `class_declaration` for the empty-subclass
    /// cluster).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<BindingSourceKind>,
}

/// The validated `member_of_module` target (`MemberSelector::selected` resolves
/// the selector into this). `module`/`member` are always present; `kind` narrows.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub struct MemberOfModuleTarget {
    pub module: String,
    pub member: String,
    pub kind: Option<BindingSourceKind>,
}

impl MemberOfModuleSelector {
    fn target(&self) -> MemberOfModuleTarget {
        MemberOfModuleTarget {
            module: self.module.clone(),
            member: self.member.clone(),
            kind: self.kind,
        }
    }
}

/// Pin a member by it being **passed as an argument** to a call of a known callee
/// — the `resolves_to`-of-argument primitive — instead of by this member's own
/// (re-minify-fragile) minified name. The canonical shape is a registry-style
/// target: a top-level `class FooAccessor {}` (often empty or otherwise shapeless)
/// whose only stable identity is an external `registry.register(FooAccessor)`
/// statement. The target *is the argument*; the call that names it lives elsewhere.
/// This is the inverse direction of [`MemberOfModuleSelector`], which pins the
/// owner whose *own* subtree consumes `mod.X` and explicitly cannot reach a target
/// distinguished only by such an external registration. `callee_member` (the call's
/// method name) and the registry's own stable identity are re-minify-invariant, so
/// the edge survives a bundle rebuild. Resolved through the owner graph's
/// `passed_to_call` EDB: the unique declaring owner whose binding is the call
/// argument.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct PassedToCallSelector {
    /// The callee **member name** `.method` of the call the target is passed to
    /// (`registry.register(Target)` ⟹ `callee_member: register`). Required — the
    /// relation is "passed to a call of `.callee_member`".
    pub callee_member: String,
    /// Optional callee **object** constraint: the readable `name:` of another
    /// member that is the call's receiver (`@registry.register(...)`). Narrows "the
    /// target passed to `.register`" to "the target passed to `@registry.register`"
    /// — the registry singleton being the canonical object. Resolved like a
    /// `cross_ref` / `reads_member` anchor: the object's already-resolved minified
    /// binding rides the relational edge.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub object: Option<String>,
    /// Optional **argument position** constraint (0-based): which argument of the
    /// call the target occupies (`h.define("widget", Target)` ⟹ `arg_index: 1`).
    /// Absent ⟹ any position. Narrows when one callee takes the target at a fixed
    /// slot alongside other arguments.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub arg_index: Option<usize>,
    /// Optional statement-kind constraint disambiguating when several owners are
    /// passed to the callee member (e.g. `class_declaration` for the registry
    /// empty-subclass cluster).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<BindingSourceKind>,
}

/// The validated `passed_to_call` target (`MemberSelector::selected` resolves the
/// selector into this). `callee_member` is always present; `object`/`arg_index`/
/// `kind` narrow it.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub struct PassedToCallTarget {
    pub callee_member: String,
    pub object: Option<String>,
    pub arg_index: Option<usize>,
    pub kind: Option<BindingSourceKind>,
}

impl PassedToCallSelector {
    fn target(&self) -> PassedToCallTarget {
        PassedToCallTarget {
            callee_member: self.callee_member.clone(),
            object: self.object.clone(),
            arg_index: self.arg_index,
            kind: self.kind,
        }
    }
}

/// `makes_decorate_call` EDB: the unique declaring owner whose binding is the
/// **callee** of an esbuild/TypeScript `__decorate`-style decorator application.
/// The inverse direction of `passed_to_call` (the target *makes* the call rather
/// than being *passed to* it). The byte-identical-across-modules helper copies have
/// no anchor in their own body; this selector pins each by the **class it
/// decorates** — a separately-pinned entity reached through `resolves_to` — so the
/// edge survives a rebuild while the helper's minified name does not.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct MakesDecorateCallSelector {
    /// The decorated class: the readable `name:` of another member that is the base
    /// of the decorator application's 2nd argument (`C.prototype` or bare `C`).
    /// Required — it is the re-minify-invariant anchor the selector rides ("the
    /// helper that decorates `@ClassAnchor`"). Resolved like a `passed_to_call`
    /// object anchor: the class's already-resolved minified binding rides the edge.
    pub class: String,
    /// Optional decorated **member name** (the 3rd-argument string literal,
    /// `"isVisible"`). Narrows "the helper that decorates `@C`" to "the helper that
    /// decorates `@C`'s `isVisible`" when needed. Absent ⟹ any decorated member of
    /// the class (the common case — one helper decorates many members of one class).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub member: Option<String>,
    /// Optional statement-kind constraint. The esbuild decorate helper is always a
    /// `variable_declarator`; the constraint narrows past any non-var owner that
    /// spuriously shares the class anchor on a full bundle.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<BindingSourceKind>,
}

/// The validated `makes_decorate_call` target (`MemberSelector::selected` resolves
/// the selector into this). `class` is always present; `member`/`kind` narrow it.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub struct MakesDecorateCallTarget {
    pub class: String,
    pub member: Option<String>,
    pub kind: Option<BindingSourceKind>,
}

impl MakesDecorateCallSelector {
    fn target(&self) -> MakesDecorateCallTarget {
        MakesDecorateCallTarget {
            class: self.class.clone(),
            member: self.member.clone(),
            kind: self.kind,
        }
    }
}

/// Pin a member that is an **intrinsic-method alias off the unshadowed global
/// `Object`** (`var X = Object.defineProperty` / `var X =
/// Object.getOwnPropertyDescriptor`) by the helper that **references** it, instead
/// of by this member's own (re-minify-fragile) minified name. These are the esbuild
/// decorate-trio's two companions: byte-identical across modules, with no anchor in
/// their own body — no `source_match` can pin them (N identical copies, the anchor
/// is the global `Object`, not a spec member). The one re-minify-invariant edge each
/// carries is that it is read **only inside** its trio's `__decorate` helper body, so
/// the selector pairs the structural recognition of `var X = Object.<property>` with
/// an inverse-`references` edge to the helper. `referenced_by` names that helper —
/// now a stable `@Name` because `makes_decorate_call` pins it; the intrinsic
/// `property` is a spec-level method name the bundler does not rewrite. Resolved
/// through the owner graph's `intrinsic_alias` EDB: the unique declaring owner whose
/// binding is the `Object.<property>` alias referenced by the helper.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct IntrinsicAliasSelector {
    /// The intrinsic method name aliased off the global `Object` (`defineProperty`,
    /// `getOwnPropertyDescriptor`). Required — the re-minify-invariant label half of
    /// the relation ("the `Object.<property>` alias").
    pub property: String,
    /// The helper that references this alias: the readable `name:` of another member
    /// (the trio's `__decorate` helper, itself pinned by `makes_decorate_call`).
    /// Required — the disambiguating anchor that picks the unique copy among the N
    /// byte-identical aliases. Resolved like a `makes_decorate_call` class anchor:
    /// the helper's already-resolved minified binding rides the `references` edge.
    pub referenced_by: String,
}

/// The validated `intrinsic_alias` target (`MemberSelector::selected` resolves the
/// selector into this). Both fields are always present.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub struct IntrinsicAliasTarget {
    pub property: String,
    pub referenced_by: String,
}

impl IntrinsicAliasSelector {
    fn target(&self) -> IntrinsicAliasTarget {
        IntrinsicAliasTarget {
            property: self.property.clone(),
            referenced_by: self.referenced_by.clone(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct BindingSelector {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub kind: Option<BindingSourceKind>,
}

impl MemberSelector {
    pub fn selected(&self) -> std::result::Result<MemberSelectorSpec, MemberSelectorError> {
        match (
            &self.binding,
            &self.source_match,
            &self.cross_ref,
            &self.reads_member,
            &self.member_of_module,
            &self.passed_to_call,
            &self.makes_decorate_call,
            &self.intrinsic_alias,
        ) {
            (Some(binding), None, None, None, None, None, None, None) => {
                Ok(MemberSelectorSpec::Binding(binding.clone()))
            }
            (None, Some(source_match), None, None, None, None, None, None) => {
                Ok(MemberSelectorSpec::SourceMatch(source_match.selector()))
            }
            (None, None, Some(cross_ref), None, None, None, None, None) => {
                cross_ref.target().map(MemberSelectorSpec::CrossRef)
            }
            (None, None, None, Some(reads_member), None, None, None, None) => {
                Ok(MemberSelectorSpec::ReadsMember(reads_member.target()))
            }
            (None, None, None, None, Some(member_of_module), None, None, None) => Ok(
                MemberSelectorSpec::MemberOfModule(member_of_module.target()),
            ),
            (None, None, None, None, None, Some(passed_to_call), None, None) => {
                Ok(MemberSelectorSpec::PassedToCall(passed_to_call.target()))
            }
            (None, None, None, None, None, None, Some(makes_decorate_call), None) => Ok(
                MemberSelectorSpec::MakesDecorateCall(makes_decorate_call.target()),
            ),
            (None, None, None, None, None, None, None, Some(intrinsic_alias)) => {
                Ok(MemberSelectorSpec::IntrinsicAlias(intrinsic_alias.target()))
            }
            (None, None, None, None, None, None, None, None) => Err(MemberSelectorError {
                message: "members[].selector must include one of `binding`, `source_match`, \
                          `cross_ref`, `reads_member`, `member_of_module`, `passed_to_call`, \
                          `makes_decorate_call`, or `intrinsic_alias`",
            }),
            _ => Err(MemberSelectorError {
                message: "members[].selector must use exactly one of `binding`, `source_match`, \
                          `cross_ref`, `reads_member`, `member_of_module`, `passed_to_call`, \
                          `makes_decorate_call`, or `intrinsic_alias`",
            }),
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub enum MemberSelectorSpec {
    Binding(BindingSelector),
    SourceMatch(AnonymousStatementSelector),
    CrossRef(CrossRefTarget),
    ReadsMember(ReadsMemberTarget),
    MemberOfModule(MemberOfModuleTarget),
    PassedToCall(PassedToCallTarget),
    MakesDecorateCall(MakesDecorateCallTarget),
    IntrinsicAlias(IntrinsicAliasTarget),
}

impl MemberSelectorSpec {
    /// The `members[].selector.<field>` key this selector deserializes from,
    /// used to label the claim origin and the `name:`-required diagnostic.
    pub fn selector_kind_label(&self) -> &'static str {
        match self {
            Self::Binding(_) => "binding",
            Self::SourceMatch(_) => "source_match",
            Self::CrossRef(_) => "cross_ref",
            Self::ReadsMember(_) => "reads_member",
            Self::MemberOfModule(_) => "member_of_module",
            Self::PassedToCall(_) => "passed_to_call",
            Self::MakesDecorateCall(_) => "makes_decorate_call",
            Self::IntrinsicAlias(_) => "intrinsic_alias",
        }
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct MemberSelectorError {
    message: &'static str,
}

impl fmt::Display for MemberSelectorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.message)
    }
}

impl std::error::Error for MemberSelectorError {}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
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

#[cfg(test)]
mod tests {
    use super::*;

    /// The YAML override list parses into the typed set and unknown
    /// check names are rejected (strict data mapping).
    #[test]
    fn admission_overrides_parse_from_list() {
        let options: OwnerGraphOptions =
            serde_json::from_str(r#"{ "admission_overrides": ["a1_eval", "a5_import_meta"] }"#)
                .unwrap();
        assert!(options.admission_overrides.contains(AdmissionCheck::A1Eval));
        assert!(
            options
                .admission_overrides
                .contains(AdmissionCheck::A5ImportMeta)
        );
        assert!(
            !options
                .admission_overrides
                .contains(AdmissionCheck::A3DynamicImport)
        );

        let invalid: Result<OwnerGraphOptions, _> =
            serde_json::from_str(r#"{ "admission_overrides": ["a99_bogus"] }"#);
        assert!(invalid.is_err(), "unknown admission check must be rejected");
    }

    /// Default (empty) overrides serialize away entirely so untouched
    /// specs round-trip without a spurious `admission_overrides: []`.
    #[test]
    fn empty_admission_overrides_are_skipped_on_serialize() {
        let serialized = serde_json::to_string(&OwnerGraphOptions::default()).unwrap();
        assert!(
            !serialized.contains("admission_overrides"),
            "default overrides must not serialize: {serialized}"
        );
    }

    #[test]
    fn source_match_unknown_field_reports_unsupported_selector_capability() {
        let error: serde_json::Error = serde_json::from_str::<SourceMatch>(
            r#"{
              "identifiers": "alpha_all",
              "match": "const readable = 1;",
              "object_props": true
            }"#,
        )
        .unwrap_err();
        let message = error.to_string();
        assert!(
            message.contains("unsupported selector capability"),
            "unexpected error: {message}"
        );
        assert!(
            message.contains("object_props"),
            "unexpected error: {message}"
        );
    }

    #[test]
    fn source_match_rejects_legacy_wildcard_string_literals() {
        let error: serde_json::Error = serde_json::from_str::<SourceMatch>(
            r#"{
              "identifiers": "alpha_all",
              "match": "const readable = \"TOKEN\";",
              "wildcard_string_literals": ["TOKEN"]
            }"#,
        )
        .unwrap_err();
        let message = error.to_string();
        assert!(
            message.contains("unsupported selector capability"),
            "unexpected error: {message}"
        );
        assert!(
            message.contains("wildcard_string_literals"),
            "unexpected error: {message}"
        );
    }

    #[test]
    fn source_match_defaults_to_alpha_all_identifiers() {
        let source_match: SourceMatch =
            serde_json::from_str(r#"{ "match": "const readable = runtime;" }"#).unwrap();
        assert_eq!(
            source_match.identifiers,
            SourceMatchIdentifierMode::AlphaAll
        );
    }

    #[test]
    fn source_match_rejects_exact_identifier_mode() {
        let error: serde_json::Error = serde_json::from_str::<SourceMatch>(
            r#"{
              "identifiers": "exact",
              "match": "const readable = runtime;"
            }"#,
        )
        .unwrap_err();
        let message = error.to_string();
        assert!(
            message.contains("identifiers: exact is no longer supported"),
            "unexpected error: {message}"
        );
    }

    #[test]
    fn cross_ref_references_selector_resolves_to_a_cross_ref_target() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "cross_ref": { "references": "isTranscriptionProvider", "kind": "function_declaration" } }"#,
        )
        .unwrap();
        assert_eq!(
            selector.selected().unwrap(),
            MemberSelectorSpec::CrossRef(CrossRefTarget {
                relation: CrossRefRelation::References,
                anchor: "isTranscriptionProvider".to_string(),
                kind: Some(BindingSourceKind::FunctionDeclaration),
            })
        );
    }

    #[test]
    fn cross_ref_aliases_selector_resolves_to_an_alias_target() {
        let selector: MemberSelector =
            serde_json::from_str(r#"{ "cross_ref": { "aliases": "NodeAttributeAccessor" } }"#)
                .unwrap();
        assert_eq!(
            selector.selected().unwrap(),
            MemberSelectorSpec::CrossRef(CrossRefTarget {
                relation: CrossRefRelation::Aliases,
                anchor: "NodeAttributeAccessor".to_string(),
                kind: None,
            })
        );
    }

    #[test]
    fn cross_ref_requires_exactly_one_relation() {
        let both: MemberSelector =
            serde_json::from_str(r#"{ "cross_ref": { "references": "A", "aliases": "B" } }"#)
                .unwrap();
        assert!(both.selected().is_err(), "both relations must be rejected");

        let neither: MemberSelector =
            serde_json::from_str(r#"{ "cross_ref": { "kind": "class_declaration" } }"#).unwrap();
        assert!(neither.selected().is_err(), "no relation must be rejected");
    }

    #[test]
    fn cross_ref_conflicts_with_other_selector_kinds() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "binding": { "name": "x" }, "cross_ref": { "references": "A" } }"#,
        )
        .unwrap();
        assert!(
            selector.selected().is_err(),
            "a member must use exactly one selector kind",
        );
    }

    #[test]
    fn cross_ref_unknown_field_is_rejected() {
        let result: std::result::Result<MemberSelector, _> =
            serde_json::from_str(r#"{ "cross_ref": { "references": "A", "calls": "B" } }"#);
        assert!(result.is_err(), "unknown cross_ref field must be rejected");
    }

    #[test]
    fn reads_member_selector_resolves_to_a_reads_member_target() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "reads_member": { "member": "uniqueId", "object": "codegenContext", "kind": "function_declaration" } }"#,
        )
        .unwrap();
        assert_eq!(
            selector.selected().unwrap(),
            MemberSelectorSpec::ReadsMember(ReadsMemberTarget {
                member: "uniqueId".to_string(),
                object: Some("codegenContext".to_string()),
                kind: Some(BindingSourceKind::FunctionDeclaration),
            })
        );
    }

    #[test]
    fn reads_member_selector_defaults_object_and_kind() {
        let selector: MemberSelector =
            serde_json::from_str(r#"{ "reads_member": { "member": "render" } }"#).unwrap();
        assert_eq!(
            selector.selected().unwrap(),
            MemberSelectorSpec::ReadsMember(ReadsMemberTarget {
                member: "render".to_string(),
                object: None,
                kind: None,
            })
        );
    }

    #[test]
    fn reads_member_requires_member() {
        let result: std::result::Result<MemberSelector, _> =
            serde_json::from_str(r#"{ "reads_member": { "object": "ctx" } }"#);
        assert!(result.is_err(), "reads_member without `member` is rejected");
    }

    #[test]
    fn reads_member_conflicts_with_other_selector_kinds() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "binding": { "name": "x" }, "reads_member": { "member": "id" } }"#,
        )
        .unwrap();
        assert!(
            selector.selected().is_err(),
            "a member must use exactly one selector kind",
        );
    }

    #[test]
    fn reads_member_unknown_field_is_rejected() {
        let result: std::result::Result<MemberSelector, _> =
            serde_json::from_str(r#"{ "reads_member": { "member": "id", "writes": "x" } }"#);
        assert!(
            result.is_err(),
            "unknown reads_member field must be rejected"
        );
    }

    #[test]
    fn member_accepts_and_round_trips_note() {
        // `note:` is a YAML-only annotation that must survive `deny_unknown_fields`
        // and round-trip intact (see `Member.note` for the non-emitting contract).
        let member: Member = serde_json::from_str(
            r#"{ "selector": { "binding": { "name": "x" } }, "note": "no forward-stable anchor yet" }"#,
        )
        .unwrap();
        assert_eq!(member.note.as_deref(), Some("no forward-stable anchor yet"));
        assert!(member.comment.is_none());
        let round_tripped = serde_json::to_string(&member).unwrap();
        assert!(
            round_tripped.contains(r#""note":"no forward-stable anchor yet""#),
            "note must survive round-trip: {round_tripped}",
        );
    }

    #[test]
    fn binding_group_accepts_and_round_trips_note() {
        // `note:` on a binding group is the same non-emitting annotation as
        // `Member.note`: it must survive `deny_unknown_fields` and round-trip.
        let group: BindingGroup = serde_json::from_str(
            r#"{ "source_match": { "match": "const x = 1;" }, "note": "no stable anchor yet" }"#,
        )
        .unwrap();
        assert_eq!(group.note.as_deref(), Some("no stable anchor yet"));
        let round_tripped = serde_json::to_string(&group).unwrap();
        assert!(
            round_tripped.contains(r#""note":"no stable anchor yet""#),
            "note must survive round-trip: {round_tripped}",
        );
    }

    #[test]
    fn logical_module_accepts_and_round_trips_note() {
        // Module-level `note:` is the same non-emitting annotation as
        // `Member.note` / `BindingGroup.note` (`modules merge` writes its
        // `merged from:` provenance here): it must survive
        // `deny_unknown_fields`, round-trip intact, and — being absent from
        // every lowering plan struct — never reach generated JS.
        let module: LogicalModule =
            serde_json::from_str(r#"{ "members": [], "note": "merged from: src.yaml" }"#).unwrap();
        assert_eq!(module.note.as_deref(), Some("merged from: src.yaml"));
        assert!(module.comment.is_none());
        let round_tripped = serde_json::to_string(&module).unwrap();
        assert!(
            round_tripped.contains(r#""note":"merged from: src.yaml""#),
            "note must survive round-trip: {round_tripped}",
        );
    }

    #[test]
    fn logical_module_note_is_absent_from_serialization_when_unset() {
        // `skip_serializing_if = "Option::is_none"` keeps the field off the
        // wire (and out of generated YAML/JSON) when no note is present.
        let module: LogicalModule = serde_json::from_str(r#"{ "members": [] }"#).unwrap();
        assert!(module.note.is_none());
        let round_tripped = serde_json::to_string(&module).unwrap();
        assert!(
            !round_tripped.contains("note"),
            "unset note must not serialize: {round_tripped}",
        );
    }

    #[test]
    fn member_of_module_selector_resolves_to_a_target() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "member_of_module": { "module": "./accessors", "member": "CardsView", "kind": "class_declaration" } }"#,
        )
        .unwrap();
        assert_eq!(
            selector.selected().unwrap(),
            MemberSelectorSpec::MemberOfModule(MemberOfModuleTarget {
                module: "./accessors".to_string(),
                member: "CardsView".to_string(),
                kind: Some(BindingSourceKind::ClassDeclaration),
            })
        );
    }

    #[test]
    fn member_of_module_selector_defaults_kind() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "member_of_module": { "module": "react", "member": "memo" } }"#,
        )
        .unwrap();
        assert_eq!(
            selector.selected().unwrap(),
            MemberSelectorSpec::MemberOfModule(MemberOfModuleTarget {
                module: "react".to_string(),
                member: "memo".to_string(),
                kind: None,
            })
        );
    }

    #[test]
    fn member_of_module_requires_module_and_member() {
        let no_member: std::result::Result<MemberSelector, _> =
            serde_json::from_str(r#"{ "member_of_module": { "module": "./m" } }"#);
        assert!(
            no_member.is_err(),
            "member_of_module without `member` is rejected"
        );
        let no_module: std::result::Result<MemberSelector, _> =
            serde_json::from_str(r#"{ "member_of_module": { "member": "X" } }"#);
        assert!(
            no_module.is_err(),
            "member_of_module without `module` is rejected"
        );
    }

    #[test]
    fn member_of_module_conflicts_with_other_selector_kinds() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "binding": { "name": "x" }, "member_of_module": { "module": "./m", "member": "X" } }"#,
        )
        .unwrap();
        assert!(
            selector.selected().is_err(),
            "a member must use exactly one selector kind",
        );
    }

    #[test]
    fn member_of_module_unknown_field_is_rejected() {
        let result: std::result::Result<MemberSelector, _> = serde_json::from_str(
            r#"{ "member_of_module": { "module": "./m", "member": "X", "object": "y" } }"#,
        );
        assert!(
            result.is_err(),
            "unknown member_of_module field must be rejected"
        );
    }

    #[test]
    fn passed_to_call_selector_resolves_to_a_target() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "passed_to_call": { "callee_member": "register", "object": "viewRegistry", "arg_index": 1, "kind": "class_declaration" } }"#,
        )
        .unwrap();
        assert_eq!(
            selector.selected().unwrap(),
            MemberSelectorSpec::PassedToCall(PassedToCallTarget {
                callee_member: "register".to_string(),
                object: Some("viewRegistry".to_string()),
                arg_index: Some(1),
                kind: Some(BindingSourceKind::ClassDeclaration),
            })
        );
    }

    #[test]
    fn passed_to_call_selector_defaults_object_index_and_kind() {
        let selector: MemberSelector =
            serde_json::from_str(r#"{ "passed_to_call": { "callee_member": "register" } }"#)
                .unwrap();
        assert_eq!(
            selector.selected().unwrap(),
            MemberSelectorSpec::PassedToCall(PassedToCallTarget {
                callee_member: "register".to_string(),
                object: None,
                arg_index: None,
                kind: None,
            })
        );
    }

    #[test]
    fn passed_to_call_requires_callee_member() {
        let result: std::result::Result<MemberSelector, _> =
            serde_json::from_str(r#"{ "passed_to_call": { "object": "r" } }"#);
        assert!(
            result.is_err(),
            "passed_to_call without `callee_member` is rejected"
        );
    }

    #[test]
    fn passed_to_call_conflicts_with_other_selector_kinds() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "binding": { "name": "x" }, "passed_to_call": { "callee_member": "register" } }"#,
        )
        .unwrap();
        assert!(
            selector.selected().is_err(),
            "a member must use exactly one selector kind",
        );
    }

    #[test]
    fn passed_to_call_unknown_field_is_rejected() {
        let result: std::result::Result<MemberSelector, _> = serde_json::from_str(
            r#"{ "passed_to_call": { "callee_member": "register", "module": "./m" } }"#,
        );
        assert!(
            result.is_err(),
            "unknown passed_to_call field must be rejected"
        );
    }

    #[test]
    fn makes_decorate_call_selector_resolves_to_a_target() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "makes_decorate_call": { "class": "ComponentPopover", "member": "componentInstance", "kind": "variable_declarator" } }"#,
        )
        .unwrap();
        assert_eq!(
            selector.selected().unwrap(),
            MemberSelectorSpec::MakesDecorateCall(MakesDecorateCallTarget {
                class: "ComponentPopover".to_string(),
                member: Some("componentInstance".to_string()),
                kind: Some(BindingSourceKind::VariableDeclarator),
            })
        );
    }

    #[test]
    fn makes_decorate_call_selector_defaults_member_and_kind() {
        let selector: MemberSelector =
            serde_json::from_str(r#"{ "makes_decorate_call": { "class": "PopoverBase" } }"#)
                .unwrap();
        assert_eq!(
            selector.selected().unwrap(),
            MemberSelectorSpec::MakesDecorateCall(MakesDecorateCallTarget {
                class: "PopoverBase".to_string(),
                member: None,
                kind: None,
            })
        );
    }

    #[test]
    fn makes_decorate_call_requires_class() {
        let result: std::result::Result<MemberSelector, _> =
            serde_json::from_str(r#"{ "makes_decorate_call": { "member": "x" } }"#);
        assert!(
            result.is_err(),
            "makes_decorate_call without `class` is rejected"
        );
    }

    #[test]
    fn makes_decorate_call_conflicts_with_other_selector_kinds() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "binding": { "name": "x" }, "makes_decorate_call": { "class": "C" } }"#,
        )
        .unwrap();
        assert!(
            selector.selected().is_err(),
            "a member must use exactly one selector kind",
        );
    }

    #[test]
    fn makes_decorate_call_unknown_field_is_rejected() {
        let result: std::result::Result<MemberSelector, _> = serde_json::from_str(
            r#"{ "makes_decorate_call": { "class": "C", "callee_member": "register" } }"#,
        );
        assert!(
            result.is_err(),
            "unknown makes_decorate_call field must be rejected"
        );
    }

    #[test]
    fn intrinsic_alias_selector_resolves_to_a_target() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "intrinsic_alias": { "property": "defineProperty", "referenced_by": "decorateClassMember" } }"#,
        )
        .unwrap();
        assert_eq!(
            selector.selected().unwrap(),
            MemberSelectorSpec::IntrinsicAlias(IntrinsicAliasTarget {
                property: "defineProperty".to_string(),
                referenced_by: "decorateClassMember".to_string(),
            })
        );
    }

    #[test]
    fn intrinsic_alias_requires_property() {
        let result: std::result::Result<MemberSelector, _> =
            serde_json::from_str(r#"{ "intrinsic_alias": { "referenced_by": "helper" } }"#);
        assert!(
            result.is_err(),
            "intrinsic_alias without `property` is rejected"
        );
    }

    #[test]
    fn intrinsic_alias_requires_referenced_by() {
        let result: std::result::Result<MemberSelector, _> =
            serde_json::from_str(r#"{ "intrinsic_alias": { "property": "defineProperty" } }"#);
        assert!(
            result.is_err(),
            "intrinsic_alias without `referenced_by` is rejected"
        );
    }

    #[test]
    fn intrinsic_alias_conflicts_with_other_selector_kinds() {
        let selector: MemberSelector = serde_json::from_str(
            r#"{ "binding": { "name": "x" }, "intrinsic_alias": { "property": "defineProperty", "referenced_by": "h" } }"#,
        )
        .unwrap();
        assert!(
            selector.selected().is_err(),
            "a member must use exactly one selector kind",
        );
    }

    #[test]
    fn intrinsic_alias_unknown_field_is_rejected() {
        let result: std::result::Result<MemberSelector, _> = serde_json::from_str(
            r#"{ "intrinsic_alias": { "property": "defineProperty", "referenced_by": "h", "object": "Object" } }"#,
        );
        assert!(
            result.is_err(),
            "unknown intrinsic_alias field must be rejected"
        );
    }
}
