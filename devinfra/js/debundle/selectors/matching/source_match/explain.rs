//! Local explanation of one caller-selected source range. Candidate discovery,
//! spec ownership, reference resolution, and all-different belong to the
//! resolver; this module only asks whether the given range fits one template.

use super::chunk_resolver::{item_facts, single_declarator_item};
use super::*;

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExplainRangeKind {
    Member,
    BindingGroup,
    Anonymous,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RangeExplanationStatus {
    Matched,
    Mismatch,
    Unsupported,
    Limited,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RangeFailureCategory {
    Shape,
    Literal,
    Binding,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct RangeFailure {
    pub selector_statement_index: usize,
    pub source_statement_index: usize,
    /// Node IDs are local to the statement's fact projection.
    pub selector_node: u32,
    pub source_node: u32,
    pub category: RangeFailureCategory,
    pub reason: String,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct RangeExplanation {
    pub status: RangeExplanationStatus,
    /// Selector statement → position in the caller's selected source range.
    /// `None` marks a top-level `STMT_LIST` hole.
    pub aligned_statements: Vec<Option<usize>>,
    /// The selected source binding claimed by a member selector, when matched.
    pub matched_binding: Option<String>,
    /// Mappings observed within the template match. Free names still require
    /// the caller's chunk/spec-level reference checks.
    pub free_bindings: BTreeMap<String, String>,
    pub failure: Option<RangeFailure>,
    pub reason: Option<String>,
}

impl RangeExplanation {
    fn unsupported(reason: impl Into<String>) -> Self {
        Self {
            status: RangeExplanationStatus::Unsupported,
            aligned_statements: Vec::new(),
            matched_binding: None,
            free_bindings: BTreeMap::new(),
            failure: None,
            reason: Some(reason.into()),
        }
    }

    fn limited(reason: impl Into<String>) -> Self {
        Self {
            status: RangeExplanationStatus::Limited,
            aligned_statements: Vec::new(),
            matched_binding: None,
            free_bindings: BTreeMap::new(),
            failure: None,
            reason: Some(reason.into()),
        }
    }
}

/// Explain a parsed selector against exactly `selected`. A member uses the
/// production `ChunkResolver` candidate path to decide its verdict, retaining
/// per-declarator projection and target-binding rules. Anonymous ranges use
/// the same `selector_match::homo` relation with shared alpha state and
/// anchored top-level hole backtracking. Diagnostic descent also uses `homo`.
pub fn explain_range(
    parsed: &ParsedSourceMatchSelector,
    selected: &[ModuleItem],
    kind: ExplainRangeKind,
) -> RangeExplanation {
    let needle_indices = match parsed
        .body()
        .iter()
        .map(index_item)
        .collect::<Result<Vec<_>>>()
    {
        Ok(indices) => indices,
        Err(err) => return RangeExplanation::unsupported(err.to_string()),
    };
    let free = free_identifiers(&needle_indices);
    let mode = selector_mode(parsed.selector());
    if let Some(target) = parsed.selector().target_binding.as_ref()
        && free.contains(target)
    {
        return RangeExplanation::unsupported(format!(
            "target `{target}` is a free reference whose declaration cannot be checked locally; use --anonymous with the inline pattern to compare only its use-site shape"
        ));
    }
    // The member candidate resolver is the production matcher and has no work
    // budget. Constrain this diagnostic API before entering it. The limits do
    // not affect normal selector resolution.
    const MAX_RANGE_STATEMENTS: usize = 32;
    const MAX_RANGE_NODES: usize = 2_048;
    const MAX_RANGE_DECLARATORS: usize = 32;
    if needle_indices.len() > MAX_RANGE_STATEMENTS || selected.len() > MAX_RANGE_STATEMENTS {
        return RangeExplanation::limited(format!(
            "selector or selected source exceeds {MAX_RANGE_STATEMENTS} statements"
        ));
    }
    let subject_indices = match selected.iter().map(index_item).collect::<Result<Vec<_>>>() {
        Ok(indices) => indices,
        Err(err) => return RangeExplanation::unsupported(err.to_string()),
    };
    let nodes: usize = needle_indices
        .iter()
        .chain(&subject_indices)
        .map(selector_match::Index::node_count)
        .sum();
    if nodes > MAX_RANGE_NODES {
        return RangeExplanation::limited(format!(
            "selector plus selected source exceeds {MAX_RANGE_NODES} matcher nodes"
        ));
    }
    if needle_indices
        .iter()
        .chain(&subject_indices)
        .any(|index| !selector_match::diagnostic_shape_bounded(index))
    {
        return RangeExplanation::limited("selected range exceeds the nesting or hole limit");
    }
    let declarators: usize = selected
        .iter()
        .filter_map(item_var_decl)
        .map(|var| var.decls.len())
        .sum();
    if declarators > MAX_RANGE_DECLARATORS {
        return RangeExplanation::limited(format!(
            "selected source exceeds {MAX_RANGE_DECLARATORS} declarators"
        ));
    }

    // Group candidate enumeration lists all placements before the traced
    // comparison. Bound top-level hole placements before entering that path.
    let holes = parsed
        .body()
        .iter()
        .filter(|item| module_item_list_hole_name(item).is_some())
        .count();
    let placement_bound = (0..holes).fold(1usize, |bound, _| {
        bound.saturating_mul(selected.len().saturating_add(1))
    });
    if kind == ExplainRangeKind::BindingGroup && placement_bound > 50_000 {
        return RangeExplanation::limited(
            "top-level hole placements exceed the local group-enumeration limit",
        );
    }

    let mut multiple_group_alignments = false;
    let authoritative = match kind {
        ExplainRangeKind::Member => {
            // The slice itself is the whole synthetic module. A multi-statement
            // member candidate can only start at zero when lengths agree.
            let module = Module {
                span: Default::default(),
                body: selected.to_vec(),
                shebang: None,
            };
            match chunk_resolver::ChunkResolver::new(&module)
                .member_candidates("explain range", parsed)
            {
                Ok(candidates) => {
                    let matched = parsed.body().len() == selected.len();
                    let mut candidates = candidates
                        .into_iter()
                        .filter(|candidate| matched && candidate.body_idx < selected.len());
                    let first = candidates.next();
                    if candidates.next().is_some() {
                        return RangeExplanation {
                            status: RangeExplanationStatus::Matched,
                            aligned_statements: Vec::new(),
                            matched_binding: None,
                            free_bindings: BTreeMap::new(),
                            failure: None,
                            reason: Some(
                                "multiple bindings in the selected range fit this member selector"
                                    .into(),
                            ),
                        };
                    }
                    first.map(|candidate| (candidate.binding.binding_name, candidate.free_bindings))
                }
                Err(err) => return RangeExplanation::unsupported(err.to_string()),
            }
        }
        ExplainRangeKind::BindingGroup => {
            let Some(target) = parsed.selector().target_binding.as_ref() else {
                return RangeExplanation::unsupported(
                    "binding-group explanation needs target_binding",
                );
            };
            let group = parsed.with_target_binding(None);
            let exports = BTreeMap::from([(target.clone(), target.clone())]);
            let module = Module {
                span: Default::default(),
                body: selected.to_vec(),
                shebang: None,
            };
            match chunk_resolver::ChunkResolver::new(&module).member_group_candidates(
                "explain range",
                &group,
                &exports,
            ) {
                Ok(candidates) => {
                    let mut candidates = candidates.into_iter().filter_map(|candidate| {
                        let binding = candidate.bindings.get(target)?;
                        (binding.body_idx < selected.len()).then(|| {
                            (
                                binding.binding.binding_name.clone(),
                                candidate.free_bindings,
                            )
                        })
                    });
                    let first = candidates.next();
                    multiple_group_alignments = candidates.next().is_some();
                    first
                }
                Err(err) => return RangeExplanation::unsupported(err.to_string()),
            }
        }
        ExplainRangeKind::Anonymous => None,
    };

    let variants = diagnostic_subject_variants(parsed, selected, kind);
    let mut best = None;
    let mut any_limited = false;
    for (variant, replaced_declarator) in variants {
        let subject_indices = match variant.iter().map(index_item).collect::<Result<Vec<_>>>() {
            Ok(indices) => indices,
            Err(err) => return RangeExplanation::unsupported(err.to_string()),
        };
        let result = match selector_match::explain_exact_range_indexed(
            &needle_indices,
            &subject_indices,
            mode,
            &free,
        ) {
            Ok(result) => result,
            Err(err) => return RangeExplanation::unsupported(err.reason),
        };
        any_limited |= result.diagnostic_limited;
        if result.matched {
            if let ExplainRangeKind::Anonymous = kind {
                return RangeExplanation {
                    status: RangeExplanationStatus::Matched,
                    aligned_statements: result.alignment,
                    matched_binding: None,
                    free_bindings: result.free_bindings,
                    failure: None,
                    reason: None,
                };
            }
            if let Some((binding, free_bindings)) = authoritative.as_ref() {
                let binding_is_local = !multiple_group_alignments
                    && !(kind == ExplainRangeKind::BindingGroup
                        && parsed.body().len() != selected.len());
                return RangeExplanation {
                    status: RangeExplanationStatus::Matched,
                    aligned_statements: result.alignment,
                    matched_binding: binding_is_local.then(|| binding.clone()),
                    free_bindings: if binding_is_local { free_bindings.clone() } else { BTreeMap::new() },
                    failure: None,
                    reason: (!binding_is_local).then(|| "binding-group target cannot be assigned uniquely within the selected range".into()),
                };
            }
        }
        let score = result.failure.as_ref().map_or(0, |failure| {
            failure.needle_statement_index.saturating_mul(1_000_000) + failure.progress
        });
        if best
            .as_ref()
            .is_none_or(|(best_score, _, _)| score > *best_score)
        {
            best = Some((score, result, replaced_declarator));
        }
    }

    // A member's candidate path is authoritative even when a diagnostic
    // variant missed a special projection. Keep that success honest and leave
    // alignment empty rather than inventing one.
    if kind == ExplainRangeKind::Member
        && let Some((binding, free_bindings)) = authoritative
    {
        return RangeExplanation {
            status: RangeExplanationStatus::Matched,
            aligned_statements: Vec::new(),
            matched_binding: Some(binding),
            free_bindings,
            failure: None,
            reason: Some("member matched through a declarator projection".into()),
        };
    }
    let Some((_, result, replaced_declarator)) = best else {
        return RangeExplanation {
            status: RangeExplanationStatus::Mismatch,
            aligned_statements: Vec::new(),
            matched_binding: None,
            free_bindings: BTreeMap::new(),
            failure: None,
            reason: Some("the selected range has no statement alignment".into()),
        };
    };
    let failure = result.failure.map(|failure| RangeFailure {
        selector_statement_index: failure.needle_statement_index,
        source_statement_index: failure.subject_statement_index,
        selector_node: failure.needle_node,
        source_node: failure.subject_node,
        category: match failure.category {
            selector_match::TraceCategory::Shape => RangeFailureCategory::Shape,
            selector_match::TraceCategory::Literal => RangeFailureCategory::Literal,
            selector_match::TraceCategory::Binding => RangeFailureCategory::Binding,
        },
        reason: failure.reason,
    });
    RangeExplanation {
        status: if any_limited {
            RangeExplanationStatus::Limited
        } else {
            RangeExplanationStatus::Mismatch
        },
        aligned_statements: Vec::new(),
        matched_binding: None,
        free_bindings: BTreeMap::new(),
        failure,
        reason: Some(if any_limited {
            "diagnostic comparison limit reached before every alignment was checked".into()
        } else {
            match replaced_declarator {
                Some(index) => format!("selected declarator {index} did not fit the selector"),
                None => "selected range did not fit the selector".into(),
            }
        }),
    }
}

fn index_item(item: &ModuleItem) -> Result<selector_match::Index> {
    let facts = item_facts(item)
        .ok_or_else(|| anyhow::anyhow!("selected statement did not project to matcher facts"))?;
    Ok(selector_match::Index::build(&facts))
}

/// For a member with one target declarator, inspect each possible declarator
/// projection while keeping all surrounding statements in the same match.
fn diagnostic_subject_variants(
    parsed: &ParsedSourceMatchSelector,
    selected: &[ModuleItem],
    kind: ExplainRangeKind,
) -> Vec<(Vec<ModuleItem>, Option<usize>)> {
    if kind == ExplainRangeKind::Member
        || (kind == ExplainRangeKind::BindingGroup && parsed.body().len() == 1)
    {
        let target = parsed.selector().target_binding.as_deref();
        let target_statement = parsed.body().iter().enumerate().find_map(|(index, item)| {
            let var = item_var_decl(item)?;
            if var.decls.len() != 1 {
                return None;
            }
            let is_target = target.is_none_or(|name| {
                declared_bindings(item)
                    .iter()
                    .any(|binding| binding.binding_name == name)
            });
            is_target.then_some(index)
        });
        if let Some(index) = target_statement
            && let Some(item) = selected.get(index)
            && let Some(var) = item_var_decl(item)
            && var.decls.len() > 1
        {
            return var
                .decls
                .iter()
                .enumerate()
                .map(|(declarator_index, declarator)| {
                    let mut variant = selected.to_vec();
                    variant[index] = single_declarator_item(item, declarator);
                    (variant, Some(declarator_index))
                })
                .collect();
        }
    }
    vec![(selected.to_vec(), None)]
}
