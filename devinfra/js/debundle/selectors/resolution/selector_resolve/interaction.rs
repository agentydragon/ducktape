//! Splitting a projection into independent groups and deciding the ones that need no solver.

use std::collections::{BTreeMap, BTreeSet};

use selector_ir::{
    ClaimOutcome, ResolvedClaim, SelectorAtom, SelectorTargetId, SelectorVariableId, SolverClaim,
};
use source_identity::SourceUnitOrdinal;

use crate::projection::{Place, Projection};

impl Projection {
    /// The program's targets, grouped so that no two groups interact: no
    /// atom relates them, and they can never take the same place. A target
    /// whose places are unknown before the solve (a relational selector)
    /// interacts with every target it is kept distinct from.
    pub(crate) fn interacting_groups(&self) -> Vec<BTreeSet<SelectorTargetId>> {
        let program = &self.program;
        let mut sets = UnionFind::new(program.variables.len());
        for variables in program.atoms.iter().map(SelectorAtom::variable_ids) {
            if let Some(first) = variables.first() {
                for variable in &variables {
                    sets.union(*first, *variable);
                }
            }
        }
        let places_by_target = self
            .projected
            .iter()
            .flat_map(|entity| {
                let places = entity.rows.iter().flatten().collect::<BTreeSet<_>>();
                entity
                    .targets
                    .iter()
                    .map(move |target| (*target, places.clone()))
            })
            .chain(
                self.pin_places
                    .iter()
                    .map(|(target, places)| (*target, places.iter().collect())),
            )
            .collect::<BTreeMap<_, _>>();
        for distinct in &program.all_different {
            let source_unit = |target: &SelectorTargetId| program.targets[target.0].source_unit;
            let mut by_source_unit = BTreeMap::new();
            let mut by_binding = BTreeMap::new();
            for target in distinct {
                let Some(places) = places_by_target.get(target) else {
                    for other in distinct {
                        sets.union(source_unit(target), source_unit(other));
                    }
                    continue;
                };
                for place in places {
                    let first = *by_source_unit
                        .entry(place.source_unit)
                        .or_insert(source_unit(target));
                    sets.union(first, source_unit(target));
                    if let Some(binding) = &place.binding {
                        let first = *by_binding.entry(binding).or_insert(source_unit(target));
                        sets.union(first, source_unit(target));
                    }
                }
            }
        }
        let mut groups = BTreeMap::<SelectorVariableId, BTreeSet<SelectorTargetId>>::new();
        for target in &program.targets {
            groups
                .entry(sets.find(target.source_unit))
                .or_default()
                .insert(target.id);
        }
        let mut groups = groups.into_values().collect::<Vec<_>>();
        groups.sort();
        groups
    }

    /// The claims of `group` when it can be decided without the solver: it
    /// is one `source_match` or anonymous statement, whose candidates are
    /// exactly its own rows, or one name pin with one place.
    pub(crate) fn decide_alone(
        &self,
        group: &BTreeSet<SelectorTargetId>,
    ) -> Option<Vec<SolverClaim>> {
        let claim = |place: &Place| ResolvedClaim {
            source_unit: place.source_unit,
            statement_ordinal: SourceUnitOrdinal(place.source_unit.0),
            binding: place.binding.clone(),
        };
        if let Some(entity) = self.projected.iter().find(|entity| {
            entity.targets.len() == group.len()
                && entity.targets.iter().all(|target| group.contains(target))
        }) {
            return Some(
                entity
                    .targets
                    .iter()
                    .enumerate()
                    .map(|(position, target)| {
                        // Rows of a multi-binding entity can agree on one
                        // binding's place and differ on another's; each
                        // target is decided by its own distinct places, as
                        // the solver's per-target support search does.
                        let places = entity
                            .rows
                            .iter()
                            .map(|row| &row[position])
                            .collect::<BTreeSet<_>>();
                        SolverClaim {
                            target: *target,
                            outcome: match places.len() {
                                1 => ClaimOutcome::Unique {
                                    claim: claim(places.first().expect("one place")),
                                },
                                _ => ClaimOutcome::Ambiguous {
                                    candidates: places.into_iter().map(claim).collect(),
                                    candidates_truncated: false,
                                },
                            },
                        }
                    })
                    .collect(),
            );
        }
        let [target] = <[_; 1]>::try_from(group.iter().copied().collect::<Vec<_>>()).ok()?;
        let places = self.pin_places.get(&target)?;
        let outcome = match places.len() {
            0 => ClaimOutcome::NoMatch,
            1 => ClaimOutcome::Unique {
                claim: claim(places.first().expect("one place")),
            },
            _ => return None,
        };
        Some(vec![SolverClaim { target, outcome }])
    }
}

/// Disjoint sets of selector variables.
struct UnionFind {
    parent: Vec<usize>,
}

impl UnionFind {
    fn new(len: usize) -> Self {
        Self {
            parent: (0..len).collect(),
        }
    }

    fn find(&mut self, variable: SelectorVariableId) -> SelectorVariableId {
        let mut root = variable.0;
        while self.parent[root] != root {
            root = self.parent[root];
        }
        let mut node = variable.0;
        while self.parent[node] != root {
            node = std::mem::replace(&mut self.parent[node], root);
        }
        SelectorVariableId(root)
    }

    fn union(&mut self, left: SelectorVariableId, right: SelectorVariableId) {
        let (left, right) = (self.find(left), self.find(right));
        self.parent[left.0.max(right.0)] = left.0.min(right.0);
    }
}
