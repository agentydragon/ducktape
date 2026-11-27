//! Exact ESM evaluation over a canonical module graph. Preferred ordering
//! lives in `esm_import_order`; concrete traversal lives in `esm_schedule`.

use std::collections::{BTreeMap, BTreeSet};

use analysis::ids::ModuleId;

use esm_schedule::EsmImportOrder;
use esm_schedule::EsmImportSchedule;

/// Exact evaluation for a canonical module graph. Preserve the preferred
/// policy when valid; otherwise derive a concrete ESM import schedule. When no
/// schedule exists, retain the preferred traversal to render a rejection
/// witness. It has no production caller yet.
#[derive(Debug, Clone, Eq, PartialEq)]
pub(super) struct EsmEvaluationSimulator {
    /// Post-order index per module after DFS from entry. Lower
    /// index = earlier post-order = body evaluates earlier. Modules
    /// unreachable from entry are absent — ESM doesn't load them,
    /// so the simulator skips constraining-edge checks involving
    /// them.
    pub(super) post_order: BTreeMap<ModuleId, usize>,
    /// False when the constraining pairs are cyclic: `EsmImportOrder` then
    /// has no linker order and every non-entry import list falls back to
    /// `ModuleId` order.
    linker_order_available: bool,
}

impl EsmEvaluationSimulator {
    /// Build from the module import graph and its constraining pairs.
    pub(super) fn build(
        i_successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
        constraining_pairs: &BTreeSet<(ModuleId, ModuleId)>,
        entry_module_id: ModuleId,
    ) -> Self {
        // The simulator's module universe: every module that appears
        // in the I-graph (plus entry itself). The emitted entry
        // imports every logical module; modules with no I-edges are
        // DFS dead-ends that cannot affect any other module's
        // post-order, so restricting the universe to I-graph
        // participants is exact.
        let mut nodes: BTreeSet<ModuleId> = BTreeSet::new();
        nodes.insert(entry_module_id);
        for (from, succs) in i_successors {
            nodes.insert(*from);
            nodes.extend(succs.iter().copied());
        }
        let import_order = EsmImportOrder::build(constraining_pairs, i_successors, &nodes);
        let schedule = EsmImportSchedule::construct(
            entry_module_id,
            constraining_pairs,
            i_successors,
            &nodes,
            &import_order,
        )
        .unwrap_or_else(|_| {
            EsmImportSchedule::from_policy(entry_module_id, i_successors, &nodes, &import_order)
        });
        Self {
            post_order: schedule.post_order().clone(),
            linker_order_available: constraining_pairs.is_empty()
                || !import_order.linker_order().is_empty(),
        }
    }

    /// Yields the `(from, to)` constraining pairs inside `modules`
    /// whose simulator-derived post-order has `to` evaluating at or
    /// after `from` — i.e. the at-init read of `to`'s binding from
    /// `from`'s body would TDZ. Returns the surgical TDZ subset
    /// callers use for diagnostics; an empty iterator means Lemma 2
    /// rescues the SCC.
    ///
    /// Endpoints unreachable from entry are skipped — ESM never
    /// loads them, so they can't fire a TDZ at runtime. Without a linker
    /// order (cyclic constraining pairs, which Pass 1 rejects) there is no
    /// import order to check, so nothing is yielded.
    pub(super) fn tdz_pairs<'a>(
        &'a self,
        modules: &'a BTreeSet<ModuleId>,
        constraining_pairs: &'a BTreeSet<(ModuleId, ModuleId)>,
    ) -> impl Iterator<Item = (ModuleId, ModuleId)> + 'a {
        constraining_pairs
            .iter()
            .copied()
            .filter(|_| self.linker_order_available)
            .filter(move |&(from, to)| {
                if !modules.contains(&from) || !modules.contains(&to) {
                    return false;
                }
                let (Some(from_idx), Some(to_idx)) =
                    (self.post_order.get(&from), self.post_order.get(&to))
                else {
                    return false;
                };
                to_idx >= from_idx
            })
    }
}
