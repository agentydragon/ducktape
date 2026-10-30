//! Source facts and report shapes for the loader-closed derivation of the
//! export-reentry policy (SPEC.md § Host behavior).

use std::collections::{BTreeMap, BTreeSet};

use analysis::analysis_inputs::HostBehaviorKey;
use analysis::loader_closure::{
    ChunkLoaderSurface, LoaderClosure, LoaderClosureFailureKind, LoaderClosureWitness,
    LoaderRequest, LoaderRequestTarget,
};
use anyhow::Result;
use artifact::reports::{InteropUseKind, StaticModuleRequestMode};
use artifact::{ArtifactIndexes, ChunkBundle, ChunkId};
use serde::Serialize;

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
        #[serde(skip_serializing_if = "Vec::is_empty")]
        requests: Vec<ExportReentryLoaderRequest>,
        /// Chunks a loader request can evaluate, with their closures.
        #[serde(skip_serializing_if = "Vec::is_empty")]
        loaded_chunks: Vec<String>,
        /// Mandatorily suspended sites excluded from the closure.
        #[serde(skip_serializing_if = "is_zero")]
        suspended_sites: usize,
        /// The first witness and occurrence count of each failure kind.
        #[serde(skip_serializing_if = "Vec::is_empty")]
        failures: Vec<ExportReentryLoaderFailure>,
    },
}

impl ExportReentryLoaderDerivation {
    pub fn permits_export_reentry_proof(&self) -> bool {
        match self {
            Self::NoSynchronousLoader | Self::NoHostLoaderSites => true,
            Self::LoaderClosure { closed, .. } => *closed,
        }
    }

    pub(super) fn from_closure(
        closure: &LoaderClosure<LoaderSiteWitness>,
        requests: &[ExportReentryLoaderRequest],
        artifact: &ChunkBundle,
    ) -> Self {
        Self::LoaderClosure {
            closed: closure.is_closed(),
            host_keys: closure.dependencies.host_behavior.clone(),
            requests: requests.to_vec(),
            loaded_chunks: closure
                .loaded_chunks
                .iter()
                .map(|chunk| artifact.chunk_table.name(*chunk).to_string())
                .collect(),
            suspended_sites: closure.suspended_sites,
            failures: closure
                .failures
                .iter()
                .map(|(reason, failure)| ExportReentryLoaderFailure {
                    reason: *reason,
                    occurrences: failure.occurrences,
                    first: failure.first.clone(),
                })
                .collect(),
        }
    }
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
    Builtin(spec::NodeBuiltinModule),
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
    TopLevelAwait,
    VendorChunk,
}

pub(super) struct LoaderSurfaces {
    pub chunks: BTreeMap<ChunkId, ChunkLoaderSurface<LoaderSiteWitness>>,
    pub requests: Vec<ExportReentryLoaderRequest>,
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
    matches!(specifier, "module" | "node:module")
}

/// Whether a census site can load a module on the stack that runs it.
fn is_loader_site(kind: InteropUseKind) -> bool {
    match kind {
        InteropUseKind::ImportMetaRequireLiteralCall
        | InteropUseKind::ImportMetaRequireDynamicCall
        | InteropUseKind::ImportMetaRequireReference
        | InteropUseKind::GlobalThisRequire
        | InteropUseKind::UnboundRequire
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

/// Classify every analyzed chunk's loader sites and the facts that matter
/// when a loader request evaluates it.
pub(super) fn loader_surfaces(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    host_behavior: &spec::HostBehavior,
    vendor_affected_chunk_ids: &BTreeSet<ChunkId>,
) -> Result<LoaderSurfaces> {
    let mut chunks = BTreeMap::new();
    let mut request_counts = BTreeMap::<(String, String, LoaderRequestReportTarget), usize>::new();
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
        };
        if runtime.has_top_level_await {
            surface.load_failures.push((
                LoaderClosureFailureKind::LoadedClosureTopLevelAwait,
                witness(LoaderSite::TopLevelAwait),
            ));
        }
        if vendor_affected_chunk_ids.contains(&source) {
            surface.load_failures.push((
                LoaderClosureFailureKind::LoadedClosureVendorChunk,
                witness(LoaderSite::VendorChunk),
            ));
        }
        for request in &runtime.static_requests {
            let resolved = indexes.resolve_runtime_import_reference(
                &request.specifier,
                source,
                &chunk.analysis.entry_file,
                &artifact.chunk_table,
            );
            let static_witness = || {
                witness(LoaderSite::StaticRequest {
                    specifier: request.specifier.clone(),
                    resolved_target: resolved.as_ref().map(|reference| {
                        report_target(reference.target_chunk_id, &reference.target_file, artifact)
                    }),
                })
            };
            if is_loader_factory(&request.specifier) {
                surface.site_failures.push((
                    LoaderClosureFailureKind::LoaderFactoryRequest,
                    static_witness(),
                ));
            }
            let closed_request = match &resolved {
                Some(reference) if artifact.has_chunk(reference.target_chunk_id) => {
                    surface.static_targets.insert(reference.target_chunk_id);
                    artifact
                        .chunk(reference.target_chunk_id)?
                        .analysis
                        .entry_file
                        == reference.target_file
                }
                Some(_) => false,
                None => spec::NodeBuiltinModule::from_static_specifier(&request.specifier)
                    .is_some_and(|builtin| {
                        host_behavior
                            .reentry_free_builtin_modules
                            .contains(&builtin)
                    }),
            };
            if !closed_request || request.mode != StaticModuleRequestMode::OrdinaryEsm {
                surface.load_failures.push((
                    LoaderClosureFailureKind::LoadedClosureRequest,
                    static_witness(),
                ));
            }
        }
        for site in &runtime.interop_uses {
            let resolved = site.specifier.as_deref().and_then(|specifier| {
                indexes.resolve_runtime_import_reference(
                    specifier,
                    source,
                    &chunk.analysis.entry_file,
                    &artifact.chunk_table,
                )
            });
            let resolved_target = resolved.as_ref().map(|reference| {
                report_target(reference.target_chunk_id, &reference.target_file, artifact)
            });
            let site_witness = witness(LoaderSite::Interop {
                kind: site.kind,
                item: site.item_index,
                line: site.line,
                specifier: site.specifier.clone(),
                resolved_target: resolved_target.clone(),
            });
            if !is_loader_site(site.kind) {
                continue;
            }
            // A suspended site runs in a promise job; a synchronous load can
            // start one only by draining microtasks.
            if site.mandatory_suspension
                && host_behavior.synchronous_load_drains_microtasks
                    == Some(spec::SynchronousLoadDrainsMicrotasks::Never)
            {
                surface.suspended_sites.push(site_witness);
                continue;
            }
            let failure = match site.kind {
                InteropUseKind::ImportMetaRequireLiteralCall => {
                    let specifier = site
                        .specifier
                        .as_deref()
                        .expect("literal loader calls carry their specifier");
                    let builtin = spec::NodeBuiltinModule::from_static_specifier(specifier);
                    let target = match (&resolved, builtin) {
                        _ if is_loader_factory(specifier) => {
                            Err(LoaderClosureFailureKind::LoaderFactoryRequest)
                        }
                        (Some(reference), _)
                            if artifact.has_chunk(reference.target_chunk_id)
                                && artifact
                                    .chunk(reference.target_chunk_id)?
                                    .analysis
                                    .entry_file
                                    == reference.target_file =>
                        {
                            Ok((
                                LoaderRequestTarget::Chunk(reference.target_chunk_id),
                                LoaderRequestReportTarget::Chunk(
                                    resolved_target.clone().expect("resolved target"),
                                ),
                            ))
                        }
                        (Some(_), _) => Err(LoaderClosureFailureKind::UnresolvableLoaderRequest),
                        (None, Some(builtin))
                            if host_behavior
                                .reentry_free_builtin_modules
                                .contains(&builtin) =>
                        {
                            Ok((
                                LoaderRequestTarget::ReentryFreeBuiltin(builtin),
                                LoaderRequestReportTarget::Builtin(builtin),
                            ))
                        }
                        (None, Some(_)) => {
                            Err(LoaderClosureFailureKind::NonReentryFreeBuiltinRequest)
                        }
                        (None, None) => Err(LoaderClosureFailureKind::UnresolvableLoaderRequest),
                    };
                    match target {
                        Ok((target, report)) => {
                            *request_counts
                                .entry((source_chunk.clone(), specifier.to_string(), report))
                                .or_default() += 1;
                            surface.requests.push(LoaderRequest {
                                target,
                                witness: site_witness,
                            });
                            continue;
                        }
                        Err(kind) => Some(kind),
                    }
                }
                _ => Some(LoaderClosureFailureKind::OpaqueLoaderSite),
            };
            if let Some(kind) = failure {
                surface.site_failures.push((kind, site_witness));
            }
        }
        chunks.insert(source, surface);
    }
    Ok(LoaderSurfaces {
        chunks,
        requests: request_counts
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
