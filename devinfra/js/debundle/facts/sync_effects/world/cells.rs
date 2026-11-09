//! Lexical cells: the mutable bindings closures capture by identity.
//!
//! A cell id names one instantiated binding for the whole run; ids are never
//! reused, so two worlds forked from one prefix can be joined cell by cell.

use super::super::local_operations::LocalBinding;
use super::super::value_model::{AbstractValue, Captured, LexicalCellId, Values};
use rustc_hash::FxHashSet;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(in crate::facts::sync_effects) enum CellInitialization {
    Uninitialized,
    Initialized,
    MaybeInitialized,
}

#[derive(Debug, Clone, PartialEq)]
pub(in crate::facts::sync_effects) struct LexicalCell {
    pub(in crate::facts::sync_effects) values: Values,
    pub(in crate::facts::sync_effects) kind: LocalBinding,
    pub(in crate::facts::sync_effects) initialization: CellInitialization,
}

/// Slots are never reused. Retiring a cell frees its box and value graph,
/// leaving only an empty identity slot behind.
#[derive(Debug, Clone, PartialEq)]
pub(in crate::facts::sync_effects) struct LexicalCells {
    pub(super) slots: Vec<Option<Box<LexicalCell>>>,
    pub(super) occupied: Vec<LexicalCellId>,
    pub(super) next_collection_at: usize,
}

impl Default for LexicalCells {
    fn default() -> Self {
        Self {
            slots: Vec::new(),
            occupied: Vec::new(),
            next_collection_at: 1,
        }
    }
}

impl LexicalCells {
    pub(in crate::facts::sync_effects) fn insert(&mut self, cell: LexicalCell) -> LexicalCellId {
        let id = LexicalCellId(self.slots.len());
        self.slots.push(Some(Box::new(cell)));
        self.occupied.push(id);
        id
    }

    pub(in crate::facts::sync_effects) fn collection_due(&self, last_owner: bool) -> bool {
        !self.occupied.is_empty() && (last_owner || self.slots.len() >= self.next_collection_at)
    }

    pub(in crate::facts::sync_effects) fn sweep(
        &mut self,
        marked: &FxHashSet<LexicalCellId>,
        trace_work: usize,
    ) {
        self.occupied.retain(|id| {
            if marked.contains(id) {
                true
            } else {
                self.slots[id.0] = None;
                false
            }
        });
        // Allocation-driven scheduling amortizes the root walk. This counts
        // graph nodes for collection policy, not elapsed time or telemetry.
        self.next_collection_at = self.slots.len().saturating_add(trace_work.max(64));
    }
}

impl std::ops::Index<usize> for LexicalCells {
    type Output = LexicalCell;

    fn index(&self, index: usize) -> &Self::Output {
        self.slots[index]
            .as_deref()
            .expect("live lexical-cell reference")
    }
}

impl std::ops::IndexMut<usize> for LexicalCells {
    fn index_mut(&mut self, index: usize) -> &mut Self::Output {
        self.slots[index]
            .as_deref_mut()
            .expect("live lexical-cell reference")
    }
}

/// All references here borrow the one settled owner-boundary state. Separate
/// visited sets respect the two interner identity namespaces and break cycles
/// through cells without cloning captured graphs.
pub(in crate::facts::sync_effects) struct CellTrace<'a> {
    arena: &'a LexicalCells,
    pending: Vec<&'a Values>,
    cells: FxHashSet<LexicalCellId>,
    contexts: FxHashSet<usize>,
    callback_sets: FxHashSet<usize>,
    work: usize,
}

impl<'a> CellTrace<'a> {
    pub(in crate::facts::sync_effects) fn new(arena: &'a LexicalCells) -> Self {
        Self {
            arena,
            pending: Vec::new(),
            cells: FxHashSet::default(),
            contexts: FxHashSet::default(),
            callback_sets: FxHashSet::default(),
            work: 0,
        }
    }

    pub(in crate::facts::sync_effects) fn values(&mut self, values: &'a Values) {
        self.pending.push(values);
    }

    pub(in crate::facts::sync_effects) fn cell(&mut self, id: LexicalCellId) {
        if self.cells.insert(id) {
            self.work = self.work.saturating_add(1);
            self.pending.push(&self.arena[id.0].values);
        }
    }

    pub(in crate::facts::sync_effects) fn retained(&mut self, captured: &'a Captured<Values>) {
        if self.callback_sets.insert(captured.cache_identity()) {
            self.values(&captured.values);
        }
    }

    pub(in crate::facts::sync_effects) fn finish(mut self) -> (FxHashSet<LexicalCellId>, usize) {
        while let Some(values) = self.pending.pop() {
            self.work = self.work.saturating_add(values.len()).saturating_add(1);
            for value in values {
                match value {
                    AbstractValue::Closure(_, captures) => {
                        if self.contexts.insert(captures.cache_identity()) {
                            for values in captures.values.snapshots.values() {
                                self.values(values);
                            }
                            for cell in captures.values.cells.values() {
                                self.cell(*cell);
                            }
                        }
                    }
                    AbstractValue::Instance(_, captured, _)
                    | AbstractValue::DeferredCallbacks(captured)
                    | AbstractValue::AuditedRetainedResult(_, captured) => self.retained(captured),
                    AbstractValue::Value(_)
                    | AbstractValue::SymbolicInput(_)
                    | AbstractValue::ClassMembers(_)
                    | AbstractValue::CallbackFactory
                    | AbstractValue::InlineCallbackFactory
                    | AbstractValue::OwnDescriptorMap(_)
                    | AbstractValue::ForeignDeferredRecord
                    | AbstractValue::ObjectAssign
                    | AbstractValue::ArrayIntrinsic
                    | AbstractValue::GlobalObject => {}
                }
            }
        }
        (self.cells, self.work)
    }
}
