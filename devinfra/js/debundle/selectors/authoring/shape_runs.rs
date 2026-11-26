//! Anti-unification: merges adjacent same-shape single-target groups into one claim.

use std::collections::BTreeSet;

use binding_targets::DeclShape;
use swc_common::DUMMY_SP;
use swc_ecma_ast::*;
use swc_ecma_visit::{VisitMut, VisitMutWith};

use crate::chunk_index::ChunkSelectorIndex;
use crate::group_synthesis::{SynthesizedDeclGroup, SynthesizedSelectorGroup};
use crate::proof::prove_synthesized_selector;

// Anti-unification grouping.
//
// `synthesize_simplest_selector_for_group` already groups members that share an
// enclosing declaration (multi-declarator var statements). The second grouping
// trigger — "minimal selectors overlap beyond a threshold" — collapses a run of
// adjacent, near-identical *single-target declarations* whose individually-
// minimized selectors share the bulk of their shape into one run-based
// grouped source_match, instead of N standalone source_match selectors. This started
// function-only (four context-accessor hooks each `function useX() { return
// ANYTHING.…; }`) and now generalizes to any single-target declaration kind:
// sibling class declarations, statement-run functions, object/var declarations,
// etc. — the same co-occurrence idea, not specialized to functions.
//
// The overlap test runs on the *minimized* selectors: same-purpose siblings
// collapse to the same minimal shape (`ANYTHING.<key>`), so two selectors are
// "same shape" iff their canonical signatures — every identifier, member/key
// name, and literal value blanked, leaving only structure ([`selector_shape_signature`])
// — are equal. The merged run-selector is re-proven through the matcher gate
// (kind-agnostic multi-statement alignment); on failure the run is emitted as
// individual selectors, never an unproven group.

/// Collapse maximal runs of adjacent, same-shape single-target declaration
/// groups into one run-based grouped source_match. Other groups (multi-declarator var
/// groups, lone declarations, anything whose merged run fails the matcher gate)
/// pass through unchanged, preserving source order.
pub(crate) fn merge_adjacent_same_shape_runs(
    index: &ChunkSelectorIndex<'_>,
    groups: Vec<SynthesizedDeclGroup>,
) -> Vec<SynthesizedDeclGroup> {
    let mut merged = Vec::with_capacity(groups.len());
    let mut run: Vec<SynthesizedDeclGroup> = Vec::new();
    for group in groups {
        let extends_run = run
            .last()
            .is_some_and(|prev| same_shape_run_extends(index, prev, &group));
        if !extends_run {
            flush_same_shape_run(index, std::mem::take(&mut run), &mut merged);
        }
        run.push(group);
    }
    flush_same_shape_run(index, run, &mut merged);
    merged
}

/// Emit a candidate run: merge it into one grouped source_match when it holds ≥2 groups
/// and the merged selector proves unique, else emit each group individually.
fn flush_same_shape_run(
    index: &ChunkSelectorIndex<'_>,
    run: Vec<SynthesizedDeclGroup>,
    out: &mut Vec<SynthesizedDeclGroup>,
) {
    if run.len() >= 2
        && let Some(group) = merge_same_shape_run(index, &run)
    {
        out.push(group);
        return;
    }
    out.extend(run);
}

/// Whether `next` continues a same-shape run started by `prev`: both are
/// single-target declaration groups of the same declaration kind, they are
/// consecutive in source order, and their minimized selectors share the same
/// canonical shape.
fn same_shape_run_extends(
    index: &ChunkSelectorIndex<'_>,
    prev: &SynthesizedDeclGroup,
    next: &SynthesizedDeclGroup,
) -> bool {
    matches!(
        (single_target_decl_kind(index, prev), single_target_decl_kind(index, next)),
        (Some(prev_kind), Some(next_kind)) if prev_kind == next_kind
    ) && next.synthesized.body_idx == prev.synthesized.body_idx + 1
        && same_selector_shape(
            &prev.synthesized.match_source,
            &next.synthesized.match_source,
        )
}

/// The declaration kind of a single-target (one-member) group, or `None` when
/// the group covers multiple members (a multi-declarator var group, already
/// grouped by its shared declaration) or its declaration index is unknown. A
/// run only merges declarations of one kind so the concatenated selector stays a
/// homogeneous sibling run.
fn single_target_decl_kind(
    index: &ChunkSelectorIndex<'_>,
    group: &SynthesizedDeclGroup,
) -> Option<DeclShape> {
    if group.members.len() != 1 {
        return None;
    }
    Some(index.decls.get(group.decl_idx)?.kind)
}

/// Build one run-based grouped source_match from `run`: the selector is the run's
/// declarations concatenated in source order and binds every target. The
/// merged selector is re-proven through the matcher gate; `None` (proof failed)
/// leaves the run to be emitted individually.
fn merge_same_shape_run(
    index: &ChunkSelectorIndex<'_>,
    run: &[SynthesizedDeclGroup],
) -> Option<SynthesizedDeclGroup> {
    let first = run.first()?;
    let decl = index.decls.get(first.decl_idx)?;
    let match_source = run
        .iter()
        .map(|group| group.synthesized.match_source.as_str())
        .collect::<Vec<_>>()
        .join("\n");
    let targets = run
        .iter()
        .flat_map(|group| group.synthesized.target_bindings.iter().cloned())
        .collect::<Vec<_>>();
    prove_synthesized_selector(index, decl, &targets, &match_source).ok()?;
    let members = run
        .iter()
        .flat_map(|group| group.members.iter().cloned())
        .collect::<Vec<_>>();
    Some(SynthesizedDeclGroup {
        decl_idx: first.decl_idx,
        members,
        synthesized: SynthesizedSelectorGroup {
            body_idx: first.synthesized.body_idx,
            target_bindings: targets,
            rewritten_holes: run
                .iter()
                .flat_map(|group| group.synthesized.rewritten_holes.iter().cloned())
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect(),
            match_source,
            alternatives: Vec::new(),
        },
    })
}

/// Whether two single-declaration selector sources have the same canonical shape
/// (equal once every value-bearing leaf is blanked). `false` if either fails to
/// parse, so an unparseable source never grafts onto a run.
fn same_selector_shape(left: &str, right: &str) -> bool {
    matches!(
        (selector_shape_signature(left), selector_shape_signature(right)),
        (Some(left), Some(right)) if left == right
    )
}

/// Canonical structural signature of a selector source: the AST re-emitted with
/// every identifier, member/property name, object key, and literal value blanked
/// to a fixed placeholder, so selectors that differ only in their discriminating
/// anchors (a DRY accessor cluster) share a signature.
pub(crate) fn selector_shape_signature(match_source: &str) -> Option<String> {
    let mut module =
        js_ast::parse_js_module_ast("<selector shape signature>", match_source).ok()?;
    module.visit_mut_with(&mut ShapeSignatureCanonicalizer);
    js_ast::emit_module_source(&module).ok()
}

/// Placeholder every value-bearing leaf collapses to in a shape signature.
const SHAPE_SIGNATURE_BLANK: &str = "_";

/// Blanks identifiers, member/property names, object keys, and literal values so
/// only structural shape survives (see [`selector_shape_signature`]).
struct ShapeSignatureCanonicalizer;

impl VisitMut for ShapeSignatureCanonicalizer {
    fn visit_mut_ident(&mut self, ident: &mut Ident) {
        ident.sym = SHAPE_SIGNATURE_BLANK.into();
    }

    fn visit_mut_binding_ident(&mut self, ident: &mut BindingIdent) {
        ident.id.sym = SHAPE_SIGNATURE_BLANK.into();
        ident.type_ann.visit_mut_with(self);
    }

    fn visit_mut_member_prop(&mut self, prop: &mut MemberProp) {
        match prop {
            MemberProp::Ident(ident) => ident.sym = SHAPE_SIGNATURE_BLANK.into(),
            MemberProp::PrivateName(_) => {}
            MemberProp::Computed(computed) => computed.visit_mut_children_with(self),
        }
    }

    fn visit_mut_prop_name(&mut self, name: &mut PropName) {
        match name {
            PropName::Ident(ident) => ident.sym = SHAPE_SIGNATURE_BLANK.into(),
            PropName::Str(str_lit) => js_ast::set_str_value(str_lit, SHAPE_SIGNATURE_BLANK.into()),
            other => other.visit_mut_children_with(self),
        }
    }

    fn visit_mut_expr(&mut self, expr: &mut Expr) {
        if matches!(expr, Expr::Lit(_)) {
            *expr = Expr::Lit(Lit::Null(Null { span: DUMMY_SP }));
            return;
        }
        expr.visit_mut_children_with(self);
    }
}

#[cfg(test)]
mod adjacent_function_grouping_tests {
    use super::*;

    fn signature(source: &str) -> String {
        js_ast::with_swc_globals(|| selector_shape_signature(source).expect("selector parses"))
    }

    #[test]
    fn different_shapes_do_not_share_a_signature() {
        // A zero-arg member-return accessor anti-unifies to neither a one-arg
        // arithmetic helper (param count + body differ) nor a bare call (body
        // structure differs), so such neighbors never merge into one run.
        let accessor = "function a() { return ANYTHING.alpha; }";
        assert_ne!(
            signature(accessor),
            signature("function h(value) { return value * 2; }")
        );
        assert_ne!(
            signature(accessor),
            signature("function c() { return ANYTHING(); }")
        );
    }

    #[test]
    fn member_chain_depth_is_structural() {
        // Blanking erases key *names* but keeps chain depth, so a one-member and a
        // two-member access are distinct shapes and stay separate selectors.
        assert_ne!(
            signature("function a() { return ANYTHING.alpha; }"),
            signature("function a() { return ANYTHING.services.alpha; }")
        );
    }

    #[test]
    fn same_selector_shape_matches_same_shape_only() {
        js_ast::with_swc_globals(|| {
            assert!(same_selector_shape(
                "function a() { return ANYTHING.alpha; }",
                "function b() { return ANYTHING.beta; }",
            ));
            // A structurally different neighbor never grafts onto the run.
            assert!(!same_selector_shape(
                "function a() { return ANYTHING.alpha; }",
                "function h(value) { return value * 2; }",
            ));
        });
    }
}
