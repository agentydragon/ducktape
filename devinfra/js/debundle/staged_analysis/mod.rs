//! Reusable source-analysis stage and its compiled compatibility boundary.
//!
//! Everything that computes, encodes or loads saved facts belongs to this
//! closure. Ownership planning, lowering and output reports depend on it.

use analysis::facts::ExportReentryPolicy;
use analysis_artifact::{AnalysisArtifactBody, AnalysisArtifactHeader};
use analysis_codec::ContextOrdinals;
use analysis_projection::AnalysisSpec;
use anyhow::{Context, Result, bail, ensure};
use artifact::reports::{StaticModuleRequest, StaticModuleRequestMode};
use artifact::{
    ArtifactIndexes, ChunkBundle, JsFile, chunk_runtime_entry, export_policies,
    normalized_chunk_ids, prune_artifact_to_chunk_ids,
};
use chunk_facts::analyze_logical_chunk;
use graph_provenance::GraphProvenance;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::GLOBALS;

pub mod analysis_artifact;
pub mod chunk_facts;
pub mod loader_surface;
pub mod program_summary;
pub mod progress;
mod request;
mod saved_program_summary;
pub use loader_surface::{
    ExportReentryLoaderDerivation, ExportReentryLoaderFailure, ExportReentryLoaderRequest,
    LoaderRequestReportTarget, LoaderSite, LoaderSiteWitness,
};
pub use request::{AnalyzeRequest, analyze_from_projection};

/// Why the unknown-call fallback includes the chunk's export surface.
/// The policy remains conservative whenever either blocker list is non-empty
/// or the loader derivation does not permit the proof.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExportReentryReport {
    /// The declared host behavior the policy was derived under, echoed as read.
    pub host_behavior: spec::HostBehavior,
    /// The declared program premises, echoed as read.
    #[serde(default, skip_serializing_if = "is_default_premises")]
    pub program_premises: spec::ProgramPremises,
    pub policy: ExportReentryPolicy,
    pub loader: ExportReentryLoaderDerivation,
    pub global_blockers: Vec<ExportReentryGlobalBlocker>,
    pub closure_blockers: Vec<ExportReentryClosureBlocker>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub host_builtin_leaves: Vec<ExportReentryHostBuiltinLeaf>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExportReentryHostBuiltinLeaf {
    pub source_chunk: String,
    pub specifier: String,
    pub builtin: spec::HostModule,
    pub request_count: usize,
}

#[derive(Default)]
struct ExportReentryClosureInspection {
    blockers: Vec<ExportReentryClosureBlocker>,
    host_builtin_leaves: Vec<ExportReentryHostBuiltinLeaf>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum ExportReentryGlobalBlocker {
    IncompleteArtifact {
        present_chunks: usize,
        expected_chunks: usize,
    },
    NonOrdinaryStaticRequest {
        source_chunk: String,
        specifier: String,
        resolved_target: Option<ExportReentryTarget>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum ExportReentryClosureBlocker {
    NonEsmChunk {
        source_chunk: String,
    },
    TopLevelAwait {
        source_chunk: String,
    },
    OpenReplacementBundle {
        source_chunk: String,
    },
    UnsupportedStaticRequest {
        source_chunk: String,
        specifier: String,
        resolved_target: Option<ExportReentryTarget>,
    },
    UnresolvedStaticRequest {
        source_chunk: String,
        specifier: String,
    },
    MissingTargetChunk {
        source_chunk: String,
        specifier: String,
        target_chunk: String,
    },
    NonEntryStaticRequest {
        source_chunk: String,
        specifier: String,
        resolved_target: ExportReentryTarget,
        entry_file: String,
    },
    TargetCycle {
        target_chunk: String,
    },
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct ExportReentryTarget {
    pub chunk: String,
    pub file: String,
}

/// Hygiene-context ordinals for an analysis artifact: the analysed chunks'
/// runtime ASTs in analysis order, every retained entry AST in chunk table
/// order, then the cross-module pass's re-parsed entries in chunk order.
/// `debundle analyze` and a run that loads its artifact parse the same files,
/// retain the same ASTs and re-parse the same entries, so both number the same
/// contexts.
fn context_ordinals(
    artifact: &ChunkBundle,
    analysed_chunk_ids: &[String],
    file: Option<&str>,
    reparsed: &BTreeMap<ChunkId, program_summary::ReparsedEntry>,
) -> Result<ContextOrdinals> {
    let mut modules = Vec::new();
    for chunk_id in analysed_chunk_ids {
        let interned = artifact
            .chunk_table
            .get(chunk_id)
            .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
        let (_, _, runtime_ast) = chunk_runtime_entry(artifact, chunk_id, interned, file)?;
        modules.push(&runtime_ast.module);
    }
    for chunk in &artifact.chunks {
        if let Some(ast) = chunk
            .js
            .get_file(&chunk.js.entry_file)
            .and_then(JsFile::ast)
        {
            modules.push(&ast.module);
        }
    }
    modules.extend(reparsed.values().map(|entry| &entry.parsed.module));
    Ok(ContextOrdinals::new(modules))
}

fn chunk_source_digests(provenance: &GraphProvenance) -> BTreeMap<String, String> {
    provenance
        .chunk_sources
        .iter()
        .map(|source| (source.chunk_id.clone(), source.sha256.clone()))
        .collect()
}

/// Read an analysis artifact for this run, refusing one computed by another
/// engine or from other parser inputs. The projection is checked once the
/// selector solve has completed it.
pub fn load_analysis(
    bytes: &[u8],
    artifact: &ChunkBundle,
    analysed_chunk_ids: &[String],
    file: Option<&str>,
    reparsed: &BTreeMap<ChunkId, program_summary::ReparsedEntry>,
    provenance: Option<&GraphProvenance>,
    analysis_engine_sha256: &str,
) -> Result<(AnalysisArtifactHeader, AnalysisArtifactBody)> {
    let (header, body) = analysis_artifact::read_header(bytes)?;
    let provenance = provenance.context("an analysis artifact requires graph provenance")?;
    ensure!(
        header.analysis_engine_sha256 == analysis_engine_sha256,
        "analysis artifact was computed by engine {}, this engine is {}",
        header.analysis_engine_sha256,
        analysis_engine_sha256
    );
    let sources = chunk_source_digests(provenance);
    if header.chunk_sources != sources {
        let differing: BTreeSet<&String> = header
            .chunk_sources
            .keys()
            .chain(sources.keys())
            .filter(|chunk| header.chunk_sources.get(*chunk) != sources.get(*chunk))
            .collect();
        bail!(
            "analysis artifact was computed from other parser inputs for chunks: {}",
            differing
                .into_iter()
                .map(String::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        );
    }
    let ordinals = context_ordinals(artifact, analysed_chunk_ids, file, reparsed)?;
    let body = analysis_artifact::read_body(body, &ordinals)?;
    Ok((header, body))
}

/// Inputs to [`analyze_program`] besides the parsed program.
pub struct AnalyzeProgramInputs<'a> {
    pub spec: &'a AnalysisSpec,
    /// Host loader per embedded input file (`inputs.embedded_loaders_path`).
    pub embedded_loaders: &'a BTreeMap<String, spec::BunLoader>,
    pub vendor_plan: &'a vendor::VendorResolutionPlan,
    pub graph_provenance: &'a GraphProvenance,
    pub analysis_engine_sha256: &'a str,
}

/// `debundle analyze`: the cross-module pass and every analysed chunk's facts,
/// from the parsed program and a projection alone, encoded as an analysis
/// artifact. A run with the same projection, engine and parser inputs reads it
/// instead of recomputing them.
pub fn analyze_program(
    mut artifact: ChunkBundle,
    inputs: AnalyzeProgramInputs<'_>,
) -> Result<Vec<u8>> {
    let AnalyzeProgramInputs {
        spec,
        embedded_loaders,
        vendor_plan,
        graph_provenance,
        analysis_engine_sha256,
    } = inputs;
    let selected_chunk_ids = normalized_chunk_ids(spec.analysed_chunks())?;
    let resolved_export_purity =
        export_policies::resolve_source_export_policies(&artifact, &spec.chunk_export_purity)?;
    if spec.prune_other_chunks {
        prune_artifact_to_chunk_ids(&mut artifact, &selected_chunk_ids);
    }
    let artifact_indexes = ArtifactIndexes::build(&artifact)?;
    // With closure diagnostics: a run that writes reports shows them.
    let export_reentry_reports = export_reentry_reports(
        &artifact,
        &artifact_indexes,
        loader_surface::LoaderAssumptions {
            host_behavior: &spec.host_behavior,
            program_premises: &spec.program_premises,
            embedded_loaders,
        },
        vendor_plan,
        &selected_chunk_ids,
        true,
    )?;
    let export_reentry_policies: BTreeMap<_, _> = export_reentry_reports
        .iter()
        .map(|(chunk_id, report)| (*chunk_id, report.policy.clone()))
        .collect();
    let program_summary_chunk_closure = analysis_projection::selected_static_import_closure(
        &artifact,
        &artifact_indexes,
        &selected_chunk_ids,
    )?;
    let projection = spec.projection(resolved_export_purity.as_ref());
    let reparsed = program_summary::reparse_unretained_entries(
        &artifact,
        Some(&program_summary_chunk_closure),
    );
    let program_summary = program_summary::collect_program_summary(
        &artifact,
        &artifact_indexes,
        &projection,
        program_summary::ProgramSummaryOptions {
            graph_provenance: Some(graph_provenance),
            vendor_plan: Some(vendor_plan),
            replacement_packages: &vendor_plan.replacement_packages(),
        },
        Some(&program_summary_chunk_closure),
        &reparsed,
    )?;
    // Reparsed entries are no longer needed once cross-module analysis and
    // context numbering are complete. Build the ordinal table now so those
    // ASTs can be released before retaining every chunk's analysis facts.
    let ordinals = context_ordinals(
        &artifact,
        &selected_chunk_ids,
        spec.entry_file.as_deref(),
        &reparsed,
    )?;
    drop(reparsed);
    let chunks = GLOBALS
        .with(|globals| {
            selected_chunk_ids
                .par_iter()
                .map(|chunk_id| {
                    GLOBALS.set(globals, || {
                        analyze_logical_chunk(
                            &artifact,
                            &artifact_indexes,
                            chunk_id,
                            spec.entry_file.as_deref(),
                            &export_reentry_policies,
                            &projection,
                            &program_summary,
                        )
                        .map(|facts| (chunk_id.clone(), facts))
                    })
                })
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<BTreeMap<_, _>>>()?;
    let header = AnalysisArtifactHeader {
        analysis_engine_sha256: analysis_engine_sha256.to_string(),
        projection: spec.encode()?,
        chunk_sources: chunk_source_digests(graph_provenance),
    };
    let body = AnalysisArtifactBody {
        program_summary,
        export_reentry: export_reentry_reports
            .into_iter()
            .map(|(chunk_id, report)| (artifact.chunk_table.name(chunk_id).to_string(), report))
            .collect(),
        chunks,
    };
    // Facts and context ordinals own everything encoding needs. Release the
    // parsed program and its indexes before allocating the encoded artifact.
    drop(artifact_indexes);
    drop(artifact);
    analysis_artifact::write_artifact(&header, &body, &ordinals)
}

pub fn export_reentry_reports(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    assumptions: loader_surface::LoaderAssumptions<'_>,
    vendor_plan: &vendor::VendorResolutionPlan,
    selected_chunk_names: &[String],
    include_closure_diagnostics: bool,
) -> Result<BTreeMap<ChunkId, ExportReentryReport>> {
    let loader_surface::LoaderAssumptions {
        host_behavior,
        program_premises,
        ..
    } = assumptions;
    // An incomplete artifact cannot certify incoming host/nonordinary edges.
    let all_chunks_present = artifact.chunks.len() == artifact.chunk_table.len();
    let mut global_blockers = Vec::new();
    if !all_chunks_present {
        global_blockers.push(ExportReentryGlobalBlocker::IncompleteArtifact {
            present_chunks: artifact.chunks.len(),
            expected_chunks: artifact.chunk_table.len(),
        });
    }
    for chunk in &artifact.chunks {
        let source_chunk = artifact.chunk_table.name(chunk.chunk_id).to_string();
        let runtime = &chunk.analysis.runtime_facts;
        for request in &runtime.static_requests {
            if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                global_blockers.push(ExportReentryGlobalBlocker::NonOrdinaryStaticRequest {
                    source_chunk: source_chunk.clone(),
                    specifier: request.specifier.clone(),
                    resolved_target: resolve_report_target(
                        request,
                        chunk.chunk_id,
                        &chunk.analysis.entry_file,
                        artifact,
                        indexes,
                    ),
                });
            }
        }
    }

    let global_proof_is_blocked = !global_blockers.is_empty();
    let targets: BTreeSet<ChunkId> = selected_chunk_names
        .iter()
        .filter_map(|name| artifact.chunk_table.get(name))
        .collect();
    let mut loader_derivations = loader_derivations(artifact, indexes, assumptions, &targets)?;
    let mut reports = BTreeMap::new();
    for name in selected_chunk_names {
        let Some(target) = artifact.chunk_table.get(name) else {
            continue;
        };
        let closure = if artifact.has_chunk(target)
            && (include_closure_diagnostics || !global_proof_is_blocked)
        {
            collect_closure_blockers(target, artifact, indexes, host_behavior, vendor_plan)?
        } else {
            ExportReentryClosureInspection::default()
        };
        let loader = loader_derivations
            .remove(&target)
            .expect("every selected target has a loader derivation");
        let policy = match loader.reentry_through() {
            _ if global_proof_is_blocked || !closure.blockers.is_empty() => {
                ExportReentryPolicy::Conservative
            }
            _ if loader.permits_export_reentry_proof() => {
                ExportReentryPolicy::NoStaticImporterReentry
            }
            Some(names) => ExportReentryPolicy::ReentryThrough(names.clone()),
            None => ExportReentryPolicy::Conservative,
        };
        reports.insert(
            target,
            ExportReentryReport {
                host_behavior: host_behavior.clone(),
                program_premises: program_premises.clone(),
                policy,
                loader,
                global_blockers: global_blockers.clone(),
                closure_blockers: closure.blockers,
                host_builtin_leaves: closure.host_builtin_leaves,
            },
        );
    }
    Ok(reports)
}

/// Derive the loader premise for each target (SPEC.md § Host behavior).
fn loader_derivations(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    assumptions: loader_surface::LoaderAssumptions<'_>,
    targets: &BTreeSet<ChunkId>,
) -> Result<BTreeMap<ChunkId, ExportReentryLoaderDerivation>> {
    let host_behavior = assumptions.host_behavior;
    let uniform = |derivation: ExportReentryLoaderDerivation| {
        targets
            .iter()
            .map(|target| (*target, derivation.clone()))
            .collect()
    };
    if host_behavior.synchronous_module_loader == spec::SynchronousModuleLoader::None {
        return Ok(uniform(ExportReentryLoaderDerivation::NoSynchronousLoader));
    }
    let surfaces = loader_surface::loader_surfaces(artifact, indexes, assumptions)?;
    if !surfaces.has_loader_sites() {
        return Ok(uniform(ExportReentryLoaderDerivation::NoHostLoaderSites));
    }
    analysis::loader_closure::solve_loader_closure(&surfaces.chunks, host_behavior, targets)
        .into_iter()
        .map(|(target, closure)| {
            Ok((
                target,
                ExportReentryLoaderDerivation::from_closure(
                    target, &closure, &surfaces, artifact, indexes,
                )?,
            ))
        })
        .collect()
}

fn is_default_premises(premises: &spec::ProgramPremises) -> bool {
    *premises == spec::ProgramPremises::default()
}

fn collect_closure_blockers(
    target: ChunkId,
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    host_behavior: &spec::HostBehavior,
    vendor_plan: &vendor::VendorResolutionPlan,
) -> Result<ExportReentryClosureInspection> {
    // Follow resolved static requests even when another blocker is present,
    // so the report exposes independent predicates and structural cycles.
    let mut outgoing = BTreeMap::<ChunkId, BTreeSet<ChunkId>>::new();
    let mut closure = BTreeSet::from([target]);
    let mut pending = vec![target];
    let mut blockers = Vec::new();
    let mut host_builtin_leaf_counts = BTreeMap::<(String, String, spec::HostModule), usize>::new();
    while let Some(source) = pending.pop() {
        let chunk = artifact.chunk(source)?;
        let runtime = &chunk.analysis.runtime_facts;
        let source_name = artifact.chunk_table.name(source).to_string();
        if !runtime.module_kind.supports_static_esm_closure() {
            blockers.push(ExportReentryClosureBlocker::NonEsmChunk {
                source_chunk: source_name.clone(),
            });
        }
        if runtime.has_top_level_await {
            blockers.push(ExportReentryClosureBlocker::TopLevelAwait {
                source_chunk: source_name.clone(),
            });
        }
        if vendor_plan.replacement_bundle_is_open(source) {
            blockers.push(ExportReentryClosureBlocker::OpenReplacementBundle {
                source_chunk: source_name.clone(),
            });
        }
        for request in &runtime.static_requests {
            if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                blockers.push(ExportReentryClosureBlocker::UnsupportedStaticRequest {
                    source_chunk: source_name.clone(),
                    specifier: request.specifier.clone(),
                    resolved_target: resolve_report_target(
                        request,
                        source,
                        &chunk.analysis.entry_file,
                        artifact,
                        indexes,
                    ),
                });
            }
            let Some(resolved) = indexes.resolve_runtime_import_reference(
                &request.specifier,
                source,
                &chunk.analysis.entry_file,
                &artifact.chunk_table,
            ) else {
                if request.mode == StaticModuleRequestMode::OrdinaryEsm
                    && let Some(builtin) = host_behavior.reentry_free_module(&request.specifier)
                {
                    *host_builtin_leaf_counts
                        .entry((source_name.clone(), request.specifier.clone(), builtin))
                        .or_default() += 1;
                    continue;
                }
                blockers.push(ExportReentryClosureBlocker::UnresolvedStaticRequest {
                    source_chunk: source_name.clone(),
                    specifier: request.specifier.clone(),
                });
                continue;
            };
            if !artifact.has_chunk(resolved.target_chunk_id) {
                blockers.push(ExportReentryClosureBlocker::MissingTargetChunk {
                    source_chunk: source_name.clone(),
                    specifier: request.specifier.clone(),
                    target_chunk: artifact
                        .chunk_table
                        .name(resolved.target_chunk_id)
                        .to_string(),
                });
                continue;
            }
            let target_entry = &artifact
                .chunk(resolved.target_chunk_id)?
                .analysis
                .entry_file;
            if target_entry != &resolved.target_file {
                blockers.push(ExportReentryClosureBlocker::NonEntryStaticRequest {
                    source_chunk: source_name.clone(),
                    specifier: request.specifier.clone(),
                    resolved_target: report_target(
                        resolved.target_chunk_id,
                        &resolved.target_file,
                        artifact,
                    ),
                    entry_file: target_entry.clone(),
                });
                continue;
            }
            outgoing
                .entry(source)
                .or_default()
                .insert(resolved.target_chunk_id);
            if closure.insert(resolved.target_chunk_id) {
                pending.push(resolved.target_chunk_id);
            }
        }
    }

    let mut incoming = BTreeMap::<ChunkId, BTreeSet<ChunkId>>::new();
    for (source, targets) in &outgoing {
        for destination in targets {
            incoming.entry(*destination).or_default().insert(*source);
        }
    }
    let mut reaches_target = BTreeSet::from([target]);
    let mut pending = vec![target];
    while let Some(destination) = pending.pop() {
        if let Some(sources) = incoming.get(&destination) {
            for source in sources {
                if reaches_target.insert(*source) {
                    pending.push(*source);
                }
            }
        }
    }
    let target_has_cycle = outgoing.get(&target).is_some_and(|destinations| {
        destinations
            .iter()
            .any(|destination| reaches_target.contains(destination))
    });
    if target_has_cycle {
        blockers.push(ExportReentryClosureBlocker::TargetCycle {
            target_chunk: artifact.chunk_table.name(target).to_string(),
        });
    }
    Ok(ExportReentryClosureInspection {
        blockers,
        host_builtin_leaves: host_builtin_leaf_counts
            .into_iter()
            .map(|((source_chunk, specifier, builtin), request_count)| {
                ExportReentryHostBuiltinLeaf {
                    source_chunk,
                    specifier,
                    builtin,
                    request_count,
                }
            })
            .collect(),
    })
}

fn resolve_report_target(
    request: &StaticModuleRequest,
    source: ChunkId,
    entry_file: &str,
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
) -> Option<ExportReentryTarget> {
    let resolved = indexes.resolve_runtime_import_reference(
        &request.specifier,
        source,
        entry_file,
        &artifact.chunk_table,
    )?;
    Some(report_target(
        resolved.target_chunk_id,
        &resolved.target_file,
        artifact,
    ))
}

fn report_target(target: ChunkId, file: &str, artifact: &ChunkBundle) -> ExportReentryTarget {
    ExportReentryTarget {
        chunk: artifact.chunk_table.name(target).to_string(),
        file: file.to_string(),
    }
}
