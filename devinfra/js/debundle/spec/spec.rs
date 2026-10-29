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
//! synthetic atomic-group module per atomic group). See [`UnassignedMode`].
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
    /// Loading contract used by the export-reentry proof. Native browser ESM
    /// has no synchronous host API for obtaining an evaluating module namespace.
    /// This is independent of whether a browser harness is emitted.
    #[serde(default)]
    pub execution_runtime: ExecutionRuntime,

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
    /// module is created for these bindings, no separate residual
    /// file is emitted, and the residual module (the entry file) keeps
    /// owning the bindings — which
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
    /// precondition (see `devinfra/js/debundle/README.md` →
    /// "Conditionally-correct optimizations"). Default-empty: every
    /// chunk uses the strictly-conservative analysis paths unless the
    /// spec explicitly opts in.
    #[serde(default)]
    pub chunk_analysis_options: BTreeMap<String, DependencyGraphOptions>,
    /// Author-asserted contracts for exports, keyed by the *defining*
    /// chunk. Purity assertions seed trusted axioms in the cross-module
    /// purity oracle; callback-factory assertions separately describe
    /// callback flow and do not imply purity. Each assertion is made once,
    /// where the audited code lives, not per consumer. Incorrect assertions
    /// can produce a buggy debundle; soundness shifts to the spec author.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub chunk_export_purity: BTreeMap<String, ChunkExportPurity>,
    /// Version-scoped author assertion about host callback timing. This does
    /// not change module-loader policy, purity, throws, or return values.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub host_no_reentry: Option<HostNoReentry>,

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

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HostNoReentry {
    #[serde(with = "serde_yaml::with::singleton_map")]
    pub runtime: HostNoReentryRuntime,
    #[serde(default)]
    pub operations: BTreeSet<HostNoReentryOperation>,
}

#[derive(Debug, Clone, Eq, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum HostNoReentryRuntime {
    #[serde(rename = "bun_standalone_1_3_10")]
    BunStandalone1_3_10,
    /// Author-audited identity of the executable that supplies the host APIs.
    /// The analyzer records this identity; it does not execute or authenticate it.
    BunStandaloneBuild {
        version: String,
        revision: String,
        executable_sha256: String,
    },
}

/// Exact zero-argument operations audited for the versioned host above.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum HostNoReentryOperation {
    AsyncLocalStorageConstruct,
    PromiseResolve,
}

impl TransformSpec {
    /// Chunk ids selected by the `materialize_logical_modules` stage.
    ///
    /// Keep this selection shared by the pipeline and commands that need to
    /// validate stage completion; input chunks without stage metadata are not
    /// selected for materialization.
    pub fn materialize_chunk_ids(&self) -> BTreeSet<String> {
        self.logical_modules
            .keys()
            .chain(self.unassigned_mode.keys())
            .chain(self.chunk_renames.keys())
            .cloned()
            .collect()
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionRuntime {
    /// The embedding host may expose a synchronous module loader.
    #[default]
    Generic,
    /// Modules execute through the browser's native ESM loader. Host code
    /// cannot synchronously load the namespace of an evaluating module.
    NativeBrowserEsm,
}

/// Author-asserted contracts for one defining chunk's exports. See
/// [`TransformSpec::chunk_export_purity`].
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ChunkExportPurity {
    /// When present, identify the defining provider by a conjunction of
    /// purpose-bearing substrings in its loaded entry source. The map key
    /// becomes a policy label; provider identity and emission stay unchanged.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_markers: Option<BTreeSet<String>>,
    /// Export names whose *calls* the author asserts have no observable
    /// side effects. Applies to the export regardless of how the binding
    /// is produced (function declaration, function-valued const, interop
    /// wrapper), so it also covers callables the static classifier cannot
    /// see into.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub pure_exports: BTreeSet<String>,
    /// Export names whose *construction* the author asserts has no observable
    /// side effects beyond evaluating constructor arguments. Applies only to
    /// `new Export(...)`, not ordinary calls of the exported value.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub pure_new_exports: BTreeSet<String>,
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
    /// Export names whose calls accept one inline callback without invoking
    /// it or other caller/host/export callbacks synchronously, or publishing
    /// the argument outside the returned fresh ordinary callable/plain-object
    /// graph. That result may later expose or invoke the captured callback.
    /// No synchronous access to importer-owned state through other routes is
    /// allowed, including imported lexical cells or caller-owned objects.
    /// Provider-private effects are allowed.
    /// Call sites must pass exactly one non-spread inline function or arrow
    /// callback, or a retained result from another audited factory, and must
    /// not construct the export; other shapes stay conservative. A retained
    /// result is a fresh ordinary callable/plain-object graph. The author
    /// must audit every implementation/helper and type branch, property
    /// access in the retained argument and result graphs, and prototype
    /// integrity. Neither graph may contain proxies or accessors at the call.
    /// This is a callback-flow contract, not a purity assertion: the call may
    /// still have observable effects and remains ordered as an impure call.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub callback_factory_exports: BTreeSet<String>,
    /// Export names with the callback-factory contract restricted to exactly
    /// one non-spread inline function or arrow argument. Retained results of
    /// other factories are not admitted as inputs.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub inline_callback_factory_exports: BTreeSet<String>,
    /// Audited callback factories reached through static members of an
    /// exported namespace. Independent of `pure_members`; each member has
    /// the same return-only callback contract as `callback_factory_exports`.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub callback_factory_members: BTreeMap<String, BTreeSet<String>>,
    /// Separately audited factories admitting existing local callbacks and
    /// multiple retained callback arguments. See `RetainedCallbackSignature`.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub retained_callback_factory_exports: BTreeMap<String, Vec<RetainedCallbackSignature>>,
    /// Audited direct imports which copy one fresh literal's own descriptors
    /// into a fresh returned wrapper, then install the listed data methods.
    /// This is a return-only callback timing assertion, never purity.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub descriptor_copy_factory_exports: BTreeMap<String, Vec<DescriptorCopyFactorySignature>>,
    /// The same stronger contract on direct static namespace member calls.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub retained_callback_factory_members:
        BTreeMap<String, BTreeMap<String, Vec<RetainedCallbackSignature>>>,
    /// Direct imported constructors whose audited construction retains the
    /// listed callbacks in fresh instance storage without synchronous reentry.
    /// Independent of constructor purity and ordinary-call factory contracts.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub retained_callback_constructor_exports: BTreeMap<String, Vec<RetainedCallbackSignature>>,
    /// Named exports whose direct calls with checked primitive arguments
    /// cannot synchronously invoke caller/importer/host callbacks or read
    /// importer-owned lexical state. This changes call timing only: provider
    /// effects and the opaque return value remain conservative.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub no_reentry_exports: BTreeSet<String>,
    /// Exact direct imported call signatures that cannot synchronously reenter
    /// caller/importer/host code, including their callback arguments. Callbacks
    /// become globally reachable after the call; the result remains opaque.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub no_reentry_callback_exports: BTreeMap<String, Vec<RetainedCallbackSignature>>,
    /// Static members of an imported namespace whose calls cannot synchronously
    /// invoke caller, importer, or host callbacks or read importer-owned cells.
    /// This asserts call timing only, not purity or the shape of the result.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub no_reentry_members: BTreeMap<String, BTreeSet<String>>,
    /// Separate opt-in for those members when a direct call passes a fresh
    /// static-key object record with recursively inert literal values. The
    /// provider branch for records must be audited independently of primitive
    /// arguments; the primitive-only assertion does not imply this one.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub no_reentry_record_members: BTreeMap<String, BTreeSet<String>>,
    /// Stable lookups running no caller/importer/host code, whose capabilities cannot expose their namespace
    /// or replace audited factories except through supplied caller inputs.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub namespace_independent_members: BTreeMap<String, BTreeSet<String>>,
}

/// Exact provider operations that can otherwise observe an input accessor at
/// registration or run a setter while installing returned methods.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DescriptorCopyFactorySignature {
    pub argument_count: usize,
    pub object_argument: usize,
    pub eager_own_data_keys: BTreeSet<String>,
    pub assigned_data_keys: BTreeSet<String>,
}

/// One exact callback-bearing call shape. Non-callback positions must be
/// syntactically inert primitives. The policy containing this signature
/// determines whether callbacks stay in a fresh returned graph or become
/// globally reachable after the call. No policy is implied by another.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub struct RetainedCallbackSignature {
    pub argument_count: usize,
    pub callback_arguments: BTreeSet<usize>,
}

impl<'de> Deserialize<'de> for RetainedCallbackSignature {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Signature {
            argument_count: usize,
            callback_arguments: BTreeSet<usize>,
        }
        let signature = Signature::deserialize(deserializer)?;
        if signature.callback_arguments.is_empty() {
            return Err(serde::de::Error::custom(
                "retained callback signature must list at least one callback argument",
            ));
        }
        if signature
            .callback_arguments
            .iter()
            .any(|position| *position >= signature.argument_count)
        {
            return Err(serde::de::Error::custom(
                "retained callback signature position must be less than argument_count",
            ));
        }
        Ok(Self {
            argument_count: signature.argument_count,
            callback_arguments: signature.callback_arguments,
        })
    }
}

/// Per-chunk dependency-graph build options. Each field defaults to the
/// strictly-conservative behavior; opt-ins enable conditionally-correct
/// inferences that hold only when the input satisfies a checkable
/// precondition (see `devinfra/js/debundle/AGENTS.md` →
/// "Conditionally-correct optimizations").
///
/// Serves two roles with one type: it's the YAML surface for
/// `TransformSpec::chunk_analysis_options` (each per-chunk entry
/// deserializes into this), and it's the input to
/// `analysis::build_dependency_graph_with`. The materializer threads it
/// straight through — no per-chunk copy.
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DependencyGraphOptions {
    /// Author-conditioned value facts for exact top-level provider bindings.
    /// These are resolved by the whole-program source analysis before graph
    /// construction and never select emission roots.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub value_inputs: Vec<ValueInputContract>,
    /// Author-certified identity of one direct native operation initializer.
    /// The author audits the ordinary initial global binding and every
    /// synchronous initialization schedule admitted by emission. This is
    /// neither a source inference nor a whole-lifetime integrity claim. See
    /// docs/purity_soundness.md.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_operation_inputs: Vec<NativeOperationInputContract>,
    /// Author-certified original native callee at one selected direct call in
    /// a top-level function body. This supplies identity only; the modeled
    /// operation must still pass its ordinary argument and effect guards.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_call_identity_inputs: Vec<NativeCallIdentityInputContract>,
    /// Author-certified browser-global data slots whose writes publish a
    /// callback only for use after synchronous chunk initialization. The
    /// slot must be an ordinary inert own data property, or an absent slot
    /// whose creation cannot invoke an inherited setter, at the write. No
    /// importer, host, or source consumer may read/call the stored capability
    /// during that initialization. This is a timing assertion, not purity:
    /// the write, RHS evaluation, and ordering effects remain observable.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub deferred_global_publications: BTreeMap<BuiltinGlobalRoot, BTreeSet<String>>,
    /// Author-certified literal-key membership probes on direct unshadowed
    /// builtin roots that cannot synchronously invoke or publish callbacks,
    /// or access importer-owned cells, through host/proxy/prototype behavior.
    /// Host identity and the complete prototype/alias surface must remain
    /// intact at every admitted probe. This narrows protocol reentry only;
    /// evaluation, dependencies, impurity and possible throws remain.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub non_reentrant_global_membership: BTreeMap<BuiltinGlobalRoot, BTreeSet<String>>,
    /// Emit the side-effect ordering chain using per-statement
    /// (writes, reads) summaries instead of the adjacent-impure
    /// transitive reduction. See the S-chain block in
    /// `build_dependency_graph_with` and `README.md` →
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
    /// statements into one atomic group.
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

/// A source-bound value premise attached to one exact provider chunk.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ValueInputContract {
    /// Structural selector for exactly one declared local binding.
    pub source_match: SourceMatchClaim,
    pub value: CapturedValueContract,
    /// SHA-256 of the parser input bytes recorded for this provider entry.
    pub audited_source_sha256: String,
    /// Optional direct export name that must resolve to the selected local cell.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expected_export: Option<String>,
    /// YAML-only author rationale. It does not strengthen the premise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// Author premise for one source-selected direct native operation initializer.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct NativeOperationInputContract {
    /// Structural selector for exactly one declared local binding.
    pub source_match: SourceMatchClaim,
    /// Operation whose original native binding the author has audited.
    pub operation: NativeOperationKind,
    /// SHA-256 of the parser input bytes recorded for this provider entry.
    pub audited_source_sha256: String,
    /// YAML-only author rationale. It does not strengthen the premise.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// Exact source-selected native callee identity, scoped to every synchronous
/// at-init activation of the selected call site across permitted schedules.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct NativeCallIdentityInputContract {
    pub source_match: SourceMatchClaim,
    pub callee: NativeCallIdentityKind,
    pub audited_source_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// Typed native callee models eligible for exact-site identity premises.
#[derive(Debug, Clone, Copy, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum NativeCallIdentityKind {
    ObjectFreeze,
    SetEmptyPrivateField,
}

/// Native operations admitted by `native_operation_inputs`.
#[derive(Debug, Clone, Copy, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum NativeOperationKind {
    Symbol,
    SetFromPrimitiveArray,
    SetFromSetSpread,
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CapturedValueContract {
    pub kind: CapturedValueKind,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum CapturedValueKind {
    OrdinaryObject,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Deserialize, Serialize)]
pub enum BuiltinGlobalRoot {
    #[serde(rename = "globalThis")]
    GlobalThis,
    #[serde(rename = "window")]
    Window,
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

/// Omitted fields take their value from the `Default` impl, so `{}` and an
/// omitted `swap_vendor_chunks` agree by construction.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields, default)]
pub struct SwapVendorChunksConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output_manifest_path: Option<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output_wrapper_dir: Option<PathBuf>,
    /// Defaults to `true` — actually write the manifest / wrapper files
    /// to disk. Set `false` for dry-run.
    #[serde(skip_serializing_if = "is_true")]
    pub write: bool,
}

impl Default for SwapVendorChunksConfig {
    fn default() -> Self {
        Self {
            output_manifest_path: None,
            output_wrapper_dir: None,
            write: true,
        }
    }
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
    pub members: Vec<ChunkRenameMember>,
    /// Semantic hints keyed by the final readable name in `members`.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub annotations: BTreeMap<String, BindingAnnotation>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ChunkRenameMember {
    #[serde(default)]
    pub name: Option<String>,
    pub selector: ChunkRenameSelector,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ChunkRenameSelector {
    pub binding: BindingSelector,
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

fn is_default_initializer_purity(purity: &InitializerPurity) -> bool {
    matches!(purity, InitializerPurity::Default)
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
/// any logical module. The atomic-group primitive in
/// `analysis::atomic_groups` partitions the chunk's source_units into
/// minimal co-location groups; this enum decides what destination
/// each *unclaimed* atomic group lands in.
///
/// The three variants are mutually exclusive — every chunk picks
/// exactly one destination policy.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case", tag = "kind", deny_unknown_fields)]
pub enum UnassignedMode {
    /// Unclaimed bindings stay inline in the chunk's entry file (the
    /// residual module); no separate residual file is emitted. Renames
    /// against unclaimed bindings come from
    /// [`TransformSpec::chunk_renames`] and are applied in-place by the
    /// lowerer.
    InlineInEntry,
    /// Unclaimed bindings emit to a separate logical module at
    /// `target` (defaults to [`DEFAULT_RESIDUAL_MODULE_PATH`]). The
    /// module behaves like any other logical module — it can be a
    /// peel destination for propose proposals — but structurally
    /// is the catch-all for unclaimed code. Renames for bindings
    /// that land in this catch-all should be expressed by listing
    /// them as members of a regular `logical_modules` entry at the
    /// same `target` path; the materializer joins explicit member
    /// claims with unclaimed overflow on a per-binding basis.
    CatchallFile {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        target: Option<String>,
    },
    /// One synthetic atomic-group module per unclaimed atomic factor group.
    /// The residual catch-all collapses to whatever truly cannot
    /// be peeled (typically empty for clean chunks). See
    /// `docs/design.md` §"Layered mental model" + §"Two classes of atom".
    PerAtomicGroup,
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
    /// to `<namespace>.<upstream_export>(...)`. Dots in `upstream_export`
    /// separate nested properties; each segment is emitted as a safe
    /// property access. In contrast, `kind: named` uses a literal export name.
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
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub members: Vec<Member>,
    /// Compact source-backed ownership claims. Each entry describes one source
    /// shape and names the selector-local bindings this module owns from that
    /// shape. Semantic metadata for the claimed readable names lives in
    /// [`LogicalModule::annotations`].
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub source_matches: Vec<SourceMatchClaim>,
    /// Module-level semantic annotations keyed by the final readable binding
    /// name. This keeps shared metadata out of repeated selector entries and
    /// lets source-match binding claims stay focused on ownership.
    #[serde(
        default,
        deserialize_with = "deserialize_unique_annotations",
        skip_serializing_if = "BTreeMap::is_empty"
    )]
    pub annotations: BTreeMap<String, BindingAnnotation>,
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
    /// which emits a `//` block. Same non-emitting contract and STYLE.md local
    /// exemption (see AGENTS.md "Spec `note:` field").
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

impl LogicalModule {
    /// No ownership claims or binding metadata. Explicit deletion of this
    /// module cannot alter the assignment, even if it has a comment or note.
    pub fn is_structurally_empty(&self) -> bool {
        self.members.is_empty()
            && self.source_matches.is_empty()
            && self.annotations.is_empty()
            && self.anonymous_statements.is_empty()
    }

    /// Automatic cleanup must also preserve authored module metadata, including
    /// explicit empty comments/notes. Callers decide which paths are eligible.
    pub fn is_auto_deletable(&self) -> bool {
        self.is_structurally_empty() && self.comment.is_none() && self.note.is_none()
    }
}

fn deserialize_unique_annotations<'de, D>(
    deserializer: D,
) -> Result<BTreeMap<String, BindingAnnotation>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    struct UniqueAnnotations;

    impl<'de> serde::de::Visitor<'de> for UniqueAnnotations {
        type Value = BTreeMap<String, BindingAnnotation>;

        fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str("a map with unique binding annotation names")
        }

        fn visit_map<M>(self, mut map: M) -> Result<Self::Value, M::Error>
        where
            M: serde::de::MapAccess<'de>,
        {
            let mut annotations = BTreeMap::new();
            while let Some(name) = map.next_key::<String>()? {
                if annotations.contains_key(&name) {
                    return Err(serde::de::Error::custom(format!(
                        "duplicate annotation key {name:?}"
                    )));
                }
                annotations.insert(name, map.next_value()?);
            }
            Ok(annotations)
        }
    }

    deserializer.deserialize_map(UniqueAnnotations)
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
    /// Author assertion that the selected top-level call (bare or the rightmost
    /// operand of `&&`) cannot synchronously invoke callbacks or read caller
    /// lexical state. Arguments remain exposed after the call; not purity.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub no_reentry_call: Option<NoReentryCall>,
    /// Optional YAML-only note. Use this for provenance,
    /// uncertainty, and other scratch reverse-engineering notes that
    /// should survive spec edits without appearing in generated JS.
    /// Ignored by the materializer.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    /// Optional human-readable comment. Preserved on round-trip and
    /// emitted immediately above the matched anonymous statement in
    /// generated JS. Accepted for symmetry with the top-level
    /// [`LogicalModule::comment`] field and binding `annotations:` comments.
    /// Prefer `note:` for scratch text that should stay YAML-only.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comment: Option<String>,
}

/// Callback positions on one source-selected top-level call. The matched
/// source supplies the argument count; every other argument must satisfy the
/// existing inert-primitive syntax admission.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct NoReentryCall {
    pub callback_arguments: Vec<usize>,
}

impl<'de> Deserialize<'de> for NoReentryCall {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct NoReentryCallFields {
            callback_arguments: Vec<usize>,
        }

        let fields = NoReentryCallFields::deserialize(deserializer)?;
        if fields.callback_arguments.is_empty() {
            return Err(serde::de::Error::custom(
                "no_reentry_call must list at least one callback argument position",
            ));
        }
        let mut seen = BTreeSet::new();
        for position in &fields.callback_arguments {
            if !seen.insert(*position) {
                return Err(serde::de::Error::custom(format!(
                    "duplicate no_reentry_call callback argument position {position}"
                )));
            }
        }
        Ok(Self {
            callback_arguments: fields.callback_arguments,
        })
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Default)]
#[serde(deny_unknown_fields)]
pub struct BindingAnnotation {
    /// Audited prototype premises for a source-checked unary local constructor
    /// factory: missing input fields are actually absent and instance assignments
    /// successfully create ordinary own data properties. Source execution, eager reads and
    /// effects remain checked; this does not imply purity or native integrity.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub retained_record_factory: bool,
    /// Spec-level purity annotation for the annotated binding.
    #[serde(default, skip_serializing_if = "is_default_member_purity")]
    pub purity: MemberPurity,
    /// Trust assertion for evaluation of one selected simple top-level
    /// variable initializer; it says nothing about later calls or construction.
    #[serde(default, skip_serializing_if = "is_default_initializer_purity")]
    pub initializer_purity: InitializerPurity,
    /// Spec-level local-effect annotation for the annotated binding.
    #[serde(default, skip_serializing_if = "is_default_member_effect")]
    pub effect: MemberEffect,
    /// Property names whose member calls are author-asserted pure.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub pure_members: Vec<String>,
    /// Property names whose calls do not synchronously invoke callback
    /// arguments.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub no_sync_callback_members: Vec<String>,
    /// Optional human-readable comment emitted above the matched binding's
    /// source_unit statement in generated JS.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comment: Option<String>,
    /// Optional YAML-only note. Preserved by spec edits but never emitted.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SourceMatchClaim {
    /// Identifier matching mode for this source-backed claim. Omitted means
    /// alpha-equivalent identifier matching, matching [`SourceMatch`].
    #[serde(skip)]
    pub identifiers: SourceMatchIdentifierMode,
    /// JS source pattern to match against one top-level source statement.
    #[serde(rename = "match")]
    pub match_source: String,
    /// Bindings to claim from the matched source pattern: names the pattern
    /// declares, or free identifiers it uses, which claim the top-level
    /// declaration they bind to. String shorthand means
    /// `{ local: <name>, name: <name> }`; object entries can override the final
    /// readable binding name with `name:`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub bindings: Vec<SourceMatchBinding>,
    /// Optional YAML-only note about the selector shape as a whole. Per-binding
    /// semantic notes live in [`LogicalModule::annotations`].
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

impl SourceMatchClaim {
    pub fn source_match(&self) -> SourceMatch {
        SourceMatch {
            identifiers: self.identifiers,
            target_binding: None,
            match_source: self.match_source.clone(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(untagged)]
pub enum SourceMatchBinding {
    Local(String),
    Detailed(SourceMatchBindingDetail),
}

impl SourceMatchBinding {
    pub fn local(&self) -> &str {
        match self {
            Self::Local(local) => local,
            Self::Detailed(detail) => &detail.local,
        }
    }

    pub fn name(&self) -> &str {
        match self {
            Self::Local(local) => local,
            Self::Detailed(detail) => detail.name.as_deref().unwrap_or(&detail.local),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SourceMatchBindingDetail {
    pub local: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
}

impl AnonymousStatement {
    pub fn selector(
        &self,
    ) -> std::result::Result<AnonymousStatementSelector, AnonymousStatementSelectorError> {
        match (&self.match_source, &self.source_match) {
            (Some(match_source), None) => Ok(AnonymousStatementSelector::exact(match_source)),
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
    #[serde(default, skip_serializing)]
    pub identifiers: SourceMatchIdentifierMode,
    /// Internal selector-local binding name to project after parsing a
    /// canonical `source_matches[].bindings` claim. Public YAML selector bodies
    /// do not accept this field; claim projection lives outside the selector.
    #[serde(default, skip_serializing)]
    pub target_binding: Option<String>,
    #[serde(rename = "match")]
    pub match_source: String,
}

#[derive(Deserialize)]
struct SourceMatchWire {
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
        Ok(Self {
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: None,
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
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub struct AnonymousStatementSelector {
    pub match_source: String,
    pub identifiers: SourceMatchIdentifierMode,
    pub target_binding: Option<String>,
}

impl AnonymousStatementSelector {
    pub fn exact(match_source: impl Into<String>) -> Self {
        Self {
            match_source: match_source.into(),
            identifiers: SourceMatchIdentifierMode::Exact,
            target_binding: None,
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

/// Default value the [`UnassignedMode::CatchallFile`] target path
/// falls back to when the spec author omits it. SSOT consumed by
/// the materializer (`logical_modules` residual synthesis) and by
/// analysis tools that want to match the canonical residual
/// catch-all path.
pub const DEFAULT_RESIDUAL_MODULE_PATH: &str = "residual/unhandled";

/// Authored module-path namespace for the residual catch-all
/// (default emit target [`DEFAULT_RESIDUAL_MODULE_PATH`]
/// per `UnassignedMode::CatchallFile`). Files under any
/// directory whose top-level segment is `residual/` are treated as
/// "the still-to-be-factorized pile". Detection is by module-path
/// prefix only (matches both the default and any spec author's
/// custom `residual/<other>.yaml`).
pub fn is_residual_namespace_path(module_path: &str) -> bool {
    module_path == "residual" || module_path.starts_with("residual/")
}

/// The single, canonical identity of a logical module: **relative,
/// slash-separated, lowercase** (e.g. `domains/system/ids`), equal to
/// the module's destination path.
///
/// [`parse`] is the only constructor, so the several historical
/// spellings of one module (clean spec path, chunk-prefixed
/// `PlannedModule.id`, report label) collapse to one value and `==` is
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
    /// A leading `"<chunk_id>::"` (the production `PlannedModule.id`
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
    use super::{ModulePath, is_residual_namespace_path};

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
    fn is_residual_namespace_path_matches_residual_subtree_only() {
        assert!(is_residual_namespace_path("residual"));
        assert!(is_residual_namespace_path("residual/unhandled"));
        assert!(is_residual_namespace_path("residual/custom/sub"));
        assert!(!is_residual_namespace_path("ui/sidebar"));
        // The substring "residual" inside a path segment shouldn't
        // match — only the top-level segment counts.
        assert!(!is_residual_namespace_path("ui/residual"));
        assert!(!is_residual_namespace_path("residualish/foo"));
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
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MemberSelector {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding: Option<BindingSelector>,
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
/// member by its readable name; the target is resolved through the dependency graph as
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
    /// Optional statement-kind constraint disambiguating when several source_units
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
/// never by the minified `ls`. Resolved through the dependency graph's `reads_member`
/// EDB: the unique declaring source_unit whose body reads the named member.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct ReadsMemberSelector {
    /// The property name `X` the target reads (`obj.X`). Required — the relation
    /// is "reads member `.member`".
    pub member: String,
    /// Optional object constraint: the readable `name:` of another member the
    /// property is read off (`@object.member`). Narrows "the source_unit that reads
    /// `.X`" to "the source_unit that reads `.X` **off `@object`**" — the codegen
    /// context being the canonical object. Resolved like a `cross_ref` anchor:
    /// the object's already-resolved minified binding rides the relational edge.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub object: Option<String>,
    /// Optional statement-kind constraint disambiguating when several source_units read
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
/// Resolved through the dependency graph's `member_of_module` EDB: the unique declaring
/// source_unit whose body consumes `<module>.<member>`.
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
    /// Optional statement-kind constraint disambiguating when several source_units
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
/// source_unit whose *own* subtree consumes `mod.X` and explicitly cannot reach a target
/// distinguished only by such an external registration. `callee_member` (the call's
/// method name) and the registry's own stable identity are re-minify-invariant, so
/// the edge survives a bundle rebuild. Resolved through the dependency graph's
/// `passed_to_call` EDB: the unique declaring source_unit whose binding is the call
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
    /// Optional statement-kind constraint disambiguating when several source_units are
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

/// `makes_decorate_call` EDB: the unique declaring source_unit whose binding is the
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
    /// `variable_declarator`; the constraint narrows past any non-var source_unit that
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
/// through the dependency graph's `intrinsic_alias` EDB: the unique declaring source_unit whose
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
            &self.cross_ref,
            &self.reads_member,
            &self.member_of_module,
            &self.passed_to_call,
            &self.makes_decorate_call,
            &self.intrinsic_alias,
        ) {
            (Some(binding), None, None, None, None, None, None) => {
                Ok(MemberSelectorSpec::Binding(binding.clone()))
            }
            (None, Some(cross_ref), None, None, None, None, None) => {
                cross_ref.target().map(MemberSelectorSpec::CrossRef)
            }
            (None, None, Some(reads_member), None, None, None, None) => {
                Ok(MemberSelectorSpec::ReadsMember(reads_member.target()))
            }
            (None, None, None, Some(member_of_module), None, None, None) => Ok(
                MemberSelectorSpec::MemberOfModule(member_of_module.target()),
            ),
            (None, None, None, None, Some(passed_to_call), None, None) => {
                Ok(MemberSelectorSpec::PassedToCall(passed_to_call.target()))
            }
            (None, None, None, None, None, Some(makes_decorate_call), None) => Ok(
                MemberSelectorSpec::MakesDecorateCall(makes_decorate_call.target()),
            ),
            (None, None, None, None, None, None, Some(intrinsic_alias)) => {
                Ok(MemberSelectorSpec::IntrinsicAlias(intrinsic_alias.target()))
            }
            (None, None, None, None, None, None, None) => Err(MemberSelectorError {
                message: "members[].selector must include one of `binding`, `cross_ref`, \
                          `reads_member`, `member_of_module`, `passed_to_call`, \
                          `makes_decorate_call`, or `intrinsic_alias`",
            }),
            _ => Err(MemberSelectorError {
                message: "members[].selector must use exactly one of `binding`, `cross_ref`, \
                          `reads_member`, `member_of_module`, `passed_to_call`, \
                          `makes_decorate_call`, or `intrinsic_alias`",
            }),
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub enum MemberSelectorSpec {
    Binding(BindingSelector),
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
    /// sites. See <docs/purity_soundness.md> "Declared purity" + docs/design.md A9.
    Pure,
    /// Author asserts that `new <binding>(...)` has no observable side
    /// effects beyond evaluating its constructor arguments. The analyzer
    /// still requires every argument expression to classify pure; this
    /// annotation does not affect ordinary `<binding>(...)` calls.
    PureNew,
}

/// Trust contract scoped to evaluation of one selected variable initializer.
/// Unlike [`MemberPurity`], this says nothing about later uses of its value.
#[derive(Debug, Clone, Copy, Deserialize, Serialize, Default, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum InitializerPurity {
    #[default]
    Default,
    Pure,
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
    /// Stronger, application-wide author contract: every recognized member
    /// decoration only records metadata on the target. The helper and every
    /// supplied decorator neither invoke target behavior nor publish callbacks
    /// or synchronously reenter other caller/host/export callbacks. See SPEC.md.
    TypescriptMetadataDecorateHelper,
    /// Like `typescript_metadata_decorate_helper`, additionally admits an
    /// audited one-hop static member of an imported decorator provider.
    TypescriptMetadataDecorateHelperWithMemberDecorators,
    /// The same metadata-only contract for standalone three-argument property
    /// applications. The helper determines its descriptor without a kind argument.
    TypescriptMetadataDecorateHelperThreeArgs,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_match_unknown_field_is_rejected_naming_the_field() {
        let error: serde_json::Error = serde_json::from_str::<SourceMatch>(
            r#"{
              "match": "const readable = 1;",
              "object_props": true
            }"#,
        )
        .unwrap_err();
        let message = error.to_string();
        assert!(
            message.contains("object_props"),
            "unexpected error: {message}"
        );
    }

    #[test]
    fn source_match_serializes_only_public_match_field() {
        let source_match = SourceMatch {
            identifiers: SourceMatchIdentifierMode::Exact,
            target_binding: Some("readable".to_string()),
            match_source: "const readable = runtime;".to_string(),
        };
        let serialized = serde_json::to_value(source_match).unwrap();
        assert_eq!(
            serialized,
            serde_json::json!({ "match": "const readable = runtime;" })
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
}
