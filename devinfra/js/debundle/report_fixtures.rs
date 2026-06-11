//! Shared test-only helpers for building owner-graph wire fixtures.
//!
//! The owner-graph report interns module identity: a reference is a
//! [`ModuleKey`] (`"logical:N"`) and each module's canonical
//! [`spec::ModulePath`] + residual flag live once in the module table
//! (`OwnerGraphQuotientReport::nodes`, a list of [`ModuleEntry`]). Tests
//! across several crates build that table — and the owner/edge/atomic-unit
//! report nodes around it — the same way, so the builders live here
//! instead of being copy-pasted per test module.
//!
//! Convention: a test destination key's string **is** the module's
//! canonical path (e.g. `"residual"`, `"ui/plugins"`), so [`module_table`]
//! recovers the path by parsing the key and derives `residual` from
//! [`spec::ModulePath::is_residual`].

use std::collections::{BTreeMap, BTreeSet};

use analysis::{
    AtomicGraphReport, AtomicUnitEdgeReport, AtomicUnitReport, BindingReport, DepKind, LineRange,
    ModuleEntry, ModuleKey, OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphQuotientReport,
    OwnerGraphReport, Purity, SourceLocation, StatementKind, StatementOrdinal,
};
use spec::ModulePath;
use swc_atoms::Atom;

/// A destination key whose string is the module's canonical path.
pub fn module_ref(path: &str) -> ModuleKey {
    ModuleKey(path.to_string())
}

/// The module-table entry for a single destination key: the canonical
/// `ModulePath` (parsed from the key) and its residual flag.
pub fn module_entry(key: &ModuleKey) -> ModuleEntry {
    let path = ModulePath::parse(key.as_str(), "").expect("test module key is a valid path");
    let residual = path.is_residual();
    ModuleEntry {
        key: key.clone(),
        path,
        residual,
    }
}

/// Build the module table (`quotient.nodes`) from a set of destination
/// keys, deduplicating. The single source of truth for each module's
/// path + residual flag in a test fixture.
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
/// spec paths. [`no_claims`] is the empty case.
pub fn claims(pairs: &[(&str, &str)]) -> BTreeMap<String, ModulePath> {
    pairs
        .iter()
        .map(|(binding, path)| (binding.to_string(), ModulePath::parse(path, "").unwrap()))
        .collect()
}

pub fn no_claims() -> BTreeMap<String, ModulePath> {
    BTreeMap::new()
}

/// Owner node with an explicit destination. The source location is
/// synthesized from the ordinal (`start_line = ordinal * 100`) so
/// distinct owners never overlap. Prefer [`residual_owner`] /
/// [`active_owner`] when the destination is one of the common cases —
/// this 5-arg form is the only `owner` builder, so call sites always
/// spell the destination decision.
pub fn owner(
    id: &str,
    ordinal: usize,
    bindings: &[&str],
    lines: usize,
    destination: ModuleKey,
) -> OwnerGraphNodeReport {
    owner_node(
        id,
        ordinal,
        bindings.iter().map(|b| binding(b)).collect(),
        Some(SourceLocation {
            source_path: "x.js".to_string(),
            start_line: ordinal * 100,
            end_line: ordinal * 100 + lines.saturating_sub(1),
        }),
        destination,
    )
}

/// Fully-explicit owner-node core: callers control the declared
/// bindings (e.g. renamed exports via [`member`]) and the exact
/// source location.
pub fn owner_node(
    id: &str,
    ordinal: usize,
    declared_bindings: Vec<BindingReport>,
    source_location: Option<SourceLocation>,
    destination: ModuleKey,
) -> OwnerGraphNodeReport {
    OwnerGraphNodeReport {
        id: id.to_string(),
        statement_ordinal: StatementOrdinal(ordinal),
        source_location,
        declared_bindings,
        statement_kind: StatementKind::VarDecl,
        purity: Purity::Pure,
        destination,
    }
}

/// [`owner`] destined for the residual catch-all.
pub fn residual_owner(
    id: &str,
    ordinal: usize,
    bindings: &[&str],
    lines: usize,
) -> OwnerGraphNodeReport {
    owner(id, ordinal, bindings, lines, module_ref("residual"))
}

/// [`owner`] destined for an active (spec-claimed) module.
pub fn active_owner(
    id: &str,
    ordinal: usize,
    bindings: &[&str],
    lines: usize,
    module_path: &str,
) -> OwnerGraphNodeReport {
    owner(id, ordinal, bindings, lines, module_ref(module_path))
}

/// Owner-level edge with no binding label.
pub fn owner_edge(
    id: &str,
    source: &str,
    target: &str,
    kind: DepKind,
    constrains: bool,
) -> OwnerGraphEdgeReport {
    owner_edge_for_binding(id, source, target, kind, constrains, None)
}

/// Owner-level edge labeled with the binding the source reads.
pub fn owner_edge_for_binding(
    id: &str,
    source: &str,
    target: &str,
    kind: DepKind,
    constrains: bool,
    binding: Option<&str>,
) -> OwnerGraphEdgeReport {
    OwnerGraphEdgeReport {
        id: id.to_string(),
        source: source.to_string(),
        target: target.to_string(),
        edge_kind: kind,
        binding: binding.map(Atom::from),
        statement_ordinal: StatementOrdinal(0),
        constrains_init_order: constrains,
        role: None,
    }
}

/// Atomic unit aggregating the given owners (ids, members,
/// destinations, line range, ordinal span).
pub fn atomic_unit_for(id: &str, owners: &[&OwnerGraphNodeReport]) -> AtomicUnitReport {
    let mut owner_ids = Vec::new();
    let mut members = Vec::new();
    let mut destinations = BTreeMap::<ModuleKey, ModuleKey>::new();
    let mut line_range = LineRange::new();
    let mut min_ordinal = usize::MAX;
    let mut max_ordinal = 0usize;
    for owner in owners {
        owner_ids.push(owner.id.clone());
        members.extend(owner.declared_bindings.clone());
        destinations.insert(owner.destination.clone(), owner.destination.clone());
        if let Some(location) = &owner.source_location {
            line_range.expand(location);
        }
        min_ordinal = min_ordinal.min(owner.statement_ordinal.0);
        max_ordinal = max_ordinal.max(owner.statement_ordinal.0);
    }
    AtomicUnitReport {
        id: id.to_string(),
        owner_ids,
        members,
        anonymous_statement_owner_ids: Vec::new(),
        destinations: destinations.into_values().collect(),
        causes: Vec::new(),
        size_lines_estimate: line_range.size_estimate(),
        source_line_range: line_range.into_array(),
        ordinal_span: max_ordinal.saturating_sub(min_ordinal),
    }
}

/// Constraining eager atomic-unit edge. The underlying owner-edge id
/// is derived from `id` by substituting `atomic` → `edge`; use
/// [`atomic_edge_for_owner_edge`] to spell it explicitly.
pub fn atomic_edge(id: &str, source: &str, target: &str) -> AtomicUnitEdgeReport {
    atomic_edge_for_owner_edge(id, source, target, &id.replace("atomic", "edge"))
}

/// [`atomic_edge`] with the underlying owner-edge id spelled
/// explicitly.
pub fn atomic_edge_for_owner_edge(
    id: &str,
    source: &str,
    target: &str,
    owner_edge_id: &str,
) -> AtomicUnitEdgeReport {
    AtomicUnitEdgeReport {
        id: id.to_string(),
        source: source.to_string(),
        target: target.to_string(),
        edge_kinds: vec![DepKind::EagerUse],
        owner_edge_ids: vec![owner_edge_id.to_string()],
        constrains_init_order: true,
    }
}

/// Assemble a full `OwnerGraphReport` wire fixture. The module table
/// is derived from the distinct owner destinations.
pub fn graph_of(
    nodes: Vec<OwnerGraphNodeReport>,
    edges: Vec<OwnerGraphEdgeReport>,
    units: Vec<AtomicUnitReport>,
    unit_edges: Vec<AtomicUnitEdgeReport>,
) -> OwnerGraphReport {
    let module_nodes = module_table(nodes.iter().map(|n| &n.destination));
    OwnerGraphReport {
        chunk_id: "x".to_string(),
        nodes,
        edges,
        quotient: OwnerGraphQuotientReport {
            nodes: module_nodes,
            edges: vec![],
            sccs: vec![],
        },
        atomic_graph: AtomicGraphReport {
            nodes: units,
            edges: unit_edges,
        },
    }
}
