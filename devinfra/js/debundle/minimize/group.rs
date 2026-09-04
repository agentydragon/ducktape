//! Multi-target `var` binding-group minimization: per-slot read-off
//! (`try_var_group_read_off`) with the keep-shallow cover as fallback.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use readoff_render::kept_spans_for_anchor_set;
use swc_common::Spanned;
use swc_ecma_ast::*;

use super::object::{object_anchor_ranking, try_object_read_off, try_object_read_off_candidates};
use super::var::{try_var_read_off, try_var_read_off_candidates};
use super::{hole_var_init_padded, render_var_slots, render_via_neighbor_context};
use crate::regex_anchor::{accepted_regex_anchors, collect_regex_anchor_candidates};
use crate::render::{
    AnchorSpan, MAX_MINIMIZER_ANCHORS, hole_expr, holes_present, node_holds_anchor, span_key,
};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
    prove_synthesized_selector, single_ident_pat_name, solve_single_member_selector,
};

/// Candidate concrete anchors, split into preference tiers. Literal values are
/// stable, meaningful landmarks (a magic string, a config number/bool), so they
/// are tried before structural member/property and key names. Among literals,
/// shallower ones (fewer enclosing call/new levels) are preferred: a direct
/// `key: "primary"` pins less nested structure — and is more rebuild-robust —
/// than a literal buried inside `mk("primary")`.
#[derive(Default)]
struct AnchorCandidates {
    /// `(span, call-nesting depth)` for each literal.
    literals: Vec<(AnchorSpan, u32)>,
    structural: Vec<AnchorSpan>,
}

/// A literal at most this many call/new levels deep counts as a *direct* value
/// or argument — a meaningful, stable pin worth keeping. Deeper literals are
/// treated as buried in incidental computation and ranked below structural
/// key/member presence.
const SHALLOW_LITERAL_DEPTH: u32 = 1;

impl AnchorCandidates {
    /// Shallow literal anchors — direct values/args worth keeping as meaningful
    /// per-slot pins even when structure alone would already discriminate. This
    /// includes object-property values (`kind: "primary"`): the unified anchor
    /// policy favors keeping shallow literals over an exact-minimum cover, so an
    /// occasional over-pin (a shared `enabled: true`) is accepted as the price
    /// of a single policy across single and group targets.
    fn shallow_literals(&self) -> Vec<AnchorSpan> {
        self.literals
            .iter()
            .filter(|(_, depth)| *depth <= SHALLOW_LITERAL_DEPTH)
            .map(|(span, _)| *span)
            .collect()
    }

    /// Tiers consulted only when shallow literals are not enough on their own:
    /// structural key/member presence, then deeper literals by ascending depth.
    fn deep_cover_tiers(&self) -> Vec<Vec<AnchorSpan>> {
        let mut tiers = Vec::new();
        if !self.structural.is_empty() {
            tiers.push(self.structural.clone());
        }
        if let Some(max_depth) = self.literals.iter().map(|(_, depth)| *depth).max() {
            for depth in (SHALLOW_LITERAL_DEPTH + 1)..=max_depth {
                let tier: Vec<AnchorSpan> = self
                    .literals
                    .iter()
                    .filter(|(_, anchor_depth)| *anchor_depth == depth)
                    .map(|(span, _)| *span)
                    .collect();
                if !tier.is_empty() {
                    tiers.push(tier);
                }
            }
        }
        tiers
    }
}

/// `depth` counts enclosing call/new levels, so the keep-shallow group path can
/// prefer shallower literal anchors. Object-property values (`kind: "primary"`)
/// are collected at the enclosing depth like any other literal; the keep-shallow
/// policy retains them rather than treating them as incidental.
fn collect_expr_anchors(expr: &Expr, depth: u32, candidates: &mut AnchorCandidates) {
    match expr {
        Expr::Lit(_) | Expr::Tpl(_) => {
            candidates.literals.push((span_key(expr.span()), depth));
        }
        Expr::Paren(paren) => collect_expr_anchors(&paren.expr, depth, candidates),
        Expr::Member(member) => {
            collect_expr_anchors(&member.obj, depth, candidates);
            if let MemberProp::Ident(ident) = &member.prop {
                candidates.structural.push(span_key(ident.span));
            } else if let MemberProp::Computed(computed) = &member.prop {
                collect_expr_anchors(&computed.expr, depth, candidates);
            }
        }
        Expr::Call(call) => {
            if let Callee::Expr(callee) = &call.callee {
                collect_expr_anchors(callee, depth, candidates);
            }
            for arg in &call.args {
                if arg.spread.is_none() {
                    collect_expr_anchors(&arg.expr, depth + 1, candidates);
                }
            }
        }
        Expr::New(new_expr) => {
            collect_expr_anchors(&new_expr.callee, depth, candidates);
            for arg in new_expr.args.as_deref().unwrap_or_default() {
                if arg.spread.is_none() {
                    collect_expr_anchors(&arg.expr, depth + 1, candidates);
                }
            }
        }
        Expr::Object(object) => {
            for prop in &object.props {
                if let PropOrSpread::Prop(prop) = prop {
                    if let Prop::KeyValue(key_value) = prop.as_ref() {
                        candidates.structural.push(span_key(key_value.key.span()));
                        collect_expr_anchors(&key_value.value, depth, candidates);
                    }
                }
            }
        }
        Expr::Await(await_expr) => collect_expr_anchors(&await_expr.arg, depth, candidates),
        Expr::Unary(unary) => collect_expr_anchors(&unary.arg, depth, candidates),
        Expr::Bin(bin) => {
            collect_expr_anchors(&bin.left, depth, candidates);
            collect_expr_anchors(&bin.right, depth, candidates);
        }
        _ => {}
    }
}

/// Ranked candidate anchor spans for one binding-group slot, best-first. Object
/// slots reuse the key-set ranking ([`object_anchor_ranking`]: direct values then
/// keys); every other init ranks its direct shallow literals first (a meaningful,
/// rebuild-stable value pin), then structural key/member presence, then deeper
/// literals — the keep-shallow tier order, flattened into a single best-first
/// list the per-slot greedy consumes.
fn slot_anchor_ranking(declarator: &VarDeclarator) -> Vec<AnchorSpan> {
    let Some(init) = declarator.init.as_deref() else {
        return Vec::new();
    };
    if let Expr::Object(object) = init {
        return object_anchor_ranking(object);
    }
    let mut candidates = AnchorCandidates::default();
    collect_expr_anchors(init, 0, &mut candidates);
    let mut ranked: Vec<AnchorSpan> = candidates.shallow_literals();
    for tier in candidates.deep_cover_tiers() {
        for span in tier {
            if !ranked.contains(&span) {
                ranked.push(span);
            }
        }
    }
    ranked
}

/// Per-slot minimal anchor set for a binding-group read-off: the smallest kept
/// span subset (drawn from `ranked`) that makes the matcher resolve **this slot's
/// own export binding** to its declarator, viewed as a single-target selector
/// (every other target slot holed to `DECLARATORS_*`). This is the per-slot
/// declarator-tuple resolution the chunk-wide `minimal_anchor_set` (which is
/// per-statement and resolves by distinct body index) cannot express: it cannot
/// see two sibling declarators of the *same* statement.
///
/// Reuses [`cover_object_slot`]'s slot-aware scoring — `(target slot not yet
/// resolved, total matches)` via the single-binding-form matcher
/// ([`solve_single_member_selector`]) — so an anchor that flips the target binding
/// to the resolved one and rules out the most competitors wins. `seed` (the
/// chunk-wide read-off spans restricted to this slot) is kept up front when
/// non-empty: it is the index's already-ranked `selective × stable` choice, so
/// the greedy only adds to it. Returns the kept spans for the slot, or `None`
/// when the slot's own anchors cannot single it out (the caller then falls back
/// to the keep-shallow group path).
fn slot_minimal_anchors(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    slot: usize,
    target: &SynthesizedTargetBinding,
    seed: &BTreeSet<AnchorSpan>,
    ranked: &[AnchorSpan],
) -> Result<Option<BTreeSet<AnchorSpan>>> {
    let export = target.export_name.as_str();
    let runtime = target.runtime_binding.as_str();
    let slot_decl_span = var.decls[slot].span();
    // Single-target view of this slot: the slot is the lone target, every other
    // declarator holes to a `DECLARATORS_*` run.
    let only_this = BTreeSet::from([slot]);
    let export_for = |name: &str| (name == runtime).then(|| export.to_string());
    let no_regex = BTreeMap::new();
    let render_slot = |kept: &BTreeSet<AnchorSpan>| -> Result<String> {
        render_var_slots(
            var,
            &only_this,
            &export_for,
            kept,
            &no_regex,
            &hole_var_init_padded,
        )
    };
    // The slot resolves when the single-binding matcher singles out exactly this
    // declarator: one match, at the target statement, bound to the target slot's
    // runtime name.
    let slot_resolves = |kept: &BTreeSet<AnchorSpan>| -> Result<bool> {
        let matches = solve_single_member_selector(index, export, &render_slot(kept)?)?;
        let [m] = matches.as_slice() else {
            return Ok(false);
        };
        Ok(m.body_idx == decl.body_idx && m.binding.binding_name == runtime)
    };
    let mut kept: BTreeSet<AnchorSpan> = seed.clone();
    while !slot_resolves(&kept)? {
        let mut best: Option<((bool, usize), AnchorSpan)> = None;
        for &anchor in ranked.iter().take(MAX_MINIMIZER_ANCHORS) {
            if kept.contains(&anchor) || !node_holds_anchor(slot_decl_span, anchor) {
                continue;
            }
            let mut trial = kept.clone();
            trial.insert(anchor);
            let matches = solve_single_member_selector(index, export, &render_slot(&trial)?)?;
            let target_unresolved = !matches.iter().any(|m| m.binding.binding_name == runtime);
            let score = (target_unresolved, matches.len());
            if best.is_none_or(|(best_score, _)| score < best_score) {
                best = Some((score, anchor));
            }
        }
        let Some((_, anchor)) = best else {
            return Ok(None);
        };
        kept.insert(anchor);
    }
    Ok(Some(kept))
}

/// Multi-target binding-group read-off (the per-slot declarator-tuple resolution
/// the chunk-wide read-off cannot express): for each target declarator slot read
/// its minimal anchor off the shape index (restricted to that slot) and, when the
/// chunk-wide set does not single the slot out within the statement, extend it
/// with a slot-aware greedy ([`slot_minimal_anchors`]). UNION the per-slot kept
/// spans, render the whole group through [`render_var_slots`], and prove
/// the tuple with [`prove_synthesized_selector`] (the binding-group matcher as the
/// resolves-uniquely oracle). The regex-literal upgrade applies across all slots.
///
/// Returns `None` — caller falls back to the keep-shallow path — when any slot's
/// own anchors cannot single it out or the unioned selector does not resolve the
/// tuple uniquely.
fn try_var_group_read_off(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    target_slots: &BTreeSet<usize>,
) -> Result<Option<SpecializedSelector>> {
    let export_for = |runtime: &str| {
        targets
            .iter()
            .find(|target| target.runtime_binding == runtime)
            .map(|target| target.export_name.clone())
    };
    // Chunk-wide read-off anchors for the whole statement, mapped to byte spans;
    // each slot's seed is the subset lying inside that declarator.
    let chunk_kept: BTreeSet<AnchorSpan> = match index.shape_index.minimal_anchor_set(decl.body_idx)
    {
        Some(anchor_set) => {
            let item = index
                .parsed
                .module
                .body
                .get(decl.body_idx)
                .context("read-off body index no longer in module")?;
            kept_spans_for_anchor_set(item, &anchor_set)
        }
        None => BTreeSet::new(),
    };

    let mut union: BTreeSet<AnchorSpan> = BTreeSet::new();
    for &slot in target_slots {
        let runtime = single_ident_pat_name(&var.decls[slot].name)
            .expect("target declarator is a plain identifier");
        let target = targets
            .iter()
            .find(|target| target.runtime_binding == runtime)
            .expect("target slot has a target");
        let slot_decl_span = var.decls[slot].span();
        let seed: BTreeSet<AnchorSpan> = chunk_kept
            .iter()
            .copied()
            .filter(|span| node_holds_anchor(slot_decl_span, *span))
            .collect();
        let ranked = slot_anchor_ranking(&var.decls[slot]);
        let Some(slot_kept) = slot_minimal_anchors(index, var, decl, slot, target, &seed, &ranked)?
        else {
            return Ok(None);
        };
        union.extend(slot_kept);
    }

    let no_regex = BTreeMap::new();
    // Prove the unioned tuple resolves uniquely through the binding-group matcher
    // before offering the regex upgrade; on failure the caller falls back to the
    // keep-shallow group path.
    if prove_synthesized_selector(
        index,
        decl,
        targets,
        &render_var_slots(
            var,
            target_slots,
            &export_for,
            &union,
            &no_regex,
            &hole_var_init_padded,
        )?,
    )
    .is_err()
    {
        return Ok(None);
    }

    // Regex-literal upgrade over the kept string literals of every target slot
    // (shared policy with the keep-shallow path).
    let mut regex_candidates: BTreeMap<AnchorSpan, String> = BTreeMap::new();
    for &slot in target_slots {
        if let Some(init) = &var.decls[slot].init {
            regex_candidates.extend(collect_regex_anchor_candidates(init));
        }
    }
    let render_with = |kept: &BTreeSet<AnchorSpan>,
                       regex_anchors: &BTreeMap<AnchorSpan, String>|
     -> Result<String> {
        render_var_slots(
            var,
            target_slots,
            &export_for,
            kept,
            regex_anchors,
            &hole_var_init_padded,
        )
    };
    let regex_anchors = accepted_regex_anchors(
        index,
        decl,
        targets,
        &regex_candidates,
        &union,
        &render_with,
    )?;

    let source = render_with(&union, &regex_anchors)?;
    let rewritten_holes = holes_present(&source);
    Ok(Some(SpecializedSelector {
        match_source: source,
        rewritten_holes,
    }))
}

/// Up to `limit` ranked candidate selectors for a `var` declaration — the
/// `synthesize-selectors --candidates N` menu. A single-target object or non-object
/// var slot returns its read-off menu; a multi-declarator binding group returns the
/// sparse union-minimal tuple plus the keep-shallow tuple as a robustness
/// alternative ([`group_read_off_candidates`]). `limit == 1` reproduces
/// [`minimize_var_group_selector`].
pub(crate) fn minimize_var_group_selector_candidates(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    limit: usize,
) -> Result<Vec<SpecializedSelector>> {
    let export_for = |runtime: &str| {
        targets
            .iter()
            .find(|target| target.runtime_binding == runtime)
            .map(|target| target.export_name.clone())
    };
    let target_slots: BTreeSet<usize> = var
        .decls
        .iter()
        .enumerate()
        .filter_map(|(idx, declarator)| {
            let name = single_ident_pat_name(&declarator.name)?;
            export_for(name).map(|_| idx)
        })
        .collect();
    if target_slots.len() != targets.len() {
        return Ok(Vec::new());
    }

    let object = try_object_read_off_candidates(index, var, decl, targets, &target_slots, limit)?;
    if !object.is_empty() {
        return Ok(object);
    }
    if let [target] = targets
        && target_slots.len() == 1
    {
        let slot = *target_slots.iter().next().expect("one target slot");
        let var_candidates = try_var_read_off_candidates(index, var, decl, target, slot, limit)?;
        if !var_candidates.is_empty() {
            return Ok(var_candidates);
        }
    }
    // Multi-target binding-group (and the single-target case that fell through the
    // object / non-object read-off menus): the sparse union-minimal tuple, then the
    // keep-shallow tuple as a robustness alternative.
    group_read_off_candidates(index, var, decl, targets, &target_slots, limit)
}

/// Up to `limit` ranked binding-group selectors: the sparse union-minimal tuple
/// ([`try_var_group_read_off`]) first, then the keep-shallow tuple
/// ([`keep_shallow_group_selector`]) as a robustness alternative, deduped by
/// source. `limit == 1` reproduces the single pick [`minimize_var_group_selector`]
/// returns for the group / fell-through path (group read-off, else keep-shallow).
fn group_read_off_candidates(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    target_slots: &BTreeSet<usize>,
    limit: usize,
) -> Result<Vec<SpecializedSelector>> {
    let mut out: Vec<SpecializedSelector> = Vec::new();
    if let Some(selector) = try_var_group_read_off(index, var, decl, targets, target_slots)? {
        out.push(selector);
    }
    if out.len() < limit
        && let Some(selector) =
            keep_shallow_group_selector(index, var, decl, targets, target_slots)?
        && !out
            .iter()
            .any(|kept| kept.match_source == selector.match_source)
    {
        out.push(selector);
    }
    Ok(out)
}

/// Minimal anchor cover for a `var`/`let`/`const` binding group: render the
/// shared declaration keeping the target declarators (each `export = <holed
/// init>`) with `DECLARATORS_*` holes for non-target runs, and pin just enough
/// anchors for the group to resolve uniquely to the right declaration and
/// bindings. A single-declarator target is the N=1 case of this path: one
/// target slot, no `DECLARATORS_*` gaps, and the binding-group matcher's tuple
/// proof degenerates to the single-binding case in `prove_synthesized_selector`.
///
/// The proof uses `prove_synthesized_selector` as a boolean oracle (it yields a
/// count or an error rather than a candidate set), adding anchors tier by tier
/// (shallow literals first) until the group resolves correctly.
pub(crate) fn minimize_var_group_selector(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<Option<SpecializedSelector>> {
    let export_for = |runtime: &str| {
        targets
            .iter()
            .find(|target| target.runtime_binding == runtime)
            .map(|target| target.export_name.clone())
    };
    let target_slots: BTreeSet<usize> = var
        .decls
        .iter()
        .enumerate()
        .filter_map(|(idx, declarator)| {
            let name = single_ident_pat_name(&declarator.name)?;
            export_for(name).map(|_| idx)
        })
        .collect();
    if target_slots.len() != targets.len() {
        return Ok(None);
    }

    // W3 + key-set minimization: a single-target var whose target declarator is
    // an object literal reads its minimal selector off the shape index, then
    // (slot-aware) covers the target object's own keys — `ANYTHING` holes
    // around the discriminating key subset instead of keeping every key (the
    // `object_keys_over_pinned` / key-set group over-pin). Works whether the
    // object stands alone or sits inside a multi-declarator group. The matcher
    // proves it (gate 1); on `None` we fall through to the keep-shallow group path
    // below, so a target neither pass can single out is handled exactly as before.
    if let Some(selector) = try_object_read_off(index, var, decl, targets, &target_slots)? {
        return Ok(Some(selector));
    }

    // W2 read-off for the single-target non-object var (mirroring function/class):
    // read the sparse `selective × stable` anchor off the shape index and hole the
    // initializer down to it, instead of the keep-shallow path's "keep every
    // shallow literal" over-pin. On `None` (no anchor set, anchors outside the
    // target slot, or not uniquely resolved) we fall through to the keep-shallow
    // group path below; multi-target groups always use that path — the read-off is
    // single-target (per-slot tuple resolution is still the cover's job).
    if let [target] = targets
        && target_slots.len() == 1
        && let Some(selector) = try_var_read_off(
            index,
            var,
            decl,
            target,
            *target_slots.iter().next().expect("one target slot"),
        )?
    {
        return Ok(Some(selector));
    }

    // Multi-target binding-group read-off: read each target declarator slot's
    // minimal anchor off the shape index (restricted to that slot) plus a
    // slot-aware greedy, UNION them, and prove the tuple through the binding-group
    // matcher. This is the per-slot declarator-tuple resolution the chunk-wide
    // read-off cannot express. On `None` (a slot the read-off cannot single out,
    // or a non-resolving union) we fall through to the keep-shallow group path
    // below — the same target is then handled exactly as before.
    if let Some(selector) = try_var_group_read_off(index, var, decl, targets, &target_slots)? {
        return Ok(Some(selector));
    }

    // Keep-shallow cover (with enclosing-context fallback): the robustness path
    // when no read-off form singled the group out.
    keep_shallow_group_selector(index, var, decl, targets, &target_slots)
}

/// Keep-shallow cover for a binding group: keep each target slot's shallow
/// literal anchors, escalating to structural / deeper tiers only if the group
/// does not yet resolve uniquely to the right bindings. Unlike the read-off
/// paths, it holes every init (objects included) through plain [`hole_expr`] (an
/// object init holes via `hole_object` — list hole only where a run dropped — not
/// the padded key-set form). The robustness-favoring counterpart to the sparse
/// [`try_var_group_read_off`]; for a single target that no anchor set singles out,
/// falls back to enclosing-context anchoring. Returns `None` for a multi-target
/// residual no cover resolves.
fn keep_shallow_group_selector(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    target_slots: &BTreeSet<usize>,
) -> Result<Option<SpecializedSelector>> {
    let export_for = |runtime: &str| {
        targets
            .iter()
            .find(|target| target.runtime_binding == runtime)
            .map(|target| target.export_name.clone())
    };
    let render_with = |kept: &BTreeSet<AnchorSpan>,
                       regex_anchors: &BTreeMap<AnchorSpan, String>|
     -> Result<String> {
        render_var_slots(
            var,
            target_slots,
            &export_for,
            kept,
            regex_anchors,
            &hole_expr,
        )
    };

    let mut candidates = AnchorCandidates::default();
    for (idx, declarator) in var.decls.iter().enumerate() {
        if target_slots.contains(&idx) {
            if let Some(init) = &declarator.init {
                collect_expr_anchors(init, 0, &mut candidates);
            }
        }
    }

    let no_regex = BTreeMap::new();
    let resolves = |kept: &BTreeSet<AnchorSpan>| -> Result<bool> {
        Ok(
            prove_synthesized_selector(index, decl, targets, &render_with(kept, &no_regex)?)
                .is_ok(),
        )
    };
    // Always keep each slot's shallow literals (a group's declarators are its
    // meaningful targets), then escalate to structural/deeper anchors only if
    // the group does not yet resolve uniquely to the right bindings.
    let mut kept: BTreeSet<AnchorSpan> = candidates.shallow_literals().into_iter().collect();
    if !resolves(&kept)? {
        for tier in candidates.deep_cover_tiers() {
            kept.extend(tier);
            if resolves(&kept)? {
                break;
            }
        }
        if !resolves(&kept)? {
            // Enclosing-context anchoring (#2315): the var's own anchors (read-off
            // + keep-shallow) cannot single it out among same-shape siblings —
            // an alpha-only initializer (`new ()`) or a near-duplicate emitted
            // helper. For the single-target case, pin a stable adjacent declaration
            // as context; the degenerate `const X = ANYTHING` scaffold is acceptable
            // here because the neighbor carries the uniqueness. Multi-target group
            // residuals stay as debt (tuple-aware context anchoring is future work).
            if let [target] = targets {
                let scaffold = render_with(&BTreeSet::new(), &no_regex)?;
                return render_via_neighbor_context(index, decl, target, &scaffold);
            }
            return Ok(None);
        }
    }

    // Regex-literal upgrade over the kept string literals of every target slot
    // (shared with the read-off path).
    let mut regex_candidates: BTreeMap<AnchorSpan, String> = BTreeMap::new();
    for (idx, declarator) in var.decls.iter().enumerate() {
        if !target_slots.contains(&idx) {
            continue;
        }
        if let Some(init) = &declarator.init {
            regex_candidates.extend(collect_regex_anchor_candidates(init));
        }
    }
    let regex_anchors =
        accepted_regex_anchors(index, decl, targets, &regex_candidates, &kept, &render_with)?;

    let source = render_with(&kept, &regex_anchors)?;
    let rewritten_holes = holes_present(&source);
    Ok(Some(SpecializedSelector {
        match_source: source,
        rewritten_holes,
    }))
}
