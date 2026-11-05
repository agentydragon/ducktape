//! Assignment assembly: produces the authoritative per-source-unit
//! [`ModuleAssignment`] from [`crate::atomic_groups`]' SCCs + the spec's YAML
//! claims. When two or more distinct claims fall inside one atomic
//! group the spec is unrealizable; we surface an
//! [`AtomicGroupConflict`] (members + each conflicting claim + the
//! `DepKind` causes from the group) so the materializer can reject
//! before emit. This pass deliberately does *not* extend a single-
//! member claim to cover the rest of the group — that promotion is
//! an authoring decision surfaced by `debundle coverage`.
//! See `docs/design.md` §"Assignment assembly inside `debundle run`".

use analysis_model::DepKind;
use std::collections::{BTreeSet, HashMap};

use crate::atomic_groups::AtomicGroup;
use crate::graph::DependencyGraph;
use crate::ids::{BindingKind, EntryDestination, LocalBindings, ModuleId, PlannedModule};
use crate::module_assignment::ModuleAssignment;
use serde::Serialize;
use source_identity::SourceUnitId;
use swc_atoms::Atom;

/// One source unit's claimed destination inside a conflicting atomic
/// group. Listed inside an [`AtomicGroupConflict`].
#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct ConflictingClaim {
    pub source_unit: SourceUnitId,
    pub binding_names: Vec<Atom>,
    pub module: ModuleId,
}

/// An atomic factor group whose members the spec routes to two or
/// more distinct destinations — unrealizable by construction.
///
/// Atom-level (binding members claimed across distinct destinations),
/// not SCC-level — the atom-level sibling of the module-quotient-SCC
/// shape [`crate::reports::SccCore`]. Both carry the "unrealizable,
/// here's why" framing, but this one's domain is source units in an atomic
/// group rather than modules in an SCC, so it does not reuse that core
/// and stays a separate type.
#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct AtomicGroupConflict {
    /// Members sorted by `SourceUnitId`.
    pub members: Vec<SourceUnitId>,
    pub claims: Vec<ConflictingClaim>,
    /// Constraining-edge kinds inside the group (mirrors
    /// [`AtomicGroup::causes`]). Lets the materializer's diagnostic
    /// tell the author what kind of edge forced co-location (eager
    /// cycle, rebind, sequenced side-effect chain). `BTreeSet` so
    /// iteration is `DepKind`-`Ord`-stable for downstream rendering.
    pub causes: BTreeSet<DepKind>,
}

#[derive(Debug, Clone)]
pub struct AssemblyOutcome {
    pub assignment: ModuleAssignment,
    pub conflicts: Vec<AtomicGroupConflict>,
}

/// Build the assignment + any atomic-group claim conflicts. The assignment is
/// best-effort when conflicts exist (first-seen claim wins per atomic group)
/// so downstream dependency graph/report code keeps working — the
/// conflict list is what surfaces the rejection to the user.
///
/// `entry_destination` is where unclaimed source units fall back (and the
/// conflict detector reports as the effective destination of unclaimed
/// atomic-group members).
pub fn assemble_assignment(
    dependency_graph: &DependencyGraph,
    atomic_groups: &[AtomicGroup],
    bindings: &LocalBindings<'_>,
    planned_modules: &[PlannedModule],
    entry_destination: EntryDestination,
) -> AssemblyOutcome {
    let mut claims = compute_source_unit_binding_claims(dependency_graph, bindings.as_map());
    for (idx, module) in planned_modules.iter().enumerate() {
        let dest = ModuleId(idx);
        for ordinal in &module.anonymous_source_unit_ordinals {
            if let Some(node) = dependency_graph
                .iter_nodes()
                .find(|n| n.statement_ordinal == *ordinal)
            {
                claims[node.id.0] = Some(dest);
            }
        }
    }
    assemble_assignment_from_claims(dependency_graph, atomic_groups, claims, entry_destination)
}

/// Assemble an assignment from the borrowed binding catalogue plus explicit
/// source-unit destinations. This is the shared conversion seam for callers
/// that have already projected whole-statement ownership to checked graph IDs,
/// such as residual routing. The graph and atomic groups stay borrowed.
pub fn assemble_assignment_with_source_unit_claims(
    dependency_graph: &DependencyGraph,
    atomic_groups: &[AtomicGroup],
    bindings: &HashMap<swc_ecma_ast::Id, BindingKind>,
    source_unit_claims: &[(SourceUnitId, ModuleId)],
    entry_destination: EntryDestination,
) -> anyhow::Result<AssemblyOutcome> {
    let mut claims = compute_source_unit_binding_claims(dependency_graph, bindings);
    for (source_unit, destination) in source_unit_claims {
        anyhow::ensure!(
            source_unit.0 < claims.len(),
            "source-unit claim {} is outside the dependency graph with {} nodes",
            source_unit.0,
            claims.len()
        );
        claims[source_unit.0] = Some(*destination);
    }
    Ok(assemble_assignment_from_claims(
        dependency_graph,
        atomic_groups,
        claims,
        entry_destination,
    ))
}

fn assemble_assignment_from_claims(
    dependency_graph: &DependencyGraph,
    atomic_groups: &[AtomicGroup],
    claims: Vec<Option<ModuleId>>,
    entry_destination: EntryDestination,
) -> AssemblyOutcome {
    let entry_module_id = entry_destination.module_id();
    let mut assignment = ModuleAssignment::new(dependency_graph, entry_destination);
    for (idx, claim) in claims.iter().enumerate() {
        if let Some(dest) = claim {
            assignment.set(SourceUnitId(idx), *dest);
        }
    }
    let mut conflicts = Vec::<AtomicGroupConflict>::new();
    for atomic_group in atomic_groups {
        if let Some(conflict) = detect_atomic_group_conflict(
            atomic_group,
            |source_unit| claims[source_unit.0].unwrap_or(entry_module_id),
            |source_unit| {
                dependency_graph
                    .node(source_unit)
                    .map(|node| node.declared.iter().map(|id| id.0.clone()).collect())
                    .unwrap_or_default()
            },
        ) {
            conflicts.push(conflict);
        }
    }
    AssemblyOutcome {
        assignment,
        conflicts,
    }
}

/// One [`ModuleId`] slot per source unit; `None` means no explicit claim.
fn compute_source_unit_binding_claims(
    dependency_graph: &DependencyGraph,
    bindings: &HashMap<swc_ecma_ast::Id, BindingKind>,
) -> Vec<Option<ModuleId>> {
    let mut claims = vec![None; dependency_graph.num_nodes()];
    for node in dependency_graph.iter_nodes() {
        for binding_id in &node.declared {
            let claim = bindings.get(binding_id).and_then(|kind| match kind {
                BindingKind::Owned { module: dest } => Some(*dest),
                _ => None,
            });
            if let Some(dest) = claim {
                claims[node.id.0] = Some(dest);
                break;
            }
        }
    }
    claims
}

/// Detect a split atomic group from its effective destinations and build the
/// common conflict model. Callers retain ownership of destination resolution
/// and binding labels, which come from different sources in the materializer
/// and graph-backed edit gate.
pub fn detect_atomic_group_conflict(
    atomic_group: &AtomicGroup,
    mut destination: impl FnMut(SourceUnitId) -> ModuleId,
    mut binding_names: impl FnMut(SourceUnitId) -> Vec<Atom>,
) -> Option<AtomicGroupConflict> {
    let resolved: Vec<(SourceUnitId, ModuleId)> = atomic_group
        .members
        .iter()
        .map(|source_unit| (*source_unit, destination(*source_unit)))
        .collect();

    let (_, first_destination) = resolved.first()?;
    if resolved
        .iter()
        .all(|(_, module)| module == first_destination)
    {
        return None;
    }

    let claims: Vec<ConflictingClaim> = resolved
        .into_iter()
        .map(|(source_unit, module)| ConflictingClaim {
            source_unit,
            binding_names: binding_names(source_unit),
            module,
        })
        .collect();

    Some(AtomicGroupConflict {
        members: atomic_group.members.iter().copied().collect(),
        claims,
        causes: atomic_group.causes.clone(),
    })
}
