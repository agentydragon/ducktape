//! Single-target class selector minimization (read-off + class-member holing).

use std::collections::BTreeSet;

use anyhow::Result;
use swc_common::Spanned;
use swc_ecma_ast::*;

use super::read_off_candidates;
use crate::render::{
    AnchorSpan, anything_expr, class_member_hole, collapse_omitted_runs, emit_selector,
    hole_function, holed_function_body, ident_node, node_retains_any, retain_or_hole_param,
    retained_body_identifiers,
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
        emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Class(ClassDecl {
            ident: ident_node(&target.export_name),
            declare: false,
            class: Box::new(hole_class(class, kept)),
        }))))
    }
}

/// Shared pruning for declarations, expression initializers and neighbor context.
pub(super) fn hole_class(class: &Class, kept: &BTreeSet<AnchorSpan>) -> Class {
    let mut holed = class.clone();
    // Preserve superclass presence, not its rebuild-volatile identifier.
    holed.super_class = class
        .super_class
        .as_ref()
        .map(|_| Box::new(anything_expr()));
    holed.body = hole_class_members(&class.body, kept);
    holed
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
    index: &ChunkSelectorIndex<'_>,
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

fn hole_class_members(members: &[ClassMember], kept: &BTreeSet<AnchorSpan>) -> Vec<ClassMember> {
    let mut out = collapse_omitted_runs(
        members.iter().map(|member| {
            node_retains_any(member.span(), kept).then(|| hole_class_member(member, kept))
        }),
        class_member_hole,
    );
    if out.is_empty() {
        out.push(class_member_hole());
    }
    out
}

fn hole_class_member(member: &ClassMember, kept: &BTreeSet<AnchorSpan>) -> ClassMember {
    match member {
        ClassMember::Method(m) => {
            let mut holed = m.clone();
            holed.function = Box::new(hole_function(&m.function, kept));
            ClassMember::Method(holed)
        }
        ClassMember::PrivateMethod(m) => {
            let mut holed = m.clone();
            holed.function = Box::new(hole_function(&m.function, kept));
            ClassMember::PrivateMethod(holed)
        }
        ClassMember::Constructor(ctor) => {
            let mut holed = ctor.clone();
            holed.body = ctor
                .body
                .as_ref()
                .map(|body| holed_function_body(body, kept));
            let retained = retained_body_identifiers(holed.body.as_ref());
            holed.params = ctor
                .params
                .iter()
                .map(|param| match param {
                    ParamOrTsParamProp::Param(param) => {
                        ParamOrTsParamProp::Param(retain_or_hole_param(param, &retained, kept))
                    }
                    // TypeScript parameter properties are not part of the JS
                    // fixtures; keeping them verbatim is safer than severing
                    // a retained body reference.
                    ParamOrTsParamProp::TsParamProp(_) => param.clone(),
                })
                .collect();
            ClassMember::Constructor(holed)
        }
        // Class fields and other members carrying a kept anchor: keep verbatim.
        _ => member.clone(),
    }
}
