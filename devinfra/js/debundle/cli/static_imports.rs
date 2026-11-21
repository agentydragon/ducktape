//! Source-only reachability through original, ordinary static ESM requests.

use source_identity::ChunkId;
use std::collections::{BTreeSet, VecDeque};
use transform_command::TransformArgs;

use anyhow::{Context, Result};
use artifact::reports::{InteropUse, InteropUseKind, StaticModuleRequestMode};
use artifact::{
    ArtifactIndexes, ChunkArtifact, ChunkBundle, ImportReferenceKind, ResolvedImportReference,
};
use clap::Args;
use peel::OutputFormat;
use pipeline::load_transform_spec_source;
use prepare_chunks::prepare_js_chunks;
use serde::Serialize;
use std::io::Write;

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

#[derive(Clone, Copy, Serialize, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
enum EdgeKind {
    OrdinaryStaticEsm,
    ConditionalImportMetaRequire,
}

#[derive(Clone, Copy, Serialize, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
enum ResolutionKind {
    ArtifactPath,
    SourcePath,
}

#[derive(Clone, Serialize)]
struct RequestIssue {
    from: String,
    specifier: String,
    reason: RequestIssueReason,
    #[serde(skip_serializing_if = "Option::is_none")]
    resolved_target: Option<String>,
}

#[derive(Clone, Copy, Serialize, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
enum RequestIssueReason {
    NonordinaryStaticRequest,
    UnresolvedStaticRequest,
    MissingTargetChunk,
    NonentryStaticRequest,
    UnresolvedHostLoaderCandidate,
    NonentryHostLoaderCandidate,
    MissingHostLoaderTarget,
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
    resolution: Option<Landing>,
}

/// Where an import specifier lands relative to the original chunk entry files.
#[derive(Clone, Copy, Serialize, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
enum Landing {
    ArtifactPath,
    SourcePath,
    Unresolved,
    MissingTargetChunk,
    NonentryTarget,
}

impl Landing {
    fn of(artifact: &ChunkBundle, resolved: Option<&ResolvedImportReference>) -> Self {
        let Some(reference) = resolved else {
            return Self::Unresolved;
        };
        match artifact.find_chunk(reference.target_chunk_id) {
            None => Self::MissingTargetChunk,
            Some(chunk) if chunk.analysis.entry_file != reference.target_file => {
                Self::NonentryTarget
            }
            Some(_) => match reference.kind {
                ImportReferenceKind::ArtifactPath => Self::ArtifactPath,
                ImportReferenceKind::SourcePath => Self::SourcePath,
            },
        }
    }
}

struct RequestGraph {
    edges: Vec<Vec<(ChunkId, RequestEdge)>>,
    issues: Vec<Vec<RequestIssue>>,
}

pub(super) fn run(args: StaticImportsArgs, output: &mut dyn Write) -> Result<()> {
    let format = args.format;
    let from_name = args.from;
    let target_names = args.target;
    let max_issues = args.max_issues;
    let include_host_loader_candidates = args.include_host_loader_candidates;
    let interop_census = args.interop_census;
    let max_interop_sites = args.max_interop_sites;
    let cli = args.transform.context.resolve()?;
    // Load only the authored input locations. Selector claims, vendor plans,
    // realizability, and output roots are deliberately outside this query.
    let spec = load_transform_spec_source(&cli.spec_source)?;
    let loaded = artifact::load_js_chunks(&spec.inputs.input_root, &spec.inputs.js_list_path)?;
    let prepared = prepare_js_chunks(
        &spec.materialize_chunk_ids(),
        &BTreeSet::new(),
        &spec.vendor,
        loaded,
    )?;
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
        collect_requests(artifact, source.indexes(), include_host_loader_candidates);
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
            .then(|| collect_interop_census(artifact, source.indexes(), max_interop_sites)),
    };
    emit_report(
        output,
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
                                edge.edge_kind,
                                edge.resolution,
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
                    issue.reason,
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
                    let kinds = serde_json::to_string(&chunk.kinds)?;
                    out.push_str(&format!(
                        "  {}: {} sites {}\n",
                        chunk.chunk, chunk.site_count, kinds
                    ));
                }
                for site in &census.sites {
                    let kind = site.kind.machine_code();
                    out.push_str(&format!(
                        "  {} item {} line {:?}: {}{}{}\n",
                        site.chunk,
                        site.item,
                        site.line,
                        kind,
                        site.specifier
                            .as_deref()
                            .map_or(String::new(), |specifier| format!(" {specifier:?}")),
                        site.resolution.map_or(String::new(), |resolution| {
                            format!(
                                " -> {} ({})",
                                site.resolved_target.as_deref().unwrap_or("?"),
                                resolution
                            )
                        })
                    ));
                }
            }
            Ok(())
        },
        "writing static-imports output",
    )
}

fn collect_requests(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    include_host_loader_candidates: bool,
) -> RequestGraph {
    let mut edges = vec![Vec::new(); artifact.chunk_table.len()];
    let mut issues = (0..artifact.chunk_table.len())
        .map(|_| Vec::new())
        .collect::<Vec<_>>();
    for chunk in &artifact.chunks {
        let from = chunk.chunk_id;
        let from_name = artifact.chunk_table.name(from);
        // Records an edge for an `Ok` verdict and an issue for an `Err`. `site` is
        // the `import.meta.require` call of a host-loader candidate.
        let mut record = |specifier: &str,
                          resolved: Option<ResolvedImportReference>,
                          verdict: Result<ResolutionKind, RequestIssueReason>,
                          edge_kind: EdgeKind,
                          site: Option<&InteropUse>| match verdict {
            Err(reason) => issues[from.0].push(RequestIssue {
                from: from_name.to_string(),
                specifier: specifier.to_string(),
                reason,
                resolved_target: resolved
                    .as_ref()
                    .map(|reference| target_label(artifact, reference)),
            }),
            Ok(resolution) => {
                let reference = resolved.expect("an entry landing is resolved");
                edges[from.0].push((
                    reference.target_chunk_id,
                    RequestEdge {
                        from: from_name.to_string(),
                        specifier: specifier.to_string(),
                        edge_kind,
                        resolution,
                        target_file: reference.target_file,
                        to: artifact
                            .chunk_table
                            .name(reference.target_chunk_id)
                            .to_string(),
                        source_item: site.map(|site| site.item_index),
                        source_line: site.and_then(|site| site.line),
                    },
                ));
            }
        };
        for request in &chunk.analysis.runtime_facts.static_requests {
            let resolved = resolve_import(artifact, indexes, chunk, &request.specifier);
            let verdict = if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                Err(RequestIssueReason::NonordinaryStaticRequest)
            } else {
                match Landing::of(artifact, resolved.as_ref()) {
                    Landing::Unresolved => Err(RequestIssueReason::UnresolvedStaticRequest),
                    Landing::MissingTargetChunk => Err(RequestIssueReason::MissingTargetChunk),
                    Landing::NonentryTarget => Err(RequestIssueReason::NonentryStaticRequest),
                    Landing::ArtifactPath => Ok(ResolutionKind::ArtifactPath),
                    Landing::SourcePath => Ok(ResolutionKind::SourcePath),
                }
            };
            record(
                &request.specifier,
                resolved,
                verdict,
                EdgeKind::OrdinaryStaticEsm,
                None,
            );
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
                let resolved = resolve_import(artifact, indexes, chunk, specifier);
                let verdict = match Landing::of(artifact, resolved.as_ref()) {
                    Landing::Unresolved => Err(RequestIssueReason::UnresolvedHostLoaderCandidate),
                    Landing::MissingTargetChunk => Err(RequestIssueReason::MissingHostLoaderTarget),
                    Landing::NonentryTarget => Err(RequestIssueReason::NonentryHostLoaderCandidate),
                    Landing::ArtifactPath => Ok(ResolutionKind::ArtifactPath),
                    Landing::SourcePath => Ok(ResolutionKind::SourcePath),
                };
                record(
                    specifier,
                    resolved,
                    verdict,
                    EdgeKind::ConditionalImportMetaRequire,
                    Some(site),
                );
            }
        }
    }
    RequestGraph { edges, issues }
}

fn resolve_import(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    chunk: &ChunkArtifact,
    specifier: &str,
) -> Option<ResolvedImportReference> {
    indexes.resolve_runtime_import_reference(
        specifier,
        chunk.chunk_id,
        &chunk.analysis.entry_file,
        &artifact.chunk_table,
    )
}

fn target_label(artifact: &ChunkBundle, reference: &ResolvedImportReference) -> String {
    format!(
        "{}/{}",
        artifact.chunk_table.name(reference.target_chunk_id),
        reference.target_file
    )
}

fn collect_interop_census(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    max_sites: usize,
) -> InteropCensus {
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
            let resolved = site
                .specifier
                .as_deref()
                .and_then(|specifier| resolve_import(artifact, indexes, chunk, specifier));
            sites.push(InteropSite {
                chunk: name.clone(),
                item: site.item_index,
                line: site.line,
                kind: site.kind,
                specifier: site.specifier.clone(),
                resolved_target: resolved
                    .as_ref()
                    .map(|reference| target_label(artifact, reference)),
                resolution: site
                    .specifier
                    .is_some()
                    .then(|| Landing::of(artifact, resolved.as_ref())),
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
    InteropCensus {
        chunks,
        sites,
        site_count,
        sites_truncated: site_count > max_sites,
    }
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
