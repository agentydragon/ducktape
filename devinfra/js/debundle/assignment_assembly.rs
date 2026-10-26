//! Assignment assembly: produces the authoritative per-owner
//! [`ModuleAssignment`] from [`crate::atomic_groups`]' SCCs + the spec's YAML
//! claims. When two or more distinct claims fall inside one atomic
//! group the spec is unrealizable; we surface an
//! [`AtomicGroupConflict`] (members + each conflicting claim + the
//! `DepKind` causes from the group) so the materializer can reject
//! before emit. This pass deliberately does *not* extend a single-
//! member claim to cover the rest of the group — that promotion is
//! an authoring decision surfaced by `debundle coverage`.
//! See `docs/design.md` §"Assignment assembly inside `debundle run`".

use std::collections::{BTreeSet, HashMap};

use serde::Serialize;
use swc_atoms::Atom;
use swc_ecma_ast::Id;

use crate::atomic_groups::AtomicGroup;
use crate::graph::{DepKind, DependencyGraph, SourceUnitId};
use crate::ids::{BindingKind, ModuleId, PlannedModule};
use crate::module_assignment::ModuleAssignment;

/// One owner's claimed destination inside a conflicting atomic
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
/// here's why" framing, but this one's domain is owners in an atomic
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
/// so downstream dependency-graph/report code keeps working — the
/// conflict list is what surfaces the rejection to the user.
///
/// `default_destination` is the residual logical module id that
/// unclaimed owners fall back to (and the conflict detector reports as
/// the effective destination of unclaimed atomic-group members).
pub fn assemble_assignment(
    dependency_graph: &DependencyGraph,
    atomic_groups: &[AtomicGroup],
    bindings: &HashMap<Id, BindingKind>,
    logical_modules: &[PlannedModule],
    default_destination: ModuleId,
) -> AssemblyOutcome {
    let claims = compute_source_unit_claims(dependency_graph, bindings, logical_modules);
    let mut assignment = ModuleAssignment::new(dependency_graph, default_destination);
    for (idx, claim) in claims.iter().enumerate() {
        if let Some(dest) = claim {
            assignment.set(SourceUnitId(idx), *dest);
        }
    }
    let mut conflicts = Vec::<AtomicGroupConflict>::new();
    for atomic_group in atomic_groups {
        if let Some(conflict) = detect_atomic_group_conflict(
            atomic_group,
            &claims,
            dependency_graph,
            default_destination,
        ) {
            conflicts.push(conflict);
        }
    }
    AssemblyOutcome {
        assignment,
        conflicts,
    }
}

/// One [`ModuleId`] slot per owner; `None` means no explicit claim.
fn compute_source_unit_claims(
    dependency_graph: &DependencyGraph,
    bindings: &HashMap<Id, BindingKind>,
    logical_modules: &[PlannedModule],
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
    for (idx, module) in logical_modules.iter().enumerate() {
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
    claims
}

fn detect_atomic_group_conflict(
    atomic_group: &AtomicGroup,
    claims: &[Option<ModuleId>],
    dependency_graph: &DependencyGraph,
    default_destination: ModuleId,
) -> Option<AtomicGroupConflict> {
    // Each member's effective destination: explicit claim or
    // residual fallback. Two or more distinct destinations is
    // unrealizable (covers the spec-claims-some-but-not-all case).
    let resolved: Vec<(SourceUnitId, ModuleId)> = atomic_group
        .members
        .iter()
        .map(|source_unit| {
            (
                *source_unit,
                claims[source_unit.0].unwrap_or(default_destination),
            )
        })
        .collect();
    let mut first: Option<ModuleId> = None;
    let mut has_distinct = false;
    for &(_, m) in &resolved {
        match first {
            None => first = Some(m),
            Some(existing) if existing != m => {
                has_distinct = true;
                break;
            }
            _ => {}
        }
    }
    if !has_distinct {
        return None;
    }

    let claims_report: Vec<ConflictingClaim> = resolved
        .into_iter()
        .map(|(source_unit, module)| {
            let binding_names = dependency_graph
                .node(source_unit)
                .map(|node| {
                    node.declared
                        .iter()
                        .map(|id| id.0.clone())
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            ConflictingClaim {
                source_unit,
                binding_names,
                module,
            }
        })
        .collect();

    Some(AtomicGroupConflict {
        members: atomic_group.members.iter().copied().collect(),
        claims: claims_report,
        causes: atomic_group.causes.clone(),
    })
}
