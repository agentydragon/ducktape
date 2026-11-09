//! Lexical cells: the mutable bindings closures capture by identity.
//!
//! A cell id names one instantiated binding for the whole run; ids are never
//! reused, so two worlds forked from one prefix can be joined cell by cell.

use super::super::local_operations::LocalBinding;
use super::super::value_model::{AbstractValue, Captured, LexicalCellId, Values};
use crate::facts::abstract_domain::value::Value;
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

    /// Ids handed out so far, retired ones included.
    pub(in crate::facts::sync_effects) fn identities(&self) -> usize {
        self.slots.len()
    }

    /// Skip ids up to `identities` so that the next cell cannot share an id
    /// with one a sibling fork leaf instantiated.
    pub(in crate::facts::sync_effects) fn reserve_identities(&mut self, identities: usize) {
        if self.slots.len() < identities {
            self.slots.resize_with(identities, || None);
        }
    }

    pub(in crate::facts::sync_effects) fn get(&self, id: LexicalCellId) -> Option<&LexicalCell> {
        self.slots.get(id.0)?.as_deref()
    }

    fn get_mut(&mut self, id: LexicalCellId) -> Option<&mut LexicalCell> {
        self.slots.get_mut(id.0)?.as_deref_mut()
    }

    /// A store through a captured reference. The call that performs it may
    /// be skipped on another path, so the cell joins the stored values with
    /// its old contents. Returns the assignment's result, with `Unavailable`
    /// for the abrupt alternative of a const, a named self binding or a cell
    /// that may still be in TDZ, and whether the contents changed; `None` for
    /// a retired cell.
    pub(in crate::facts::sync_effects) fn store_captured(
        &mut self,
        id: LexicalCellId,
        values: &Values,
    ) -> Option<(Values, bool)> {
        let cell = self.get_mut(id)?;
        if matches!(cell.kind, LocalBinding::Const | LocalBinding::NamedSelf(_))
            || cell.initialization == CellInitialization::Uninitialized
        {
            return Some((
                Values::from([AbstractValue::Value(Value::Unavailable)]),
                false,
            ));
        }
        let mut result = values.clone();
        if cell.initialization == CellInitialization::MaybeInitialized {
            result.insert(AbstractValue::Value(Value::Unavailable));
        }
        let before = cell.values.len();
        cell.values.extend(values.iter().cloned());
        Some((result, cell.values.len() != before))
    }

    /// The declaring activation's own store or initialization: it replaces
    /// the contents and leaves TDZ. Returns whether the cell changed.
    pub(in crate::facts::sync_effects) fn store_declared(
        &mut self,
        id: LexicalCellId,
        values: &Values,
    ) -> bool {
        let Some(cell) = self.get_mut(id) else {
            return false;
        };
        let changed =
            cell.values != *values || cell.initialization != CellInitialization::Initialized;
        cell.values = values.clone();
        cell.initialization = CellInitialization::Initialized;
        changed
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

#[cfg(test)]
mod tests {
    use super::{CellInitialization, LexicalCell, LexicalCells};
    use crate::facts::abstract_domain::value::{Kind, Value};
    use crate::facts::sync_effects::local_operations::LocalBinding;
    use crate::facts::sync_effects::value_model::{AbstractValue, Values};

    const UNAVAILABLE: AbstractValue = AbstractValue::Value(Value::Unavailable);

    fn object(allocation: u32) -> AbstractValue {
        AbstractValue::Value(Value::Alloc(Kind::Object, allocation))
    }

    fn cells_with(kind: LocalBinding, initialization: CellInitialization) -> LexicalCells {
        let mut cells = LexicalCells::default();
        cells.insert(LexicalCell {
            values: Values::from([object(1)]),
            kind,
            initialization,
        });
        cells
    }

    #[test]
    fn captured_store_in_tdz_is_abrupt_and_stores_nothing() {
        for (kind, initialization) in [
            (LocalBinding::Let, CellInitialization::Uninitialized),
            (LocalBinding::Const, CellInitialization::Initialized),
            (LocalBinding::NamedSelf(0), CellInitialization::Initialized),
        ] {
            let mut cells = cells_with(kind, initialization);
            let id = cells.occupied[0];

            let stored = cells.store_captured(id, &Values::from([object(2)]));

            assert_eq!(stored, Some((Values::from([UNAVAILABLE]), false)));
            assert_eq!(cells[id.0].values, Values::from([object(1)]));
        }
    }

    #[test]
    fn captured_store_joins_and_maybe_initialized_keeps_the_abrupt_alternative() {
        for (initialization, result) in [
            (CellInitialization::Initialized, Values::from([object(2)])),
            (
                CellInitialization::MaybeInitialized,
                Values::from([object(2), UNAVAILABLE]),
            ),
        ] {
            let mut cells = cells_with(LocalBinding::Let, initialization);
            let id = cells.occupied[0];

            let stored = cells.store_captured(id, &Values::from([object(2)]));

            assert_eq!(stored, Some((result, true)));
            assert_eq!(cells[id.0].values, Values::from([object(1), object(2)]));
        }
    }

    #[test]
    fn declared_store_replaces_and_leaves_tdz() {
        let mut cells = cells_with(LocalBinding::Let, CellInitialization::Uninitialized);
        let id = cells.occupied[0];

        assert!(cells.store_declared(id, &Values::from([object(2)])));
        assert!(!cells.store_declared(id, &Values::from([object(2)])));

        assert_eq!(cells[id.0].values, Values::from([object(2)]));
        assert_eq!(cells[id.0].initialization, CellInitialization::Initialized);
    }
}
