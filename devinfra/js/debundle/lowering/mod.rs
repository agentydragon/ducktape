use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use swc_common::GLOBALS;

use analysis::RedundantPurityHint;
use analysis::facts::ExportReentryPolicy;
use analysis::reports::author_inputs::AuthorInputLocations;

use analysis_artifact::{AnalysisArtifactBody, AnalysisArtifactHeader};
use analysis_codec::ContextOrdinals;
use artifact::paths::normalize_module_path;
use artifact::reports::{SelectedModuleLowering, StaticModuleRequest, StaticModuleRequestMode};
use artifact::{ArtifactIndexes, ChunkBundle, ChunkId, JsFile};
use graph_provenance::GraphProvenance;

use output_layout::MODULES_REPORT;
use reconstruction::{ChunkGateRejection, UnmatchedSpecClaim};

use spec::{
    ChunkExportPurity, ChunkRenames, DependencyGraphOptions, LogicalModule, UnassignedMode,
};

pub mod analysis_artifact;
mod analysis_projection;
pub use analysis_projection::AnalysisSpec;
mod anonymous;
mod body_facts;
mod chunk_ast;
mod chunk_renames;
mod commuting_statements;
mod cross_module;
mod export_policies;
mod exports;
mod imports;
mod io;
mod loader_surface;
mod lower;
mod materialize;
mod module_output;
mod naturalize;
mod placement;
mod plans;
mod rebind_fold;
pub mod rename_ledger;
#[cfg(test)]
mod rename_ledger_proptest;
mod rewrite_runtime;
mod scope_names;
mod util;
mod visitors;

pub use imports::naturalize_cross_chunk_imports;
pub use loader_surface::{
    ExportReentryLoaderDerivation, ExportReentryLoaderFailure, ExportReentryLoaderRequest,
    LoaderRequestReportTarget, LoaderSite, LoaderSiteWitness,
};

use imports::VendorReimportOracle;
use io::{
    ProgramGraphOutput, prepare_output_dir, prune_artifact_to_chunk_ids, write_chunk_report_json,
};

use analysis_projection::AnalysisProjection;
use materialize::{
    ChunkContext, ChunkSpec, ClaimedLogicalChunk, CommutingStatementsContext, EmissionChunkOutputs,
    MaterializeLogicalChunkInputs, claim_prepared_chunk, collect_materialized_logical_chunks,
    finish_logical_chunk, prepare_logical_chunk,
};
use spec_resolution::solve_projected_chunks;

use util::normalize_optional_relative_dir;

pub struct MaterializeLogicalModulesResult {
    /// Finalized lowered and pass-through files, kept outside a bundle until
    /// the remaining post-lowering passes need their legacy input.
    pub output: EmissionChunkOutputs,
    pub selected_lowerings: Vec<SelectedModuleLowering>,
    pub module_count: usize,
    /// Per-symbol counts of vendor-swap rewrites applied at
    /// construction time in materialized module bodies, keyed by
    /// (swapped chunk, chunk export). Folded into the vendor waves'
    /// `references_rewritten` manifest fields by the pipeline so the
    /// manifest keeps counting emitted references across both
    /// application sites.
    pub vendor_reference_rewrites: BTreeMap<(ChunkId, String), usize>,
    /// Spec claims that named a binding for which no top-level
    /// declaration exists in the source chunk. Previously dropped
    /// silently — the binding would fall through to the residual and
    /// the named module's export list would be one entry short. Now
    /// collected across every chunk so reconstruction can diagnose
    /// all claims (the chunk lowers as if the missing claim were absent).
    /// Final validation rejects the full list before writing the output. See
    /// [`UnmatchedSpecClaim`].
    pub unmatched_spec_claims: Vec<UnmatchedSpecClaim>,
    /// Chunks the realizability gate rejected but lowered anyway under
    /// [`GateRejectionPolicy::LowerUnrunnable`]; empty under
    /// [`GateRejectionPolicy::Refuse`], which fails the run instead.
    pub gate_rejections: Vec<ChunkGateRejection>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkModulesReport {
    pub chunk_id: String,
    pub counts: ChunkModulesCounts,
    pub final_module_contents: Vec<FinalModuleContent>,
    pub requested_logical_modules: Vec<RequestedLogicalModule>,
    /// Bare imported callable capabilities used by this chunk, with their
    /// defining evidence. Empty when no such import was resolved.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub imported_callables: BTreeMap<String, analysis::CallableSummary>,
    /// `purity: pure` hints the analyzer inferred automatically.
    /// Same content as the stderr warnings the build prints; carried
    /// on the report so JSON consumers can pin behavior across runs.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub redundant_purity_hints: Vec<RedundantPurityHint>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkModulesCounts {
    pub applied: usize,
    pub selected_bindings: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct FinalModuleContent {
    pub binding_names: Vec<String>,
    pub file: String,
    pub member_names: Vec<String>,
    /// Canonical [`spec::ModulePath`] (the report is per-chunk, so
    /// the chunk is implicit).
    pub path: spec::ModulePath,
    pub source_unit_ids: Vec<String>,
    /// True for a planned output with `ModuleRole::Unassigned`, never entry.
    pub residual: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct RequestedLogicalModule {
    /// Canonical [`spec::ModulePath`] of the requested target.
    pub target_path: spec::ModulePath,
    /// True for the synthesized memberless catchall request, not for an
    /// explicit module later assigned `ModuleRole::Unassigned`.
    pub residual: bool,
}

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

/// Where (and when) per-chunk validation reports are written.
#[derive(Debug, Clone)]
pub enum ReportEmission {
    /// No reports.
    None,
    /// Write every per-chunk report (dependency graph, modules report,
    /// rejection evidence) under this root. The directory is prepared
    /// (must be empty) up front.
    Full(PathBuf),
    /// Write reports only when the realizability gate rejects. Used by
    /// `debundle run --dry-run`: the accept path writes nothing (the
    /// no-output contract), but a rejection still materializes
    /// `dependency_graph.json.gz` + `cycles.json` / `atomic_group_conflicts.json`
    /// so the documented `debundle gate list/describe` follow-up works.
    OnRejection(PathBuf),
}

impl ReportEmission {
    /// Root for accept-path reports (dependency graph, modules report).
    pub fn full_dir(&self) -> Option<&Path> {
        match self {
            Self::Full(dir) => Some(dir),
            Self::None | Self::OnRejection(_) => None,
        }
    }

    /// Root for rejection evidence (written on gate rejection).
    pub fn rejection_dir(&self) -> Option<&Path> {
        match self {
            Self::Full(dir) | Self::OnRejection(dir) => Some(dir),
            Self::None => None,
        }
    }
}

/// What a chunk whose assignment the realizability gate rejected becomes.
/// The gate's verdict and evidence reports are the same either way.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GateRejectionPolicy {
    /// Fail materialization with the rendered rejection.
    Refuse,
    /// Lower the rejected assignment anyway, with a banner comment on the
    /// chunk's entry file, and report the rejection in
    /// [`MaterializeLogicalModulesResult::gate_rejections`] for the caller to
    /// mark and fail the run (`debundle run --emit-on-rejection`).
    LowerUnrunnable,
}

#[derive(Debug, Clone)]
pub struct MaterializeLogicalModulesOptions {
    pub graph_provenance: Option<graph_provenance::GraphProvenance>,
    pub config: spec::MaterializeLogicalModulesConfig,
    pub chunk_ids: Vec<String>,
    pub keep_going: bool,
    /// See `pipeline::TransformRunOptions::list_template_identifiers`.
    pub list_template_identifiers: bool,
    pub report_emission: ReportEmission,
    pub effect_conflicts_jsonl: bool,
    pub effect_conflicts_chunks: BTreeSet<String>,
    pub gate_rejection: GateRejectionPolicy,
    pub analysis: AnalysisMode,
}

/// What a materialization does about analysis, the cross-module pass and
/// per-chunk facts.
#[derive(Debug, Clone)]
pub enum AnalysisMode {
    Compute,
    /// Stop after the selector solve and return the completed projection
    /// (`debundle spec project-analysis`).
    Project(AnalysisSpec),
    /// Take the analysis from an artifact (`debundle run --analysis`). The
    /// run refuses an artifact computed by another engine, from other
    /// sources, or for a projection other than `base` completed by this
    /// run's selector solve.
    Load {
        base: AnalysisSpec,
        artifact: Vec<u8>,
    },
}

/// A materialization's result: lowered modules, or the projection when
/// [`AnalysisMode::Project`] stopped it after the selector solve.
pub enum Materialized {
    Lowered(MaterializeLogicalModulesResult),
    Projected(AnalysisSpec),
}

/// Spec-derived inputs to [`materialize_logical_modules`], all keyed by
/// chunk id.
pub struct MaterializeSpecInputs<'a> {
    pub host_behavior: &'a spec::HostBehavior,
    pub program_premises: &'a spec::ProgramPremises,
    /// Host loader per embedded input file (`inputs.embedded_loaders_path`).
    pub embedded_loaders: &'a BTreeMap<String, spec::BunLoader>,
    pub logical_modules: &'a BTreeMap<String, BTreeMap<String, LogicalModule>>,
    pub chunk_renames: &'a BTreeMap<String, ChunkRenames>,
    pub unassigned_mode: &'a BTreeMap<String, UnassignedMode>,
    pub chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
    pub chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
}

pub fn materialize_logical_modules(
    mut artifact: ChunkBundle,
    spec_inputs: MaterializeSpecInputs<'_>,
    vendor_plan: &vendor::VendorResolutionPlan,
    options: MaterializeLogicalModulesOptions,
) -> Result<Materialized> {
    let MaterializeSpecInputs {
        host_behavior,
        program_premises,
        embedded_loaders,
        logical_modules,
        chunk_renames,
        unassigned_mode,
        chunk_analysis_options,
        chunk_export_purity,
    } = spec_inputs;
    if options.chunk_ids.is_empty() {
        bail!("materialize_logical_modules requires at least one chunk_id");
    }
    let target_dir = normalize_optional_relative_dir(&options.config.target_dir)?;
    let selected_chunk_ids = normalized_chunk_ids(&options.chunk_ids)?;
    let seen: BTreeSet<&str> = selected_chunk_ids.iter().map(String::as_str).collect();

    // A chunk's `commuting_statements` are resolved only when its dependency
    // graph is built, so an entry for a chunk this run does not materialize
    // would never be checked.
    for (chunk, chunk_options) in chunk_analysis_options {
        ensure!(
            chunk_options.commuting_statements.is_empty()
                || seen.contains(normalize_module_path(chunk)?.as_str()),
            "chunk_analysis_options.{chunk}.commuting_statements names a chunk this run does not materialize"
        );
    }

    if let Some(dir) = options.report_emission.full_dir() {
        prepare_output_dir(dir)?;
    }

    let resolved_export_purity =
        export_policies::resolve_source_export_policies(&artifact, chunk_export_purity)?;
    let chunk_export_purity = resolved_export_purity.as_ref();

    if options.config.prune_other_chunks {
        prune_artifact_to_chunk_ids(&mut artifact, &selected_chunk_ids);
    }
    let artifact_indexes = ArtifactIndexes::build(&artifact)?;
    // Program-level pass over the selected chunks' retained static-import
    // closure: cross-module purity verdicts for each chunk's imported
    // function bindings, consumed per chunk via
    // `AnalysisInputs::imported_purities`. Vendor-swapped providers take part
    // through their bundled bodies (SPEC.md § Vendor swap equivalence).
    let cross_module_chunk_closure = cross_module::selected_static_import_closure(
        &artifact,
        &artifact_indexes,
        &selected_chunk_ids,
    )?;
    let reparsed = match &options.analysis {
        AnalysisMode::Project(_) => BTreeMap::new(),
        AnalysisMode::Compute | AnalysisMode::Load { .. } => {
            cross_module::reparse_unretained_entries(&artifact, Some(&cross_module_chunk_closure))
        }
    };
    let mut loaded = match &options.analysis {
        AnalysisMode::Load {
            artifact: bytes, ..
        } => Some(load_analysis(
            bytes,
            &artifact,
            &selected_chunk_ids,
            options.config.file.as_deref(),
            &reparsed,
            options.graph_provenance.as_ref(),
        )?),
        AnalysisMode::Compute | AnalysisMode::Project(_) => None,
    };
    let export_reentry_reports = match (&options.analysis, &mut loaded) {
        (_, Some((_, body))) => std::mem::take(&mut body.export_reentry)
            .into_iter()
            .map(|(chunk, mut report)| {
                // The artifact's copies of these echoed sections carry no
                // author notes; the report shows the run's spec.
                report.host_behavior = host_behavior.clone();
                report.program_premises = program_premises.clone();
                let chunk_id = artifact
                    .chunk_table
                    .get(&chunk)
                    .with_context(|| format!("analysis artifact names unknown chunk {chunk}"))?;
                Ok((chunk_id, report))
            })
            .collect::<Result<_>>()?,
        // The projection needs only the selector solve.
        (AnalysisMode::Project(_), None) => BTreeMap::new(),
        _ => export_reentry_reports(
            &artifact,
            &artifact_indexes,
            loader_surface::LoaderAssumptions {
                host_behavior,
                program_premises,
                embedded_loaders,
            },
            vendor_plan,
            &selected_chunk_ids,
            options.report_emission.rejection_dir().is_some(),
        )?,
    };
    let export_reentry_policies = export_reentry_reports
        .iter()
        .map(|(chunk_id, report)| (*chunk_id, report.policy.clone()))
        .collect();
    let artifact_ref: &ChunkBundle = &artifact;
    // Vendor consultation for lowering's import construction: runtime re-imports whose source
    // directive targets a partially / bundled-partially swapped chunk
    // are constructed as package / facade imports from the plan oracle
    // instead of chunk re-imports the post-materialize wave would
    // rewrite. `None` when no vendor mark could affect construction.
    let vendor_import_oracle =
        VendorReimportOracle::new(vendor_plan, &artifact.chunk_table, &artifact_indexes);
    // SWC's `swc_common::GLOBALS` is a `scoped_tls` thread-local, so the
    // outer `GLOBALS.set` wrap in `main.rs` / `run_agent` does NOT carry
    // into rayon worker threads. Capture a reference to the current
    // `Globals` and re-set inside each worker closure so `Mark::new()`
    // and `Id`-comparisons stay consistent across the whole pipeline.
    //
    // Chunks are planned in parallel up to the selector resolve, resolved
    // together, then analysed and lowered in parallel. Each phase's errors
    // are taken in chunk-id order, so which one fails a run (under
    // `--fail-fast`, which selector outcome) does not depend on scheduling.
    let prepared = GLOBALS
        .with(|globals| {
            selected_chunk_ids
                .par_iter()
                .map(|chunk_id| {
                    GLOBALS.set(globals, || {
                        prepare_logical_chunk(MaterializeLogicalChunkInputs {
                            context: ChunkContext {
                                artifact: artifact_ref,
                                artifact_indexes: &artifact_indexes,
                                chunk_id,
                                file: options.config.file.as_deref(),
                                target_dir: &target_dir,
                                keep_going: options.keep_going,
                                list_template_identifiers: options.list_template_identifiers,
                                report_emission: &options.report_emission,
                                effect_conflicts_jsonl: options.effect_conflicts_jsonl,
                                effect_conflicts_chunks: &options.effect_conflicts_chunks,
                                gate_rejection: options.gate_rejection,
                                export_reentry_reports: &export_reentry_reports,
                                vendor_import_oracle: vendor_import_oracle.as_ref(),
                                export_reentry_policies: &export_reentry_policies,
                            },
                            spec: ChunkSpec {
                                logical_modules,
                                chunk_renames,
                                unassigned_mode,
                            },
                        })
                    })
                })
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<Vec<_>>>()?;
    let (prepared, projections): (Vec<_>, Vec<_>) = prepared.into_iter().unzip();
    let resolutions = solve_projected_chunks(projections)?;
    let mut claimed = GLOBALS
        .with(|globals| {
            prepared
                .into_par_iter()
                .zip(resolutions)
                .map(|(chunk, resolution)| {
                    GLOBALS.set(globals, || claim_prepared_chunk(chunk, resolution))
                })
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<Vec<_>>>()?;
    let declared_assumptions = claimed
        .iter_mut()
        .map(ClaimedLogicalChunk::take_declared_assumptions)
        .collect();
    let mut resolved_member_sites = BTreeMap::new();
    for chunk in &claimed {
        for site in chunk.resolved_member_sites() {
            resolved_member_sites
                .entry((site.module.clone(), site.name.clone()))
                .or_insert_with(Vec::new)
                .push(site);
        }
    }
    let projection = AnalysisProjection::new(
        chunk_analysis_options,
        chunk_export_purity,
        host_behavior,
        program_premises.intrinsics_never_written.as_ref(),
        declared_assumptions,
        &resolved_member_sites,
    );
    let completed = |base: &AnalysisSpec| {
        base.clone().with_premises(
            projection.declared_assumptions.clone(),
            projection.premise_members.clone(),
        )
    };
    let (cross_module_purities, mut loaded_chunks) = match (&options.analysis, loaded) {
        (AnalysisMode::Project(base), _) => return Ok(Materialized::Projected(completed(base)?)),
        (AnalysisMode::Load { base, .. }, Some((header, body))) => {
            let completed = completed(base)?;
            if completed.encode()? != header.projection {
                let differences =
                    completed.differences(&AnalysisSpec::decode(&header.projection)?)?;
                bail!(
                    "analysis artifact was computed for another projection; differing: {}. Rerun `debundle spec project-analysis` and `debundle analyze`",
                    differences.join(", ")
                );
            }
            (body.cross_module, Some(body.chunks))
        }
        _ => (
            cross_module::collect_cross_module_imported_purities(
                &artifact,
                &artifact_indexes,
                &projection,
                cross_module::CrossModuleAnalysisOptions {
                    graph_provenance: options.graph_provenance.as_ref(),
                    replacement_packages: &vendor_plan.replacement_packages(),
                },
                Some(&cross_module_chunk_closure),
                &reparsed,
            )?,
            None,
        ),
    };
    let claimed = claimed
        .into_iter()
        .map(|chunk| {
            let facts = loaded_chunks
                .as_mut()
                .map(|chunks| {
                    chunks.remove(chunk.chunk_id()).with_context(|| {
                        format!(
                            "analysis artifact has no facts for chunk {}",
                            chunk.chunk_id()
                        )
                    })
                })
                .transpose()?;
            Ok((chunk, facts))
        })
        .collect::<Result<Vec<_>>>()?;
    let program_graph = ProgramGraphOutput::new(
        options.graph_provenance.as_ref(),
        cross_module_purities.native_integrity.as_ref(),
        AuthorInputLocations::new(options.graph_provenance.as_ref(), chunk_analysis_options)?,
    );
    let chunk_results = GLOBALS
        .with(|globals| {
            claimed
                .into_par_iter()
                .map(|(chunk, facts)| {
                    GLOBALS.set(globals, || {
                        finish_logical_chunk(
                            chunk,
                            &projection,
                            &cross_module_purities,
                            facts,
                            &program_graph,
                            CommutingStatementsContext {
                                graph_provenance: options.graph_provenance.as_ref(),
                                resolved_member_sites: &resolved_member_sites,
                            },
                        )
                    })
                })
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<Vec<_>>>()?;

    let mut reports = Vec::with_capacity(chunk_results.len());
    let mut applied = Vec::<SelectedModuleLowering>::new();
    let mut unmatched_spec_claims = Vec::<UnmatchedSpecClaim>::new();
    let mut gate_rejections = Vec::<ChunkGateRejection>::new();
    let mut vendor_reference_rewrites = BTreeMap::<(ChunkId, String), usize>::new();
    for chunk_result in &chunk_results {
        if let Some(report_out_dir) = options.report_emission.full_dir() {
            write_chunk_report_json(
                report_out_dir,
                artifact.chunk_table.name(chunk_result.chunk_id),
                MODULES_REPORT,
                &chunk_result.report,
            )?;
        }
        applied.extend(chunk_result.applied.iter().cloned());
        reports.push(chunk_result.report.clone());
        unmatched_spec_claims.extend(chunk_result.unmatched_spec_claims.iter().cloned());
        gate_rejections.extend(chunk_result.gate_rejection.iter().cloned());
        for (key, count) in &chunk_result.vendor_reference_rewrites {
            *vendor_reference_rewrites.entry(key.clone()).or_insert(0) += count;
        }
    }
    let output = collect_materialized_logical_chunks(artifact, &target_dir, chunk_results)?;

    let module_count: usize = reports.iter().map(|r| r.final_module_contents.len()).sum();
    Ok(Materialized::Lowered(MaterializeLogicalModulesResult {
        output,
        selected_lowerings: applied,
        module_count,
        vendor_reference_rewrites,
        unmatched_spec_claims,
        gate_rejections,
    }))
}

/// Chunk ids normalized, first occurrence kept.
fn normalized_chunk_ids(chunk_ids: &[String]) -> Result<Vec<String>> {
    let mut seen = BTreeSet::new();
    let mut normalized_ids = Vec::new();
    for chunk_id in chunk_ids {
        let normalized = normalize_module_path(chunk_id)?;
        if seen.insert(normalized.clone()) {
            normalized_ids.push(normalized);
        }
    }
    Ok(normalized_ids)
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
    reparsed: &BTreeMap<ChunkId, cross_module::ReparsedEntry>,
) -> Result<ContextOrdinals> {
    let mut modules = Vec::new();
    for chunk_id in analysed_chunk_ids {
        let interned = artifact
            .chunk_table
            .get(chunk_id)
            .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
        let (_, _, runtime_ast) =
            materialize::chunk_runtime_entry(artifact, chunk_id, interned, file)?;
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
fn load_analysis(
    bytes: &[u8],
    artifact: &ChunkBundle,
    analysed_chunk_ids: &[String],
    file: Option<&str>,
    reparsed: &BTreeMap<ChunkId, cross_module::ReparsedEntry>,
    provenance: Option<&GraphProvenance>,
) -> Result<(AnalysisArtifactHeader, AnalysisArtifactBody)> {
    let (header, body) = analysis_artifact::read_header(bytes)?;
    let provenance = provenance.context("an analysis artifact requires graph provenance")?;
    ensure!(
        header.engine_sha256 == provenance.engine_sha256,
        "analysis artifact was computed by engine {}, this engine is {}",
        header.engine_sha256,
        provenance.engine_sha256
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
    let cross_module_chunk_closure = cross_module::selected_static_import_closure(
        &artifact,
        &artifact_indexes,
        &selected_chunk_ids,
    )?;
    let projection = AnalysisProjection::new(
        &spec.chunk_analysis_options,
        resolved_export_purity.as_ref(),
        &spec.host_behavior,
        spec.program_premises.intrinsics_never_written.as_ref(),
        spec.declared_assumptions.clone(),
        &spec.premise_members,
    );
    let reparsed =
        cross_module::reparse_unretained_entries(&artifact, Some(&cross_module_chunk_closure));
    let cross_module = cross_module::collect_cross_module_imported_purities(
        &artifact,
        &artifact_indexes,
        &projection,
        cross_module::CrossModuleAnalysisOptions {
            graph_provenance: Some(graph_provenance),
            replacement_packages: &vendor_plan.replacement_packages(),
        },
        Some(&cross_module_chunk_closure),
        &reparsed,
    )?;
    let chunks = GLOBALS
        .with(|globals| {
            selected_chunk_ids
                .par_iter()
                .map(|chunk_id| {
                    GLOBALS.set(globals, || {
                        materialize::analyze_logical_chunk(
                            &artifact,
                            &artifact_indexes,
                            chunk_id,
                            spec.entry_file.as_deref(),
                            &export_reentry_policies,
                            &projection,
                            &cross_module,
                        )
                        .map(|facts| (chunk_id.clone(), facts))
                    })
                })
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<BTreeMap<_, _>>>()?;
    let header = AnalysisArtifactHeader {
        engine_sha256: graph_provenance.engine_sha256.clone(),
        projection: spec.encode()?,
        chunk_sources: chunk_source_digests(graph_provenance),
    };
    let body = AnalysisArtifactBody {
        cross_module,
        export_reentry: export_reentry_reports
            .into_iter()
            .map(|(chunk_id, report)| (artifact.chunk_table.name(chunk_id).to_string(), report))
            .collect(),
        chunks,
    };
    let ordinals = context_ordinals(
        &artifact,
        &selected_chunk_ids,
        spec.entry_file.as_deref(),
        &reparsed,
    )?;
    analysis_artifact::write_artifact(&header, &body, &ordinals)
}

fn export_reentry_reports(
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
