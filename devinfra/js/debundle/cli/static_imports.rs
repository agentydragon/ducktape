//! Source-only reachability through original, ordinary static ESM requests.

use std::collections::{BTreeSet, VecDeque};

use anyhow::{Context, Result};
use artifact::reports::StaticModuleRequestMode;
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
    #[arg(long, value_enum)]
    pub(super) format: Option<OutputFormat>,
}

#[derive(Clone, Serialize)]
struct RequestEdge {
    from: String,
    specifier: String,
    resolution: ResolutionKind,
    target_file: String,
    to: String,
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
}

impl RequestIssueReason {
    fn label(self) -> &'static str {
        match self {
            Self::NonordinaryStaticRequest => "nonordinary_static_request",
            Self::UnresolvedStaticRequest => "unresolved_static_request",
            Self::MissingTargetChunk => "missing_target_chunk",
            Self::NonentryStaticRequest => "nonentry_static_request",
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
    let RequestGraph { edges, issues } = collect_requests(artifact, source.indexes())?;
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
    };
    emit_report(
        format,
        &report,
        |report, out| {
            out.push_str("Resolved ordinary static ESM requests in original source chunks.\n");
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
                                "    {} --{}--> {} ({}/{})\n",
                                edge.from,
                                edge.specifier,
                                edge.to,
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
        },
        "writing static-imports output",
    )
}

fn collect_requests(
    artifact: &ChunkBundle,
    indexes: &artifact::ArtifactIndexes,
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
                    resolution: match reference.kind {
                        ImportReferenceKind::ArtifactPath => ResolutionKind::ArtifactPath,
                        ImportReferenceKind::SourcePath => ResolutionKind::SourcePath,
                    },
                    target_file: reference.target_file,
                    to: artifact
                        .chunk_table
                        .name(reference.target_chunk_id)
                        .to_string(),
                },
            ));
        }
    }
    Ok(RequestGraph { edges, issues })
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
