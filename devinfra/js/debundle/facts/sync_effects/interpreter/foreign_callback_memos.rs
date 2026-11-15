//! Pure source-owner callback reachability, reused only against its read witness.

use super::{CallbackGraphReads, RuntimeSourceUnitId, Values, World};
use std::collections::{BTreeMap, VecDeque};

/// Bound retained graph snapshots independently of semantic invocation budgets.
const MAX_FOREIGN_CALLBACK_MEMOS: usize = 128;

#[derive(Default)]
pub(super) struct ForeignCallbackMemos {
    entries: BTreeMap<RuntimeSourceUnitId, ForeignCallbackMemo>,
    insertion_order: VecDeque<RuntimeSourceUnitId>,
}

struct ForeignCallbackMemo {
    roots: Values,
    reads: CallbackGraphReads,
    callbacks: Values,
}

impl ForeignCallbackMemos {
    pub(super) fn get(
        &self,
        owner: RuntimeSourceUnitId,
        roots: &Values,
        world: &World,
    ) -> Option<Values> {
        self.entries
            .get(&owner)
            .filter(|memo| memo.roots == *roots && memo.reads.unchanged(world))
            .map(|memo| memo.callbacks.clone())
    }

    pub(super) fn store(
        &mut self,
        owner: RuntimeSourceUnitId,
        roots: Values,
        reads: CallbackGraphReads,
        callbacks: Values,
    ) {
        if !self.entries.contains_key(&owner) {
            if self.entries.len() == MAX_FOREIGN_CALLBACK_MEMOS {
                let oldest = self
                    .insertion_order
                    .pop_front()
                    .expect("a full memo bank has an oldest entry");
                self.entries.remove(&oldest);
            }
            self.insertion_order.push_back(owner);
        }
        self.entries.insert(
            owner,
            ForeignCallbackMemo {
                roots,
                reads,
                callbacks,
            },
        );
    }
}
