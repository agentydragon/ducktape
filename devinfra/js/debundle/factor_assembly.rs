//! Factor assembly: produces the authoritative per-owner
//! [`Partition`] from [`crate::atomic_units`]' SCCs + the spec's YAML
//! claims. When two or more distinct claims fall inside one atomic
//! unit the spec is unrealizable; we surface an
//! [`AtomicUnitConflict`] (members + each conflicting claim + the
//! `DepKind` causes from the unit) so the materializer can reject
//! before emit. This pass deliberately does *not* extend a single-
//! member claim to cover the rest of its unit — that promotion is
//! the factorize-proposal layer's job (see [`crate::factorize`]).
//! See `FACTORIZE.md`.

use std::collections::{HashMap, HashSet};

use serde::Serialize;

use crate::atomic_units::AtomicUnit;
use crate::graph::{DepKind, OwnerGraph, OwnerId};
use crate::ids::{BindingKind, BindingName, LogicalModule, LogicalModuleIndex, ModuleId};
use crate::partition::Partition;

/// One owner's claimed destination inside a conflicting atomic
/// unit. Listed inside an [`AtomicUnitConflict`].
#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct ConflictingClaim {
    pub owner: OwnerId,
    pub binding_names: Vec<BindingName>,
    pub module: ModuleId,
}

/// An atomic factor unit whose members the spec routes to two or
/// more distinct destinations — unrealizable by construction.
#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct AtomicUnitConflict {
    /// Members sorted by `OwnerId`.
    pub members: Vec<OwnerId>,
    pub claims: Vec<ConflictingClaim>,
    /// Constraining-edge kinds inside the unit (mirrors
    /// [`AtomicUnit::causes`]). Lets the materializer's diagnostic
    /// tell the author what kind of edge forced co-location (eager
    /// cycle, rebind, sequenced side-effect chain).
    pub causes: HashSet<DepKind>,
}

#[derive(Debug, Clone)]
pub struct AssemblyOutcome {
    pub partition: Partition,
    pub conflicts: Vec<AtomicUnitConflict>,
}

/// Build the partition + any unit-claim conflicts. The partition is
/// best-effort when conflicts exist (first-seen claim wins per unit)
/// so downstream owner-graph/report code keeps working — the
/// conflict list is what surfaces the rejection to the user.
pub fn assemble_partition(
    owner_graph: &OwnerGraph,
    atomic_units: &[AtomicUnit],
    bindings: &HashMap<BindingName, BindingKind>,
    logical_modules: &[LogicalModule],
) -> AssemblyOutcome {
    let claims = compute_owner_claims(owner_graph, bindings, logical_modules);
    let mut partition = Partition::new(owner_graph);
    for (idx, claim) in claims.iter().enumerate() {
        if let Some(dest) = claim {
            partition.set(OwnerId(idx), *dest);
        }
    }
    let mut conflicts = Vec::<AtomicUnitConflict>::new();
    for unit in atomic_units {
        if let Some(conflict) = detect_unit_conflict(unit, &claims, owner_graph) {
            conflicts.push(conflict);
        }
    }
    AssemblyOutcome {
        partition,
        conflicts,
    }
}

/// One [`ModuleId`] slot per owner; `None` means no explicit claim.
fn compute_owner_claims(
    owner_graph: &OwnerGraph,
    bindings: &HashMap<BindingName, BindingKind>,
    logical_modules: &[LogicalModule],
) -> Vec<Option<ModuleId>> {
    let mut claims = vec![None; owner_graph.nodes.len()];
    for node in &owner_graph.nodes {
        for binding_id in &node.declared {
            let Some(name) = owner_graph.binding_table.name(*binding_id) else {
                continue;
            };
            if let Some(BindingKind::Owned { owner: dest }) = bindings.get(name) {
                claims[node.id.0] = Some(*dest);
                break;
            }
        }
    }
    for (idx, module) in logical_modules.iter().enumerate() {
        let dest = ModuleId::Logical(LogicalModuleIndex(idx));
        for ordinal in &module.anonymous_statement_ordinals {
            if let Some(node) = owner_graph
                .nodes
                .iter()
                .find(|n| n.statement_ordinal.0 == *ordinal)
            {
                claims[node.id.0] = Some(dest);
            }
        }
    }
    claims
}

fn detect_unit_conflict(
    unit: &AtomicUnit,
    claims: &[Option<ModuleId>],
    owner_graph: &OwnerGraph,
) -> Option<AtomicUnitConflict> {
    // Each member's effective destination: explicit claim or
    // residual fallback. Two or more distinct destinations is
    // unrealizable (covers the spec-claims-some-but-not-all case).
    let resolved: Vec<(OwnerId, ModuleId)> = unit
        .members
        .iter()
        .map(|owner| (*owner, claims[owner.0].unwrap_or(ModuleId::ResidualEntry)))
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
        .map(|(owner, module)| {
            let binding_names = owner_graph
                .node(owner)
                .map(|node| {
                    node.declared
                        .iter()
                        .filter_map(|b| owner_graph.binding_table.name(*b).cloned())
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            ConflictingClaim {
                owner,
                binding_names,
                module,
            }
        })
        .collect();

    Some(AtomicUnitConflict {
        members: unit.members.iter().copied().collect(),
        claims: claims_report,
        causes: unit.causes.clone(),
    })
}
