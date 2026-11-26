//! Typed deserialisation surface for flat-spec YAML files ([`TransformSpec`]).
//!
//! Two declarative top-level maps describe what the spec wants applied:
//!
//! - `vendor` keyed by chunk path (`"static/lib.js"` → [`VendorMark`]).
//! - `logical_modules` keyed by chunk id, then target path
//!   (`"static/app"` → `"foo/bar/baz.js"` → [`LogicalModule`]).
//!
//! A third per-chunk map, `unassigned_mode`, decides what happens to
//! top-level statements that no `logical_modules` entry explicitly
//! claims (entry, catchall file, one module per unclaimed atomic group, or
//! coarser residual groups). See [`UnassignedMode`].
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

pub mod annotations;
pub mod input_contracts;
pub mod member_selectors;
pub mod vendor_marks;

use annotations::AnonymousStatement;
use annotations::BindingAnnotation;
use annotations::SourceMatchClaim;
use input_contracts::ArgumentInputContract;
use input_contracts::CommutingStatementsContract;
use input_contracts::ImportedPrimitiveArraySpreadInputContract;
use input_contracts::InitializerCallEffectInputContract;
use input_contracts::NativeCallIdentityInputContract;
use input_contracts::NativeOperationInputContract;
use input_contracts::NoAccessorInstallCallableContract;
use input_contracts::ValueInputContract;
use member_selectors::BindingSelector;
use member_selectors::Member;
use vendor_marks::VendorMark;

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TransformSpec {
    pub inputs: LoadJsChunksArgs,
    /// Declared behaviors of the embedding host (SPEC.md § Host behavior).
    /// Omitted keys take their conservative value, so an absent block is the
    /// generic host with no audited behavior.
    #[serde(default)]
    pub host_behavior: HostBehavior,
    /// Author premises about the analyzed program the engine cannot derive
    /// (SPEC.md § Program premises). An absent block asserts nothing.
    #[serde(default)]
    pub program_premises: ProgramPremises,

    #[serde(default)]
    pub vendor: BTreeMap<String, VendorMark>,
    #[serde(default)]
    pub logical_modules: BTreeMap<String, BTreeMap<String, LogicalModule>>,
    /// Per-chunk priority over emitted non-entry owners. The shared gate checks
    /// that each materialized chunk supplies every emitted owner exactly once.
    /// An empty list is valid when the chunk emits no non-entry owners.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub import_order: BTreeMap<String, Vec<ModulePath>>,
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

/// Author-declared behaviors of the embedding host. Each key is one assertion
/// with one engine reader; an omitted key takes its conservative value. The
/// identity of the executable a behavior was audited against is provenance in
/// `note` and the project's pinning doc, never an engine input.
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HostBehavior {
    #[serde(default)]
    pub synchronous_module_loader: SynchronousModuleLoader,
    /// Only with [`SynchronousModuleLoader::Present`]; validated in the pipeline.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub loader_result_for_evaluating_module: Option<LoaderResultForEvaluatingModule>,
    /// Only with [`SynchronousModuleLoader::Present`]; validated in the pipeline.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub synchronous_load_runs_app_callbacks: Option<SynchronousLoadRunsAppCallbacks>,
    /// Only with [`SynchronousModuleLoader::Present`]; validated in the pipeline.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub synchronous_load_drains_microtasks: Option<SynchronousLoadDrainsMicrotasks>,
    /// Closed Node builtin IDs whose bare and `node:` static import spellings
    /// the author audited for resolver identity and no synchronous application
    /// reentry during module evaluation. Independent of `reentry_free_operations`.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub reentry_free_builtin_modules: BTreeSet<NodeBuiltinModule>,
    /// Closed host-provided non-Node module IDs the author audited the same
    /// way, including that a bare request always resolves to the host module
    /// rather than an installed package.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub reentry_free_bun_modules: BTreeSet<BunModule>,
    /// Loaders the author audited as running no application code when the host
    /// requires an embedded file it assigned that loader to.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub reentry_free_embedded_loaders: BTreeSet<EmbeddedLoader>,
    /// Exact zero-argument host operations the author audited as unable to run
    /// application code synchronously. Grants timing only, never purity.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub reentry_free_operations: BTreeSet<ReentryFreeOperation>,
    /// Which properties the host defines on the global object, own or
    /// inherited through its host prototypes, before the program runs.
    #[serde(default, skip_serializing_if = "predefined_globals_is_unknown")]
    pub predefined_globals: PredefinedGlobals,
    /// Audit provenance (executable identity, revision, digest). YAML-only.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// Author premises about the analyzed program itself, as opposed to its host.
/// Each key is one assertion with one engine reader; an omitted key asserts
/// nothing.
#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProgramPremises {
    #[serde(
        default,
        skip_serializing_if = "is_unknown_process_prototype_chain_accessors"
    )]
    pub process_prototype_chain_accessors: ProcessPrototypeChainAccessors,
    #[serde(default, skip_serializing_if = "is_unknown_non_literal_loader_calls")]
    pub non_literal_loader_calls: NonLiteralLoaderCalls,
    /// The program writes no slot of these intrinsic families, beyond the
    /// writes the native census sees on their exact objects (SPEC.md §
    /// Intrinsics never written).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub intrinsics_never_written: Option<IntrinsicsNeverWritten>,
    /// Audit provenance. YAML-only.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// `program_premises.intrinsics_never_written`.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct IntrinsicsNeverWritten {
    pub families: IntrinsicFamilies,
    /// Audit provenance: the generated audit list and the runtime probe.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// `all`, or a list of census families.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(untagged)]
pub enum IntrinsicFamilies {
    All(AllIntrinsicFamilies),
    Listed(BTreeSet<IntrinsicFamily>),
}

pub use spec_wire_proto::debundle::spec::AllIntrinsicFamilies;

/// A native census family, named by its global binding.
pub use spec_wire_proto::debundle::spec::IntrinsicFamily;

pub const ALL_INTRINSIC_FAMILIES: [IntrinsicFamily; 13] = [
    IntrinsicFamily::Error,
    IntrinsicFamily::FinalizationRegistry,
    IntrinsicFamily::Function,
    IntrinsicFamily::Object,
    IntrinsicFamily::Array,
    IntrinsicFamily::Math,
    IntrinsicFamily::RegExp,
    IntrinsicFamily::Symbol,
    IntrinsicFamily::Promise,
    IntrinsicFamily::Map,
    IntrinsicFamily::Set,
    IntrinsicFamily::WeakMap,
    IntrinsicFamily::WeakSet,
];

impl IntrinsicFamilies {
    pub fn families(&self) -> BTreeSet<IntrinsicFamily> {
        match self {
            Self::All(_) => ALL_INTRINSIC_FAMILIES.into_iter().collect(),
            Self::Listed(families) => families.clone(),
        }
    }
}

/// Whether the program defines accessors on `process` or its prototype chain.
pub use spec_wire_proto::debundle::spec::ProcessPrototypeChainAccessors;

fn is_unknown_process_prototype_chain_accessors(value: &ProcessPrototypeChainAccessors) -> bool {
    *value == ProcessPrototypeChainAccessors::Unknown
}

/// What a loader call with a non-literal argument can load.
pub use spec_wire_proto::debundle::spec::NonLiteralLoaderCalls;

fn is_unknown_non_literal_loader_calls(value: &NonLiteralLoaderCalls) -> bool {
    *value == NonLiteralLoaderCalls::Unknown
}

/// The global object's host-defined properties before the program runs.
pub use spec_wire_proto::debundle::spec::PredefinedGlobals;

fn predefined_globals_is_unknown(value: &PredefinedGlobals) -> bool {
    *value == PredefinedGlobals::Unknown
}

/// Whether any host API can synchronously obtain a module namespace while that
/// module is evaluating. `none` is what native browser ESM provides.
pub use spec_wire_proto::debundle::spec::SynchronousModuleLoader;

pub use spec_wire_proto::debundle::spec::LoaderResultForEvaluatingModule;

pub use spec_wire_proto::debundle::spec::SynchronousLoadRunsAppCallbacks;

pub use spec_wire_proto::debundle::spec::SynchronousLoadDrainsMicrotasks;

/// Exact zero-argument host operations recognized by the synchronous-effect
/// interpreter (SPEC.md § Host behavior).
pub use spec_wire_proto::debundle::spec::ReentryFreeOperation;

/// Exact Node builtin module identities admissible in
/// `reentry_free_builtin_modules`. Unknown bare packages are absent; Bun's
/// package replacements are [`BunModule`]s.
pub use spec_wire_proto::debundle::spec::NodeBuiltinModule;

pub trait NodeBuiltinModuleExt: Sized {
    /// Recognize only explicitly supported static-import spellings. This is not a
    /// bare-package or `node:` prefix fallback resolver.
    fn from_static_specifier(specifier: &str) -> Option<Self>;
    /// Building or loading this builtin's namespace can run accessors the
    /// program defines on `process` or its prototype chain.
    fn reads_process_accessors(self) -> bool;
}

impl NodeBuiltinModuleExt for NodeBuiltinModule {
    fn from_static_specifier(specifier: &str) -> Option<Self> {
        Some(match specifier {
            "assert" | "node:assert" => Self::Assert,
            "async_hooks" | "node:async_hooks" => Self::AsyncHooks,
            "buffer" | "node:buffer" => Self::Buffer,
            "child_process" | "node:child_process" => Self::ChildProcess,
            "constants" | "node:constants" => Self::Constants,
            "crypto" | "node:crypto" => Self::Crypto,
            "dns" | "node:dns" => Self::Dns,
            "dns/promises" | "node:dns/promises" => Self::DnsPromises,
            "events" | "node:events" => Self::Events,
            "fs" | "node:fs" => Self::Fs,
            "fs/promises" | "node:fs/promises" => Self::FsPromises,
            "http" | "node:http" => Self::Http,
            "http2" | "node:http2" => Self::Http2,
            "https" | "node:https" => Self::Https,
            "net" | "node:net" => Self::Net,
            "os" | "node:os" => Self::Os,
            "path" | "node:path" => Self::Path,
            "path/posix" | "node:path/posix" => Self::PathPosix,
            "path/win32" | "node:path/win32" => Self::PathWin32,
            "perf_hooks" | "node:perf_hooks" => Self::PerfHooks,
            "process" | "node:process" => Self::Process,
            "querystring" | "node:querystring" => Self::Querystring,
            "readline" | "node:readline" => Self::Readline,
            "stream" | "node:stream" => Self::Stream,
            "stream/consumers" | "node:stream/consumers" => Self::StreamConsumers,
            "stream/promises" | "node:stream/promises" => Self::StreamPromises,
            "string_decoder" | "node:string_decoder" => Self::StringDecoder,
            "timers/promises" | "node:timers/promises" => Self::TimersPromises,
            "tls" | "node:tls" => Self::Tls,
            "tty" | "node:tty" => Self::Tty,
            "url" | "node:url" => Self::Url,
            "util" | "node:util" => Self::Util,
            "util/types" | "node:util/types" => Self::UtilTypes,
            "vm" | "node:vm" => Self::Vm,
            "worker_threads" | "node:worker_threads" => Self::WorkerThreads,
            "module" | "node:module" => Self::Module,
            "zlib" | "node:zlib" => Self::Zlib,
            _ => return None,
        })
    }

    fn reads_process_accessors(self) -> bool {
        matches!(self, Self::Process | Self::WorkerThreads)
    }
}

/// Exact host-provided modules outside Node's builtin namespace admissible in
/// `reentry_free_bun_modules`: Bun's own `bun:` modules and the packages Bun
/// replaces with built-in implementations.
pub use spec_wire_proto::debundle::spec::BunModule;

pub trait BunModuleExt: Sized {
    /// Recognize only the exact request spellings.
    fn from_static_specifier(specifier: &str) -> Option<Self>;
}

impl BunModuleExt for BunModule {
    fn from_static_specifier(specifier: &str) -> Option<Self> {
        Some(match specifier {
            "bun:ffi" => Self::BunFfi,
            "undici" => Self::Undici,
            "ws" => Self::Ws,
            _ => return None,
        })
    }
}

/// A module the host provides itself, named in one of the closed lists.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Deserialize, Serialize)]
#[serde(untagged)]
pub enum HostModule {
    Node(NodeBuiltinModule),
    Bun(BunModule),
}

impl HostModule {
    pub fn from_static_specifier(specifier: &str) -> Option<Self> {
        NodeBuiltinModule::from_static_specifier(specifier)
            .map(Self::Node)
            .or_else(|| BunModule::from_static_specifier(specifier).map(Self::Bun))
    }
}

impl HostBehavior {
    /// Whether the author declared `module` reentry-free.
    pub fn admits(&self, module: HostModule) -> bool {
        match module {
            HostModule::Node(builtin) => self.reentry_free_builtin_modules.contains(&builtin),
            HostModule::Bun(bun) => self.reentry_free_bun_modules.contains(&bun),
        }
    }

    /// The declared reentry-free host module `specifier` names.
    pub fn reentry_free_module(&self, specifier: &str) -> Option<HostModule> {
        HostModule::from_static_specifier(specifier).filter(|module| self.admits(*module))
    }
}

impl TransformSpec {
    /// Provider ASTs needed to resolve source-bound premises, independently of
    /// which chunks are selected for materialization.
    pub fn source_premise_chunk_ids(&self) -> BTreeSet<String> {
        let mut chunks: BTreeSet<String> = self
            .chunk_analysis_options
            .iter()
            .filter(|(_, options)| {
                !options.value_inputs.is_empty()
                    || !options.native_operation_inputs.is_empty()
                    || !options.initializer_call_effect_inputs.is_empty()
                    || !options.imported_primitive_array_spread_inputs.is_empty()
                    || !options.native_call_identity_inputs.is_empty()
                    || !options.argument_inputs.is_empty()
                    || !options.no_accessor_install_callables.is_empty()
            })
            .map(|(chunk, _)| chunk.clone())
            .collect();
        chunks.extend(
            self.chunk_analysis_options
                .values()
                .flat_map(|options| &options.imported_primitive_array_spread_inputs)
                .flat_map(|input| &input.operands)
                .map(|operand| operand.provider_chunk.clone()),
        );
        chunks
    }

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
    /// Export names the author asserts are **deeply** pure fluent-API
    /// roots: member reads and calls on the export, *and on every value
    /// transitively derived from it* through static member reads and
    /// calls, have no observable side effects (call arguments are still
    /// classified normally). This is the only surface that reaches
    /// builder-style chains whose receivers are call results rather
    /// than bindings — `z.object({...}).optional().describe(...)` —
    /// which `pure_exports` and binding-level `pure_members` cannot.
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
    /// exported namespace. Independent of binding-level `pure_members`; each member has
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

impl RetainedCallbackSignature {
    pub fn validate(&self) -> std::result::Result<(), &'static str> {
        if self.callback_arguments.is_empty() {
            return Err("retained callback signature must list at least one callback argument");
        }
        if self
            .callback_arguments
            .iter()
            .any(|position| *position >= self.argument_count)
        {
            return Err("retained callback signature position must be less than argument_count");
        }
        Ok(())
    }
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
        let signature = Self {
            argument_count: signature.argument_count,
            callback_arguments: signature.callback_arguments,
        };
        signature.validate().map_err(serde::de::Error::custom)?;
        Ok(signature)
    }
}

/// Per-chunk analysis and dependency-graph options. Each field defaults to the
/// strictly-conservative behavior; opt-ins enable conditionally-correct
/// inferences that hold only when the input satisfies a checkable
/// precondition (see `devinfra/js/debundle/AGENTS.md` →
/// "Conditionally-correct optimizations").
///
/// The YAML authoring surface for per-chunk source-analysis and graph inputs.
/// The stage boundary maps every field into the source-analysis inputs, graph
/// options, commuting contracts, or normalized native-operation premises.
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DependencyGraphOptions {
    /// Author-conditioned value facts for exact top-level provider bindings.
    /// These are resolved by the whole-program source analysis before graph
    /// construction and never select emission roots.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub value_inputs: Vec<ValueInputContract>,
    /// Author-certified native behavior for one exact initializer.
    /// The author audits the ordinary initial global binding and every
    /// synchronous initialization schedule admitted by emission. This is
    /// neither a source inference nor a whole-lifetime integrity claim. See
    /// docs/purity_soundness.md.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_operation_inputs: Vec<NativeOperationInputContract>,
    /// Exact direct imported-call initializers whose provider invocation has
    /// author-audited allocation-only nonlexical effects. This is independent
    /// of call timing, result shape, purity, and callback retention contracts.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub initializer_call_effect_inputs: Vec<InitializerCallEffectInputContract>,
    /// Exact-site author premise for spreading directly imported arrays whose
    /// exported identity and contents otherwise remain mutable. The selected
    /// provider declarations and import edges are resolved from source; this
    /// input only supplies the explicit lifetime audit for the exact spread.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub imported_primitive_array_spread_inputs: Vec<ImportedPrimitiveArraySpreadInputContract>,
    /// Author-certified pairs of this chunk's source units whose synchronous
    /// initialization effects are order-independent. Each endpoint selects
    /// exactly one source unit; the pair is resolved and validated when the
    /// chunk's dependency graph is built. See SPEC.md § Commuting statements.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub commuting_statements: Vec<CommutingStatementsContract>,
    /// Author-certified original native callee at one selected direct call in
    /// a top-level function body. This supplies identity only; the modeled
    /// operation must still pass its ordinary argument and effect guards.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_call_identity_inputs: Vec<NativeCallIdentityInputContract>,
    /// Audited no-mutation/no-retention contract for one parameter of an
    /// exact local helper. Calls and callbacks retain their ordinary effects.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub argument_inputs: Vec<ArgumentInputContract>,
    /// Author-certified callables whose computed-key method calls never
    /// install an accessor through `__defineGetter__` or `__defineSetter__`.
    /// Each entry selects exactly one outermost function. See SPEC.md § Bundle-owned
    /// globals.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub no_accessor_install_callables: Vec<NoAccessorInstallCallableContract>,
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

/// A previously resolved logical member in this provider chunk.
pub use spec_wire_proto::debundle::spec::ResolvedMemberSelector;

/// Typed native callee models eligible for exact-site identity premises.
pub use spec_wire_proto::debundle::spec::NativeCallIdentityKind;

/// Native operations admitted by `native_operation_inputs`.
pub use spec_wire_proto::debundle::spec::NativeOperationKind;

pub use spec_wire_proto::debundle::spec::CapturedValueKind;

pub use spec_wire_proto::debundle::spec::BuiltinGlobalRoot;

/// One named input-chunk admission check, identified by the
/// docs/design.md assumption it enforces. Used both as the
/// spec-override list-element type and as the violation tag in
/// admission diagnostics.
///
/// `Display` is the spec-facing snake_case name (the `admission_overrides`
/// list element spelling), for diagnostics.
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Deserialize, Serialize, strum::Display,
)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum AdmissionCheck {
    /// A1: no `eval(...)` / `(0, eval)(...)` at module top level, and no
    /// direct `eval(...)` in a chunk the assignment splits, reorders or
    /// renames.
    A1Eval,
    /// A3: no dynamic `import()` of a debundled internal module
    /// (same-chunk literal target, or non-literal specifier at
    /// module top level).
    A3DynamicImport,
    /// A5 (partial): no `import.meta` reflection beyond
    /// `import.meta.url` at module top level.
    A5ImportMeta,
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
    /// JSON object mapping each embedded file's input path to the loader the
    /// host applies when it is required (the extractor's `--embedded-loaders`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embedded_loaders_path: Option<PathBuf>,
}

/// A Bun `options.Loader`, as an embedded-loader manifest names it.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BunLoader {
    Jsx,
    Js,
    Ts,
    Tsx,
    Css,
    File,
    Json,
    Jsonc,
    Toml,
    Wasm,
    Napi,
    Base64,
    Dataurl,
    Text,
}

impl BunLoader {
    /// The closed-list entry for a loader the host applies by the recorded
    /// byte and that evaluates no JavaScript. A `file` entry is absent: the
    /// host loads it by extension, so a `.js` or `.mjs` file runs as code.
    pub fn reentry_free(self) -> Option<EmbeddedLoader> {
        Some(match self {
            Self::Text => EmbeddedLoader::Text,
            Self::Napi => EmbeddedLoader::Napi,
            _ => return None,
        })
    }
}

/// Embedded-file loaders admissible in `reentry_free_embedded_loaders`.
pub use spec_wire_proto::debundle::spec::EmbeddedLoader;

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

/// What [`TransformSpec::unassigned_mode`] means for a chunk's top-level
/// statements that the YAML doesn't explicitly claim for any logical module.
/// The atomic-group primitive in `graph::atomic_groups` partitions the
/// chunk's source units into minimal co-location groups; this enum decides how
/// unclaimed source units are routed, with an explicit named-owner completion
/// opt-in available under [`CoarseAtomicGroups`](Self::CoarseAtomicGroups).
///
/// The variants are mutually exclusive — every chunk picks
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
    /// Merge wholly-unclaimed atomic groups with equal reachable fixed-owner
    /// sets in the strict constraining module projection. Output paths use
    /// `__auto/residual_groups/NNNN`. The final assignment still goes through
    /// the ordinary realizability gate. Each listed authored module may also
    /// adopt unclaimed source units from an atomic group when it is that group's
    /// sole authored owner.
    CoarseAtomicGroups {
        /// Explicitly opted-in authored module paths allowed to adopt the
        /// unclaimed remainder of atomic groups they already own. Every path
        /// must name a module in this chunk's authored `logical_modules` map.
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        complete_partial_atomic_groups_for: Vec<String>,
    },
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

    /// Authored module paths explicitly allowed to complete a partial atomic
    /// group under [`CoarseAtomicGroups`](Self::CoarseAtomicGroups).
    pub fn partial_atomic_group_completion_owners(&self) -> &[String] {
        match self {
            UnassignedMode::CoarseAtomicGroups {
                complete_partial_atomic_groups_for,
            } => complete_partial_atomic_groups_for,
            _ => &[],
        }
    }
}

pub use spec_wire_proto::debundle::spec::PartialSwapKind;

pub use spec_wire_proto::debundle::spec::WrapperShape;

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq, Eq)]
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
    /// Required when a source-unit assignment includes side-effect
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
    /// `docs/spec_editing.md` § Comments and notes.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comment: Option<String>,
    /// Optional YAML-only note: module-level provenance / honest-debt
    /// rationale (e.g. `merged from: …` provenance written by
    /// `modules merge`) that survives spec edits without appearing in
    /// generated JS. Ignored by the lowering pass — unlike `comment:`,
    /// which emits a `//` block. Same non-emitting contract and STYLE.md local
    /// exemption (see AGENTS.md § Spec).
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

pub use spec_wire_proto::debundle::spec::SourceMatchBindingDetail;

pub use spec_wire_proto::debundle::spec::SourceMatchIdentifierMode;

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
        // Not `relative-path`: its components skip the empty segments rejected here.
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
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("not a valid relative module path: {0:?}")]
pub struct ModulePathError(String);

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

pub use spec_wire_proto::debundle::spec::BindingSourceKind;

pub use spec_wire_proto::debundle::spec::MemberPurity;

/// Trust contract scoped to evaluation of one selected variable initializer.
/// Unlike [`MemberPurity`], this says nothing about later uses of its value.
pub use spec_wire_proto::debundle::spec::InitializerPurity;

/// Lexical effects allowed while evaluating one selected top-level variable
/// initializer.
pub use spec_wire_proto::debundle::spec::InitializerBindingEffects;

/// External effects of one selected simple top-level variable initializer.
pub use spec_wire_proto::debundle::spec::InitializerExternalEffects;

pub use spec_wire_proto::debundle::spec::MemberEffect;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_selector_leaves_keep_yaml_requiredness_and_json_shape() {
        let resolved: ResolvedMemberSelector =
            serde_yaml::from_str("module: ''\nname: ''\n").unwrap();
        assert_eq!(resolved.module, "");
        assert_eq!(resolved.name, "");
        assert_eq!(
            serde_json::to_value(&resolved).unwrap(),
            serde_json::json!({"module": "", "name": ""})
        );
        assert!(serde_yaml::from_str::<ResolvedMemberSelector>("module: provider\n").is_err());
        assert!(
            serde_yaml::from_str::<ResolvedMemberSelector>(
                "module: provider\nname: factory\nextra: rejected\n"
            )
            .is_err()
        );

        let detail: SourceMatchBindingDetail = serde_yaml::from_str("local: ''\n").unwrap();
        assert_eq!(detail.local, "");
        assert_eq!(detail.name, None);
        assert_eq!(
            serde_json::to_value(&detail).unwrap(),
            serde_json::json!({"local": ""})
        );
        let named_empty: SourceMatchBindingDetail =
            serde_yaml::from_str("local: alias\nname: ''\n").unwrap();
        assert_eq!(named_empty.name.as_deref(), Some(""));
        assert_eq!(
            serde_json::to_value(&named_empty).unwrap(),
            serde_json::json!({"local": "alias", "name": ""})
        );
        assert!(serde_yaml::from_str::<SourceMatchBindingDetail>("name: alias\n").is_err());
        assert!(
            serde_yaml::from_str::<SourceMatchBindingDetail>(
                "local: alias\nname: alias\nextra: rejected\n"
            )
            .is_err()
        );
    }

    #[test]
    fn generated_enums_keep_authored_spellings_and_defaults() {
        fn check<T>(value: T, spelling: &str)
        where
            T: std::fmt::Debug + PartialEq + Serialize + serde::de::DeserializeOwned,
        {
            assert_eq!(serde_yaml::from_str::<T>(spelling).unwrap(), value);
            assert_eq!(
                serde_json::to_string(&value).unwrap(),
                format!("\"{spelling}\"")
            );
        }

        check(
            WrapperShape::NamedFromJsonDefault,
            "named_from_json_default",
        );
        check(BindingSourceKind::ImportSpecifier, "import_specifier");
        check(MemberPurity::PureNew, "pure_new");
        check(InitializerPurity::Pure, "pure");
        check(InitializerBindingEffects::SourceOnly, "source_only");
        check(
            MemberEffect::TypescriptMetadataDecorateHelperThreeArgs,
            "typescript_metadata_decorate_helper_three_args",
        );
        check(BuiltinGlobalRoot::GlobalThis, "global_this");
        check(NodeBuiltinModule::FsPromises, "fs_promises");
        check(BunModule::BunFfi, "bun_ffi");
        check(AllIntrinsicFamilies::All, "all");
        check(CapturedValueKind::OrdinaryObject, "ordinary_object");
        check(EmbeddedLoader::Napi, "napi");
        check(
            LoaderResultForEvaluatingModule::ReturnsPartialNamespace,
            "returns_partial_namespace",
        );
        check(NativeCallIdentityKind::ObjectFreeze, "object_freeze");
        check(PartialSwapKind::Namespace, "namespace");
        check(SourceMatchIdentifierMode::AlphaAll, "alpha_all");
        check(SynchronousLoadDrainsMicrotasks::Never, "never");
        check(SynchronousLoadRunsAppCallbacks::Unknown, "unknown");
        check(SynchronousModuleLoader::Present, "present");
        check(ProcessPrototypeChainAccessors::None, "none");
        check(
            NonLiteralLoaderCalls::NativeAddonsOnly,
            "native_addons_only",
        );
        check(
            IntrinsicFamily::FinalizationRegistry,
            "finalization_registry",
        );
        check(PredefinedGlobals::Standard, "standard");
        check(
            ReentryFreeOperation::AsyncLocalStorageConstruct,
            "async_local_storage_construct",
        );
        check(
            InitializerExternalEffects::PrivateMetadata,
            "private_metadata",
        );

        assert_eq!(MemberPurity::default(), MemberPurity::Default);
        assert_eq!(InitializerPurity::default(), InitializerPurity::Default);
        assert_eq!(MemberEffect::default(), MemberEffect::Default);
        assert_eq!(PartialSwapKind::default(), PartialSwapKind::Member);
        assert_eq!(
            SourceMatchIdentifierMode::default(),
            SourceMatchIdentifierMode::AlphaAll
        );
        assert_eq!(
            SynchronousModuleLoader::default(),
            SynchronousModuleLoader::Present
        );
        assert_eq!(
            ProcessPrototypeChainAccessors::default(),
            ProcessPrototypeChainAccessors::Unknown
        );
        assert_eq!(
            NonLiteralLoaderCalls::default(),
            NonLiteralLoaderCalls::Unknown
        );
        assert_eq!(PredefinedGlobals::default(), PredefinedGlobals::Unknown);
    }
}
