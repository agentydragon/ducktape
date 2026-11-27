//! Read-only graph inventories for agents maintaining a debundle spec:
//! atomic-group and graph-summary reports and their shared arguments.

use std::collections::BTreeMap;
use std::path::PathBuf;

use anyhow::Result;
use clap::Args as ClapArgs;
use serde::Serialize;

use graph::DependencyGraphInput;
use graph::report::{AtomicGroupReport, BindingReport, DependencyGraphReport};

use crate::output_format::OutputFormat;

#[derive(Debug, Clone, ClapArgs)]
pub struct CommonArgs {
    /// Path to `dependency_graph.json.gz` (debundler analysis output).
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub dependency_graph_path: PathBuf,

    /// Root of emitted-module `*.yaml` spec files.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,
}

#[derive(Debug, Clone, ClapArgs)]
pub struct ProposeArgs {
    /// Path to the one chunk's dependency graph.
    #[arg(
        long = "graph",
        env = "DEBUNDLE_GRAPH",
        required_unless_present = "graphs_root",
        conflicts_with = "graphs_root"
    )]
    pub dependency_graph_path: Option<PathBuf>,

    /// Root containing a recursively discovered graph corpus. Requires
    /// --tree-config; discovery runs once for each configured chunk.
    #[arg(
        long = "graphs",
        required_unless_present = "dependency_graph_path",
        conflicts_with = "dependency_graph_path",
        requires = "tree_config"
    )]
    pub graphs_root: Option<PathBuf>,

    /// Root of emitted-module `*.yaml` spec files.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,

    /// Name the one new logical owner to append to the current import order.
    #[arg(long, conflicts_with = "graphs_root")]
    pub into: Vec<String>,
    /// Require these residual units to be extracted, with their full feasible
    /// successor closure.
    #[arg(long = "require", conflicts_with = "graphs_root")]
    pub required: Vec<String>,
    /// JSON array of units permitted to move. Must contain whole atomic groups.
    #[arg(long, conflicts_with = "graphs_root")]
    pub only_units: Option<PathBuf>,

    /// Root for the chunk's source files, used to preflight that the selected
    /// plan can be realized as authored selectors before it is executable.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Tree config for chunk-scoped module claims in a multi-chunk spec.
    #[arg(long = "tree-config", env = "DEBUNDLE_TREE_CONFIG")]
    pub tree_config: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, ClapArgs)]
pub struct AtomicGroupsArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    /// Maximum number of atomic groups to emit. Zero means unlimited.
    #[arg(long, default_value_t = 0)]
    pub limit: usize,

    /// Filter to atomic groups containing at least one residual source unit.
    #[arg(long = "residual-only")]
    pub residual_only: bool,

    /// Filter to atomic groups with at least one renamed export.
    #[arg(long = "readable-only")]
    pub readable_only: bool,

    /// Also group emitted atomic groups by current destination.
    #[arg(long = "by-destination")]
    pub by_destination: bool,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, ClapArgs)]
pub struct GraphSummaryArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    /// Maximum number of largest residual atomic groups to emit. Zero means unlimited.
    #[arg(long, default_value_t = 10)]
    pub limit: usize,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct AtomicGroupsReport {
    pub atomic_groups: Vec<AtomicGroupReport>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub destination_groups: Vec<DestinationGroup>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DestinationGroup {
    pub destination: String,
    pub atomic_group_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct GraphSummaryReport {
    pub source_unit_count: usize,
    pub dependency_edge_count: usize,
    pub atomic_group_count: usize,
    pub residual_atomic_group_count: usize,
    pub atomic_edge_count: usize,
    pub module_count: usize,
    pub module_edge_count: usize,
    pub largest_residual_atomic_groups: Vec<AtomicGroupSummary>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct AtomicGroupSummary {
    pub atomic_group_id: String,
    pub size_lines_estimate: usize,
    pub members: Vec<BindingReport>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct LimitReport {
    pub limit: usize,
    pub sections: BTreeMap<&'static str, LimitSectionReport>,
}

impl LimitReport {
    pub fn new(limit: usize) -> Self {
        Self {
            limit,
            sections: BTreeMap::new(),
        }
    }

    /// Truncate a section and record its original and emitted row counts.
    pub fn apply<T>(&mut self, section: &'static str, records: &mut Vec<T>) {
        let total = records.len();
        apply_limit(records, self.limit);
        let emitted = records.len();
        self.sections.insert(
            section,
            LimitSectionReport {
                total,
                emitted,
                truncated: emitted < total,
            },
        );
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct LimitSectionReport {
    pub total: usize,
    pub emitted: usize,
    pub truncated: bool,
}

pub fn run_atomic_groups_report(args: &AtomicGroupsArgs) -> Result<AtomicGroupsReport> {
    let input = DependencyGraphInput::read(&args.common.dependency_graph_path)?;
    let graph = input.report();
    let mut atomic_groups = graph.atomic_graph.nodes.clone();
    if args.residual_only {
        atomic_groups.retain(|atomic_group| {
            atomic_group
                .destinations
                .iter()
                .any(|destination| graph.is_unassigned_or_entry(destination))
        });
    }
    if args.readable_only {
        atomic_groups.retain(|atomic_group| {
            atomic_group
                .members
                .iter()
                .any(|member| member.binding != member.export_name)
        });
    }
    atomic_groups.sort_by_key(|atomic_group| {
        (
            atomic_group
                .source_line_range
                .map(|range| range[0])
                .unwrap_or(usize::MAX),
            atomic_group.id.clone(),
        )
    });
    apply_limit(&mut atomic_groups, args.limit);
    let destination_groups = if args.by_destination {
        group_atomic_groups_by_destination(graph, &atomic_groups)
    } else {
        Vec::new()
    };
    Ok(AtomicGroupsReport {
        atomic_groups,
        destination_groups,
    })
}

pub fn run_graph_summary_report(args: &GraphSummaryArgs) -> Result<GraphSummaryReport> {
    let input = DependencyGraphInput::read(&args.common.dependency_graph_path)?;
    let graph = input.report();
    let mut residual_groups: Vec<_> = graph
        .atomic_graph
        .nodes
        .iter()
        .filter(|atomic_group| {
            atomic_group
                .destinations
                .iter()
                .any(|destination| graph.is_unassigned_or_entry(destination))
        })
        .collect();
    let residual_atomic_group_count = residual_groups.len();
    residual_groups.sort_by_key(|group| (std::cmp::Reverse(group.size_lines_estimate), &group.id));
    apply_limit(&mut residual_groups, args.limit);
    let largest_residual_atomic_groups = residual_groups
        .into_iter()
        .map(|atomic_group| AtomicGroupSummary {
            atomic_group_id: atomic_group.id.clone(),
            size_lines_estimate: atomic_group.size_lines_estimate,
            members: atomic_group.members.clone(),
        })
        .collect();
    Ok(GraphSummaryReport {
        source_unit_count: graph.nodes.len(),
        dependency_edge_count: graph.edges.len(),
        atomic_group_count: graph.atomic_graph.nodes.len(),
        residual_atomic_group_count,
        atomic_edge_count: graph.atomic_graph.edges.len(),
        module_count: graph.quotient.nodes.len(),
        module_edge_count: graph.quotient.edges.len(),
        largest_residual_atomic_groups,
    })
}

fn apply_limit<T>(records: &mut Vec<T>, limit: usize) {
    if limit > 0 {
        records.truncate(limit);
    }
}

fn group_atomic_groups_by_destination(
    graph: &DependencyGraphReport,
    atomic_groups: &[AtomicGroupReport],
) -> Vec<DestinationGroup> {
    let mut groups: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for atomic_group in atomic_groups {
        for destination in &atomic_group.destinations {
            // Group by the human-facing path (the module's identity),
            // resolved once via the module table; fall back to the raw
            // key if the table lacks the entry.
            let label = graph
                .module(destination)
                .map(|entry| entry.path.to_string())
                .unwrap_or_else(|| destination.0.clone());
            groups
                .entry(label)
                .or_default()
                .push(atomic_group.id.clone());
        }
    }
    groups
        .into_iter()
        .map(|(destination, atomic_group_ids)| DestinationGroup {
            destination,
            atomic_group_ids,
        })
        .collect()
}
