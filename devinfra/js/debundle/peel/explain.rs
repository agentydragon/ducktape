//! `describe`: dependency evidence around a selected set of source units.

use analysis_model::DepKind;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use clap::ValueEnum;
use serde::Serialize;

use graph::DependencyGraphInput;
use graph::report::{
    AtomicGroupEdgeReport, AtomicGroupReport, BindingReport, DependencyEdgeReport,
    DependencyGraphReport, ProgramGraphReport, QuotientEdgeReport, SourceUnitReport,
};
use graph_provenance::VerifiedGraphSourceContext;
use spec_modules::{collect_module_files, module_path_from_file, read_module_file};
use spec_tree::ModuleTreeScope;

use crate::plan::{CommonArgs, LimitReport};
use crate::selection::{
    QueryReport, ResolveSourceUnitContext, ResolvedSelection, SelectionKind, query_report,
    resolve_source_unit_ids, source_units_for_ids,
};

#[derive(Debug, Clone)]
pub struct ExplainArgs {
    pub selection: SelectionKind,
    /// Maximum number of rows to emit per report section. Zero means unlimited.
    pub limit: usize,
    /// Which dependency-graph edge directions to include in the report.
    pub direction: ExplainDirection,
    /// Keep edges whose typed reason matches any listed kind; empty means all.
    pub reasons: Vec<DepKind>,
}

#[derive(Debug, Clone, Copy, ValueEnum, PartialEq, Eq)]
#[value(rename_all = "snake_case")]
pub enum ExplainDirection {
    Incoming,
    Outgoing,
    Both,
}

pub const DEFAULT_DESCRIBE_LIMIT: usize = 20;

#[derive(Debug, Clone, Serialize)]
pub struct ExplainReport {
    pub native_integrity_witnesses: Vec<analysis_vocabulary::report_schema::NativeMutationWitness>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_prototype_mutations:
        Vec<analysis_vocabulary::report_schema::NativePrototypeMutation>,
    pub query: QueryReport,
    pub source_unit_ids: Vec<String>,
    pub source_units: Vec<SourceUnitReport>,
    pub neighbor_source_units: Vec<SourceUnitReport>,
    pub bindings: Vec<BindingReport>,
    pub binding_homes: Vec<BindingHomeReport>,
    pub incoming_edges: Vec<DependencyEdgeReport>,
    pub outgoing_edges: Vec<DependencyEdgeReport>,
    pub edge_census: EdgeCensus,
    pub atomic_groups: Vec<AtomicGroupReport>,
    pub incoming_atomic_edges: Vec<AtomicGroupEdgeReport>,
    pub outgoing_atomic_edges: Vec<AtomicGroupEdgeReport>,
    pub quotient_edges: Vec<QuotientEdgeReport>,
    /// Bindings claimed by a module-path selection that do not
    /// appear in the dependency graph. Always empty for other selections.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unknown_binding_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub limits: Option<LimitReport>,
}

#[derive(Debug, Clone, Serialize)]
pub struct EdgeCensus {
    pub incoming: Vec<EdgeCensusRow>,
    pub outgoing: Vec<EdgeCensusRow>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct EdgeCensusRow {
    pub edge_kind: DepKind,
    pub role: EdgeCensusRole,
    pub count: usize,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq, PartialOrd, Ord, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum EdgeCensusRole {
    Direct,
    PromotedAtInit,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct BindingHomeReport {
    pub binding: String,
    pub name: String,
    pub source_kind: BindingHomeSourceKind,
    pub path: String,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum BindingHomeSourceKind {
    Module,
}

/// One command's loaded graph and shared authoring lookups. This cache never
/// survives the invocation or suppresses the caller's compatibility check.
pub struct ExplainSession {
    graph: DependencyGraphInput,
    program_graph: ProgramGraphReport,
    common: CommonArgs,
    module_files: Vec<PathBuf>,
    source_root: Option<PathBuf>,
    source_context: Option<VerifiedGraphSourceContext>,
    binding_homes: Option<Vec<BindingHomeReport>>,
}

#[derive(Clone, Copy)]
struct SourceLookup<'a> {
    root: Option<&'a Path>,
    verified: Option<&'a VerifiedGraphSourceContext>,
}

impl ExplainSession {
    pub fn load(
        mut common: CommonArgs,
        tree_config: Option<PathBuf>,
        source_root: Option<PathBuf>,
        source_context: Option<VerifiedGraphSourceContext>,
    ) -> Result<Self> {
        // `--modules` may name the exact sole chunk subtree, which graph
        // compatibility accepts as a legacy scope. The configured resolver
        // expects the full tree root, so continue from the provenance-verified
        // root when one is available.
        if tree_config.is_some()
            && let Some(modules_root) = source_context
                .as_ref()
                .and_then(VerifiedGraphSourceContext::modules_root)
        {
            common.modules_root = modules_root.to_path_buf();
        }
        let graph = DependencyGraphInput::read(&common.dependency_graph_path)?;
        let report = graph.report();
        let module_files = if let Some(config) = &tree_config {
            let scope = ModuleTreeScope::from_config(config, &report.chunk_id)?;
            let mut module_files = Vec::new();
            for root in scope.roots() {
                let root = common.modules_root.join(root);
                if root.exists() {
                    module_files.extend(collect_module_files(&root)?);
                }
            }
            module_files.sort();
            module_files
        } else {
            collect_module_files(&common.modules_root)?
        };
        Ok(Self {
            program_graph: load_program_graph(&common.dependency_graph_path, report)?,
            graph,
            common,
            module_files,
            source_root,
            source_context,
            binding_homes: None,
        })
    }

    pub fn graph(&self) -> &DependencyGraphReport {
        self.graph.report()
    }

    pub fn module_paths(&self) -> Vec<String> {
        self.module_files
            .iter()
            .map(|path| module_path_from_file(path, &self.common.modules_root))
            .collect()
    }

    pub fn explain(
        &mut self,
        args: &ExplainArgs,
        diagnostics: &analysis::diagnostics::Diagnostics,
    ) -> Result<ExplainReport> {
        if self.binding_homes.is_none() {
            self.binding_homes = Some(binding_homes(
                &self.common.modules_root,
                &self.module_files,
            )?);
        }
        explain_from_graph(self, args, diagnostics)
    }
}

fn explain_from_graph(
    session: &ExplainSession,
    args: &ExplainArgs,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<ExplainReport> {
    let ExplainSession {
        graph,
        program_graph,
        common,
        source_root,
        source_context,
        binding_homes,
        ..
    } = session;
    let report = graph.report();
    let source_lookup = SourceLookup {
        root: source_root.as_deref(),
        verified: source_context.as_ref(),
    };
    let homes = binding_homes
        .as_deref()
        .expect("loaded by ExplainSession::explain");
    let selection = &args.selection;
    let query = query_report(selection);
    let ResolvedSelection {
        source_unit_ids,
        mut unknown_binding_ids,
    } = resolve_source_unit_ids(
        selection,
        graph,
        &ResolveSourceUnitContext {
            common,
            source_root: source_lookup.root,
            source_context: source_lookup.verified,
            module_files: &session.module_files,
        },
        diagnostics,
    )?;
    let mut source_units = source_units_for_ids(report, &source_unit_ids);

    let (mut incoming_edges, mut outgoing_edges) =
        selected_dependency_edges(graph, &source_unit_ids, args)?;
    let selected_atomic_group_ids = atomic_group_ids_for_source_unit_set(report, &source_unit_ids);
    let mut atomic_groups: Vec<AtomicGroupReport> = report
        .atomic_graph
        .nodes
        .iter()
        .filter(|atomic_group| selected_atomic_group_ids.contains(&atomic_group.id))
        .cloned()
        .collect();
    let mut incoming_atomic_edges: Vec<AtomicGroupEdgeReport> = report
        .atomic_graph
        .edges
        .iter()
        .filter(|edge| selected_atomic_group_ids.contains(&edge.target))
        .cloned()
        .collect();
    let mut outgoing_atomic_edges: Vec<AtomicGroupEdgeReport> = report
        .atomic_graph
        .edges
        .iter()
        .filter(|edge| selected_atomic_group_ids.contains(&edge.source))
        .cloned()
        .collect();

    let mut bindings: Vec<BindingReport> = source_units
        .iter()
        .flat_map(|source_unit| source_unit.declared_bindings.iter().cloned())
        .collect();
    bindings.sort();
    bindings.dedup();
    let binding_ids: BTreeSet<String> = bindings
        .iter()
        .map(|binding| binding.binding.to_string())
        .collect();
    let mut binding_homes: Vec<BindingHomeReport> = homes
        .iter()
        .filter(|home| binding_ids.contains(&home.binding))
        .cloned()
        .collect();

    let selected_destinations: BTreeSet<graph::report::ModuleKey> = source_units
        .iter()
        .map(|source_unit| source_unit.destination.clone())
        .collect();
    let mut quotient_edges: Vec<QuotientEdgeReport> = report
        .quotient
        .edges
        .iter()
        .filter(|edge| {
            selected_destinations.contains(&edge.source)
                || selected_destinations.contains(&edge.target)
        })
        .cloned()
        .collect();

    let matches_reason = |kinds: &[DepKind]| {
        args.reasons.is_empty() || kinds.iter().any(|kind| args.reasons.contains(kind))
    };
    if matches!(args.direction, ExplainDirection::Outgoing) {
        incoming_edges.clear();
        incoming_atomic_edges.clear();
    }
    if matches!(args.direction, ExplainDirection::Incoming) {
        outgoing_edges.clear();
        outgoing_atomic_edges.clear();
    }
    incoming_atomic_edges.retain(|edge| matches_reason(&edge.edge_kinds));
    outgoing_atomic_edges.retain(|edge| matches_reason(&edge.edge_kinds));
    quotient_edges.retain(|edge| {
        let incoming = selected_destinations.contains(&edge.target);
        let outgoing = selected_destinations.contains(&edge.source);
        let direction_matches = match args.direction {
            ExplainDirection::Incoming => incoming,
            ExplainDirection::Outgoing => outgoing,
            ExplainDirection::Both => incoming || outgoing,
        };
        direction_matches && matches_reason(&edge.edge_kinds)
    });

    let edge_census = EdgeCensus {
        incoming: edge_census_rows(&incoming_edges),
        outgoing: edge_census_rows(&outgoing_edges),
    };

    let mut neighbor_ids = BTreeSet::new();
    for edge in &incoming_edges {
        if !source_unit_ids.contains(&edge.source) {
            neighbor_ids.insert(edge.source.clone());
        }
    }
    for edge in &outgoing_edges {
        if !source_unit_ids.contains(&edge.target) {
            neighbor_ids.insert(edge.target.clone());
        }
    }
    let mut neighbor_source_units = source_units_for_ids(report, &neighbor_ids);

    source_units.sort_by(|left, right| left.id.cmp(&right.id));
    neighbor_source_units.sort_by(|left, right| left.id.cmp(&right.id));
    bindings.sort();
    binding_homes.sort();
    incoming_edges.sort_by(|left, right| left.id.cmp(&right.id));
    outgoing_edges.sort_by(|left, right| left.id.cmp(&right.id));
    atomic_groups.sort_by(|left, right| left.id.cmp(&right.id));
    incoming_atomic_edges.sort_by(|left, right| left.id.cmp(&right.id));
    outgoing_atomic_edges.sort_by(|left, right| left.id.cmp(&right.id));
    quotient_edges.sort_by(|left, right| left.id.cmp(&right.id));
    unknown_binding_ids.sort_unstable();

    let mut preferred_source_unit_ids = BTreeSet::new();
    match selection {
        SelectionKind::SourceUnit(source_unit_id) => {
            preferred_source_unit_ids.insert(source_unit_id.clone());
        }
        SelectionKind::Binding(binding_id) => {
            move_to_front(&mut bindings, |binding| {
                binding.binding.as_str() == binding_id || binding.export_name.as_str() == binding_id
            });
            move_to_front(&mut binding_homes, |home| {
                home.binding == *binding_id || home.name == *binding_id
            });
            preferred_source_unit_ids.extend(
                source_units
                    .iter()
                    .filter(|source_unit| {
                        source_unit.declared_bindings.iter().any(|binding| {
                            binding.binding.as_str() == binding_id
                                || binding.export_name.as_str() == binding_id
                        })
                    })
                    .map(|source_unit| source_unit.id.clone()),
            );
        }
        SelectionKind::AtomicGroup(atomic_group_id) => {
            move_to_front(&mut atomic_groups, |group| group.id == *atomic_group_id);
            if let Some(group) = atomic_groups
                .iter()
                .find(|group| group.id == *atomic_group_id)
            {
                preferred_source_unit_ids.extend(group.source_unit_ids.iter().cloned());
            }
        }
        SelectionKind::ModulePath(_) | SelectionKind::Module(_) => {}
    }

    source_units.sort_by_key(|source_unit| !preferred_source_unit_ids.contains(&source_unit.id));
    let mut limited_source_unit_ids: Vec<_> = source_unit_ids.into_iter().collect();
    limited_source_unit_ids.sort_by_key(|id| !preferred_source_unit_ids.contains(id));

    let (mut native_integrity_witnesses, mut native_prototype_mutations) =
        native_integrity_context(program_graph, &source_units, &neighbor_source_units);
    let mut limits = LimitReport::new(args.limit);
    limits.apply(
        "native_integrity_witnesses",
        &mut native_integrity_witnesses,
    );
    limits.apply(
        "native_prototype_mutations",
        &mut native_prototype_mutations,
    );
    limits.apply("source_unit_ids", &mut limited_source_unit_ids);
    limits.apply("source_units", &mut source_units);
    limits.apply("neighbor_source_units", &mut neighbor_source_units);
    limits.apply("bindings", &mut bindings);
    limits.apply("binding_homes", &mut binding_homes);
    limits.apply("incoming_edges", &mut incoming_edges);
    limits.apply("outgoing_edges", &mut outgoing_edges);
    limits.apply("atomic_groups", &mut atomic_groups);
    limits.apply("incoming_atomic_edges", &mut incoming_atomic_edges);
    limits.apply("outgoing_atomic_edges", &mut outgoing_atomic_edges);
    limits.apply("quotient_edges", &mut quotient_edges);
    limits.apply("unknown_binding_ids", &mut unknown_binding_ids);

    Ok(ExplainReport {
        native_integrity_witnesses,
        native_prototype_mutations,
        query,
        source_unit_ids: limited_source_unit_ids,
        source_units,
        neighbor_source_units,
        bindings,
        binding_homes,
        incoming_edges,
        outgoing_edges,
        edge_census,
        atomic_groups,
        incoming_atomic_edges,
        outgoing_atomic_edges,
        quotient_edges,
        unknown_binding_ids,
        limits: Some(limits),
    })
}

/// Dependency edges into and out of the selected source units, filtered by
/// the requested direction and reasons.
fn selected_dependency_edges(
    graph: &DependencyGraphInput,
    source_unit_ids: &BTreeSet<String>,
    args: &ExplainArgs,
) -> Result<(Vec<DependencyEdgeReport>, Vec<DependencyEdgeReport>)> {
    let mut incoming_edges = Vec::<DependencyEdgeReport>::new();
    let mut outgoing_edges = Vec::<DependencyEdgeReport>::new();
    let direction_incoming = !matches!(args.direction, ExplainDirection::Outgoing);
    let direction_outgoing = !matches!(args.direction, ExplainDirection::Incoming);
    for source_unit_id in source_unit_ids {
        let source_unit = graph
            .source_unit_id(source_unit_id)
            .with_context(|| {
                format!(
                    "explain selection contains source unit {source_unit_id:?} absent from the checked graph input"
                )
            })?;
        if direction_incoming {
            for &edge_id in graph.graph().in_edges_of(source_unit) {
                let edge = graph.graph().edge(edge_id);
                let kind = edge.reason.kind();
                if args.reasons.is_empty() || args.reasons.contains(&kind) {
                    incoming_edges.push(graph.edge_report(edge_id).clone());
                }
            }
        }
        if direction_outgoing {
            for &edge_id in graph.graph().out_edges_of(source_unit) {
                let edge = graph.graph().edge(edge_id);
                let kind = edge.reason.kind();
                if args.reasons.is_empty() || args.reasons.contains(&kind) {
                    outgoing_edges.push(graph.edge_report(edge_id).clone());
                }
            }
        }
    }
    Ok((incoming_edges, outgoing_edges))
}

/// Native prototype mutations that touch a property the selected or neighbor
/// units check, and the integrity witnesses transitively affecting their roots.
fn native_integrity_context(
    program_graph: &ProgramGraphReport,
    source_units: &[SourceUnitReport],
    neighbor_source_units: &[SourceUnitReport],
) -> (
    Vec<analysis_vocabulary::report_schema::NativeMutationWitness>,
    Vec<analysis_vocabulary::report_schema::NativePrototypeMutation>,
) {
    let checked_properties: BTreeSet<_> = source_units
        .iter()
        .chain(neighbor_source_units)
        .filter_map(|unit| unit.synchronous_execution.as_ref())
        .flat_map(|execution| {
            execution
                .native_prototype_property_checks
                .iter()
                .map(|check| &check.property)
        })
        .collect();
    let native_prototype_mutations: Vec<_> = program_graph
        .native_prototype_mutations
        .iter()
        .filter(|mutation| {
            !checked_properties.is_empty()
                && mutation.properties.as_ref().is_none_or(|properties| {
                    properties
                        .iter()
                        .any(|property| checked_properties.contains(property))
                })
        })
        .cloned()
        .collect();
    let mut affected_roots: BTreeSet<_> = source_units
        .iter()
        .chain(neighbor_source_units)
        .filter_map(|unit| unit.synchronous_execution.as_ref())
        .flat_map(|execution| &execution.native_integrity_checks)
        .flat_map(|check| check.affected_roots.iter().copied())
        .collect();
    affected_roots.extend(
        native_prototype_mutations
            .iter()
            .flat_map(|mutation| mutation.key_dependencies.iter().copied()),
    );
    loop {
        let before = affected_roots.len();
        for witness in &program_graph.native_integrity_witnesses {
            if !witness.affected_roots.is_disjoint(&affected_roots) {
                affected_roots.extend(&witness.invalidated_key_dependencies);
            }
        }
        if before == affected_roots.len() {
            break;
        }
    }
    let native_integrity_witnesses: Vec<_> = program_graph
        .native_integrity_witnesses
        .iter()
        .filter(|witness| !witness.affected_roots.is_disjoint(&affected_roots))
        .cloned()
        .collect();
    (native_integrity_witnesses, native_prototype_mutations)
}

fn edge_census_rows(edges: &[DependencyEdgeReport]) -> Vec<EdgeCensusRow> {
    let mut counts = BTreeMap::new();
    for edge in edges {
        let role = match &edge.role {
            Some(graph::report::EdgeRoleReport::PromotedAtInit { .. }) => {
                EdgeCensusRole::PromotedAtInit
            }
            None => EdgeCensusRole::Direct,
        };
        *counts.entry((edge.edge_kind, role)).or_insert(0) += 1;
    }
    counts
        .into_iter()
        .map(|((edge_kind, role), count)| EdgeCensusRow {
            edge_kind,
            role,
            count,
        })
        .collect()
}

fn load_program_graph(
    graph_path: &Path,
    graph: &DependencyGraphReport,
) -> Result<ProgramGraphReport> {
    let path = graph_provenance::program_graph_path(graph_path, &graph.program_graph);
    report_io::read_json_gzip(&path)
}

fn move_to_front<T>(records: &mut [T], mut matches: impl FnMut(&T) -> bool) {
    if let Some(index) = records.iter().position(&mut matches) {
        records[..=index].rotate_right(1);
    }
}

fn atomic_group_ids_for_source_unit_set(
    graph: &DependencyGraphReport,
    source_unit_ids: &BTreeSet<String>,
) -> BTreeSet<String> {
    graph
        .atomic_graph
        .nodes
        .iter()
        .filter(|atomic_group| {
            atomic_group
                .source_unit_ids
                .iter()
                .any(|source_unit| source_unit_ids.contains(source_unit))
        })
        .map(|atomic_group| atomic_group.id.clone())
        .collect()
}

fn binding_homes(modules_root: &Path, module_files: &[PathBuf]) -> Result<Vec<BindingHomeReport>> {
    let mut homes = BTreeSet::new();
    for path in module_files {
        let module_path = module_path_from_file(path, modules_root);
        for member in read_module_file(path)?.members {
            let spec::member_selectors::MemberSelectorSpec::Binding(binding_selector) = member
                .selector
                .validate()
                .with_context(|| format!("member selector in {}", path.display()))?
            else {
                continue;
            };
            let binding = binding_selector.name;
            homes.insert(BindingHomeReport {
                binding,
                name: member.name.unwrap_or_default(),
                source_kind: BindingHomeSourceKind::Module,
                path: module_path.clone(),
            });
        }
    }
    Ok(homes.into_iter().collect())
}
