use super::*;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(super) struct CellId(u32);

impl CellId {
    pub(super) fn index(self) -> usize {
        usize::try_from(self.0).expect("inference cell ID does not fit usize")
    }
}

#[derive(Clone, Copy)]
pub(super) struct FactId(NonZeroU32);

impl FactId {
    pub(super) fn new(index: usize) -> Self {
        let one_based = u32::try_from(index)
            .expect("inference fact ID overflow")
            .checked_add(1)
            .expect("inference fact ID overflow");
        Self(NonZeroU32::new(one_based).expect("one-based inference fact ID is nonzero"))
    }

    pub(super) fn index(self) -> usize {
        usize::try_from(self.0.get() - 1).expect("inference fact ID does not fit usize")
    }
}

pub(super) struct InferenceCell {
    pub(super) fact: Option<FactId>,
    pub(super) readers: Vec<usize>,
}

/// Stable IDs let rule-local known/changed sets share one key-to-cell index.
/// The lookup map and cell-key vector both retain a key because lookups need
/// hashing while changed IDs need their original key for deterministic order.
pub(super) struct InferenceCells<Key, Fact> {
    pub(super) ids: FxHashMap<Key, CellId>,
    pub(super) keys: Vec<Option<Key>>,
    pub(super) cells: Vec<InferenceCell>,
    pub(super) free_cells: Vec<CellId>,
    pub(super) facts: Vec<Option<Fact>>,
    pub(super) free_facts: Vec<FactId>,
}

impl<Key, Fact> Default for InferenceCells<Key, Fact> {
    fn default() -> Self {
        Self {
            ids: FxHashMap::default(),
            keys: Vec::new(),
            cells: Vec::new(),
            free_cells: Vec::new(),
            facts: Vec::new(),
            free_facts: Vec::new(),
        }
    }
}

impl<Key: Clone + Eq + Hash, Fact> InferenceCells<Key, Fact> {
    pub(super) fn intern(&mut self, key: Key) -> CellId {
        if let Some(&id) = self.ids.get(&key) {
            return id;
        }
        let id = if let Some(id) = self.free_cells.pop() {
            let index = id.index();
            debug_assert!(self.keys[index].is_none());
            debug_assert!(self.cells[index].fact.is_none());
            debug_assert!(self.cells[index].readers.is_empty());
            self.keys[index] = Some(key.clone());
            id
        } else {
            let id = CellId(u32::try_from(self.cells.len()).expect("inference cell ID overflow"));
            self.keys.push(Some(key.clone()));
            self.cells.push(InferenceCell {
                fact: None,
                readers: Vec::new(),
            });
            id
        };
        self.ids.insert(key, id);
        id
    }

    pub(super) fn id(&self, key: &Key) -> Option<CellId> {
        self.ids.get(key).copied()
    }

    pub(super) fn key(&self, id: CellId) -> &Key {
        self.keys[id.index()]
            .as_ref()
            .expect("live inference cell has a key")
    }

    pub(super) fn cell(&self, id: CellId) -> &InferenceCell {
        &self.cells[id.index()]
    }

    pub(super) fn cell_mut(&mut self, id: CellId) -> &mut InferenceCell {
        &mut self.cells[id.index()]
    }

    pub(super) fn fact(&self, id: CellId) -> Option<&Fact> {
        self.cell(id)
            .fact
            .and_then(|fact_id| self.facts[fact_id.index()].as_ref())
    }

    pub(super) fn fact_mut(&mut self, id: CellId) -> Option<&mut Fact> {
        let fact_id = self.cell(id).fact?;
        self.facts[fact_id.index()].as_mut()
    }

    pub(super) fn get(&self, key: &Key) -> Option<&Fact> {
        self.id(key).and_then(|id| self.fact(id))
    }

    pub(super) fn get_mut(&mut self, key: &Key) -> Option<&mut Fact> {
        let id = self.id(key)?;
        self.fact_mut(id)
    }

    #[cfg(test)]
    pub(super) fn len(&self) -> usize {
        self.facts.len() - self.free_facts.len()
    }

    #[cfg(test)]
    pub(super) fn values(&self) -> impl Iterator<Item = &Fact> {
        self.facts.iter().filter_map(Option::as_ref)
    }

    pub(super) fn iter(&self) -> impl Iterator<Item = (&Key, &Fact)> {
        self.keys.iter().zip(&self.cells).filter_map(|(key, cell)| {
            let key = key.as_ref()?;
            let fact_id = cell.fact?;
            Some((key, self.facts[fact_id.index()].as_ref()?))
        })
    }

    pub(super) fn insert(&mut self, id: CellId, fact: Fact) {
        assert!(
            self.cell(id).fact.is_none(),
            "inference fact already exists"
        );
        let fact_id = if let Some(fact_id) = self.free_facts.pop() {
            debug_assert!(self.facts[fact_id.index()].is_none());
            self.facts[fact_id.index()] = Some(fact);
            fact_id
        } else {
            let fact_id = FactId::new(self.facts.len());
            self.facts.push(Some(fact));
            fact_id
        };
        self.cell_mut(id).fact = Some(fact_id);
    }

    pub(super) fn remove(&mut self, id: CellId) -> Option<Fact> {
        let fact_id = self.cell_mut(id).fact.take()?;
        let fact = self.facts[fact_id.index()]
            .take()
            .expect("present inference fact slot");
        self.free_facts.push(fact_id);
        Some(fact)
    }

    /// Retire an unwatched consumed key after its activation-local IDs are gone.
    /// A reverse reader is the durable proof that some watcher still owns its ID.
    pub(super) fn retire_unwatched(&mut self, id: CellId) {
        let cell = self.cell(id);
        if cell.fact.is_some() || !cell.readers.is_empty() {
            return;
        }
        let key = self.keys[id.index()]
            .take()
            .expect("live inference cell has a key");
        assert_eq!(self.ids.remove(&key), Some(id));
        self.cells[id.index()] = InferenceCell {
            fact: None,
            readers: Vec::new(),
        };
        self.free_cells.push(id);
    }

    pub(super) fn into_facts(self) -> FxHashMap<Key, Fact> {
        let Self {
            ids,
            keys,
            cells,
            facts,
            ..
        } = self;
        drop(ids);
        let mut facts = facts;
        let mut completed = FxHashMap::default();
        for (key, cell) in keys.into_iter().zip(cells) {
            let Some(fact_id) = cell.fact else {
                continue;
            };
            completed.insert(
                key.expect("present inference fact has a key"),
                facts[fact_id.index()]
                    .take()
                    .expect("present inference fact slot"),
            );
        }
        completed
    }
}

impl<Key: Clone + Eq + Hash, Fact> std::ops::Index<&Key> for InferenceCells<Key, Fact> {
    type Output = Fact;

    fn index(&self, key: &Key) -> &Self::Output {
        self.get(key).expect("inference fact missing")
    }
}

pub(super) struct WatchedRule<Rule> {
    pub(super) rule: Rule,
    pub(super) evaluated: bool,
    pub(super) keys: FxHashSet<CellId>,
    pub(super) scheduled: Option<usize>,
    pub(super) changed: Option<FxHashSet<CellId>>,
}

impl<Rule: Ord + Clone> WatchedRule<Rule> {
    /// Queue this rule at its earliest turn after the current evaluation.
    /// Return false if it already has an equally early or earlier turn.
    pub(super) fn schedule_after(
        &mut self,
        round: usize,
        current: &Rule,
        pending: &mut BTreeSet<(usize, Rule)>,
    ) -> bool {
        let next = if &self.rule > current {
            round
        } else {
            round.checked_add(1).expect("inference round overflow")
        };
        if self.scheduled.is_some_and(|scheduled| scheduled <= next) {
            return false;
        }
        if let Some(previous) = self.scheduled.replace(next) {
            pending.remove(&(previous, self.rule.clone()));
        }
        pending.insert((next, self.rule.clone()));
        true
    }
}

/// IDs address bookkeeping only. Evaluation order always compares `Rule`.
pub(super) struct ValueWatchers<Rule> {
    pub(super) ids: FxHashMap<Rule, usize>,
    pub(super) rules: Vec<WatchedRule<Rule>>,
}

impl<Rule: Clone + Eq + Hash> ValueWatchers<Rule> {
    pub(super) fn new() -> Self {
        Self {
            ids: FxHashMap::default(),
            rules: Vec::new(),
        }
    }

    pub(super) fn register(&mut self, rule: Rule) -> (usize, bool) {
        use std::collections::hash_map::Entry;
        match self.ids.entry(rule) {
            Entry::Occupied(entry) => (*entry.get(), false),
            Entry::Vacant(entry) => {
                let id = self.rules.len();
                self.rules.push(WatchedRule {
                    rule: entry.key().clone(),
                    evaluated: false,
                    keys: FxHashSet::default(),
                    scheduled: None,
                    changed: None,
                });
                entry.insert(id);
                (id, true)
            }
        }
    }
}
