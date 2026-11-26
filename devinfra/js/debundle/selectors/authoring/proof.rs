//! The proof gate: a synthesized selector must resolve with the shared selector resolve.

use std::collections::BTreeSet;

use anyhow::{Context, Result, bail};
use selector_outcome::{Outcome, ResolvedBy};
use selector_resolve::{EntityIndex, Member, MemberSelector, SpecModule};
use source_match::{MemberBindingMatch, ParsedSourceMatchSelector};
use spec::{SourceMatch, SourceMatchIdentifierMode};
use synthesis_failure::{ProofError, ProofFailure};

use crate::chunk_index::{ChunkSelectorIndex, IndexedDeclaration, SYNTHESIS_MODULE};
use crate::group_synthesis::SynthesizedTargetBinding;

/// `match_source` as a member selector projected onto `export_name`.
fn parse_member_selector(
    export_name: &str,
    match_source: &str,
) -> Result<ParsedSourceMatchSelector> {
    Ok(ParsedSourceMatchSelector::parse(
        SYNTHESIS_MODULE,
        format!("<source_match needle in {SYNTHESIS_MODULE}>"),
        &SourceMatch {
            match_source: match_source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: Some(export_name.to_string()),
        }
        .selector(),
        source_match::SelectorField::SourceMatch,
    )?)
}

/// Every place `match_source`, projected onto `export_name`, matches in the
/// chunk: the minimizer's measure of how far a candidate is from unique.
pub(crate) fn match_single_member_selector(
    index: &ChunkSelectorIndex<'_>,
    export_name: &str,
    match_source: &str,
) -> Result<Vec<MemberBindingMatch>> {
    Ok(index.chunk.matcher().member_candidates(
        SYNTHESIS_MODULE,
        &parse_member_selector(export_name, match_source)?,
    )?)
}

/// The proof gate: `match_source`, claiming every target by its export name,
/// resolves each target on its own selector to its intended runtime binding,
/// the first of them at `decl`. A selector unique only by elimination does not
/// prove: it would move when the claimer is edited.
pub(crate) fn prove_synthesized_selector(
    index: &ChunkSelectorIndex<'_>,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    match_source: &str,
) -> Result<()> {
    if targets.is_empty() {
        bail!(ProofError(ProofFailure::EmptyTargets));
    }
    let members = targets
        .iter()
        .map(|target| {
            Ok(Member {
                export_name: target.export_name.clone(),
                selector: MemberSelector::SourceMatch(parse_member_selector(
                    &target.selector_local,
                    match_source,
                )?),
            })
        })
        .collect::<Result<Vec<_>>>()?;
    let resolution = index.chunk.resolve(&[SpecModule {
        path: SYNTHESIS_MODULE.to_string(),
        members,
        anonymous_statements: Vec::new(),
    }])?;
    let mut first_body_index = usize::MAX;
    for (member_index, target) in targets.iter().enumerate() {
        let outcome = resolution
            .outcome(0, EntityIndex::Member(member_index))
            .context("a source_match member has an outcome")?;
        match &outcome.outcome {
            Outcome::Resolved {
                body_index,
                binding: Some(binding),
                resolved_by: ResolvedBy::OwnSelector,
            } if *binding == target.runtime_binding => {
                first_body_index = first_body_index.min(*body_index)
            }
            _ => {
                bail!(ProofError(ProofFailure::TargetNotProven {
                    binding: target.runtime_binding.clone(),
                    export_name: target.export_name.clone(),
                    outcome: Box::new(outcome.clone())
                }));
            }
        }
    }
    if first_body_index != decl.body_idx {
        bail!(ProofError(ProofFailure::WrongBodyIndex {
            actual: first_body_index,
            expected: decl.body_idx
        }));
    }
    Ok(())
}

/// Distinct body indices the selector resolves to (slot alignments within one
/// body collapse). Used by the read-off structural fast path
/// (the bare-scaffold branch of `read_off_candidates`).
pub(crate) fn matched_body_indices(
    index: &ChunkSelectorIndex<'_>,
    export_name: &str,
    match_source: &str,
) -> Result<BTreeSet<usize>> {
    Ok(
        match_single_member_selector(index, export_name, match_source)?
            .iter()
            .map(|candidate| candidate.body_idx)
            .collect(),
    )
}
