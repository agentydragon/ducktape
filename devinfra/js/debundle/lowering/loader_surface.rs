//! Source facts and report shapes for the loader-closed derivation of the
//! export-reentry policy (SPEC.md § Host behavior).

use std::collections::{BTreeMap, BTreeSet};

use analysis::analysis_inputs::{HostBehaviorKey, ProgramPremiseKey};
use analysis::loader_closure::{
    ChunkLoaderSurface, LoaderClosure, LoaderClosureFailureKind, LoaderClosureWitness,
    LoaderRequest, LoaderRequestTarget, SitePlacement,
};
use anyhow::Result;
use artifact::reports::{
    ImportSpecifierKind, InteropUseKind, LoaderBinding, LoaderBindingUse, LoaderUseConsumer,
    SitePhase, StaticModuleRequestMode,
};
use artifact::{ArtifactIndexes, ChunkBundle, ChunkId, ResolvedImportReference};
use serde::Serialize;
use swc_common::Span;

use super::{ExportReentryTarget, report_target};

/// How the loader premise of the policy was derived for one target.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "rule", rename_all = "snake_case")]
pub enum ExportReentryLoaderDerivation {
    /// `synchronous_module_loader: none`.
    NoSynchronousLoader,
    /// A loader is present but no analyzed chunk has a host-loader site.
    NoHostLoaderSites,
    LoaderClosure {
        closed: bool,
        /// Host keys the closed certificate consumed.
        #[serde(skip_serializing_if = "BTreeSet::is_empty")]
        host_keys: BTreeSet<HostBehaviorKey>,
        /// Program premises the closed certificate consumed.
        #[serde(skip_serializing_if = "BTreeSet::is_empty")]
        premise_keys: BTreeSet<ProgramPremiseKey>,
        #[serde(skip_serializing_if = "Vec::is_empty")]
        requests: Vec<ExportReentryLoaderRequest>,
        /// Chunks a loader request can evaluate, with their closures.
        #[serde(skip_serializing_if = "Vec::is_empty")]
        loaded_chunks: Vec<String>,
        /// Mandatorily suspended sites excluded from the closure.
        #[serde(skip_serializing_if = "is_zero")]
        suspended_sites: usize,
        /// Non-literal loader calls excluded under the program premise.
        #[serde(skip_serializing_if = "is_zero")]
        premised_sites: usize,
        /// Requests and failing sites that cannot run in the target's window.
        #[serde(skip_serializing_if = "is_zero")]
        outside_window_sites: usize,
        /// Loader sites no reachable code can enter, excluded from the closure.
        #[serde(skip_serializing_if = "is_zero")]
        unreachable_sites: usize,
        /// The first witness and occurrence count of each failure kind.
        #[serde(skip_serializing_if = "Vec::is_empty")]
        failures: Vec<ExportReentryLoaderFailure>,
        /// When reach-back is the only failure: the target's export names that
        /// the chunks it can evaluate import (SPEC.md § Reentry through
        /// imported names).
        #[serde(skip_serializing_if = "Option::is_none")]
        reentry_through: Option<BTreeSet<String>>,
    },
}

impl ExportReentryLoaderDerivation {
    pub fn permits_export_reentry_proof(&self) -> bool {
        match self {
            Self::NoSynchronousLoader | Self::NoHostLoaderSites => true,
            Self::LoaderClosure { closed, .. } => *closed,
        }
    }

    pub fn reentry_through(&self) -> Option<&BTreeSet<String>> {
        match self {
            Self::LoaderClosure {
                reentry_through, ..
            } => reentry_through.as_ref(),
            Self::NoSynchronousLoader | Self::NoHostLoaderSites => None,
        }
    }

    pub(super) fn from_closure(
        target: ChunkId,
        closure: &LoaderClosure<LoaderSiteWitness>,
        surfaces: &LoaderSurfaces,
        artifact: &ChunkBundle,
        indexes: &ArtifactIndexes,
    ) -> Result<Self> {
        let reentry_through = reentry_names(target, closure, surfaces, artifact, indexes)?;
        Ok(Self::LoaderClosure {
            reentry_through,
            closed: closure.is_closed(),
            host_keys: closure.dependencies.host_behavior.clone(),
            premise_keys: closure.dependencies.program_premises.clone(),
            requests: surfaces.requests.clone(),
            loaded_chunks: closure
                .loaded_chunks
                .iter()
                .map(|chunk| artifact.chunk_table.name(*chunk).to_string())
                .collect(),
            suspended_sites: closure.suspended_sites,
            premised_sites: closure.premised_sites,
            outside_window_sites: closure.outside_window_sites,
            unreachable_sites: surfaces.unreachable_sites,
            failures: closure
                .failures
                .iter()
                .map(|(reason, failure)| ExportReentryLoaderFailure {
                    reason: *reason,
                    occurrences: failure.occurrences,
                    first: failure.first.clone(),
                })
                .collect(),
        })
    }
}

/// The target's export names the reach-back windows import, or `None` when a
/// window can obtain its whole namespace or reach-back is not the only
/// failure. A window chunk that finishes evaluating while the target evaluates
/// keeps live bindings to exactly what it imports: its body and every later
/// call of its functions read only those.
fn reentry_names(
    target: ChunkId,
    closure: &LoaderClosure<LoaderSiteWitness>,
    surfaces: &LoaderSurfaces,
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
) -> Result<Option<BTreeSet<String>>> {
    if closure.target_requested
        || closure.failures.keys().copied().collect::<Vec<_>>()
            != [LoaderClosureFailureKind::LoaderRequestReachesTarget]
    {
        return Ok(None);
    }
    let resolves_to_target = |specifier: &str, from: ChunkId, entry_file: &str| {
        indexes
            .resolve_runtime_import_reference(specifier, from, entry_file, &artifact.chunk_table)
            .is_some_and(|reference| reference.target_chunk_id == target)
    };
    let mut names = BTreeSet::new();
    for &chunk in &closure.reentry_window {
        if chunk == target {
            continue;
        }
        if surfaces.chunks.get(&chunk).is_some_and(|surface| {
            surface
                .requests
                .iter()
                .any(|request| request.target == LoaderRequestTarget::Chunk(target))
        }) {
            return Ok(None);
        }
        let analysis = &artifact.chunk(chunk)?.analysis;
        if analysis
            .runtime_facts
            .namespace_requests
            .iter()
            .any(|request| resolves_to_target(&request.specifier, chunk, &analysis.entry_file))
        {
            return Ok(None);
        }
        for import in &analysis.imports {
            if !resolves_to_target(&import.source, chunk, &analysis.entry_file) {
                continue;
            }
            for specifier in &import.specifiers {
                match specifier.kind {
                    ImportSpecifierKind::Named => {
                        names.insert(
                            specifier
                                .imported
                                .clone()
                                .unwrap_or_else(|| specifier.local.clone()),
                        );
                    }
                    ImportSpecifierKind::Default => {
                        names.insert("default".to_string());
                    }
                    ImportSpecifierKind::Namespace => return Ok(None),
                }
            }
        }
    }
    Ok(Some(names))
}

fn is_zero(count: &usize) -> bool {
    *count == 0
}

/// Literal loader requests that resolved, aggregated per source and target.
#[derive(Debug, Clone, Serialize)]
pub struct ExportReentryLoaderRequest {
    pub source_chunk: String,
    pub specifier: String,
    pub target: LoaderRequestReportTarget,
    pub request_count: usize,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LoaderRequestReportTarget {
    Builtin(spec::HostModule),
    EmbeddedFile(spec::EmbeddedLoader),
    Chunk(ExportReentryTarget),
}

#[derive(Debug, Clone, Serialize)]
pub struct ExportReentryLoaderFailure {
    pub reason: LoaderClosureFailureKind,
    pub occurrences: usize,
    pub first: LoaderClosureWitness<LoaderSiteWitness>,
}

/// One source site that keeps a target open. Ordered by chunk name, then
/// site, so the reported first witness is deterministic.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize)]
pub struct LoaderSiteWitness {
    pub source_chunk: String,
    #[serde(flatten)]
    pub site: LoaderSite,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize)]
#[serde(tag = "form", rename_all = "snake_case")]
pub enum LoaderSite {
    /// A census site (SPEC.md § Static-import and host-loader queries).
    Interop {
        kind: InteropUseKind,
        item: usize,
        #[serde(skip_serializing_if = "Option::is_none")]
        line: Option<usize>,
        #[serde(skip_serializing_if = "Option::is_none")]
        specifier: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        resolved_target: Option<ExportReentryTarget>,
    },
    StaticRequest {
        specifier: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        resolved_target: Option<ExportReentryTarget>,
    },
    /// A use of a binding holding an exported loader alias.
    LoaderBindingUse {
        item: usize,
        #[serde(skip_serializing_if = "Option::is_none")]
        line: Option<usize>,
        call: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        specifier: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        resolved_target: Option<ExportReentryTarget>,
    },
    /// A request exposing a loader-exporting chunk's whole namespace.
    NamespaceRequest {
        item: usize,
        specifier: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        resolved_target: Option<ExportReentryTarget>,
    },
    TopLevelAwait,
}

pub(super) struct LoaderSurfaces {
    pub chunks: BTreeMap<ChunkId, ChunkLoaderSurface<LoaderSiteWitness>>,
    pub requests: Vec<ExportReentryLoaderRequest>,
    /// Loader sites no reachable code can enter, excluded from the closure.
    pub unreachable_sites: usize,
}

impl LoaderSurfaces {
    pub fn has_loader_sites(&self) -> bool {
        self.chunks
            .values()
            .any(ChunkLoaderSurface::has_loader_sites)
    }
}

/// `createRequire` from the `module` builtin makes a loader.
fn is_loader_factory(specifier: &str) -> bool {
    spec::NodeBuiltinModule::from_static_specifier(specifier)
        == Some(spec::NodeBuiltinModule::Module)
}

/// Data members of the `module` builtin whose read cannot load or hook.
const MODULE_DATA_MEMBERS: &[&str] = &["builtinModules"];

fn is_module_data_read(consumer: &LoaderUseConsumer) -> bool {
    matches!(consumer, LoaderUseConsumer::MemberRead(name) if MODULE_DATA_MEMBERS.contains(&name.as_str()))
}

/// Whether a census site can load a module on the stack that runs it.
fn is_loader_site(kind: InteropUseKind) -> bool {
    match kind {
        InteropUseKind::ImportMetaRequireLiteralCall
        | InteropUseKind::ImportMetaRequireDynamicCall
        | InteropUseKind::ImportMetaRequireReference
        | InteropUseKind::GlobalThisRequire
        | InteropUseKind::UnboundRequire
        | InteropUseKind::EvaluatedRequireLiteralCall
        | InteropUseKind::EvaluatedRequireDynamicCall
        | InteropUseKind::UnboundModule
        | InteropUseKind::ImportMetaDynamicProperty
        | InteropUseKind::ImportMetaValue
        | InteropUseKind::TsImportEquals => true,
        // Export surfaces and fixed non-loader `import.meta` members cannot
        // load a module. `import()` evaluates its target in a later job
        // (ECMA-262 ContinueDynamicImport), never on the requester's stack;
        // A3 admission owns internal targets.
        InteropUseKind::DynamicImport
        | InteropUseKind::UnboundModuleExports
        | InteropUseKind::UnboundExports
        | InteropUseKind::ImportMetaOtherProperty
        | InteropUseKind::TsExportAssignment => false,
    }
}

/// How a classified use may load a module.
enum LoaderUse<'s> {
    /// A direct call with one string literal, resolved against `resolve_from`
    /// (the module whose `import.meta` supplied the loader).
    Literal {
        specifier: &'s str,
        resolve_from: ChunkId,
        /// The returned namespace is only read for a `module` data member.
        data_read_only: bool,
    },
    /// A direct call whose argument is not one string literal.
    NonLiteralCall,
    Opaque,
}

/// Classifies loader uses against the whole artifact.
struct Classifier<'a> {
    artifact: &'a ChunkBundle,
    indexes: &'a ArtifactIndexes,
    host_behavior: &'a spec::HostBehavior,
    program_premises: &'a spec::ProgramPremises,
    embedded_loaders: &'a BTreeMap<String, spec::BunLoader>,
    /// Chunks exporting a loader alias: any request exposing their namespace
    /// exposes the loader.
    alias_chunks: BTreeSet<ChunkId>,
    request_counts: BTreeMap<(String, String, LoaderRequestReportTarget), usize>,
    unreachable_sites: usize,
}

impl Classifier<'_> {
    fn resolve(&self, specifier: &str, from: ChunkId) -> Result<Option<ResolvedImportReference>> {
        Ok(self.indexes.resolve_runtime_import_reference(
            specifier,
            from,
            &self.artifact.chunk(from)?.analysis.entry_file,
            &self.artifact.chunk_table,
        ))
    }

    /// The analyzed chunk whose entry `reference` names.
    fn entry_chunk(&self, reference: &ResolvedImportReference) -> Result<Option<ChunkId>> {
        Ok((self.artifact.has_chunk(reference.target_chunk_id)
            && self
                .artifact
                .chunk(reference.target_chunk_id)?
                .analysis
                .entry_file
                == reference.target_file)
            .then_some(reference.target_chunk_id))
    }

    fn report_target(&self, reference: &ResolvedImportReference) -> ExportReentryTarget {
        report_target(
            reference.target_chunk_id,
            &reference.target_file,
            self.artifact,
        )
    }

    /// Classify one use that may call the loader. `witness` builds the site
    /// witness given the resolved target of a literal request.
    fn classify(
        &mut self,
        surface: &mut ChunkLoaderSurface<LoaderSiteWitness>,
        source_chunk: &str,
        phase: SitePhase,
        loader_use: LoaderUse<'_>,
        witness: impl FnOnce(Option<ExportReentryTarget>) -> LoaderSiteWitness,
    ) -> Result<()> {
        match phase {
            SitePhase::Unreachable => {
                self.unreachable_sites += 1;
                return Ok(());
            }
            // A suspended site runs in a promise job; a synchronous load can
            // start one only by draining microtasks.
            SitePhase::MandatorilySuspended
                if self.host_behavior.synchronous_load_drains_microtasks
                    == Some(spec::SynchronousLoadDrainsMicrotasks::Never) =>
            {
                surface.suspended_sites.push(witness(None));
                return Ok(());
            }
            SitePhase::MandatorilySuspended | SitePhase::Synchronous | SitePhase::ModuleBody => {}
        }
        // A direct load in top-level code runs only while its module's body
        // does; a loader value that escapes can be called from anywhere.
        let placement = match phase {
            SitePhase::ModuleBody => SitePlacement::ModuleBody,
            _ => SitePlacement::Callable,
        };
        let (specifier, resolve_from, data_read_only) = match loader_use {
            LoaderUse::Literal {
                specifier,
                resolve_from,
                data_read_only,
            } => (specifier, resolve_from, data_read_only),
            LoaderUse::NonLiteralCall
                if self.program_premises.non_literal_loader_calls
                    == spec::NonLiteralLoaderCalls::NativeAddonsOnly =>
            {
                surface.premised_sites.push(witness(None));
                return Ok(());
            }
            LoaderUse::NonLiteralCall => {
                surface.fail(
                    LoaderClosureFailureKind::OpaqueLoaderSite,
                    witness(None),
                    placement,
                );
                return Ok(());
            }
            LoaderUse::Opaque => {
                surface.fail(
                    LoaderClosureFailureKind::OpaqueLoaderSite,
                    witness(None),
                    SitePlacement::Callable,
                );
                return Ok(());
            }
        };
        let resolved = self.resolve(specifier, resolve_from)?;
        let resolved_target = resolved
            .as_ref()
            .map(|reference| self.report_target(reference));
        let witness = witness(resolved_target.clone());
        if is_loader_factory(specifier) && !data_read_only {
            // The returned `module` namespace makes loaders.
            surface.fail(
                LoaderClosureFailureKind::LoaderFactoryRequest,
                witness,
                SitePlacement::Callable,
            );
            return Ok(());
        }
        let target = match &resolved {
            Some(reference) => match self.entry_chunk(reference)? {
                Some(chunk) => {
                    if self.alias_chunks.contains(&chunk) {
                        // The returned namespace holds the exported loader.
                        surface.fail(
                            LoaderClosureFailureKind::OpaqueLoaderSite,
                            witness.clone(),
                            SitePlacement::Callable,
                        );
                    }
                    (
                        LoaderRequestTarget::Chunk(chunk),
                        LoaderRequestReportTarget::Chunk(resolved_target.expect("resolved target")),
                    )
                }
                None => {
                    surface.fail(
                        LoaderClosureFailureKind::UnresolvableLoaderRequest,
                        witness,
                        placement,
                    );
                    return Ok(());
                }
            },
            None => match spec::HostModule::from_static_specifier(specifier) {
                Some(builtin) if self.host_behavior.admits(builtin) => (
                    LoaderRequestTarget::ReentryFreeBuiltin(builtin),
                    LoaderRequestReportTarget::Builtin(builtin),
                ),
                Some(_) => {
                    surface.fail(
                        LoaderClosureFailureKind::NonReentryFreeBuiltinRequest,
                        witness,
                        placement,
                    );
                    return Ok(());
                }
                // An absolute request names an embedded file by its virtual
                // path; the manifest records the loader the host applies.
                None => match specifier
                    .strip_prefix('/')
                    .and_then(|path| self.embedded_loaders.get(path))
                {
                    Some(loader) => match loader.reentry_free() {
                        Some(embedded)
                            if self
                                .host_behavior
                                .reentry_free_embedded_loaders
                                .contains(&embedded) =>
                        {
                            (
                                LoaderRequestTarget::EmbeddedFile(embedded),
                                LoaderRequestReportTarget::EmbeddedFile(embedded),
                            )
                        }
                        _ => {
                            surface.fail(
                                LoaderClosureFailureKind::NonReentryFreeEmbeddedFile,
                                witness,
                                placement,
                            );
                            return Ok(());
                        }
                    },
                    None => {
                        surface.fail(
                            LoaderClosureFailureKind::UnresolvableLoaderRequest,
                            witness,
                            placement,
                        );
                        return Ok(());
                    }
                },
            },
        };
        *self
            .request_counts
            .entry((source_chunk.to_string(), specifier.to_string(), target.1))
            .or_default() += 1;
        surface.requests.push(LoaderRequest {
            target: target.0,
            witness,
            placement,
        });
        Ok(())
    }
}

/// Classify every analyzed chunk's loader sites and the facts that matter
/// when a loader request evaluates it.
/// The author assertions and input facts the loader-closed derivation reads.
#[derive(Clone, Copy)]
pub(super) struct LoaderAssumptions<'a> {
    pub host_behavior: &'a spec::HostBehavior,
    pub program_premises: &'a spec::ProgramPremises,
    /// Host loader per embedded input file (`inputs.embedded_loaders_path`).
    pub embedded_loaders: &'a BTreeMap<String, spec::BunLoader>,
}

pub(super) fn loader_surfaces(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    assumptions: LoaderAssumptions<'_>,
) -> Result<LoaderSurfaces> {
    let LoaderAssumptions {
        host_behavior,
        program_premises,
        embedded_loaders,
    } = assumptions;
    // Exported loader aliases by defining chunk and export name.
    let mut aliases = BTreeSet::<(ChunkId, String)>::new();
    let mut alias_definitions = BTreeSet::<(ChunkId, Span)>::new();
    for chunk in &artifact.chunks {
        for binding in &chunk.analysis.runtime_facts.loader_bindings {
            if let LoaderBinding::LoaderAlias {
                definition,
                exported_as,
            } = &binding.binding
            {
                alias_definitions.insert((chunk.chunk_id, *definition));
                for name in exported_as {
                    aliases.insert((chunk.chunk_id, name.clone()));
                }
            }
        }
    }
    let mut classifier = Classifier {
        artifact,
        indexes,
        host_behavior,
        program_premises,
        embedded_loaders,
        alias_chunks: aliases.iter().map(|(chunk, _)| *chunk).collect(),
        request_counts: BTreeMap::new(),
        unreachable_sites: 0,
    };
    let mut chunks = BTreeMap::new();
    for chunk in &artifact.chunks {
        let source = chunk.chunk_id;
        let source_chunk = artifact.chunk_table.name(source).to_string();
        let runtime = &chunk.analysis.runtime_facts;
        let witness = |site| LoaderSiteWitness {
            source_chunk: source_chunk.clone(),
            site,
        };
        let mut surface = ChunkLoaderSurface {
            static_targets: BTreeSet::new(),
            requests: Vec::new(),
            site_failures: Vec::new(),
            load_failures: Vec::new(),
            suspended_sites: Vec::new(),
            premised_sites: Vec::new(),
        };
        if runtime.has_top_level_await {
            surface.load_failures.push((
                LoaderClosureFailureKind::LoadedClosureTopLevelAwait,
                witness(LoaderSite::TopLevelAwait),
            ));
        }
        for request in &runtime.static_requests {
            let resolved = classifier.resolve(&request.specifier, source)?;
            let static_witness = || {
                witness(LoaderSite::StaticRequest {
                    specifier: request.specifier.clone(),
                    resolved_target: resolved
                        .as_ref()
                        .map(|reference| classifier.report_target(reference)),
                })
            };
            let closed_request = match &resolved {
                Some(reference) if artifact.has_chunk(reference.target_chunk_id) => {
                    surface.static_targets.insert(reference.target_chunk_id);
                    classifier.entry_chunk(reference)?.is_some()
                }
                Some(_) => false,
                None => host_behavior
                    .reentry_free_module(&request.specifier)
                    .is_some(),
            };
            if !closed_request || request.mode != StaticModuleRequestMode::OrdinaryEsm {
                surface.load_failures.push((
                    LoaderClosureFailureKind::LoadedClosureRequest,
                    static_witness(),
                ));
            }
        }
        for request in &runtime.namespace_requests {
            if is_loader_factory(&request.specifier) {
                // The namespace carries `createRequire` and the loader hooks.
                surface.fail(
                    LoaderClosureFailureKind::LoaderFactoryRequest,
                    witness(LoaderSite::NamespaceRequest {
                        item: request.item_index,
                        specifier: request.specifier.clone(),
                        resolved_target: None,
                    }),
                    SitePlacement::Callable,
                );
                continue;
            }
            let resolved = classifier.resolve(&request.specifier, source)?;
            if let Some(reference) = &resolved
                && classifier.alias_chunks.contains(&reference.target_chunk_id)
            {
                surface.fail(
                    LoaderClosureFailureKind::OpaqueLoaderSite,
                    witness(LoaderSite::NamespaceRequest {
                        item: request.item_index,
                        specifier: request.specifier.clone(),
                        resolved_target: Some(classifier.report_target(reference)),
                    }),
                    SitePlacement::Callable,
                );
            }
        }
        for site in &runtime.interop_uses {
            let interop_witness = |resolved_target| {
                witness(LoaderSite::Interop {
                    kind: site.kind,
                    item: site.item_index,
                    line: site.line,
                    specifier: site.specifier.clone(),
                    resolved_target,
                })
            };
            let loader_use = match site.kind {
                InteropUseKind::ImportMetaRequireLiteralCall => LoaderUse::Literal {
                    specifier: site
                        .specifier
                        .as_deref()
                        .expect("literal loader calls carry their specifier"),
                    resolve_from: source,
                    data_read_only: false,
                },
                // Classified through the binding it initializes.
                InteropUseKind::ImportMetaRequireReference
                    if alias_definitions.contains(&(source, site.span)) =>
                {
                    continue;
                }
                // A dynamic import is no loader site, but the namespace it
                // obtains exposes an exported loader.
                InteropUseKind::DynamicImport => {
                    let target = match site.specifier.as_deref() {
                        Some(specifier) => classifier.resolve(specifier, source)?,
                        None => None,
                    };
                    match target {
                        Some(reference)
                            if classifier.alias_chunks.contains(&reference.target_chunk_id) =>
                        {
                            let resolved_target = Some(classifier.report_target(&reference));
                            let phase = match site.phase {
                                SitePhase::Unreachable => SitePhase::Unreachable,
                                // The namespace outlives the job that loads it.
                                SitePhase::MandatorilySuspended
                                | SitePhase::Synchronous
                                | SitePhase::ModuleBody => SitePhase::Synchronous,
                            };
                            classifier.classify(
                                &mut surface,
                                &source_chunk,
                                phase,
                                LoaderUse::Opaque,
                                |_| interop_witness(resolved_target),
                            )?;
                        }
                        _ => {}
                    }
                    continue;
                }
                // The module-scope `require` a direct eval reads is the
                // module's own host loader.
                InteropUseKind::EvaluatedRequireLiteralCall => LoaderUse::Literal {
                    specifier: site
                        .specifier
                        .as_deref()
                        .expect("literal loader calls carry their specifier"),
                    resolve_from: source,
                    data_read_only: false,
                },
                InteropUseKind::ImportMetaRequireDynamicCall
                | InteropUseKind::EvaluatedRequireDynamicCall => LoaderUse::NonLiteralCall,
                kind if !is_loader_site(kind) => continue,
                _ => LoaderUse::Opaque,
            };
            classifier.classify(
                &mut surface,
                &source_chunk,
                site.phase,
                loader_use,
                interop_witness,
            )?;
        }
        for binding in &runtime.loader_bindings {
            let use_witness = |loader_use: &LoaderBindingUse, resolved_target| {
                witness(LoaderSite::LoaderBindingUse {
                    item: loader_use.item_index,
                    line: loader_use.line,
                    call: loader_use.call,
                    specifier: loader_use.literal.clone(),
                    resolved_target,
                })
            };
            let resolve_from = match &binding.binding {
                LoaderBinding::LoaderAlias { .. } => source,
                // A `module` named import loads nothing until a use makes a
                // loader from it: `createRequire` may only initialize a
                // tracked binding, and only data members may be read.
                LoaderBinding::Import {
                    source: specifier,
                    imported,
                } if is_loader_factory(specifier) => {
                    for loader_use in &binding.uses {
                        let classified = match imported.as_str() {
                            "createRequire" => {
                                loader_use.call
                                    && loader_use.consumer == LoaderUseConsumer::BindingInitializer
                            }
                            member => MODULE_DATA_MEMBERS.contains(&member),
                        };
                        if !classified {
                            surface.fail(
                                LoaderClosureFailureKind::LoaderFactoryRequest,
                                use_witness(loader_use, None),
                                SitePlacement::Callable,
                            );
                        }
                    }
                    continue;
                }
                LoaderBinding::CreatedLoader => {
                    for loader_use in &binding.uses {
                        let classified = match (&loader_use.literal, &loader_use.consumer) {
                            // A created loader resolves relative requests
                            // against its own base, so only host module
                            // names are fixed by the literal.
                            (Some(specifier), _)
                                if spec::HostModule::from_static_specifier(specifier).is_some() =>
                            {
                                LoaderUse::Literal {
                                    specifier,
                                    resolve_from: source,
                                    data_read_only: is_module_data_read(&loader_use.consumer),
                                }
                            }
                            (Some(_), _) => LoaderUse::Opaque,
                            (None, _) if loader_use.call => LoaderUse::NonLiteralCall,
                            // `resolve` computes a path and loads nothing.
                            (None, LoaderUseConsumer::MemberCall(name)) if name == "resolve" => {
                                continue;
                            }
                            (None, _) => LoaderUse::Opaque,
                        };
                        classifier.classify(
                            &mut surface,
                            &source_chunk,
                            loader_use.phase,
                            classified,
                            |resolved_target| use_witness(loader_use, resolved_target),
                        )?;
                    }
                    continue;
                }
                LoaderBinding::Import {
                    source: specifier,
                    imported,
                } => {
                    let Some(reference) = classifier.resolve(specifier, source)? else {
                        continue;
                    };
                    if !classifier.alias_chunks.contains(&reference.target_chunk_id) {
                        continue;
                    }
                    let defining = reference.target_chunk_id;
                    if classifier.entry_chunk(&reference)?.is_none() {
                        // Not the entry that exports the alias: unclassified.
                        surface.fail(
                            LoaderClosureFailureKind::OpaqueLoaderSite,
                            witness(LoaderSite::NamespaceRequest {
                                item: binding.uses.first().map_or(0, |u| u.item_index),
                                specifier: specifier.clone(),
                                resolved_target: Some(classifier.report_target(&reference)),
                            }),
                            SitePlacement::Callable,
                        );
                        continue;
                    }
                    if !aliases.contains(&(defining, imported.clone())) {
                        continue;
                    }
                    defining
                }
            };
            for loader_use in &binding.uses {
                let classified = match &loader_use.literal {
                    Some(specifier) => LoaderUse::Literal {
                        specifier,
                        resolve_from,
                        data_read_only: is_module_data_read(&loader_use.consumer),
                    },
                    None if loader_use.call => LoaderUse::NonLiteralCall,
                    None => LoaderUse::Opaque,
                };
                classifier.classify(
                    &mut surface,
                    &source_chunk,
                    loader_use.phase,
                    classified,
                    |resolved_target| use_witness(loader_use, resolved_target),
                )?;
            }
        }
        chunks.insert(source, surface);
    }
    Ok(LoaderSurfaces {
        chunks,
        unreachable_sites: classifier.unreachable_sites,
        requests: classifier
            .request_counts
            .into_iter()
            .map(
                |((source_chunk, specifier, target), request_count)| ExportReentryLoaderRequest {
                    source_chunk,
                    specifier,
                    target,
                    request_count,
                },
            )
            .collect(),
    })
}
