use super::*;

/// A reverse premise index, owned and retired with one inference closure.
/// Source-only families leave it empty, including values-only projections.
pub(super) struct ProofTransfers<Key> {
    pub(super) edges: FxHashMap<Key, FxHashSet<Key>>,
    // With explanatory premises omitted, keep just the incoming live edges
    // needed to retire a completed transfer without scanning the whole graph.
    pub(super) inputs: FxHashMap<Key, FxHashSet<Key>>,
    #[cfg(test)]
    pub(super) atom_deliveries: usize,
}

impl<Key> Default for ProofTransfers<Key> {
    fn default() -> Self {
        Self {
            edges: FxHashMap::default(),
            inputs: FxHashMap::default(),
            #[cfg(test)]
            atom_deliveries: 0,
        }
    }
}

impl<Key: Clone + Eq + Hash> ProofTransfers<Key> {
    pub(super) fn insert<const RETAIN_PREMISES: bool>(
        &mut self,
        source: &Key,
        target: &Key,
    ) -> bool {
        let added = self
            .edges
            .entry(source.clone())
            .or_default()
            .insert(target.clone());
        if added && !RETAIN_PREMISES {
            self.inputs
                .entry(target.clone())
                .or_default()
                .insert(source.clone());
        }
        added
    }

    pub(super) fn retire<const RETAIN_PREMISES: bool, Fact>(
        &mut self,
        key: &Key,
        fact: &Judgment<Key, Fact>,
    ) {
        // Reuse the explanatory premises when available. Values-only solves
        // retain their own incoming edges so retirement stays local.
        if RETAIN_PREMISES {
            for source in fact.premises() {
                Self::remove_link(&mut self.edges, source, key);
            }
        } else if let Some(sources) = self.inputs.remove(key) {
            for source in sources {
                Self::remove_link(&mut self.edges, &source, key);
            }
        }
        let outgoing = self.edges.remove(key);
        if !RETAIN_PREMISES {
            for target in outgoing.into_iter().flatten() {
                Self::remove_link(&mut self.inputs, &target, key);
            }
        }
    }

    pub(super) fn remove_link(index: &mut FxHashMap<Key, FxHashSet<Key>>, from: &Key, to: &Key) {
        if let Some(neighbors) = index.get_mut(from) {
            neighbors.remove(to);
            if neighbors.is_empty() {
                index.remove(from);
            }
        }
    }
}

/// Mutable access is scoped to one monotone producer. Both absent and present
/// reads install watchers, including reads made before a producer exists.
pub(crate) struct IterativeContext<'a, R: IterativeRules, const RETAIN_PREMISES: bool = true> {
    pub(super) facts: &'a mut InferenceCells<R::Key, Judgment<R::Key, R::Fact>>,
    pub(super) sealed: Option<&'a SealedFacts<R::Key, R::Fact>>,
    pub(super) sealed_reads: Vec<R::Key>,
    pub(super) proof_edges: &'a mut ProofTransfers<R::Key>,
    /// In read order, with repeats: appending is cheaper than a set insert
    /// for every read, and the solver skips keys the rule already watches.
    pub(super) reads: Vec<CellId>,
    pub(super) changed: FxHashSet<CellId>,
    pub(super) dependencies: ProofDependencies,
    pub(super) merge_scratch: R::MergeScratch,
    pub(super) metrics: Option<&'a mut Batch>,
    pub(super) delta_deliveries: usize,
    #[cfg(test)]
    pub(super) read_attempts: usize,
    #[cfg(test)]
    pub(super) proposals: usize,
    #[cfg(test)]
    pub(super) value_changes: usize,
    #[cfg(test)]
    pub(super) peak_facts: usize,
}

impl<'a, R: IterativeRules, const RETAIN_PREMISES: bool> IterativeContext<'a, R, RETAIN_PREMISES> {
    #[cfg(test)]
    pub(super) fn new(
        facts: &'a mut InferenceCells<R::Key, Judgment<R::Key, R::Fact>>,
        proof_edges: &'a mut ProofTransfers<R::Key>,
    ) -> Self {
        Self::new_with_scratch(facts, proof_edges, R::MergeScratch::default())
    }

    pub(super) fn new_with_scratch(
        facts: &'a mut InferenceCells<R::Key, Judgment<R::Key, R::Fact>>,
        proof_edges: &'a mut ProofTransfers<R::Key>,
        merge_scratch: R::MergeScratch,
    ) -> Self {
        Self {
            facts,
            sealed: None,
            sealed_reads: Vec::new(),
            proof_edges,
            reads: Vec::new(),
            changed: FxHashSet::default(),
            dependencies: ProofDependencies::default(),
            merge_scratch,
            metrics: None,
            delta_deliveries: 0,
            #[cfg(test)]
            read_attempts: 0,
            #[cfg(test)]
            proposals: 0,
            #[cfg(test)]
            value_changes: 0,
            #[cfg(test)]
            peak_facts: 0,
        }
    }

    pub(super) fn extend_proof(
        &mut self,
        id: CellId,
        key: &R::Key,
        dependencies: &ProofDependencies,
    ) -> ProofDependencies {
        debug_assert!(self.facts.key(id) == key);
        let fact = self.facts.fact_mut(id).expect("proposed fact exists");
        let added = dependencies.difference(fact.dependencies());
        if !added.is_empty() {
            fact.dependencies
                .get_or_insert_with(Default::default)
                .extend(&added);
        }
        added
    }

    pub(super) fn propagate_proof(&mut self, key: R::Key, added: ProofDependencies) {
        let mut pending = VecDeque::new();
        if !added.is_empty() {
            pending.push_back((key, added));
        }
        while let Some((source, added)) = pending.pop_front() {
            // Borrow the edge index separately so extending a target's proof
            // does not clone a potentially large fanout list.
            let Some(targets) = self.proof_edges.edges.get(&source) else {
                continue;
            };
            for target in targets {
                #[cfg(test)]
                {
                    self.proof_edges.atom_deliveries += added.atom_count();
                }
                let Some(fact) = self.facts.get_mut(target) else {
                    continue;
                };
                let delta = added.difference(fact.dependencies());
                if delta.is_empty() {
                    continue;
                }
                fact.dependencies
                    .get_or_insert_with(Default::default)
                    .extend(&delta);
                pending.push_back((target.clone(), delta));
            }
        }
    }

    pub(super) fn attach_proof(&mut self, id: CellId, key: &R::Key) -> bool {
        if RETAIN_PREMISES && (!self.reads.is_empty() || !self.sealed_reads.is_empty()) {
            let InferenceCells {
                keys, cells, facts, ..
            } = &mut *self.facts;
            let fact_id = cells[id.index()].fact.expect("proposed fact exists");
            let premises = facts[fact_id.index()]
                .as_mut()
                .expect("proposed fact slot")
                .premises
                .get_or_insert_with(Default::default);
            for &read in &self.reads {
                premises.insert(
                    keys[read.index()]
                        .as_ref()
                        .expect("read inference cell has a key")
                        .clone(),
                );
            }
            premises.extend(self.sealed_reads.iter().cloned());
        }
        if R::SOURCE_ONLY {
            return false;
        }
        let mut dependencies = (!self.dependencies.is_empty()).then(|| self.dependencies.clone());
        for &source_id in &self.reads {
            let source = self.facts.key(source_id);
            if source_id != id && self.proof_edges.insert::<RETAIN_PREMISES>(source, key) {
                // A newly activated edge must also receive atoms that
                // appeared after the read, even on an unchanged proposal.
                if let Some(proof) = self
                    .facts
                    .fact(source_id)
                    .and_then(|fact| fact.dependencies.as_ref())
                {
                    #[cfg(test)]
                    {
                        self.proof_edges.atom_deliveries += proof.atom_count();
                    }
                    dependencies
                        .get_or_insert_with(ProofDependencies::default)
                        .extend(proof);
                }
            }
        }
        let Some(dependencies) = dependencies else {
            return false;
        };
        let added = self.extend_proof(id, key, &dependencies);
        let changed = !added.is_empty();
        if changed {
            self.propagate_proof(key.clone(), added);
        }
        changed
    }
}

impl<R: IterativeRules, const RETAIN_PREMISES: bool> FactContext<R::Key, R::Fact>
    for IterativeContext<'_, R, RETAIN_PREMISES>
{
    fn read(&mut self, key: R::Key) -> Option<&R::Fact> {
        #[cfg(test)]
        {
            self.read_attempts += 1;
        }
        if let Some(fact) = self.sealed.and_then(|sealed| sealed.get(&key)) {
            if let Some(metrics) = &mut self.metrics {
                metrics.read(R::metric_key_family(&key), false);
            }
            self.sealed_reads.push(key);
            self.dependencies.extend(fact.dependencies());
            return Some(fact.value());
        }
        let id = self.facts.intern(key.clone());
        self.reads.push(id);
        let fact = self.facts.fact(id);
        if let Some(metrics) = &mut self.metrics {
            metrics.read(R::metric_key_family(&key), fact.is_none());
        }
        let fact = fact?;
        self.dependencies.extend(fact.dependencies());
        Some(fact.value())
    }

    /// Writes are immediately visible to subsequent reads in this producer.
    fn propose(&mut self, key: R::Key, contribution: R::Fact) -> FactChange {
        assert!(
            self.sealed.is_none_or(|sealed| sealed.get(&key).is_none()),
            "cannot produce a sealed input"
        );
        let id = self.facts.intern(key.clone());
        let value_changed = if self.facts.fact(id).is_none() {
            if let Some(metrics) = &mut self.metrics {
                metrics.new_fact(R::metric_key_family(&key));
            }
            self.facts.insert(
                id,
                Judgment {
                    value: contribution,
                    dependencies: None,
                    premises: None,
                },
            );
            true
        } else {
            let fact = self.facts.fact_mut(id).expect("present inference fact");
            if let Some(metrics) = &mut self.metrics {
                R::merge_observed_with_scratch(
                    &key,
                    &mut fact.value,
                    contribution,
                    metrics,
                    &mut self.merge_scratch,
                )
            } else {
                R::merge_with_scratch(&key, &mut fact.value, contribution, &mut self.merge_scratch)
            }
        };
        if let Some(metrics) = &mut self.metrics {
            metrics.add(
                if value_changed {
                    Operation::ChangedProposal
                } else {
                    Operation::UnchangedProposal
                },
                1,
            );
        }
        if value_changed {
            self.changed.insert(id);
        }
        #[cfg(test)]
        {
            self.proposals += 1;
            self.value_changes += usize::from(value_changed);
            self.peak_facts = self.peak_facts.max(self.facts.len());
        }
        FactChange {
            value: value_changed,
            proof: self.attach_proof(id, &key),
        }
    }

    fn propose_delta(
        &mut self,
        key: R::Key,
        contribution: R::Fact,
        delivered: &mut R::Fact,
    ) -> FactChange {
        assert!(
            self.sealed.is_none_or(|sealed| sealed.get(&key).is_none()),
            "cannot produce a sealed input"
        );
        let delta = R::difference(&key, &contribution, delivered);
        if let Some(metrics) = &mut self.metrics {
            R::merge_observed_with_scratch(
                &key,
                delivered,
                contribution,
                metrics,
                &mut self.merge_scratch,
            );
        } else {
            R::merge_with_scratch(&key, delivered, contribution, &mut self.merge_scratch);
        }
        if let Some(delta) = delta {
            self.delta_deliveries += R::delta_atoms(&delta);
            self.propose(key, delta)
        } else {
            if let Some(metrics) = &mut self.metrics {
                metrics.add(Operation::UnchangedProposal, 1);
            }
            #[cfg(test)]
            {
                self.proposals += 1;
            }
            // An existing target can acquire proof edges even without new
            // value atoms. A missing target waits for a nonempty delivery.
            let target = self
                .facts
                .id(&key)
                .filter(|&id| self.facts.fact(id).is_some());
            FactChange {
                value: false,
                proof: target.is_some_and(|id| self.attach_proof(id, &key)),
            }
        }
    }

    fn assume(&mut self, dependencies: &ProofDependencies) {
        assert!(
            !R::SOURCE_ONLY || dependencies.is_empty(),
            "source-only inference cannot introduce typed proof dependencies"
        );
        self.dependencies.extend(dependencies);
    }

    fn read_final(&mut self, key: R::Key) -> Option<&R::Fact> {
        #[cfg(test)]
        {
            self.read_attempts += 1;
        }
        let fact = self.facts.get(&key)?;
        self.dependencies.extend(fact.dependencies());
        Some(fact.value())
    }

    fn take_final(&mut self, key: R::Key) -> Option<R::Fact> {
        #[cfg(test)]
        {
            self.read_attempts += 1;
        }
        assert!(
            self.sealed.is_none_or(|sealed| sealed.get(&key).is_none()),
            "cannot consume a sealed shared input"
        );
        let id = self.facts.id(&key)?;
        let fact = self.facts.remove(id)?;
        self.dependencies.extend(fact.dependencies());
        self.proof_edges.retire::<RETAIN_PREMISES, _>(&key, &fact);
        self.reads.retain(|&read| read != id);
        self.changed.remove(&id);
        // `readers` is the durable reverse of every rule-local `keys` entry
        // and every pending changed-ID entry. The current activation's two
        // ID collections were just cleared above, so an empty reader list
        // means the consumed ID is no longer held anywhere.
        self.facts.retire_unwatched(id);
        Some(fact.value)
    }
}
