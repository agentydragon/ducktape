//! Single-target function selector minimization (read-off).

use std::collections::BTreeSet;

use anyhow::Result;
use swc_ecma_ast::*;

use super::read_off_candidates;
use crate::render::{AnchorSpan, emit_selector, hole_function, ident_node};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
};

/// The form-specific prune + codegen for a function-declaration selector: render
/// the function holed down to the spans in `kept`, named for the target export.
/// Shared by the single-pick and `--candidates N` paths.
fn function_render_with<'a>(
    function: &'a Function,
    target: &'a SynthesizedTargetBinding,
) -> impl Fn(&BTreeSet<AnchorSpan>) -> Result<String> + 'a {
    move |kept: &BTreeSet<AnchorSpan>| {
        emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Fn(FnDecl {
            ident: ident_node(&target.export_name),
            declare: false,
            function: Box::new(hole_function(function, kept)),
        }))))
    }
}

/// Up to `limit` ranked candidate selectors for the function — the
/// `synthesize-selectors --candidates N` menu. `limit == 1` is the single pick
/// (the dispatcher's single-selector path is this at `limit 1`).
///
/// Single-target functions read their minimal anchor set off the shape index.
/// The holed scaffold already pins the param arity (one `ANYTHING` per real
/// param), so a structural / skeleton anchor needs no kept span; value anchors
/// (string/number/bool literals, member/method names, member-path callees) map
/// to the spans of the tokens that exhibit them. The matcher proves the result
/// (gate 1); a target the read-off cannot single out yields no candidate and is
/// reported as debt (never a full-AST pin).
pub(crate) fn minimize_function_selector_candidates(
    index: &ChunkSelectorIndex,
    function: &Function,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    limit: usize,
) -> Result<Vec<SpecializedSelector>> {
    if function.body.is_none() {
        return Ok(Vec::new());
    }
    read_off_candidates(
        index,
        decl,
        target,
        &function_render_with(function, target),
        limit,
    )
}
