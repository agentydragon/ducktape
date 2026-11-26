//! Recording each entity's outcome from the solver's claims, with how it was made unique.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail};
use js_ast::statement_for_source_unit_ordinal;
use selector_ir::{ClaimOutcome, ResolvedClaim, SelectorProgram, SelectorTargetId, SolverResult};
use selector_outcome::{
    Candidate, ConflictSourceUnit, Entity, EntityRef, Outcome, OwnershipConflict, ResolvedBy,
};
use source_identity::{SourceUnitId, SourceUnitOrdinal};
use swc_ecma_ast::Module;

use crate::projection::{Place, Projection, anonymous_entity_outcome, member_entity_outcome};
use crate::{Chunk, Resolution, SpecModule, selector_conflict};

impl Projection {
    /// Every entity's outcome, given the claims `result` holds for each
    /// target of the program.
    pub(crate) fn record(
        self,
        chunk: &Chunk<'_>,
        modules: &[SpecModule],
        result: &SolverResult,
    ) -> Result<Resolution> {
        let resolved_by = self.resolved_by(result);
        let mut unsatisfiable = self.unsatisfiable_outcomes(chunk.module, result)?;
        let Self {
            mut outcomes,
            members,
            anonymous,
            member_references,
            pin_places,
            templates,
            ..
        } = self;
        let module = chunk.module;
        for (target, module_index, position) in anonymous {
            let outcome = match result.outcome_for(target) {
                Some(ClaimOutcome::Unique { claim }) => Outcome::Resolved {
                    body_index: claim_candidate(module, claim)?.body_index,
                    binding: None,
                    resolved_by: how_resolved(&resolved_by, target),
                },
                Some(ClaimOutcome::Unsatisfiable) => unsatisfiable
                    .remove(&target)
                    .expect("every infeasible target has a group diagnostic"),
                Some(outcome) => claim_outcome(module, outcome)?,
                None => bail!(
                    "logical_module {}::{}: global selector solver returned no outcome for anonymous \
                     statement selector",
                    chunk.name,
                    modules[module_index].path,
                ),
            };
            outcomes.push(anonymous_entity_outcome(
                &chunk.name,
                modules,
                module_index,
                position,
                outcome,
            ));
        }
        for (target, (module_index, member_index)) in members {
            // Recorded `no_match` by the projection.
            if pin_places.get(&target).is_some_and(BTreeSet::is_empty) {
                continue;
            }
            let member = &modules[module_index].members[member_index];
            let outcome = match result.outcome_for(target) {
                Some(ClaimOutcome::Unique { claim }) => {
                    let Candidate {
                        body_index,
                        binding,
                    } = claim_candidate(module, claim)?;
                    Outcome::Resolved {
                        body_index,
                        binding,
                        resolved_by: how_resolved(&resolved_by, target),
                    }
                }
                Some(ClaimOutcome::Unsatisfiable) => unsatisfiable
                    .remove(&target)
                    .expect("every infeasible target has a group diagnostic"),
                Some(outcome) => claim_outcome(module, outcome)?,
                None => bail!(
                    "logical_module {}::{}: global selector solver returned no outcome for selector \
                     member `{}`",
                    chunk.name,
                    modules[module_index].path,
                    member.export_name,
                ),
            };
            outcomes.push(member_entity_outcome(
                &chunk.name,
                modules,
                module_index,
                member_index,
                outcome,
            ));
        }
        Ok(Resolution {
            outcomes,
            templates,
            member_references,
        })
    }

    /// Explain only already-proven infeasible groups. The candidate graph is
    /// a relaxation of the CSP: success proves nothing about satisfiability,
    /// but a deficient subset is a sufficient ownership contradiction.
    fn unsatisfiable_outcomes(
        &self,
        module: &Module,
        result: &SolverResult,
    ) -> Result<BTreeMap<SelectorTargetId, Outcome>> {
        let mut outcomes = BTreeMap::new();
        if !result
            .claims
            .iter()
            .any(|claim| matches!(claim.outcome, ClaimOutcome::Unsatisfiable))
        {
            return Ok(outcomes);
        }
        let original = self
            .projected
            .iter()
            .flat_map(|entity| {
                entity
                    .targets
                    .iter()
                    .copied()
                    .zip(&entity.original_source_units)
            })
            .collect::<BTreeMap<_, _>>();
        for group in self.interacting_groups() {
            if !group.iter().any(|target| {
                matches!(
                    result.outcome_for(*target),
                    Some(ClaimOutcome::Unsatisfiable)
                )
            }) {
                continue;
            }
            let diagnostic = Outcome::Unsatisfiable {
                group: target_entity_ref(
                    &self.program.targets[group.first().expect("nonempty group").0],
                ),
                witness: self.ownership_witness(module, &group, &original)?,
            };
            for target in group {
                if matches!(
                    result.outcome_for(target),
                    Some(ClaimOutcome::Unsatisfiable)
                ) {
                    outcomes.insert(target, diagnostic.clone());
                }
            }
        }
        Ok(outcomes)
    }

    /// The first `all_different` class whose known domains within `group`
    /// leave fewer source units than claims, as an ownership contradiction.
    fn ownership_witness(
        &self,
        module: &Module,
        group: &BTreeSet<SelectorTargetId>,
        original: &BTreeMap<SelectorTargetId, &BTreeMap<SourceUnitId, BTreeSet<String>>>,
    ) -> Result<Option<OwnershipConflict>> {
        // Use exactly the IR's exclusivity classes. In particular, do not
        // turn the members of one binding group into competing claims.
        for distinct in &self.program.all_different {
            let targets = distinct
                .iter()
                .filter(|target| group.contains(target) && original.contains_key(target))
                .copied()
                .collect::<Vec<_>>();
            // Unknown relational domains are omitted. Proving a known
            // subset contradictory is sound; guessing their domains is not.
            let domains = targets
                .iter()
                .map(|target| {
                    original[target]
                        .keys()
                        .map(|source_unit| source_unit.0)
                        .collect::<Vec<_>>()
                })
                .collect::<Vec<_>>();
            let Some(deficient) = selector_conflict::deficient_set(&domains) else {
                continue;
            };
            let selectors = deficient
                .iter()
                .map(|index| target_entity_ref(&self.program.targets[targets[*index].0]))
                .collect();
            let mut source_units = BTreeMap::<SourceUnitId, BTreeSet<String>>::new();
            for index in deficient {
                for (source_unit, bindings) in original[&targets[index]] {
                    source_units
                        .entry(*source_unit)
                        .or_default()
                        .extend(bindings.iter().cloned());
                }
            }
            let source_units = source_units
                .into_iter()
                .map(|(source_unit, bindings)| {
                    Ok(ConflictSourceUnit {
                        source_unit: source_unit.0,
                        body_index: statement_for_source_unit_ordinal(
                            &module.body,
                            SourceUnitOrdinal(source_unit.0),
                        )
                        .context("conflict source unit has no source statement")?
                        .0,
                        bindings: bindings.into_iter().collect(),
                    })
                })
                .collect::<Result<Vec<_>>>()?;
            return Ok(Some(OwnershipConflict {
                selectors,
                source_units,
            }));
        }
        Ok(None)
    }

    /// How each resolved projected target was made unique, where not by its
    /// own selector: of the rows that agree with the solved bindings of the
    /// entities its template references, exactly one (by its references), or
    /// exactly one no other exclusive target's solved claim takes (by
    /// elimination).
    fn resolved_by(&self, result: &SolverResult) -> BTreeMap<SelectorTargetId, ResolvedBy> {
        let mut exclusive = self
            .program
            .all_different
            .iter()
            .flatten()
            .copied()
            .collect::<BTreeSet<_>>();
        // `all_different` names one representative per `source_matches[]`
        // group; the group's other bindings are claimed with it.
        for entity in &self.projected {
            if entity
                .targets
                .iter()
                .any(|target| exclusive.contains(target))
            {
                exclusive.extend(entity.targets.iter().copied());
            }
        }
        let solved_binding = |target: SelectorTargetId| match result.outcome_for(target) {
            Some(ClaimOutcome::Unique { claim }) => claim.binding.as_deref(),
            _ => None,
        };
        let mut resolved = BTreeMap::new();
        for entity in &self.projected {
            if entity.unreferenced_rows < 2
                || !entity.targets.iter().all(|target| {
                    matches!(
                        result.outcome_for(*target),
                        Some(ClaimOutcome::Unique { .. })
                    )
                })
            {
                continue;
            }
            let agreeing = (0..entity.rows.len())
                .filter(|at| {
                    entity.references.iter().all(|reference| {
                        reference
                            .target
                            .and_then(solved_binding)
                            .is_none_or(|binding| reference.values[*at] == binding)
                    })
                })
                .map(|at| &entity.rows[at])
                .collect::<Vec<_>>();
            let distinct = agreeing.iter().collect::<BTreeSet<_>>().len();
            let how = if !entity.references.is_empty() && distinct == 1 {
                Some(ResolvedBy::OwnReferences {
                    references: entity
                        .references
                        .iter()
                        .map(|reference| reference.entity.clone())
                        .collect(),
                })
            } else {
                elimination_claimers(&agreeing, &entity.targets, result, &exclusive)
                    .map(|claimers| ResolvedBy::Elimination {
                        claimers: target_entity_refs(&self.program, &claimers),
                    })
                    .or_else(|| {
                        // Several of its places survive every claim, yet it
                        // resolved: a template naming it picked one.
                        let referrers = self
                            .projected
                            .iter()
                            .filter(|referrer| {
                                referrer.references.iter().any(|reference| {
                                    reference
                                        .target
                                        .is_some_and(|target| entity.targets.contains(&target))
                                })
                            })
                            .flat_map(|referrer| referrer.targets.iter().copied())
                            .collect::<BTreeSet<_>>();
                        (!referrers.is_empty()).then(|| ResolvedBy::ReferencedBy {
                            referrers: target_entity_refs(&self.program, &referrers),
                        })
                    })
            };
            if let Some(how) = how {
                for target in &entity.targets {
                    resolved.insert(*target, how.clone());
                }
            }
        }
        resolved
    }
}

/// How `target`, resolved, was made unique, given [`Projection::resolved_by`].
fn how_resolved(
    resolved_by: &BTreeMap<SelectorTargetId, ResolvedBy>,
    target: SelectorTargetId,
) -> ResolvedBy {
    resolved_by
        .get(&target)
        .cloned()
        .unwrap_or(ResolvedBy::OwnSelector)
}

/// The targets whose solved claims made an entity unique: `Some` when it has
/// several candidate `rows` and exactly one survives dropping every row whose
/// source unit or binding another exclusive target's solved value holds.
/// `exclusive` are the targets the solve keeps on distinct source units; a claim by
/// any other target never took a row away.
fn elimination_claimers(
    rows: &[&Vec<Place>],
    targets: &[SelectorTargetId],
    result: &SolverResult,
    exclusive: &BTreeSet<SelectorTargetId>,
) -> Option<BTreeSet<SelectorTargetId>> {
    if rows.len() < 2 {
        return None;
    }
    let other_claims = exclusive
        .iter()
        .filter(|target| !targets.contains(target))
        .filter_map(|target| match result.outcome_for(*target) {
            Some(ClaimOutcome::Unique { claim }) => Some((*target, claim)),
            _ => None,
        })
        .collect::<Vec<_>>();
    let mut survivors = 0;
    let mut claimers = BTreeSet::new();
    for row in rows {
        let takers = other_claims
            .iter()
            .filter(|(_, claim)| {
                row.iter().any(|place| {
                    place.source_unit == claim.source_unit
                        || (place.binding.is_some() && place.binding == claim.binding)
                })
            })
            .map(|(target, _)| *target)
            .collect::<Vec<_>>();
        if takers.is_empty() {
            survivors += 1;
        }
        claimers.extend(takers);
    }
    (survivors == 1).then_some(claimers)
}

/// A solver claim as a candidate place: its source body index and binding.
fn claim_candidate(module: &Module, claim: &ResolvedClaim) -> Result<Candidate> {
    Ok(Candidate {
        body_index: statement_for_source_unit_ordinal(&module.body, claim.statement_ordinal)
            .with_context(|| {
                format!(
                    "global selector solver claimed post-split ordinal {} which has no source \
                     body item",
                    claim.statement_ordinal.0
                )
            })?
            .0,
        binding: claim.binding.clone(),
    })
}

/// The outcome of a target the solve did not resolve.
fn claim_outcome(module: &Module, outcome: &ClaimOutcome) -> Result<Outcome> {
    Ok(match outcome {
        ClaimOutcome::NoMatch => Outcome::no_match(),
        ClaimOutcome::Unsatisfiable => {
            unreachable!("infeasible groups are diagnosed with their projection")
        }
        ClaimOutcome::Ambiguous {
            candidates,
            candidates_truncated,
        } => Outcome::ambiguous(
            candidates
                .iter()
                .map(|claim| claim_candidate(module, claim))
                .collect::<Result<_>>()?,
            *candidates_truncated,
        ),
        ClaimOutcome::Undecided { reason } => Outcome::Undecided { reason: *reason },
        ClaimOutcome::Unique { .. } => {
            unreachable!("resolved claims are handled by the caller")
        }
    })
}

/// Names a solver target the way its own outcome names it.
fn target_entity_ref(target: &selector_ir::SelectorTarget) -> EntityRef {
    EntityRef {
        logical_module: target.logical_module.clone(),
        entity: match &target.claim {
            selector_ir::ClaimKind::Binding {
                export_name: Some(export_name),
            }
            | selector_ir::ClaimKind::BindingGroupMember { export_name } => {
                Some(Entity::Export(export_name.clone()))
            }
            selector_ir::ClaimKind::AnonymousStatement { index } => {
                Some(Entity::AnonymousStatement(*index))
            }
            selector_ir::ClaimKind::Binding { export_name: None } => None,
        },
    }
}

fn target_entity_refs<'a>(
    program: &SelectorProgram,
    targets: impl IntoIterator<Item = &'a SelectorTargetId>,
) -> Vec<EntityRef> {
    targets
        .into_iter()
        .map(|target| target_entity_ref(&program.targets[target.0]))
        .collect()
}
