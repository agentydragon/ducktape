//! Synthesis of the simplest proven selector for one declaration group.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use js_ast::item_decl;
use selector_render::holes_present;
use source_match_holes::STMT_LIST_HOLE_KEYWORD;
use swc_ecma_ast::*;
use synthesis_failure::{ProofFailure, SynthesisSkipReason};

use crate::chunk_index::{ChunkSelectorIndex, IndexedDeclaration};
use crate::minimize::{
    collapse_statement_hole_runs, exact_declaration_rejection, exact_declaration_source,
    minimize_class_selector_candidates, minimize_function_selector_candidates,
    minimize_var_group_selector_candidates, relax_exact_declaration,
};
use crate::name_binding_rewrite::NameBindingMember;
use crate::proof::prove_synthesized_selector;
use crate::report::SelectorAlternative;
use crate::shape_runs::selector_shape_signature;

#[derive(Debug, Clone)]
pub struct SynthesizedSelectorGroup {
    pub(crate) body_idx: usize,
    pub(crate) target_bindings: Vec<SynthesizedTargetBinding>,
    pub(crate) match_source: String,
    pub(crate) rewritten_holes: Vec<String>,
    pub(crate) alternatives: Vec<SelectorAlternative>,
}

#[derive(Debug, Clone)]
pub struct SynthesizedTargetBinding {
    pub(crate) export_name: String,
    pub(crate) runtime_binding: String,
    /// Identifier used within the alpha-matched selector, independent of the
    /// readable name exported by the claim.
    pub(crate) selector_local: String,
}

/// One synthesized selector together with the members it covers and the
/// representative (first) declaration it was proven against. The anti-unification
/// grouping pass ([`merge_adjacent_same_shape_runs`]) operates on these: a
/// single-declaration group may merge with adjacent same-shape neighbors into a
/// run-based group whose `synthesized` spans several declarations.
#[derive(Debug, Clone)]
pub(crate) struct SynthesizedDeclGroup {
    pub(crate) decl_idx: usize,
    pub(crate) members: Vec<NameBindingMember>,
    pub(crate) synthesized: SynthesizedSelectorGroup,
}

/// Outcome of trying to synthesize a selector for one declaration group.
///
/// `Skipped` is returned when neither sparse read-off nor exact-declaration
/// relaxation produces a proving selector.
pub(crate) enum GroupSelectorOutcome {
    Synthesized(SynthesizedSelectorGroup),
    Skipped(SynthesisSkipReason),
}

pub(crate) struct RequestedDeclGroup {
    pub(crate) decl_idx: usize,
    pub(crate) members: Vec<NameBindingMember>,
    pub(crate) outcome: Result<GroupSelectorOutcome>,
}

fn synthesis_targets(members: &[NameBindingMember]) -> Vec<SynthesizedTargetBinding> {
    members
        .iter()
        .map(|member| SynthesizedTargetBinding {
            export_name: member.export_name.clone(),
            runtime_binding: member.binding_name.clone(),
            selector_local: member.export_name.clone(),
        })
        .collect()
}

/// Individual declarations need not be independently identifiable. When a
/// requested maximal run has the same shape, prove that run jointly before
/// reporting its ambiguous members. Only requested declarations participate;
/// gaps and destination boundaries cannot acquire extra claims. There is one
/// joint proof per run, never a search through subwindows or binding subsets.
pub(crate) fn synthesize_requested_declaration_groups(
    index: &ChunkSelectorIndex<'_>,
    groups: BTreeMap<usize, Vec<NameBindingMember>>,
    candidates_limit: usize,
) -> Result<Vec<RequestedDeclGroup>> {
    let prepared: Vec<_> = groups
        .into_iter()
        .map(|(decl_idx, members)| {
            let outcome =
                synthesize_simplest_selector_for_group(index, decl_idx, &members, candidates_limit);
            RequestedDeclGroup {
                decl_idx,
                members,
                outcome,
            }
        })
        .collect();
    if !prepared
        .iter()
        .any(|group| matches!(&group.outcome, Ok(GroupSelectorOutcome::Skipped(_))))
    {
        return Ok(prepared);
    }

    struct RunItem {
        group: RequestedDeclGroup,
        exact: Option<String>,
        shape: Option<String>,
    }
    let mut items = prepared
        .into_iter()
        .map(|group| {
            let declaration = &index.decls[group.decl_idx];
            let exact = exact_declaration_source(
                &index.module.body[declaration.body_idx],
                &synthesis_targets(&group.members),
            )?;
            let shape = exact.as_deref().and_then(selector_shape_signature);
            Ok(RunItem {
                group,
                exact,
                shape,
            })
        })
        .collect::<Result<Vec<_>>>()?
        .into_iter()
        .peekable();
    let mut result = Vec::new();
    while let Some(first) = items.next() {
        let mut run = vec![first];
        while let Some(next) = items.peek() {
            let previous = run.last().expect("run contains its first item");
            let previous_decl = &index.decls[previous.group.decl_idx];
            let next_decl = &index.decls[next.group.decl_idx];
            if previous_decl.kind != next_decl.kind
                || previous_decl.body_idx + 1 != next_decl.body_idx
                || previous.shape.is_none()
                || previous.shape != next.shape
            {
                break;
            }
            run.push(items.next().expect("peeked item exists"));
        }
        if run.len() >= 2
            && run.iter().all(|item| item.group.outcome.is_ok())
            && run
                .iter()
                .any(|item| matches!(&item.group.outcome, Ok(GroupSelectorOutcome::Skipped(_))))
        {
            let targets: Vec<_> = run
                .iter()
                .flat_map(|item| synthesis_targets(&item.group.members))
                .collect();
            let source = run
                .iter()
                .map(|item| item.exact.as_deref().expect("a shape has exact source"))
                .collect::<Vec<_>>()
                .join("\n");
            let decl_idx = run[0].group.decl_idx;
            let declaration = &index.decls[decl_idx];
            if prove_synthesized_selector(index, declaration, &targets, &source).is_ok() {
                result.push(RequestedDeclGroup {
                    decl_idx,
                    members: run
                        .into_iter()
                        .flat_map(|item| item.group.members)
                        .collect(),
                    outcome: Ok(GroupSelectorOutcome::Synthesized(
                        SynthesizedSelectorGroup {
                            body_idx: declaration.body_idx,
                            target_bindings: targets,
                            rewritten_holes: holes_present(&source)?.into_iter().collect(),
                            match_source: source,
                            alternatives: Vec::new(),
                        },
                    )),
                });
                continue;
            }
        }
        result.extend(run.into_iter().map(|item| item.group));
    }
    Ok(result)
}

pub(crate) fn synthesize_simplest_selector_for_group(
    index: &ChunkSelectorIndex<'_>,
    decl_idx: usize,
    members: &[NameBindingMember],
    candidates_limit: usize,
) -> Result<GroupSelectorOutcome> {
    let decl = index
        .decls
        .get(decl_idx)
        .with_context(|| format!("missing indexed declaration {decl_idx}"))?;
    let item = index
        .module
        .body
        .get(decl.body_idx)
        .with_context(|| format!("missing source body index {}", decl.body_idx))?;
    let mut targets = synthesis_targets(members);
    // Try readable selector locals first. The var renderer rewrites uses of
    // selected bindings inside sibling initializers to the same local names.
    let primary =
        |targets: &[SynthesizedTargetBinding]| match synthesize_specialized_selector_candidates(
            index, item, decl, targets, 1,
        )?
        .into_iter()
        .next()
        {
            Some(selector) => Ok(Some(selector)),
            None => relax_exact_declaration(index, item, decl, targets),
        };
    let mut specialized = primary(&targets)?;
    // The old source spelling remains a fallback for name-only codemods.
    if specialized.is_none() && targets.len() > 1 {
        for target in &mut targets {
            target.selector_local = target.runtime_binding.clone();
        }
        specialized = primary(&targets)?;
    }
    let Some(specialized) = specialized else {
        return Ok(GroupSelectorOutcome::Skipped(exact_declaration_rejection(
            index, item, decl, &targets,
        )?));
    };
    // The rendered selector is JavaScript. Trailing spaces inside template
    // literals are data, so a line-based whitespace trim can invalidate a
    // selector that the resolver already proved.
    let mut match_source = specialized.match_source;
    if specialized.rewritten_holes.contains(STMT_LIST_HOLE_KEYWORD) {
        let collapsed = collapse_statement_hole_runs(&match_source)?;
        if prove_synthesized_selector(index, decl, &targets, &collapsed).is_ok() {
            match_source = collapsed;
        }
    }
    if let Err(error) = prove_synthesized_selector(index, decl, &targets, &match_source) {
        return Ok(GroupSelectorOutcome::Skipped(
            SynthesisSkipReason::NormalizationRejected {
                failure: ProofFailure::from_error(error)?,
            },
        ));
    };
    let rewritten_holes = specialized.rewritten_holes;

    // `--candidates N > 1`: collect the rest of the ranked read-off menu beyond the
    // primary pick (deduped against it).
    let alternatives = if candidates_limit > 1 {
        synthesize_specialized_selector_candidates(index, item, decl, &targets, candidates_limit)?
            .into_iter()
            .map(|candidate| SelectorAlternative {
                match_source: candidate.match_source,
                rewritten_holes: candidate.rewritten_holes.into_iter().collect(),
            })
            .filter(|alternative| alternative.match_source != match_source)
            .collect()
    } else {
        Vec::new()
    };

    Ok(GroupSelectorOutcome::Synthesized(
        SynthesizedSelectorGroup {
            body_idx: decl.body_idx,
            target_bindings: targets,
            match_source,
            rewritten_holes: rewritten_holes.into_iter().collect(),
            alternatives,
        },
    ))
}

pub(crate) struct SpecializedSelector {
    pub(crate) match_source: String,
    pub(crate) rewritten_holes: BTreeSet<String>,
}

/// Up to `limit` ranked candidate selectors for the item — the
/// `synthesize-selectors --candidates N` menu.
fn synthesize_specialized_selector_candidates(
    index: &ChunkSelectorIndex<'_>,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    limit: usize,
) -> Result<Vec<SpecializedSelector>> {
    match (item_decl(item), targets) {
        (Some(Decl::Fn(function)), [target]) => {
            minimize_function_selector_candidates(index, &function.function, decl, target, limit)
        }
        (Some(Decl::Class(class)), [target]) => {
            minimize_class_selector_candidates(index, &class.class, decl, target, limit)
        }
        (Some(Decl::Var(var)), _) => {
            minimize_var_group_selector_candidates(index, var, decl, targets, limit)
        }
        _ => Ok(Vec::new()),
    }
}
