//! Human-readable renderings of cycle, binding-pair, atomic-group and
//! sequenced-cause diagnostics for bail messages.

use super::report::{BlockingRejection, CycleEdge, CycleReport};
use analysis::ModuleId;
use analysis_vocabulary::inputs::CommutingStatementsRef;
use analysis_vocabulary::purity::{Purity, PurityReason};
use graph::assignment_assembly::AtomicGroupConflict;
use graph::{DependencyGraph, SequencedSourceUnitCause};
use spec::ModulePath;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use swc_atoms::Atom;

pub(super) const CYCLE_WITNESS_PREVIEW_STEPS: usize = 8;

/// Render the per-cycle summary used in the materializer's bail
/// message. Each cycle is a spec-induced module-quotient SCC carrying
/// realizability-constraining (R/S) edges; the renderer blames
/// **binding pairs**, not just module pairs, so spec authors can act
/// directly: "move `X` (mod_A) and `Y` (mod_B) into one module."
///
/// Full per-cycle cut still goes to
/// `reports/tree/<chunk_id>/cycles.json`. This summary keeps the
/// inline bail message terse: one header line per SCC plus the top-K
/// binding-pair blame rows.
///
/// Each binding-pair row groups cut entries by
/// `(from_binding_label, from_module, to_module, to_binding_label)`
/// and counts the underlying R/S reasons. Anonymous source
/// bindings render as `<anon stmt #ord>` so every row has a stable
/// human-readable label even when the source statement declares no
/// binding (top-level `console.log`, side-effecting expressions).
///
/// For ESM-evaluation-simulator (Pass-2 / TDZ) rejections, the cut
/// rows are followed by the lazy back-edges that close the I-cycle
/// (`CycleReport::lazy_closure`), in the same binding-pair format.
pub fn render_cycle_summary(cycles: &[CycleReport]) -> String {
    let mut out = String::new();
    const TOP_K: usize = 10;
    let roots = cycles
        .iter()
        .filter(|c| c.rejection == BlockingRejection::MutualConstrainingCycle)
        .count();
    if roots > 0 {
        let conditional = cycles
            .iter()
            .filter(|c| c.rejection == BlockingRejection::TdzAfterCollapsingPassOne)
            .count();
        out.push_str(&format!(
            "Root cause: {roots} mutual constraining cycle(s) (Pass 1). Pass 2 needs a linker order, which a Pass-1 cycle rules out, so it did not run on this assignment. {conditional} conditional SCC(s) below are what Pass 2 reports with each Pass-1 SCC collapsed into one module.\n",
        ));
    }
    for (i, cycle) in cycles.iter().enumerate() {
        out.push_str(&format!(
            "Cycle #{i}{}: {} module(s) in unrealizable SCC, {} R/S edge(s) across {} (from-module, to-module) pair(s).\n",
            cycle.rejection.cycle_summary_suffix(),
            cycle.modules.len(),
            cycle.cut.len(),
            cut_pairs_count(&cycle.cut),
        ));

        if let Some(witness) = &cycle.pass_one_witness {
            out.push_str(&format!(
                "  Complete Pass-1 closed walk ({} step(s); showing {}):\n",
                witness.steps.len(),
                witness.steps.len() - witness.text_preview_omitted_steps,
            ));
            for edge in witness.steps.iter().take(CYCLE_WITNESS_PREVIEW_STEPS) {
                let key = BindingPairKey::of(edge);
                out.push_str(&format!(
                    "    {} ({}, {}) --{}--> {} ({}, {})\n",
                    key.from_label,
                    key.from,
                    edge.from_source_unit_id,
                    edge.kind.diagnostic_label(),
                    key.to_label,
                    key.to,
                    edge.to_source_unit_id,
                ));
            }
            if witness.text_preview_omitted_steps > 0 {
                out.push_str(&format!(
                    "    ... {} step(s) omitted from this preview; full ordered walk in cycles.json\n",
                    witness.text_preview_omitted_steps,
                ));
            }
        } else {
            out.push_str("  Pass-1 closed-walk witness unsupported for this rejection kind.\n");
        }

        // Every blocking SCC carries a non-empty cut by construction:
        // Pass-1 runs FAS over a strongly connected constraining
        // subgraph; Pass-2 reports only SCCs with a non-empty
        // simulator-violated TDZ set.
        debug_assert!(
            !cycle.cut.is_empty(),
            "blocking SCC #{i} rendered with an empty cut: {cycle:?}",
        );

        out.push_str(
            "  Binding pairs forcing the cycle (count: source binding (module) → kind → target binding (module), then why each edge constrains):\n",
        );
        render_binding_pairs(&cycle.cut, Some(TOP_K), &mut out);

        if !cycle.lazy_closure.is_empty() {
            out.push_str(
                "  Constraining subgraph alone is acyclic; the I-cycle closes through nonconstraining imports below, and the ESM evaluation simulator proved the at-init pair(s) above evaluate under TDZ (docs/design.md \"Lemma 2\"):\n",
            );
            let mut lazy_groups: HashMap<BindingPairKey<'_>, usize> = HashMap::new();
            for edge in &cycle.lazy_closure {
                *lazy_groups.entry(BindingPairKey::of(edge)).or_insert(0) += 1;
            }
            let mut lazy_ranked: Vec<(BindingPairKey<'_>, usize)> =
                lazy_groups.into_iter().collect();
            lazy_ranked.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
            for (key, count) in lazy_ranked.iter().take(TOP_K) {
                out.push_str(&format!(
                    "    {n:>4}x  {from_b} ({from_m})  --lazy-->  {to_b} ({to_m})\n",
                    n = count,
                    from_b = key.from_label,
                    from_m = key.from,
                    to_b = key.to_label,
                    to_m = key.to,
                ));
            }
            if lazy_ranked.len() > TOP_K {
                out.push_str(&format!(
                    "    … +{} more lazy binding pair(s)\n",
                    lazy_ranked.len() - TOP_K,
                ));
            }
        }

        render_inert_commuting_edges(
            &cycle.inert_commuting_edges,
            &cycle.commuting_premise_locations,
            &mut out,
        );

        out.push_str(
            "  Fix: co-locate each (source, target) binding pair above into one logical module, or break the SCC's back-edges in the spec.\n",
        );
        let causes = sequenced_causes_from_cycle(cycle);
        render_sequenced_source_unit_causes(&causes, &mut out);
    }
    out
}

#[derive(Debug, Clone, Eq, PartialEq, Hash, Ord, PartialOrd)]
pub(super) struct BindingPairKey<'a> {
    pub(super) from_label: std::borrow::Cow<'a, str>,
    pub(super) from: &'a str,
    pub(super) to: &'a str,
    pub(super) to_label: std::borrow::Cow<'a, str>,
}

impl<'a> BindingPairKey<'a> {
    pub(super) fn of(edge: &'a CycleEdge) -> Self {
        let from_label = match &edge.from_binding {
            Some(atom) => std::borrow::Cow::Borrowed(atom.as_ref()),
            None => std::borrow::Cow::Owned(format!("<anon stmt #{}>", edge.statement_ordinal.0)),
        };
        let to_label = match &edge.binding {
            Some(atom) => std::borrow::Cow::Borrowed(atom.as_ref()),
            None => std::borrow::Cow::Borrowed("<side-effect>"),
        };
        BindingPairKey {
            from_label,
            from: edge.from.as_str(),
            to: edge.to.as_str(),
            to_label,
        }
    }
}

/// Rank binding pairs by edge count, then by their displayed endpoints.
pub fn render_binding_pairs(edges: &[CycleEdge], limit: Option<usize>, out: &mut String) {
    let mut groups: HashMap<BindingPairKey<'_>, Vec<&CycleEdge>> = HashMap::new();
    for edge in edges {
        groups
            .entry(BindingPairKey::of(edge))
            .or_default()
            .push(edge);
    }
    let mut ranked: Vec<(BindingPairKey<'_>, Vec<&CycleEdge>)> = groups.into_iter().collect();
    ranked.sort_by(|a, b| b.1.len().cmp(&a.1.len()).then(a.0.cmp(&b.0)));

    let limit = limit.unwrap_or(ranked.len());
    for (key, edges) in ranked.iter().take(limit) {
        out.push_str(&format!(
            "    {n:>4}x  {from_b} ({from_m})  --{kind}-->  {to_b} ({to_m})\n",
            n = edges.len(),
            from_b = key.from_label,
            from_m = key.from,
            kind = edges[0].kind.diagnostic_label(),
            to_b = key.to_label,
            to_m = key.to,
        ));
        render_explanations(edges, out);
    }
    if ranked.len() > limit {
        out.push_str(&format!(
            "    … +{} more binding pair(s)\n",
            ranked.len() - limit,
        ));
    }
}

/// Distinct explanations behind one binding-pair row, indented under
/// it. Shared by the bail summary and `gate describe`'s evidence block.
fn render_explanations(edges: &[&CycleEdge], out: &mut String) {
    const WHY_K: usize = 3;
    let distinct: BTreeSet<String> = edges
        .iter()
        .filter_map(|edge| edge.explanation.as_ref())
        .map(ToString::to_string)
        .collect();
    for why in distinct.iter().take(WHY_K) {
        out.push_str(&format!("           - {why}\n"));
    }
    if distinct.len() > WHY_K {
        out.push_str(&format!(
            "           - … +{} more distinct reason(s)\n",
            distinct.len() - WHY_K,
        ));
    }
}

/// The `sequenced` edges between SCC members an author `commuting_statements`
/// premise made inert: a count line, then up to `INERT_K` rows naming both
/// endpoints, the premise (with its spec location where `premise_locations`
/// has it) and the retained cause. Nothing when there is no inert edge.
pub fn render_inert_commuting_edges(
    edges: &[CycleEdge],
    premise_locations: &BTreeMap<CommutingStatementsRef, String>,
    out: &mut String,
) {
    const INERT_K: usize = 3;
    if edges.is_empty() {
        return;
    }
    out.push_str(&format!(
        "  Inert: {} sequenced edge(s) between SCC members made inert by commuting_statements premise(s); never in the cut:\n",
        edges.len(),
    ));
    for edge in edges.iter().take(INERT_K) {
        edge.render_header(out);
        if let Some(premise) = &edge.commuting_premise {
            let location = premise_locations
                .get(premise)
                .map(|location| format!(" at {location}"))
                .unwrap_or_default();
            out.push_str(&format!("           inert: premise {premise}{location}\n"));
        }
        render_explanations(&[edge], out);
    }
    if edges.len() > INERT_K {
        out.push_str(&format!(
            "    … +{} more inert edge(s)\n",
            edges.len() - INERT_K,
        ));
    }
}

fn cut_pairs_count(cut: &[CycleEdge]) -> usize {
    let mut seen: HashSet<(&str, &str)> = HashSet::new();
    for edge in cut {
        seen.insert((edge.from.as_str(), edge.to.as_str()));
    }
    seen.len()
}
const ATOMIC_GROUP_CONFLICT_PREVIEW_LIMIT: usize = 8;
const ATOMIC_GROUP_DETAIL_PREVIEW_LIMIT: usize = 20;

/// Render a human-readable summary of atomic-group conflicts for
/// inclusion in the materializer's bail message. One block per
/// conflict previews the group's members and claims. Large lists are
/// bounded here; the machine-readable conflict report remains complete.
/// The `module_path` callback resolves [`ModuleId`]s to the canonical
/// [`ModulePath`]s the spec author recognizes.
pub fn render_atomic_group_conflict_summary(
    conflicts: &[AtomicGroupConflict],
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    dependency_graph: &DependencyGraph,
) -> String {
    let mut out = String::new();
    let preview_count = conflicts.len().min(ATOMIC_GROUP_CONFLICT_PREVIEW_LIMIT);
    if conflicts.len() > preview_count {
        out.push_str(&format!(
            "  atomic-group conflicts: showing {preview_count} of {}; {} conflict(s) omitted from this preview\n",
            conflicts.len(),
            conflicts.len() - preview_count,
        ));
    }
    for (idx, c) in conflicts.iter().take(preview_count).enumerate() {
        if idx > 0 {
            out.push('\n');
        }
        let mut member_ids: Vec<String> = c
            .members
            .iter()
            .copied()
            .map(graph::report::source_unit_key)
            .collect();
        member_ids.sort();
        let mut conflicting_modules: Vec<String> = c
            .claims
            .iter()
            .map(|claim| module_path(claim.module).to_string())
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        conflicting_modules.sort();
        out.push_str(&format!(
            "  atomic group {{{}}} ({} members) — claims across {{{}}} ({} destinations; {} claims):\n",
            render_string_preview(&member_ids, ATOMIC_GROUP_DETAIL_PREVIEW_LIMIT),
            member_ids.len(),
            render_string_preview(&conflicting_modules, ATOMIC_GROUP_DETAIL_PREVIEW_LIMIT),
            conflicting_modules.len(),
            c.claims.len(),
        ));
        for claim in c.claims.iter().take(ATOMIC_GROUP_DETAIL_PREVIEW_LIMIT) {
            let names = if claim.binding_names.is_empty() {
                String::new()
            } else {
                format!(
                    " ({})",
                    claim
                        .binding_names
                        .iter()
                        .map(|atom| atom.as_ref())
                        .collect::<Vec<_>>()
                        .join(",")
                )
            };
            out.push_str(&format!(
                "    - {}{} → {}\n",
                graph::report::source_unit_key(claim.source_unit),
                names,
                module_path(claim.module),
            ));
        }
        if c.claims.len() > ATOMIC_GROUP_DETAIL_PREVIEW_LIMIT {
            out.push_str(&format!(
                "    ... {} claim row(s) omitted from this preview ({} total)\n",
                c.claims.len() - ATOMIC_GROUP_DETAIL_PREVIEW_LIMIT,
                c.claims.len(),
            ));
        }
        let causes = graph::report::sequenced_source_unit_causes(dependency_graph, &c.members);
        render_sequenced_source_unit_causes_with_limit(
            &causes,
            &mut out,
            Some(ATOMIC_GROUP_DETAIL_PREVIEW_LIMIT),
        );
    }
    out
}

fn render_string_preview(values: &[String], limit: usize) -> String {
    let mut preview: Vec<String> = values.iter().take(limit).cloned().collect();
    let omitted = values.len().saturating_sub(limit);
    if omitted > 0 {
        preview.push(format!("... {omitted} omitted"));
    }
    preview.join(", ")
}

/// Describe the initializer at the reason's span, falling back to its source unit.
pub fn initializer_reason(cause: &SequencedSourceUnitCause, reason: &PurityReason) -> String {
    let names = if cause.binding_names.is_empty() {
        cause.source_unit_id.clone()
    } else {
        cause
            .binding_names
            .iter()
            .map(Atom::as_ref)
            .collect::<Vec<_>>()
            .join(", ")
    };
    let location = reason
        .source_location
        .as_ref()
        .or(cause.source_location.as_ref())
        .map(|loc| {
            format!(
                "{}:{}:{}",
                loc.source_path,
                loc.start_line,
                loc.start_column
                    .map_or_else(|| "?".to_string(), |col| col.to_string()),
            )
        })
        .unwrap_or_else(|| "<location unavailable>".to_string());
    let rule = reason.rule;
    let detail = reason
        .detail
        .as_deref()
        .map(|detail| format!(" ({detail})"))
        .unwrap_or_default();
    // A statement whose synchronous execution is incomplete
    // chains conservatively; say so next to its purity reason.
    let incomplete = match cause.synchronous_execution_complete {
        Some(false) => "; synchronous execution incomplete",
        _ => "",
    };
    format!("`{names}` at {location}: {rule}{detail}{incomplete}")
}

fn sequenced_causes_from_cycle(cycle: &CycleReport) -> Vec<SequencedSourceUnitCause> {
    let mut causes = BTreeMap::new();
    for edge in &cycle.cut {
        for cause in edge.sequenced_causes() {
            causes.insert(cause.source_unit_id.clone(), cause.clone());
        }
    }
    causes.into_values().collect()
}

pub(crate) fn render_sequenced_source_unit_causes(
    causes: &[SequencedSourceUnitCause],
    out: &mut String,
) {
    render_sequenced_source_unit_causes_with_limit(causes, out, None);
}

fn render_sequenced_source_unit_causes_with_limit(
    causes: &[SequencedSourceUnitCause],
    out: &mut String,
    limit: Option<usize>,
) {
    let mut reason_class_counts = BTreeMap::new();
    for cause in causes {
        if let Purity::NotPure { reasons } = &cause.purity {
            for reason in reasons {
                *reason_class_counts
                    .entry(<&str>::from(reason.rule))
                    .or_insert(0usize) += 1;
            }
        }
    }
    let total_reasons: usize = reason_class_counts.values().sum();
    if total_reasons == 0 {
        return;
    }
    let rendered_limit = limit.unwrap_or(total_reasons).min(total_reasons);
    out.push_str("  Impure initializers behind side-effect edges");
    if total_reasons > rendered_limit {
        let classes = reason_class_counts
            .iter()
            .map(|(class, count)| format!("{class}={count}"))
            .collect::<Vec<_>>()
            .join(", ");
        out.push_str(&format!(
            " (showing {rendered_limit} of {total_reasons} reasons; classes: {classes})"
        ));
    }
    out.push_str(":\n");
    let mut rendered = 0;
    'causes: for cause in causes {
        let Purity::NotPure { reasons } = &cause.purity else {
            continue;
        };
        for reason in reasons {
            if rendered == rendered_limit {
                break 'causes;
            }
            out.push_str(&format!("    {}\n", initializer_reason(cause, reason)));
            if let Some(guidance) = &reason.author_guidance {
                out.push_str(&format!("      {guidance}\n"));
            }
            rendered += 1;
        }
    }
    if total_reasons > rendered {
        out.push_str(&format!(
            "    ... {} initializer reason(s) omitted from this preview\n",
            total_reasons - rendered,
        ));
    }
}
