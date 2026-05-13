//! Factor assembly: atomic-unit-aware partition.
//!
//! Consumes [`crate::atomic_units`]' atomic factor units plus the
//! spec's YAML claims and produces the per-owner [`Partition`] that
//! downstream code (quotient construction, realizability gate,
//! materializer) reads as the authoritative module assignment.
//!
//! Atomic units encode *structural* co-location constraints: by
//! construction, every owner in a unit must share a destination for
//! the bundle's init order to be realizable as ESM. So this module:
//!
//! 1. Computes a per-owner *claim* from `bindings` and each logical
//!    module's `anonymous_statement_ordinals`.
//! 2. For each atomic unit, takes the unique claim among its members.
//!    Multiple distinct claims is unrealizable by construction; we
//!    surface an [`AtomicUnitConflict`] that names the unit's
//!    members and each conflicting claim. The materializer rejects
//!    the spec when any conflict is present — see
//!    `Schedule::validate` (which renders the typed ids into a
//!    `ScheduleReport`) and `materialize_logical_modules` (which
//!    bails on the rendered report).
//! 3. Each owner gets its individual claim (or
//!    [`ModuleId::ResidualEntry`] when unclaimed). This pass
//!    intentionally does *not* extend a single-member claim to cover
//!    the rest of the atomic unit — that promotion belongs to the
//!    factorize proposals layer (see [`crate::factorize`]), which
//!    surfaces extensions as advisory suggestions to the spec
//!    author.
//!
//! See `FACTORIZE.md` for the broader architecture.

use std::collections::{HashMap, HashSet};

use crate::atomic_units::AtomicUnit;
use crate::graph::{DepKind, OwnerGraph, OwnerId};
use crate::ids::{BindingKind, BindingName, LogicalModule, LogicalModuleIndex, ModuleId};
use crate::partition::Partition;

/// One owner's claimed destination inside an atomic unit that has
/// multiple distinct destinations. Listed inside an
/// [`AtomicUnitConflict`].
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ConflictingClaim {
    pub owner: OwnerId,
    pub binding_names: Vec<BindingName>,
    pub module: ModuleId,
}

/// An atomic factor unit whose members are claimed for two or more
/// distinct destination modules. By construction the bundle's init
/// order is unrealizable; the materializer rejects the spec at this
/// boundary so the spec author sees a precise diagnostic instead of a
/// downstream symptom (e.g. a module cycle in `ScheduleReport`).
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct AtomicUnitConflict {
    /// Every owner in the unit, sorted by `OwnerId`.
    pub members: Vec<OwnerId>,
    /// One entry per owner in the unit that carries a claim, in the
    /// order the claims were first encountered while walking
    /// `members` (which itself is `OwnerId` order).
    pub claims: Vec<ConflictingClaim>,
    /// Constraining-edge `DepKind`s present inside the unit (i.e.
    /// edges with both endpoints in `members`). Indicates *why* the
    /// owners are forced to co-locate — the materializer's
    /// diagnostic uses this to tell the spec author whether to look
    /// at an EagerUse / EagerRebind cycle, a LazyRebind, or a
    /// Sequenced side-effect chain.
    pub causes: HashSet<DepKind>,
}

/// Result of one chunk's factor assembly: the authoritative partition
/// plus any conflicts the spec author needs to reconcile.
#[derive(Debug, Clone)]
pub struct AssemblyOutcome {
    pub partition: Partition,
    pub conflicts: Vec<AtomicUnitConflict>,
}

/// Assemble the per-chunk [`Partition`] from atomic units plus the
/// spec's claims. Returns the partition together with every atomic
/// unit whose claims collide; the materializer rejects any spec with
/// a non-empty conflict list.
///
/// The partition is best-effort when conflicts exist (first-seen
/// claim wins per unit) so downstream owner-graph and report code
/// keeps working even on an unrealizable spec — the conflict list is
/// what surfaces the rejection to the user.
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

/// One [`ModuleId`] slot per owner, indexed densely by [`OwnerId`].
/// `None` means the owner has no explicit claim — it falls through to
/// the unit's residual default.
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
    // Resolve each member's effective destination: explicit claim
    // when present, residual entry by default. A unit is unrealizable
    // when two or more distinct destinations show up — that includes
    // the case where the spec claims some members for a logical
    // module but leaves the rest unclaimed (so they default to
    // residual, splitting the unit).
    let resolved: Vec<(OwnerId, ModuleId)> = unit
        .members
        .iter()
        .map(|owner| {
            let dest = claims[owner.0].unwrap_or(ModuleId::ResidualEntry);
            (*owner, dest)
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

    let members: Vec<OwnerId> = unit.members.iter().copied().collect();
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
    let causes = constraining_causes_within(unit, owner_graph);

    Some(AtomicUnitConflict {
        members,
        claims: claims_report,
        causes,
    })
}

/// `DepKind`s of constraining edges (everything except `LazyUse`)
/// where both endpoints belong to `unit`. The set indicates what
/// forced the unit's members together — see the closure rules at the
/// top of `atomic_units.rs`.
fn constraining_causes_within(unit: &AtomicUnit, owner_graph: &OwnerGraph) -> HashSet<DepKind> {
    let mut causes = HashSet::new();
    for edge in owner_graph.iter_edges() {
        if edge.from == edge.to {
            continue;
        }
        if !unit.members.contains(&edge.from) || !unit.members.contains(&edge.to) {
            continue;
        }
        if edge.reason.kind == DepKind::LazyUse {
            continue;
        }
        causes.insert(edge.reason.kind);
    }
    causes
}
