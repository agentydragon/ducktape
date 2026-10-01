//! Rebind-only atomic-unit folding.
//!
//! Background: see `docs/design.md` and `atomic_units.rs`. The
//! structural-atomic-unit pass over the owner graph symmetrizes
//! `LazyRebind`/`EagerRebind` edges because ESM imports are read-
//! only — a peel that places the rebind's write site in one module
//! and its declaration in another would emit code that throws
//! `TypeError: Assignment to constant variable` the first time the
//! assignment fires. Without folding, such a spec would surface as
//! an `atomic_unit_conflict` and the materializer would bail.
//!
//! The pass takes the post-seed binding→module assignment (the
//! "partition" after explicit requests + destructure pull + residual
//! sweep have all run) plus the structural atomic units, and decides
//! which unclaimed cycle members should silently fold into the
//! cycle's single explicit destination. The decision is pure: it
//! only reads the owner graph + atomic-units + assignment + the
//! residual plan index. It stays beside lowering because its input is
//! the post-seed lowering assignment; mutation of the `ModulePlan`
//! list happens at the caller (today: `ChunkPlanBuilder::apply_rebind_folds`).
//! It runs after chunk analysis (`compute_chunk_analysis`) and after
//! the partition's seed phases, but before module emission.

use std::collections::HashMap;

use swc_ecma_ast::Id;

use analysis::atomic_units::OwnerGraphAndUnits;
use analysis::graph::DepKind;

/// One rebind-fold decision: a binding that should be (re)routed
/// from its current plan to `dest`.
#[derive(Debug, Clone)]
pub struct RebindFold {
    /// The chunk-top-level binding id to (re)route.
    pub binding: Id,
    /// Plan index this binding folds into.
    pub dest: usize,
    /// The residual sweep had parked this binding in the residual
    /// plan, so the caller removes it from that plan's binding list.
    /// Folding only applies to owners whose bindings are unclaimed or
    /// residual-swept (see the filter in `compute_rebind_folds`), so
    /// no other previous plan can occur.
    pub from_residual: bool,
}

/// Decide which atomic-unit members should fold into an explicit
/// destination.
///
/// Resolve rebind-only atomic-unit "soft" conflicts by extending the
/// explicit claim's plan to cover any member of the cycle that has
/// no explicit destination. When exactly one member of a rebind-only
/// cycle carries an explicit (non-residual) claim and the rest are
/// unclaimed (or were already swept into the residual landing site),
/// the conflict is the spec author's implicit oversight rather than
/// a contradiction: they peeled the writer but left the declarer at
/// the default destination, not realizing the cycle pulls them
/// together. Folding the writer's module to cover the declarer keeps
/// the rebind intra-module — the writer's assignment resolves
/// locally — and preserves the spec's peel intent.
///
/// Multi-explicit-destination conflicts return no fold so the
/// downstream materializer's bail surfaces the contradiction.
/// Conflicts with non-rebind causes (`LocalEffect`, eager cycles,
/// sequenced side-effect chains) also produce no fold — those have
/// their own resolution stories and are intentionally surfaced as
/// hard errors.
///
/// Pure: this function only inspects `precomputed`, `binding_assignment`,
/// and `residual_plan_index`. It does not mutate; the caller applies
/// the returned `Vec<RebindFold>` to its `ModulePlan` list and
/// bindings catalogue.
pub fn compute_rebind_folds(
    precomputed: &OwnerGraphAndUnits,
    binding_assignment: &HashMap<Id, usize>,
    residual_plan_index: Option<usize>,
) -> Vec<RebindFold> {
    let owner_graph = &precomputed.owner_graph;
    let mut folds = Vec::new();
    'unit: for unit in &precomputed.atomic_units {
        if unit.causes.is_empty() {
            continue;
        }
        let rebind_only = unit.causes.iter().all(|cause| {
            matches!(
                cause,
                DepKind::LazyRebind | DepKind::EagerRebind | DepKind::DeferredRebind
            )
        });
        if !rebind_only {
            continue;
        }
        let mut explicit_dest: Option<usize> = None;
        let mut owners_to_fold: Vec<analysis::graph::OwnerId> = Vec::new();
        for &owner_id in &unit.members {
            let Some(node) = owner_graph.node(owner_id) else {
                continue;
            };
            if node.declared.is_empty() {
                // Anonymous statements don't appear in `binding_assignment`;
                // their routing is via `anonymous_ordinal_assignment`. They
                // can't be the carrier of a rebind cause anyway — a rebind
                // edge needs a declared target — so skipping them is safe.
                continue;
            }
            let mut owner_claim: Option<usize> = None;
            for binding_id in &node.declared {
                let Some(&idx) = binding_assignment.get(binding_id) else {
                    continue;
                };
                // A binding that was swept into the residual landing site
                // counts as "unclaimed" for fold purposes — the user didn't
                // explicitly route it there, the sweep did.
                if Some(idx) == residual_plan_index {
                    continue;
                }
                owner_claim = Some(idx);
                break;
            }
            match owner_claim {
                Some(idx) => match explicit_dest {
                    None => explicit_dest = Some(idx),
                    Some(existing) if existing != idx => continue 'unit,
                    _ => {}
                },
                None => owners_to_fold.push(owner_id),
            }
        }
        let Some(dest) = explicit_dest else {
            continue;
        };
        if owners_to_fold.is_empty() {
            continue;
        }
        for owner_id in owners_to_fold {
            let Some(node) = owner_graph.node(owner_id) else {
                continue;
            };
            for binding_id in &node.declared {
                folds.push(RebindFold {
                    binding: binding_id.clone(),
                    dest,
                    from_residual: binding_assignment.contains_key(binding_id),
                });
            }
        }
    }
    folds
}
