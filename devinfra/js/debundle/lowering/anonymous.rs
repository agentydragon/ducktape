//! Resolve `anonymous_statements[]` selectors on a spec
//! `LogicalRequest` to pre-split body ordinals in the runtime
//! module. Match equality ignores both `Span` and `SyntaxContext`
//! because needle and runtime are parsed in different `resolver`
//! passes; the `SyntaxContextStripper` `VisitMut` clears every
//! `ctxt` field on both sides before `eq_ignore_span`.

use super::*;

/// Resolve every `anonymous_match_sources` entry on `request` to a
/// pre-split body index in `runtime_module`'s top-level body. The
/// resolver requires exactly one match per entry — a 0-match or
/// ambiguous-match selector is a spec error.
pub(super) fn resolve_anonymous_statement_ordinals(
    request: &LogicalRequest,
    runtime_module: &Module,
) -> Result<Vec<usize>> {
    let mut resolved = Vec::with_capacity(request.anonymous_match_sources.len());
    for match_source in &request.anonymous_match_sources {
        let parsed = js_ast::parse_js_module_ast(
            &format!("<anonymous_statement match in {}>", request.id),
            match_source,
        )
        .with_context(|| {
            format!(
                "logical_module {}: anonymous_statements[].match did not parse as JS:\n{match_source}",
                request.id
            )
        })?;
        let parsed_items: Vec<&ModuleItem> = parsed.body.iter().collect();
        let needle = match parsed_items.as_slice() {
            [single] => *single,
            [] => bail!(
                "logical_module {}: anonymous_statements[].match parsed to zero \
                 statements; selector source must contain exactly one top-level \
                 statement:\n{match_source}",
                request.id,
            ),
            _ => bail!(
                "logical_module {}: anonymous_statements[].match parsed to {} \
                 statements; selector source must contain exactly one top-level \
                 statement:\n{match_source}",
                request.id,
                parsed_items.len(),
            ),
        };
        // `eq_ignore_span` on `Ident` compares `(sym, ctxt)`, so we
        // must strip `SyntaxContext` from both sides before comparing:
        // `needle` was freshly parsed (gets one set of resolver marks)
        // while `runtime_module` was parsed in a different pass (got
        // different marks for the same source-level identifier).
        let needle_normalized = clear_syntax_contexts(needle);
        let matches: Vec<usize> = runtime_module
            .body
            .iter()
            .enumerate()
            .filter_map(|(ordinal, item)| {
                let item_normalized = clear_syntax_contexts(item);
                if needle_normalized.eq_ignore_span(&item_normalized) {
                    Some(ordinal)
                } else {
                    None
                }
            })
            .collect();
        match matches.as_slice() {
            [single] => resolved.push(*single),
            [] => bail!(
                "logical_module {}: anonymous_statements[].match did not match any \
                 top-level statement in the chunk. Selector:\n{match_source}",
                request.id,
            ),
            multiple => bail!(
                "logical_module {}: anonymous_statements[].match is ambiguous — \
                 matched {} top-level statements at ordinals {:?}. Refine the \
                 selector. Source:\n{match_source}",
                request.id,
                multiple.len(),
                multiple,
            ),
        }
    }
    Ok(resolved)
}

/// Walks an AST node and resets every `SyntaxContext` to
/// `SyntaxContext::empty()`. Used to compare AST nodes that were
/// parsed in different `resolver` passes — each pass mints fresh
/// marks, so two structurally-identical bindings have different
/// `(sym, ctxt)` pairs and `eq_ignore_span` (which compares ctxt)
/// would otherwise reject the match. Covers every node that carries
/// a `ctxt` field in swc_ecma_ast.
struct SyntaxContextStripper;

impl VisitMut for SyntaxContextStripper {
    fn visit_mut_ident(&mut self, ident: &mut Ident) {
        ident.visit_mut_children_with(self);
        ident.ctxt = SyntaxContext::empty();
    }

    fn visit_mut_function(&mut self, node: &mut Function) {
        node.visit_mut_children_with(self);
        node.ctxt = SyntaxContext::empty();
    }

    fn visit_mut_class(&mut self, node: &mut Class) {
        node.visit_mut_children_with(self);
        node.ctxt = SyntaxContext::empty();
    }

    fn visit_mut_private_prop(&mut self, node: &mut PrivateProp) {
        node.visit_mut_children_with(self);
        node.ctxt = SyntaxContext::empty();
    }

    fn visit_mut_constructor(&mut self, node: &mut Constructor) {
        node.visit_mut_children_with(self);
        node.ctxt = SyntaxContext::empty();
    }

    fn visit_mut_block_stmt(&mut self, node: &mut BlockStmt) {
        node.visit_mut_children_with(self);
        node.ctxt = SyntaxContext::empty();
    }

    fn visit_mut_var_decl(&mut self, node: &mut VarDecl) {
        node.visit_mut_children_with(self);
        node.ctxt = SyntaxContext::empty();
    }

    fn visit_mut_call_expr(&mut self, node: &mut CallExpr) {
        node.visit_mut_children_with(self);
        node.ctxt = SyntaxContext::empty();
    }

    fn visit_mut_new_expr(&mut self, node: &mut NewExpr) {
        node.visit_mut_children_with(self);
        node.ctxt = SyntaxContext::empty();
    }

    fn visit_mut_arrow_expr(&mut self, node: &mut ArrowExpr) {
        node.visit_mut_children_with(self);
        node.ctxt = SyntaxContext::empty();
    }

    fn visit_mut_tagged_tpl(&mut self, node: &mut TaggedTpl) {
        node.visit_mut_children_with(self);
        node.ctxt = SyntaxContext::empty();
    }

    fn visit_mut_opt_call(&mut self, node: &mut OptCall) {
        node.visit_mut_children_with(self);
        node.ctxt = SyntaxContext::empty();
    }
}

fn clear_syntax_contexts(item: &ModuleItem) -> ModuleItem {
    let mut cloned = item.clone();
    cloned.visit_mut_with(&mut SyntaxContextStripper);
    cloned
}
