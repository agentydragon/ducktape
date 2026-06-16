//! Single-target function selector minimization (read-off).

use std::collections::BTreeSet;

use anyhow::Result;
use swc_ecma_ast::*;

use super::render_via_read_off;
use crate::render::{AnchorSpan, emit_selector, hole_function, ident_node};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
};

pub(crate) fn minimize_function_selector(
    index: &ChunkSelectorIndex,
    function: &Function,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
) -> Result<Option<SpecializedSelector>> {
    if function.body.is_none() {
        return Ok(None);
    }
    let render_with = |kept: &BTreeSet<AnchorSpan>| -> Result<String> {
        emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Fn(FnDecl {
            ident: ident_node(&target.export_name),
            declare: false,
            function: Box::new(hole_function(function, kept)),
        }))))
    };
    // Single-target functions read their minimal anchor set off the shape index.
    // The holed scaffold already pins the param arity (one `ANYTHING` per real
    // param), so a structural / skeleton anchor needs no kept span; value anchors
    // (string/number/bool literals, member/method names, member-path callees) map
    // to the spans of the tokens that exhibit them. The matcher proves the result
    // (gate 1); a target the read-off cannot single out returns `None` and is
    // reported as debt (never a full-AST pin).
    render_via_read_off(index, decl, target, &render_with)
}
