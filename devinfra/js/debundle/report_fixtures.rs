//! Shared test-only helpers for building dependency-graph wire fixtures.
//!
//! The dependency-graph report interns module identity: a reference is a
//! [`ModuleKey`] (`"logical:N"`) and each module's canonical
//! [`spec::ModulePath`] + destination kind live once in the module table
//! (`ModuleQuotientReport::nodes`, a list of [`ModuleEntry`]). Tests
//! across several crates build that table — and the owner/edge/atomic-group
//! report nodes around it — the same way, so the builders live here
//! instead of being copy-pasted per test module.
//!
//! Convention: a test destination key's string **is** the module's
//! canonical path (e.g. `"residual"`, `"ui/plugins"`), so [`module_table`]
//! recovers the path by parsing the key. The `"residual"` test sentinel
//! represents the physical entry destination; other keys represent specified
//! planned outputs.

use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};

use analysis::{
    AtomicGraphReport, AtomicGroupEdgeReport, AtomicGroupReport, BindingReport, DepKind,
    DependencyEdgeReport, DependencyGraphReport, EdgeRoleReport, LineRange, ModuleDestinationKind,
    ModuleEntry, ModuleKey, ModuleQuotientReport, Purity, SourceLocation, SourceUnitReport,
    StatementKind,
};
use spec::ModulePath;

/// A destination key whose string is the module's canonical path.
pub fn module_ref(path: &str) -> ModuleKey {
    ModuleKey(path.to_string())
}

/// The module-table entry for a single destination key: the canonical
/// `ModulePath` (parsed from the key) and destination kind.
pub fn module_entry(key: &ModuleKey) -> ModuleEntry {
    let path = ModulePath::parse(key.as_str(), "").expect("test module key is a valid path");
    let kind = if path.as_str() == "residual" {
        ModuleDestinationKind::Entry
    } else {
        ModuleDestinationKind::Specified
    };
    ModuleEntry {
        key: key.clone(),
        path,
        kind,
    }
}

/// Build the module table (`quotient.nodes`) from a set of destination
/// keys, deduplicating. The single source of truth for each module's
/// path + destination kind in a test fixture.
pub fn module_table<'a>(keys: impl IntoIterator<Item = &'a ModuleKey>) -> Vec<ModuleEntry> {
    let mut seen = BTreeSet::new();
    keys.into_iter()
        .filter(|key| seen.insert((*key).clone()))
        .map(module_entry)
        .collect()
}

/// A declared binding whose readable export name differs from the
/// minified binding id.
pub fn member(binding: &str, export_name: &str) -> BindingReport {
    BindingReport {
        binding: binding.into(),
        export_name: export_name.into(),
    }
}

/// A declared binding whose export name equals the binding id.
pub fn binding(name: &str) -> BindingReport {
    member(name, name)
}

/// Active-claims map (binding name → canonical module path) from clean
/// spec paths.
pub fn claims(pairs: &[(&str, &str)]) -> BTreeMap<String, ModulePath> {
    pairs
        .iter()
        .map(|(binding, path)| (binding.to_string(), ModulePath::parse(path, "").unwrap()))
        .collect()
}

/// SourceUnit node with an explicit destination. The source location is
/// synthesized from the ordinal (`start_line = ordinal * 100`) so
/// distinct owners never overlap. Prefer [`residual_source_unit`] /
/// [`active_source_unit`] when the destination is one of the common cases —
/// this 5-arg form is the only `owner` builder, so call sites always
/// spell the destination decision.
pub fn source_unit(
    id: &str,
    ordinal: usize,
    bindings: &[&str],
    lines: usize,
    destination: ModuleKey,
) -> SourceUnitReport {
    source_unit_report(
        id,
        ordinal,
        bindings.iter().map(|b| binding(b)).collect(),
        Some(SourceLocation {
            source_path: "x.js".to_string(),
            start_line: ordinal * 100,
            end_line: ordinal * 100 + lines.saturating_sub(1),
            start_column: None,
        }),
        destination,
    )
}

/// Fully-explicit owner-node core: callers control the declared
/// bindings (e.g. renamed exports via [`member`]) and the exact
/// source location.
pub fn source_unit_report(
    id: &str,
    ordinal: usize,
    declared_bindings: Vec<BindingReport>,
    source_location: Option<SourceLocation>,
    destination: ModuleKey,
) -> SourceUnitReport {
    SourceUnitReport {
        id: id.to_string(),
        statement_ordinal: SourceUnitOrdinal(ordinal),
        source_location,
        declared_bindings,
        statement_kind: StatementKind::VarDecl,
        purity: Purity::Pure,
        destination,
    }
}

/// [`owner`] destined for the residual catch-all.
pub fn residual_source_unit(
    id: &str,
    ordinal: usize,
    bindings: &[&str],
    lines: usize,
) -> SourceUnitReport {
    source_unit(id, ordinal, bindings, lines, module_ref("residual"))
}

/// [`owner`] destined for an active (spec-claimed) module.
pub fn active_source_unit(
    id: &str,
    ordinal: usize,
    bindings: &[&str],
    lines: usize,
    module_path: &str,
) -> SourceUnitReport {
    source_unit(id, ordinal, bindings, lines, module_ref(module_path))
}

/// SourceUnit-level edge with no binding label.
pub fn dependency_edge(
    id: &str,
    source: &str,
    target: &str,
    kind: DepKind,
    constrains: bool,
) -> DependencyEdgeReport {
    DependencyEdgeReport {
        id: id.to_string(),
        source: source.to_string(),
        target: target.to_string(),
        edge_kind: kind,
        binding: None,
        statement_ordinal: SourceUnitOrdinal(0),
        constrains_init_order: constrains,
        role: None,
    }
}

/// [`dependency_edge`] promoted at init from an at-init call into
/// `callee`'s body; `callee == source` is the conservative fallback
/// for a call the analysis cannot resolve.
pub fn promoted_dependency_edge(
    id: &str,
    source: &str,
    target: &str,
    kind: DepKind,
    constrains: bool,
    callee: &str,
) -> DependencyEdgeReport {
    DependencyEdgeReport {
        role: Some(EdgeRoleReport::PromotedAtInit {
            callee_source_unit: callee.to_string(),
        }),
        ..dependency_edge(id, source, target, kind, constrains)
    }
}

/// Atomic group aggregating the given owners (ids, members,
/// destinations, line range, ordinal span).
pub fn atomic_group_for(id: &str, source_units: &[&SourceUnitReport]) -> AtomicGroupReport {
    let mut source_unit_ids = Vec::new();
    let mut members = Vec::new();
    let mut destinations = BTreeMap::<ModuleKey, ModuleKey>::new();
    let mut line_range = LineRange::new();
    let mut min_ordinal = usize::MAX;
    let mut max_ordinal = 0usize;
    for source_unit in source_units {
        source_unit_ids.push(source_unit.id.clone());
        members.extend(source_unit.declared_bindings.clone());
        destinations.insert(
            source_unit.destination.clone(),
            source_unit.destination.clone(),
        );
        if let Some(location) = &source_unit.source_location {
            line_range.expand(location);
        }
        min_ordinal = min_ordinal.min(source_unit.statement_ordinal.0);
        max_ordinal = max_ordinal.max(source_unit.statement_ordinal.0);
    }
    AtomicGroupReport {
        id: id.to_string(),
        source_unit_ids,
        members,
        anonymous_statement_source_unit_ids: Vec::new(),
        destinations: destinations.into_values().collect(),
        causes: Vec::new(),
        size_lines_estimate: line_range.size_estimate(),
        source_line_range: line_range.into_array(),
        ordinal_span: max_ordinal.saturating_sub(min_ordinal),
    }
}

/// Constraining eager atomic-group edge. The underlying dependency-edge id
/// is derived from `id` by substituting `atomic` → `edge`; use
/// [`atomic_edge_for_dependency_edge`] to spell it explicitly.
pub fn atomic_edge(id: &str, source: &str, target: &str) -> AtomicGroupEdgeReport {
    atomic_edge_for_dependency_edge(id, source, target, &id.replace("atomic", "edge"))
}

/// [`atomic_edge`] with the underlying dependency-edge id spelled
/// explicitly.
pub fn atomic_edge_for_dependency_edge(
    id: &str,
    source: &str,
    target: &str,
    dependency_edge_id: &str,
) -> AtomicGroupEdgeReport {
    AtomicGroupEdgeReport {
        id: id.to_string(),
        source: source.to_string(),
        target: target.to_string(),
        edge_kinds: vec![DepKind::EagerUse],
        dependency_edge_ids: vec![dependency_edge_id.to_string()],
        constrains_init_order: true,
    }
}

/// Assemble a full `DependencyGraphReport` wire fixture. The module table
/// is derived from the distinct owner destinations.
pub fn graph_of(
    nodes: Vec<SourceUnitReport>,
    edges: Vec<DependencyEdgeReport>,
    atomic_groups: Vec<AtomicGroupReport>,
    atomic_group_edges: Vec<AtomicGroupEdgeReport>,
) -> DependencyGraphReport {
    let module_nodes = module_table(nodes.iter().map(|n| &n.destination));
    DependencyGraphReport {
        chunk_id: "x".to_string(),
        nodes,
        purity_causes: Vec::new(),
        edges,
        quotient: ModuleQuotientReport {
            nodes: module_nodes,
            edges: vec![],
            sccs: vec![],
        },
        atomic_graph: AtomicGraphReport {
            nodes: atomic_groups,
            edges: atomic_group_edges,
        },
    }
}

/// An dependency graph whose atomic groups are one-per-owner, in owner order.
/// Atomic edges stay empty: tests exercising that distinct graph provide it explicitly.
pub fn singleton_graph(
    source_units: Vec<SourceUnitReport>,
    edges: Vec<DependencyEdgeReport>,
) -> DependencyGraphReport {
    let atomic_groups = source_units
        .iter()
        .enumerate()
        .map(|(i, source_unit)| atomic_group_for(&format!("atomic:{i}"), &[source_unit]))
        .collect();
    graph_of(source_units, edges, atomic_groups, vec![])
}
