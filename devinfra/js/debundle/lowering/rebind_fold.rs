//! Rebind-only atomic-group folding.
//!
//! Background: see `docs/design.md` and `atomic_groups.rs`. The
//! structural-atomic-group pass over the dependency graph symmetrizes
//! `LazyRebind`/`EagerRebind` edges because ESM imports are read-
//! only — a peel that places the rebind's write site in one module
//! and its declaration in another would emit code that throws
//! `TypeError: Assignment to constant variable` the first time the
//! assignment fires. Without folding, such a spec would surface as
//! an `atomic_group_conflict` and the materializer would bail.
//!
//! The pass takes the post-seed binding→module assignment (the
//! "assignment" after explicit requests + destructure pull + residual
//! sweep have all run) plus the structural atomic groups, and decides
//! which unclaimed cycle members should silently fold into the
//! cycle's single explicit destination. The decision is pure: it
//! only reads the dependency graph + atomic groups + assignment + the
//! residual plan index. It stays beside lowering because its input is
//! the post-seed lowering assignment; mutation of the `ModulePlan`
//! list happens at the caller (today: `ChunkPlanBuilder::apply_rebind_folds`).
//! It runs after chunk analysis (`compute_chunk_analysis`) and after
//! the assignment's seed phases, but before module emission.

use std::collections::HashMap;

use swc_ecma_ast::Id;

use analysis::atomic_groups::DependencyGraphAndAtomicGroups;
use analysis::graph::{DepKind, SourceUnitClass};

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
    /// Folding only applies to source_units whose bindings are unclaimed or
    /// residual-swept (see the filter in `compute_rebind_folds`), so
    /// no other previous plan can occur.
    pub from_residual: bool,
}

/// Decide which atomic-group members should fold into an explicit
/// destination.
///
/// Resolve rebind-only atomic-group "soft" conflicts by extending the
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
    precomputed: &DependencyGraphAndAtomicGroups,
    binding_assignment: &HashMap<Id, usize>,
    residual_plan_index: Option<usize>,
) -> Vec<RebindFold> {
    let dependency_graph = &precomputed.dependency_graph;
    let mut folds = Vec::new();
    'atomic_group: for atomic_group in &precomputed.atomic_groups {
        if atomic_group.causes.is_empty() {
            continue;
        }
        let rebind_only = atomic_group.causes.iter().all(|cause| {
            matches!(
                cause,
                DepKind::LazyRebind | DepKind::EagerRebind | DepKind::DeferredRebind
            )
        });
        if !rebind_only {
            continue;
        }
        let mut explicit_dest: Option<usize> = None;
        let mut source_units_to_fold: Vec<analysis::graph::SourceUnitId> = Vec::new();
        for &source_unit_id in &atomic_group.members {
            let Some(node) = dependency_graph.node(source_unit_id) else {
                continue;
            };
            if node.class() == SourceUnitClass::AnonymousStatement {
                // Anonymous statements don't appear in `binding_assignment`;
                // their routing is via `whole_statement_assignment`. They
                // can't be the carrier of a rebind cause anyway — a rebind
                // edge needs a declared target — so skipping them is safe.
                continue;
            }
            let mut source_unit_claim: Option<usize> = None;
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
                source_unit_claim = Some(idx);
                break;
            }
            match source_unit_claim {
                Some(idx) => match explicit_dest {
                    None => explicit_dest = Some(idx),
                    Some(existing) if existing != idx => continue 'atomic_group,
                    _ => {}
                },
                None => source_units_to_fold.push(source_unit_id),
            }
        }
        let Some(dest) = explicit_dest else {
            continue;
        };
        if source_units_to_fold.is_empty() {
            continue;
        }
        for source_unit_id in source_units_to_fold {
            let Some(node) = dependency_graph.node(source_unit_id) else {
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
