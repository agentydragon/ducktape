//! Single-target class selector minimization (read-off + class-member holing).

use std::collections::BTreeSet;

use anyhow::Result;
use source_match_holes::ANYTHING_HOLE_KEYWORD;
use swc_common::{DUMMY_SP, Spanned};
use swc_ecma_ast::*;

use super::read_off_candidates;
use crate::render::{
    AnchorSpan, anything_expr, anything_param, emit_selector, holed_block, ident_node,
    node_retains_any,
};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
};

/// The form-specific prune + codegen for a class-declaration selector: render the
/// class holed down to the spans in `kept` (`extends` always holed to `ANYTHING`,
/// member runs absorbed by `ANYTHING` class-member holes), named for the target
/// export. Shared by the single-pick and `--candidates N` paths.
fn class_render_with<'a>(
    class: &'a Class,
    target: &'a SynthesizedTargetBinding,
) -> impl Fn(&BTreeSet<AnchorSpan>) -> Result<String> + 'a {
    move |kept: &BTreeSet<AnchorSpan>| {
        let mut holed_class = class.clone();
        // A minified superclass identifier is alpha-wildcarded, so always hole
        // `extends` to ANYTHING — it still discriminates "has a superclass" from a
        // bare class without pinning the volatile name.
        holed_class.super_class = class
            .super_class
            .as_ref()
            .map(|_| Box::new(anything_expr()));
        holed_class.body = hole_class_members(&class.body, kept);
        emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Class(ClassDecl {
            ident: ident_node(&target.export_name),
            declare: false,
            class: Box::new(holed_class),
        }))))
    }
}

/// Up to `limit` ranked candidate selectors for the class — the
/// `synthesize-selectors --candidates N` menu. `limit == 1` is the single pick
/// (the dispatcher's single-selector path is this at `limit 1`).
///
/// Single-target classes read off their minimal anchor set the same way
/// functions and objects do: the holed scaffold pins the class kind plus
/// `extends ANYTHING`, and value anchors (a member name, a literal or callee
/// inside a member body) map to their token spans so only the member runs
/// carrying them survive between `ANYTHING` class-member run holes. A class the
/// read-off cannot single out through its own value features yields no candidate
/// and is reported as debt (never a full-AST pin).
pub(crate) fn minimize_class_selector_candidates(
    index: &ChunkSelectorIndex,
    class: &Class,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    limit: usize,
) -> Result<Vec<SpecializedSelector>> {
    read_off_candidates(
        index,
        decl,
        target,
        &class_render_with(class, target),
        limit,
    )
}

/// A class-member run-absorber hole, emitted as an `ANYTHING;` no-init field —
/// the only spelling the matcher accepts in class-member position.
fn class_rest_member() -> ClassMember {
    ClassMember::ClassProp(ClassProp {
        span: DUMMY_SP,
        key: PropName::Ident(IdentName::new(ANYTHING_HOLE_KEYWORD.into(), DUMMY_SP)),
        value: None,
        type_ann: None,
        is_static: false,
        decorators: vec![],
        accessibility: None,
        is_abstract: false,
        is_optional: false,
        is_override: false,
        readonly: false,
        declare: false,
        definite: false,
    })
}

fn hole_class_members(members: &[ClassMember], kept: &BTreeSet<AnchorSpan>) -> Vec<ClassMember> {
    let mut out = Vec::new();
    let mut dropped_run = false;
    for member in members {
        if node_retains_any(member.span(), kept) {
            if dropped_run {
                out.push(class_rest_member());
                dropped_run = false;
            }
            out.push(hole_class_member(member, kept));
        } else {
            dropped_run = true;
        }
    }
    if dropped_run || out.is_empty() {
        out.push(class_rest_member());
    }
    out
}

fn hole_class_member(member: &ClassMember, kept: &BTreeSet<AnchorSpan>) -> ClassMember {
    match member {
        ClassMember::Method(m) => {
            let mut holed = m.clone();
            holed.function.params = m.function.params.iter().map(|_| anything_param()).collect();
            holed.function.body = m.function.body.as_ref().map(|body| holed_block(body, kept));
            ClassMember::Method(holed)
        }
        ClassMember::PrivateMethod(m) => {
            let mut holed = m.clone();
            holed.function.params = m.function.params.iter().map(|_| anything_param()).collect();
            holed.function.body = m.function.body.as_ref().map(|body| holed_block(body, kept));
            ClassMember::PrivateMethod(holed)
        }
        ClassMember::Constructor(ctor) => {
            let mut holed = ctor.clone();
            holed.params = ctor
                .params
                .iter()
                .map(|_| ParamOrTsParamProp::Param(anything_param()))
                .collect();
            holed.body = ctor.body.as_ref().map(|body| holed_block(body, kept));
            ClassMember::Constructor(holed)
        }
        // Class fields and other members carrying a kept anchor: keep verbatim.
        _ => member.clone(),
    }
}
