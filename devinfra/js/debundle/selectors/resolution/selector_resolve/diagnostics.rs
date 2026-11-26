//! Repair hints on unresolved outcomes: nearest unclaimed statements and candidate differentiators.

use std::collections::BTreeSet;

use anyhow::Result;
use selector_outcome::{Differentiator, NearMiss, Outcome};
use shape_index::ShapeIndex;
use swc_ecma_ast::Module;

use crate::{Chunk, EntityIndex, MemberSelector, Resolution, SpecModule};

/// Near misses scoring below this are too far off to help a repair.
const NEAREST_UNCLAIMED_MIN_SCORE: usize = 30;
const NEAREST_UNCLAIMED_LIMIT: usize = 3;

/// Gives each `no_match` template entity the top-level statements, among
/// those no entity of the chunk claimed, that its template comes closest to.
pub(crate) fn add_nearest_unclaimed(
    chunk: &Chunk<'_>,
    modules: &[SpecModule],
    resolution: &mut Resolution,
) -> Result<()> {
    let claimed = resolution
        .outcomes
        .iter()
        .filter_map(|entity| match &entity.outcome.outcome {
            Outcome::Resolved { body_index, .. } => Some(*body_index),
            _ => None,
        })
        .collect::<BTreeSet<_>>();
    let unclaimed = (0..chunk.module.body.len())
        .filter(|body_idx| !claimed.contains(body_idx))
        .collect::<Vec<_>>();
    for entity in &mut resolution.outcomes {
        let Outcome::NoMatch { nearest_unclaimed } = &mut entity.outcome.outcome else {
            continue;
        };
        let module = &modules[entity.module];
        let template = match entity.entity {
            EntityIndex::Member(member_index) => match &module.members[member_index].selector {
                MemberSelector::SourceMatch(parsed) => Some(parsed),
                _ => None,
            },
            EntityIndex::AnonymousStatement(index) => module
                .anonymous_statements
                .iter()
                .find(|statement| statement.index == index)
                .map(|statement| &statement.selector),
        };
        let Some(template) = template else {
            continue;
        };
        *nearest_unclaimed = source_match_diagnostics::fact_near_misses(
            chunk.module,
            chunk
                .near_miss_indices
                .get_or_init(|| source_match_diagnostics::ItemMatchIndices::new(chunk.module)),
            template,
            unclaimed.iter().copied(),
            NEAREST_UNCLAIMED_MIN_SCORE,
            NEAREST_UNCLAIMED_LIMIT,
        )?
        .into_iter()
        .map(|near_miss| NearMiss {
            body_index: near_miss.body_idx,
            bindings: near_miss.declared_bindings,
            score: near_miss.score,
            reason: near_miss.reason,
        })
        .collect();
    }
    Ok(())
}

/// Gives each `ambiguous` entity whose candidates are all listed the anchor
/// that sets each candidate's statement apart from the other candidates':
/// its own best distinguishing feature, else one the statement just before it
/// has and the statements just before the others lack, else likewise after.
/// Candidates sharing a statement get none.
pub(crate) fn add_differentiators(chunk: &Chunk<'_>, resolution: &mut Resolution) {
    let body = &chunk.module.body;
    for entity in &mut resolution.outcomes {
        let Outcome::Ambiguous {
            candidates,
            truncated: false,
            differentiators,
        } = &mut entity.outcome.outcome
        else {
            continue;
        };
        let mut candidate_body_indices = BTreeSet::new();
        let mut shared = BTreeSet::new();
        for candidate in candidates.iter() {
            if !candidate_body_indices.insert(candidate.body_index) {
                shared.insert(candidate.body_index);
            }
        }
        let mut pending = candidate_body_indices
            .difference(&shared)
            .copied()
            .collect::<BTreeSet<_>>();
        for offset in [0, -1, 1] {
            if pending.is_empty() {
                break;
            }
            let statements = candidate_body_indices
                .iter()
                .filter_map(|&candidate_body_index| {
                    let anchor_body_index = candidate_body_index
                        .checked_add_signed(offset)
                        .filter(|statement| *statement < body.len())?;
                    Some((candidate_body_index, anchor_body_index))
                })
                .collect::<Vec<_>>();
            let index = ShapeIndex::new(&Module {
                span: Default::default(),
                body: statements
                    .iter()
                    .map(|(_, statement)| body[*statement].clone())
                    .collect(),
                shebang: None,
            });
            for (item, &(candidate_body_index, anchor_body_index)) in statements.iter().enumerate()
            {
                if !pending.contains(&candidate_body_index) {
                    continue;
                }
                if let Some(feature) = index.distinguishing_feature(item) {
                    pending.remove(&candidate_body_index);
                    differentiators.push(Differentiator {
                        candidate_body_index,
                        anchor_body_index,
                        anchor: feature,
                    });
                }
            }
        }
        differentiators.sort();
    }
}
