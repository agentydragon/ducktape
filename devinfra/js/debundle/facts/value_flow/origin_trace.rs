//! Temporary selected-origin diagnostic; records first contributions without
//! adding fact reads, changing transfers, or retaining the full fact store.

use super::{FlowEvent, FlowFact, FlowKey, OriginId, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::inference::{FactChange, FactContext};
use std::cell::RefCell;
use std::collections::BTreeSet;

pub(super) struct OriginTrace {
    origin: OriginId,
    seen: RefCell<BTreeSet<FlowKey>>,
}

impl OriginTrace {
    pub(super) fn new(origin: OriginId) -> Self {
        Self {
            origin,
            seen: RefCell::default(),
        }
    }

    pub(super) fn matches(&self, values: &Values) -> bool {
        values.bitmap.contains(self.origin.0)
    }

    pub(super) fn seen(&self, key: FlowKey) -> bool {
        self.seen.borrow().contains(&key)
    }
}

pub(super) struct TracedContext<'a, C> {
    inner: &'a mut C,
    trace: &'a OriginTrace,
    event: usize,
    source: &'a FlowEvent,
    reads: BTreeSet<FlowKey>,
}

impl<'a, C> TracedContext<'a, C> {
    pub(super) fn new(
        inner: &'a mut C,
        trace: &'a OriginTrace,
        event: usize,
        source: &'a FlowEvent,
    ) -> Self {
        Self {
            inner,
            trace,
            event,
            source,
            reads: BTreeSet::new(),
        }
    }

    fn record(&self, key: FlowKey, contribution: &FlowFact) {
        if let FlowFact::Values(values) = contribution
            && self.trace.matches(values)
            && self.trace.seen.borrow_mut().insert(key)
        {
            eprintln!(
                "origin-trace first key={key:?} event={} reads={:?} source={:?}",
                self.event, self.reads, self.source
            );
        }
    }
}

impl<C: FactContext<FlowKey, FlowFact>> FactContext<FlowKey, FlowFact> for TracedContext<'_, C> {
    fn read(&mut self, key: FlowKey) -> Option<&FlowFact> {
        let fact = self.inner.read(key);
        if let Some(FlowFact::Values(values)) = fact
            && self.trace.matches(values)
        {
            self.reads.insert(key);
        }
        fact
    }

    fn propose(&mut self, key: FlowKey, contribution: FlowFact) -> FactChange {
        self.record(key, &contribution);
        self.inner.propose(key, contribution)
    }

    fn propose_delta(
        &mut self,
        key: FlowKey,
        contribution: FlowFact,
        delivered: &mut FlowFact,
    ) -> FactChange {
        self.record(key, &contribution);
        self.inner.propose_delta(key, contribution, delivered)
    }

    fn assume(&mut self, dependencies: &ProofDependencies) {
        self.inner.assume(dependencies);
    }

    fn read_final(&mut self, key: FlowKey) -> Option<&FlowFact> {
        self.inner.read_final(key)
    }

    fn take_final(&mut self, key: FlowKey) -> Option<FlowFact> {
        self.inner.take_final(key)
    }
}
