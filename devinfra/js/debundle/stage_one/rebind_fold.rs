//! Stage A.5 composer: rebind-only atomic-unit folding.
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
//! Stage A.5 takes the post-seed binding→module assignment (the
//! "partition" after explicit requests + destructure pull + residual
//! sweep have all run) plus the structural atomic units, and decides
//! which unclaimed cycle members should silently fold into the
//! cycle's single explicit destination. The decision is pure: it
//! only reads the owner graph + atomic-units + assignment + the
//! residual plan index. Mutation of the lowering-side `ModulePlan`
//! list happens at the caller (today: `ChunkPlanBuilder::apply_rebind_folds`).
//!
//! "Stage A.5" because it runs after Stage A (`compute_stage_one_analysis`)
//! and after the partition's seed phases, but before Stage B (lowering
//! proper). See `ARCHITECTURE_BACKLOG.md` for the separation rationale.

use std::collections::HashMap;

use swc_ecma_ast::Id;

use analysis::atomic_units::OwnerGraphAndUnits;
use analysis::graph::DepKind;
use analysis::ids::{BindingKind, LogicalModuleIndex, ModuleId};

/// One rebind-fold decision: a binding that should be (re)routed
/// from its current plan to `dest`. Carries `previous` so the caller
/// can clean up the residual plan's binding list when the binding
/// was parked there by the residual sweep.
#[derive(Debug, Clone)]
pub struct RebindFold {
    /// The chunk-top-level binding id to (re)route.
    pub binding: Id,
    /// Source name (the `binding.0.as_ref()` form) — included so the
    /// applier can update `ModulePlan::bindings` without cloning the
    /// `Id`'s atom out separately at every call site.
    pub name: String,
    /// Plan index this binding folds into.
    pub dest: usize,
    /// The `BindingKind::Owned { module: dest_module_id }` value the
    /// caller should mirror into its bindings catalogue. Materialized
    /// here so the analysis crate owns the `ModuleId`/`BindingKind`
    /// construction (the caller doesn't need to know that the
    /// `LogicalModuleIndex` wrapping convention exists).
    pub owned_kind: BindingKind,
    /// Plan index this binding was previously routed to, if any.
    /// `Some(idx)` where `idx == residual_plan_index` signals "remove
    /// `name` from `module_plans[idx].bindings` while routing it to
    /// `dest`"; other `Some(_)` values cannot occur because folding
    /// only applies to unclaimed owners (see filter in
    /// `compute_rebind_folds`).
    pub previous: Option<usize>,
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
        let module_id = ModuleId(LogicalModuleIndex(dest));
        let owned_kind = BindingKind::Owned { module: module_id };
        for owner_id in owners_to_fold {
            let Some(node) = owner_graph.node(owner_id) else {
                continue;
            };
            for binding_id in &node.declared {
                let previous = binding_assignment.get(binding_id).copied();
                let name = binding_id.0.as_ref().to_string();
                folds.push(RebindFold {
                    binding: binding_id.clone(),
                    name,
                    dest,
                    owned_kind: owned_kind.clone(),
                    previous,
                });
            }
        }
    }
    folds
}
