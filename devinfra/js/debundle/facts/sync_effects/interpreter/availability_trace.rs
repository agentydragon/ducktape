//! Lossless run-length encoding of repeated live-cell observations.

use crate::program_execution::effects::AvailabilityState;
use std::collections::BTreeSet;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct AvailabilityTrace(Vec<Run>);

#[derive(Debug, Clone, PartialEq, Eq)]
struct Run {
    states: BTreeSet<AvailabilityState>,
    count: usize,
}

impl AvailabilityTrace {
    pub(super) fn push(&mut self, states: &BTreeSet<AvailabilityState>) {
        self.append(states, 1);
    }

    fn append(&mut self, states: &BTreeSet<AvailabilityState>, count: usize) {
        if let Some(last) = self.0.last_mut()
            && last.states == *states
        {
            last.count = last
                .count
                .checked_add(count)
                .expect("availability trace length");
        } else {
            self.0.push(Run {
                states: states.clone(),
                count,
            });
        }
    }

    pub(super) fn extend(&mut self, source: &Self, assumed: bool) {
        if assumed {
            let count = source
                .0
                .iter()
                .try_fold(0usize, |total, run| total.checked_add(run.count))
                .expect("availability trace length");
            if count != 0 {
                self.append(&BTreeSet::from([AvailabilityState::Assumed]), count);
            }
        } else {
            for run in &source.0 {
                self.append(&run.states, run.count);
            }
        }
    }

    pub(crate) fn states(&self) -> impl Iterator<Item = &BTreeSet<AvailabilityState>> {
        self.0.iter().map(|run| &run.states)
    }

    pub(super) fn observed(&self) -> impl Iterator<Item = &BTreeSet<AvailabilityState>> {
        self.0
            .iter()
            .filter(|run| !run.states.contains(&AvailabilityState::Assumed))
            .flat_map(|run| std::iter::repeat_n(&run.states, run.count))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn concatenation_preserves_order_count_and_assumed_provenance() {
        let initialized = BTreeSet::from([AvailabilityState::InitializedValue]);
        let unknown = BTreeSet::from([AvailabilityState::Unknown]);
        let mut trace = AvailabilityTrace::default();
        for states in [&initialized, &initialized, &unknown, &initialized] {
            trace.push(states);
        }
        let original = trace.clone();
        trace.extend(&original, false);
        assert_eq!(trace.0.len(), 5);
        assert_eq!(
            trace.observed().collect::<Vec<_>>(),
            vec![
                &initialized,
                &initialized,
                &unknown,
                &initialized,
                &initialized,
                &initialized,
                &unknown,
                &initialized,
            ]
        );
        trace.extend(&original, true);
        assert_eq!(trace.0.last().unwrap().count, 4);
        assert_eq!(
            trace.0.last().unwrap().states,
            BTreeSet::from([AvailabilityState::Assumed])
        );
        assert_eq!(trace.observed().count(), 8);
        let mut fewer = original.clone();
        fewer.push(&initialized);
        assert_ne!(fewer, original);
    }
}
