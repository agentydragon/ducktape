//! Source-only reachability through original, ordinary static ESM requests.

use std::collections::{BTreeSet, VecDeque};

use anyhow::{Context, Result};
use artifact::reports::{InteropUseKind, StaticModuleRequestMode};
use artifact::{ChunkBundle, ChunkId, ImportReferenceKind};
use clap::Args;
use peel::OutputFormat;
use pipeline::{TransformArgs, load_transform_spec_source};
use prepare_chunks::prepare_js_chunks;
use serde::Serialize;

use crate::emit_report;

#[derive(Debug, Args)]
pub(super) struct StaticImportsArgs {
    #[command(flatten)]
    transform: TransformArgs,
    /// Original source chunk ID at which the ordinary static import traversal starts.
    #[arg(long)]
    from: String,
    /// Original source chunk ID to compare with --from; repeat for several targets.
    #[arg(long, required = true)]
    target: Vec<String>,
    /// Maximum request diagnostics returned; the total is always reported.
    #[arg(long, default_value_t = 100)]
    max_issues: usize,
    /// Include every syntactic direct literal import.meta.require call as a
    /// conditional source edge, regardless of function nesting.
    #[arg(long)]
    include_host_loader_candidates: bool,
    /// Include typed source-site census for all chunks, including unrelated chunks.
    #[arg(long)]
    interop_census: bool,
    /// Maximum detailed interop sites returned; chunk counts remain complete.
    #[arg(long, default_value_t = 1000)]
    max_interop_sites: usize,
    #[arg(long, value_enum)]
    pub(super) format: Option<OutputFormat>,
}

#[derive(Clone, Serialize)]
struct RequestEdge {
    from: String,
    specifier: String,
    edge_kind: EdgeKind,
    resolution: ResolutionKind,
    target_file: String,
    to: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    source_item: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    source_line: Option<usize>,
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum EdgeKind {
    OrdinaryStaticEsm,
    ConditionalImportMetaRequire,
}

impl EdgeKind {
    fn label(self) -> &'static str {
        match self {
            Self::OrdinaryStaticEsm => "ordinary_static_esm",
            Self::ConditionalImportMetaRequire => "conditional_import_meta_require",
        }
    }
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum ResolutionKind {
    ArtifactPath,
    SourcePath,
}

impl ResolutionKind {
    fn label(self) -> &'static str {
        match self {
            Self::ArtifactPath => "artifact_path",
            Self::SourcePath => "source_path",
        }
    }
}

#[derive(Clone, Serialize)]
struct RequestIssue {
    from: String,
    specifier: String,
    reason: RequestIssueReason,
    #[serde(skip_serializing_if = "Option::is_none")]
    resolved_target: Option<String>,
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum RequestIssueReason {
    NonordinaryStaticRequest,
    UnresolvedStaticRequest,
    MissingTargetChunk,
    NonentryStaticRequest,
    UnresolvedHostLoaderCandidate,
    NonentryHostLoaderCandidate,
    MissingHostLoaderTarget,
}

impl RequestIssueReason {
    fn label(self) -> &'static str {
        match self {
            Self::NonordinaryStaticRequest => "nonordinary_static_request",
            Self::UnresolvedStaticRequest => "unresolved_static_request",
            Self::MissingTargetChunk => "missing_target_chunk",
            Self::NonentryStaticRequest => "nonentry_static_request",
            Self::UnresolvedHostLoaderCandidate => "unresolved_host_loader_candidate",
            Self::NonentryHostLoaderCandidate => "nonentry_host_loader_candidate",
            Self::MissingHostLoaderTarget => "missing_host_loader_target",
        }
    }
}

#[derive(Serialize)]
struct TargetReport {
    target: String,
    /// Equality is zero-edge identity; every path below requires at least one edge.
    same_chunk: bool,
    from_to_target: Option<Vec<RequestEdge>>,
    target_to_from: Option<Vec<RequestEdge>>,
    target_self_cycle: Option<Vec<RequestEdge>>,
}

#[derive(Serialize)]
struct StaticImportsReport {
    from: String,
    targets: Vec<TargetReport>,
    request_issues: Vec<RequestIssue>,
    request_issue_count: usize,
    request_issues_truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    interop_census: Option<InteropCensus>,
}

#[derive(Serialize)]
struct InteropCensus {
    chunks: Vec<InteropChunk>,
    sites: Vec<InteropSite>,
    site_count: usize,
    sites_truncated: bool,
}

#[derive(Serialize)]
struct InteropChunk {
    chunk: String,
    has_cjs_interop: bool,
    has_own_cjs_export_surface: bool,
    site_count: usize,
    kinds: std::collections::BTreeMap<InteropUseKind, usize>,
}

#[derive(Serialize)]
struct InteropSite {
    chunk: String,
    item: usize,
    line: Option<usize>,
    kind: InteropUseKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    specifier: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    resolved_target: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    resolution: Option<CandidateResolution>,
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum CandidateResolution {
    ArtifactPath,
    SourcePath,
    Unresolved,
    MissingTargetChunk,
    NonentryTarget,
}

impl CandidateResolution {
    fn label(self) -> &'static str {
        match self {
            Self::ArtifactPath => "artifact_path",
            Self::SourcePath => "source_path",
            Self::Unresolved => "unresolved",
            Self::MissingTargetChunk => "missing_target_chunk",
            Self::NonentryTarget => "nonentry_target",
        }
    }
}

struct RequestGraph {
    edges: Vec<Vec<(ChunkId, RequestEdge)>>,
    issues: Vec<Vec<RequestIssue>>,
}

pub(super) fn run(args: StaticImportsArgs) -> Result<()> {
    let format = args.format;
    let from_name = args.from;
    let target_names = args.target;
    let max_issues = args.max_issues;
    let include_host_loader_candidates = args.include_host_loader_candidates;
    let interop_census = args.interop_census;
    let max_interop_sites = args.max_interop_sites;
    let cli = args.transform.resolve()?;
    // Load only the authored input locations. Selector claims, vendor plans,
    // realizability, and output roots are deliberately outside this query.
    let spec = load_transform_spec_source(&cli.spec_source)?;
    let loaded = artifact::load_js_chunks(&spec.inputs.input_root, &spec.inputs.js_list_path)?;
    let prepared = prepare_js_chunks(&spec, loaded)?;
    let source = prepared.program;
    let artifact = source.artifact();
    let table = &artifact.chunk_table;
    let from = table
        .get(&from_name)
        .with_context(|| format!("unknown --from chunk {from_name:?}"))?;
    let targets = target_names
        .iter()
        .map(|name| {
            table
                .get(name)
                .with_context(|| format!("unknown --target chunk {name:?}"))
        })
        .collect::<Result<Vec<_>>>()?;
    let RequestGraph { edges, issues } =
        collect_requests(artifact, source.indexes(), include_host_loader_candidates)?;
    let mut relevant = reachable(from, &edges);
    let reports = targets
        .into_iter()
        .map(|target| {
            relevant.extend(reachable(target, &edges));
            TargetReport {
                target: table.name(target).to_string(),
                same_chunk: from == target,
                from_to_target: strict_path(from, target, &edges),
                target_to_from: strict_path(target, from, &edges),
                target_self_cycle: strict_path(target, target, &edges),
            }
        })
        .collect();
    let issue_count = relevant.iter().map(|id| issues[id.0].len()).sum();
    let selected_issues = relevant
        .iter()
        .flat_map(|id| issues[id.0].iter())
        .take(max_issues)
        .cloned()
        .collect();
    let report = StaticImportsReport {
        from: from_name,
        targets: reports,
        request_issues: selected_issues,
        request_issue_count: issue_count,
        request_issues_truncated: issue_count > max_issues,
        interop_census: interop_census
            .then(|| collect_interop_census(artifact, source.indexes(), max_interop_sites))
            .transpose()?,
    };
    emit_report(
        format,
        &report,
        |report, out| {
            out.push_str("Resolved ordinary static ESM requests in original source chunks.\n");
            if include_host_loader_candidates {
                out.push_str("Conditional import.meta.require edges include all syntactic literal calls, even deferred calls; their host-loader identity is an unverified author premise.\n");
            }
            out.push_str("No path means no path in this source subgraph; runtime loading and cache state are unassessed.\n");
            for target in &report.targets {
                let from_path = target
                    .from_to_target
                    .as_ref()
                    .map_or("no source path", |_| "source path");
                let back_path = target
                    .target_to_from
                    .as_ref()
                    .map_or("no source path", |_| "source path");
                let self_cycle = target
                    .target_self_cycle
                    .as_ref()
                    .map_or("no source path", |_| "source path");
                out.push_str(&format!(
                    "{} -> {}: {from_path}; {} -> {}: {back_path}; {} self-cycle: {self_cycle}; same chunk: {}\n",
                    report.from, target.target, target.target, report.from, target.target, target.same_chunk
                ));
                for (label, path) in [
                    ("from_to_target", &target.from_to_target),
                    ("target_to_from", &target.target_to_from),
                    ("target_self_cycle", &target.target_self_cycle),
                ] {
                    if let Some(path) = path {
                        out.push_str(&format!("  {label}:\n"));
                        for edge in path {
                            out.push_str(&format!(
                                "    {} --{}--> {} ({}/{}/{})\n",
                                edge.from,
                                edge.specifier,
                                edge.to,
                                edge.edge_kind.label(),
                                edge.resolution.label(),
                                edge.target_file
                            ));
                        }
                    }
                }
            }
            out.push_str(&format!(
                "request issues: {} (showing {})\n",
                report.request_issue_count,
                report.request_issues.len()
            ));
            for issue in &report.request_issues {
                out.push_str(&format!(
                    "  {} --{}: {}{}\n",
                    issue.from,
                    issue.specifier,
                    issue.reason.label(),
                    issue
                        .resolved_target
                        .as_ref()
                        .map_or(String::new(), |target| format!(" ({target})"))
                ));
            }
            if let Some(census) = &report.interop_census {
                out.push_str(&format!(
                    "interop sites: {} in {} chunks (showing {})\n",
                    census.site_count,
                    census.chunks.len(),
                    census.sites.len()
                ));
                for chunk in &census.chunks {
                    out.push_str(&format!(
                        "  {}: {} sites {}\n",
                        chunk.chunk,
                        chunk.site_count,
                        serde_json::to_string(&chunk.kinds).expect("typed census keys serialize")
                    ));
                }
                for site in &census.sites {
                    let kind =
                        serde_json::to_string(&site.kind).expect("typed census kind serializes");
                    out.push_str(&format!(
                        "  {} item {} line {:?}: {}{}{}\n",
                        site.chunk,
                        site.item,
                        site.line,
                        kind.trim_matches('"'),
                        site.specifier
                            .as_deref()
                            .map_or(String::new(), |specifier| format!(" {specifier:?}")),
                        site.resolution.map_or(String::new(), |resolution| {
                            format!(
                                " -> {} ({})",
                                site.resolved_target.as_deref().unwrap_or("?"),
                                resolution.label()
                            )
                        })
                    ));
                }
            }
        },
        "writing static-imports output",
    )
}

fn collect_requests(
    artifact: &ChunkBundle,
    indexes: &artifact::ArtifactIndexes,
    include_host_loader_candidates: bool,
) -> Result<RequestGraph> {
    let mut edges = vec![Vec::new(); artifact.chunk_table.len()];
    let mut issues = (0..artifact.chunk_table.len())
        .map(|_| Vec::new())
        .collect::<Vec<_>>();
    for chunk in &artifact.chunks {
        let from = chunk.chunk_id;
        let from_name = artifact.chunk_table.name(from);
        for request in &chunk.analysis.runtime_facts.static_requests {
            let resolved = indexes.resolve_runtime_import_reference(
                &request.specifier,
                from,
                &chunk.analysis.entry_file,
                &artifact.chunk_table,
            );
            let issue = if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                Some(RequestIssueReason::NonordinaryStaticRequest)
            } else if resolved.is_none() {
                Some(RequestIssueReason::UnresolvedStaticRequest)
            } else if let Some(reference) = &resolved {
                if !artifact.has_chunk(reference.target_chunk_id) {
                    Some(RequestIssueReason::MissingTargetChunk)
                } else if artifact
                    .chunk(reference.target_chunk_id)?
                    .analysis
                    .entry_file
                    != reference.target_file
                {
                    Some(RequestIssueReason::NonentryStaticRequest)
                } else {
                    None
                }
            } else {
                None
            };
            if let Some(reason) = issue {
                issues[from.0].push(RequestIssue {
                    from: from_name.to_string(),
                    specifier: request.specifier.clone(),
                    reason,
                    resolved_target: resolved.as_ref().map(|reference| {
                        format!(
                            "{}/{}",
                            artifact.chunk_table.name(reference.target_chunk_id),
                            reference.target_file
                        )
                    }),
                });
                continue;
            }
            let reference = resolved.expect("ordinary resolved entry request");
            edges[from.0].push((
                reference.target_chunk_id,
                RequestEdge {
                    from: from_name.to_string(),
                    specifier: request.specifier.clone(),
                    edge_kind: EdgeKind::OrdinaryStaticEsm,
                    resolution: match reference.kind {
                        ImportReferenceKind::ArtifactPath => ResolutionKind::ArtifactPath,
                        ImportReferenceKind::SourcePath => ResolutionKind::SourcePath,
                    },
                    target_file: reference.target_file,
                    to: artifact
                        .chunk_table
                        .name(reference.target_chunk_id)
                        .to_string(),
                    source_item: None,
                    source_line: None,
                },
            ));
        }
        if include_host_loader_candidates {
            for site in &chunk.analysis.runtime_facts.interop_uses {
                if site.kind != InteropUseKind::ImportMetaRequireLiteralCall {
                    continue;
                }
                let specifier = site
                    .specifier
                    .as_deref()
                    .expect("literal call has specifier");
                let resolved = indexes.resolve_runtime_import_reference(
                    specifier,
                    from,
                    &chunk.analysis.entry_file,
                    &artifact.chunk_table,
                );
                let issue = if let Some(reference) = &resolved {
                    if !artifact.has_chunk(reference.target_chunk_id) {
                        Some(RequestIssueReason::MissingHostLoaderTarget)
                    } else if artifact
                        .chunk(reference.target_chunk_id)?
                        .analysis
                        .entry_file
                        != reference.target_file
                    {
                        Some(RequestIssueReason::NonentryHostLoaderCandidate)
                    } else {
                        None
                    }
                } else {
                    Some(RequestIssueReason::UnresolvedHostLoaderCandidate)
                };
                if let Some(reason) = issue {
                    issues[from.0].push(RequestIssue {
                        from: from_name.to_string(),
                        specifier: specifier.to_string(),
                        reason,
                        resolved_target: resolved.as_ref().map(|reference| {
                            format!(
                                "{}/{}",
                                artifact.chunk_table.name(reference.target_chunk_id),
                                reference.target_file
                            )
                        }),
                    });
                    continue;
                }
                let reference = resolved.expect("resolved entry loader candidate");
                edges[from.0].push((
                    reference.target_chunk_id,
                    RequestEdge {
                        from: from_name.to_string(),
                        specifier: specifier.to_string(),
                        edge_kind: EdgeKind::ConditionalImportMetaRequire,
                        resolution: match reference.kind {
                            ImportReferenceKind::ArtifactPath => ResolutionKind::ArtifactPath,
                            ImportReferenceKind::SourcePath => ResolutionKind::SourcePath,
                        },
                        target_file: reference.target_file,
                        to: artifact
                            .chunk_table
                            .name(reference.target_chunk_id)
                            .to_string(),
                        source_item: Some(site.item_index),
                        source_line: site.line,
                    },
                ));
            }
        }
    }
    Ok(RequestGraph { edges, issues })
}

fn collect_interop_census(
    artifact: &ChunkBundle,
    indexes: &artifact::ArtifactIndexes,
    max_sites: usize,
) -> Result<InteropCensus> {
    let mut chunks = Vec::new();
    let mut sites = Vec::new();
    let mut site_count = 0;
    for chunk in &artifact.chunks {
        let uses = &chunk.analysis.runtime_facts.interop_uses;
        if uses.is_empty() && !chunk.analysis.runtime_facts.has_cjs_interop {
            continue;
        }
        let name = artifact.chunk_table.name(chunk.chunk_id).to_string();
        let mut kinds = std::collections::BTreeMap::new();
        for site in uses {
            *kinds.entry(site.kind).or_insert(0) += 1;
            site_count += 1;
            if sites.len() >= max_sites {
                continue;
            }
            let resolved = site.specifier.as_deref().and_then(|specifier| {
                indexes.resolve_runtime_import_reference(
                    specifier,
                    chunk.chunk_id,
                    &chunk.analysis.entry_file,
                    &artifact.chunk_table,
                )
            });
            let resolution = if site.specifier.is_some() {
                Some(if let Some(reference) = &resolved {
                    if !artifact.has_chunk(reference.target_chunk_id) {
                        CandidateResolution::MissingTargetChunk
                    } else if artifact
                        .chunk(reference.target_chunk_id)?
                        .analysis
                        .entry_file
                        != reference.target_file
                    {
                        CandidateResolution::NonentryTarget
                    } else {
                        match reference.kind {
                            ImportReferenceKind::ArtifactPath => CandidateResolution::ArtifactPath,
                            ImportReferenceKind::SourcePath => CandidateResolution::SourcePath,
                        }
                    }
                } else {
                    CandidateResolution::Unresolved
                })
            } else {
                None
            };
            sites.push(InteropSite {
                chunk: name.clone(),
                item: site.item_index,
                line: site.line,
                kind: site.kind,
                specifier: site.specifier.clone(),
                resolved_target: resolved.as_ref().map(|reference| {
                    format!(
                        "{}/{}",
                        artifact.chunk_table.name(reference.target_chunk_id),
                        reference.target_file
                    )
                }),
                resolution,
            });
        }
        chunks.push(InteropChunk {
            chunk: name,
            has_cjs_interop: chunk.analysis.runtime_facts.has_cjs_interop,
            has_own_cjs_export_surface: chunk.analysis.runtime_facts.has_own_cjs_export_surface,
            site_count: uses.len(),
            kinds,
        });
    }
    Ok(InteropCensus {
        chunks,
        sites,
        site_count,
        sites_truncated: site_count > max_sites,
    })
}

fn reachable(start: ChunkId, edges: &[Vec<(ChunkId, RequestEdge)>]) -> BTreeSet<ChunkId> {
    let mut seen = BTreeSet::from([start]);
    let mut pending = VecDeque::from([start]);
    while let Some(chunk) = pending.pop_front() {
        for (next, _) in &edges[chunk.0] {
            if seen.insert(*next) {
                pending.push_back(*next);
            }
        }
    }
    seen
}

/// Breadth-first nonempty path, including a real edge when start == goal.
fn strict_path(
    start: ChunkId,
    goal: ChunkId,
    edges: &[Vec<(ChunkId, RequestEdge)>],
) -> Option<Vec<RequestEdge>> {
    let mut seen = BTreeSet::from([start]);
    let mut previous: Vec<Option<(ChunkId, RequestEdge)>> = vec![None; edges.len()];
    let mut pending = VecDeque::from([start]);
    while let Some(chunk) = pending.pop_front() {
        for (next, edge) in &edges[chunk.0] {
            if *next == goal {
                let mut result = vec![edge.clone()];
                let mut cursor = chunk;
                while cursor != start {
                    let (parent, incoming) = previous[cursor.0].as_ref()?;
                    result.push(incoming.clone());
                    cursor = *parent;
                }
                result.reverse();
                return Some(result);
            }
            if seen.insert(*next) {
                previous[next.0] = Some((chunk, edge.clone()));
                pending.push_back(*next);
            }
        }
    }
    None
}
