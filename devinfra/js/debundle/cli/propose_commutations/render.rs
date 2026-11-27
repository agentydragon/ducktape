//! Text and YAML-draft rendering of a `propose-commutations` report.

use crate::propose_commutations::report::{
    CommutationProposal, EndpointSelectorFailure, ProposeCommutationsReport, ProposedEndpoint,
    RejectionReason, SccVerdict,
};
use anyhow::{Context, Result};

fn endpoint_failure_text(failure: &EndpointSelectorFailure) -> String {
    match failure {
        EndpointSelectorFailure::OutsideParsedSource {
            source_unit,
            statement_ordinal,
        } => format!(
            "source unit {source_unit} ordinal {statement_ordinal} is outside the parsed source; regenerate the dependency graph"
        ),
        EndpointSelectorFailure::WrongBodyIndex { actual, expected } => format!(
            "the synthesized selector proves body index {actual} but the source unit is body index {expected:?}"
        ),
        EndpointSelectorFailure::Synthesis { reason } => reason.explanation().to_string(),
    }
}

fn location_text(endpoint: &ProposedEndpoint) -> String {
    let location = endpoint.source_location.as_ref().map_or_else(
        || "<location unavailable>".to_owned(),
        |location| {
            format!(
                "{}:{}:{}",
                location.source_path,
                location.start_line,
                location
                    .start_column
                    .map_or_else(|| "?".to_owned(), |column| column.to_string())
            )
        },
    );
    let binding = endpoint
        .binding
        .as_ref()
        .map_or_else(|| "anonymous statement".to_owned(), |b| format!("`{b}`"));
    format!(
        "{} {binding} in {} at {location}{}",
        endpoint.source_unit,
        endpoint.module,
        endpoint
            .excerpt
            .as_deref()
            .map(|excerpt| format!(" — {excerpt}"))
            .unwrap_or_default(),
    )
}

/// The `chunk_analysis_options` fragment: one `commuting_statements` entry
/// per proposal under the graph's chunk, each preceded by comments carrying
/// the report's explanation so the draft can be reviewed on its own.
pub(super) fn render_draft(chunk: &str, proposals: &[CommutationProposal]) -> Result<String> {
    const ITEM_INDENT: &str = "      ";
    let chunk_key = serde_yaml::to_string(chunk)?;
    let mut out = format!(
        "chunk_analysis_options:\n  {}:\n    commuting_statements:\n",
        chunk_key.trim_end()
    );
    if proposals.is_empty() {
        out.push_str(&format!("{ITEM_INDENT}[]\n"));
        return Ok(out);
    }
    for proposal in proposals {
        let mut comments = vec![format!(
            "rank {} ({}) in SCC {}",
            proposal.rank,
            proposal.rank_reason.explanation(),
            proposal.scc_id,
        )];
        for edge in &proposal.inerted_edges {
            comments.push(format!(
                "inerts {} {} -> {}: {}",
                edge.edge, edge.from, edge.to, edge.explanation_text
            ));
        }
        comments.push(format!("a: {}", location_text(&proposal.a)));
        comments.push(format!("b: {}", location_text(&proposal.b)));
        for comment in comments {
            for line in comment.lines() {
                out.push_str(&format!("{ITEM_INDENT}# {line}\n"));
            }
        }
        let mut entry = serde_yaml::to_value(&proposal.entry)?;
        entry
            .as_mapping_mut()
            .context("a commuting_statements entry serializes as a mapping")?
            .insert(
                serde_yaml::Value::String("note".to_owned()),
                serde_yaml::Value::Null,
            );
        let rendered = serde_yaml::to_string(&vec![entry])?;
        for line in rendered.lines() {
            out.push_str(ITEM_INDENT);
            out.push_str(line);
            out.push('\n');
        }
    }
    Ok(out)
}

pub(super) fn render_text(report: &ProposeCommutationsReport, out: &mut String) -> Result<()> {
    out.push_str(&format!(
        "chunk {}: {} of {} proposal(s), {} of {} cut edge(s) needing co-location, {} of {} rejected candidate(s) shown\n",
        report.chunk,
        report.proposals.len(),
        report.totals.proposals,
        report.needs_co_location.len(),
        report.totals.needs_co_location,
        report.rejected.len(),
        report.totals.rejected,
    ));
    for scc in &report.sccs {
        if scc.unproven_endpoints > 0 {
            out.push_str(&format!(
                "  SCC {}: assuming {} unproven declared-binding endpoint selector(s); --all proves them\n",
                scc.scc_id, scc.unproven_endpoints
            ));
        }
        match &scc.verdict {
            SccVerdict::PairsBreak => out.push_str(&format!(
                "  SCC {} ({} modules): its proposals together break it\n",
                scc.scc_id,
                scc.modules.len()
            )),
            SccVerdict::PairsCannotBreak {
                counts_are_lower_bounds,
                residual_components,
                residual_modules,
                blocking_edges,
            } => {
                out.push_str(&format!(
                    "  SCC {} ({} modules): pairs alone cannot break it; with every proposal inerted, cycles over {}{residual_modules} modules in {} component(s) remain, held by:\n",
                    scc.scc_id,
                    scc.modules.len(),
                    if *counts_are_lower_bounds { "at least " } else { "" },
                    residual_components.len(),
                ));
                if *counts_are_lower_bounds {
                    out.push_str("    Counts and cyclic memberships are lower bounds; preservation stopped after proving a cycle.\n");
                }
                for (index, component) in residual_components.iter().enumerate() {
                    let modules = component
                        .modules
                        .iter()
                        .map(ToString::to_string)
                        .collect::<Vec<_>>()
                        .join(", ");
                    out.push_str(&format!(
                        "    residual component {} ({} modules): {modules}\n",
                        index + 1,
                        component.modules.len(),
                    ));
                }
                for row in blocking_edges {
                    out.push_str(&format!(
                        "    {} {} edge(s): {}\n",
                        row.count,
                        row.kind,
                        row.reason.explanation()
                    ));
                }
            }
        }
    }
    for proposal in &report.proposals {
        out.push_str(&format!(
            "  #{} SCC {} [{}] remaining module-pair edges: {}\n    a: {}\n    b: {}\n",
            proposal.rank,
            proposal.scc_id,
            proposal.rank_reason.explanation(),
            proposal.remaining_module_pair_edges,
            location_text(&proposal.a),
            location_text(&proposal.b),
        ));
        for edge in &proposal.inerted_edges {
            out.push_str(&format!(
                "    inerts {} {} -> {}: {}\n",
                edge.edge, edge.from, edge.to, edge.explanation_text
            ));
        }
    }
    for need in &report.needs_co_location {
        out.push_str(&format!(
            "  needs co-location: SCC {} cut[{}] {} --{}--> {}\n",
            need.scc_id,
            need.cut_index,
            need.edge.from,
            need.edge.kind.diagnostic_label(),
            need.edge.to,
        ));
    }
    for rejected in &report.rejected {
        out.push_str(&format!(
            "  rejected: SCC {} cut{:?} {} / {}: {}\n",
            rejected.scc_id,
            rejected.cut_edges,
            rejected.a.as_deref().unwrap_or("?"),
            rejected.b.as_deref().unwrap_or("?"),
            rejection_text(&rejected.reason),
        ));
    }
    if let Some(path) = &report.draft_written {
        out.push_str(&format!("draft written to {}\n", path.display()));
    }
    out.push_str("---\n");
    out.push_str(&report.draft_yaml);
    Ok(())
}

fn rejection_text(reason: &RejectionReason) -> String {
    match reason {
        RejectionReason::KnownConflict { conflicts } => format!(
            "contradicts a source fact: {}",
            conflicts
                .iter()
                .map(|c| format!("{} -> {}: {}", c.from, c.to, c.conflict))
                .collect::<Vec<_>>()
                .join("; ")
        ),
        RejectionReason::UnusedSummarized => {
            "both endpoints are summarized; the premise would inert nothing".to_owned()
        }
        RejectionReason::VerdictNotRecorded { edge } => {
            format!("{edge} has no retained premise verdict; regenerate the graph")
        }
        RejectionReason::NoUniqueSelector { endpoint, detail } => {
            format!(
                "no unique selector for {endpoint}: {}",
                endpoint_failure_text(detail)
            )
        }
        RejectionReason::CutEdgeNotInGraph => {
            "the cut edge matches no graph edge; regenerate both reports".to_owned()
        }
    }
}
