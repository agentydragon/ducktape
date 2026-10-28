//! Whole-chunk dynamic scope prevents stable lexical identity proofs.

use super::*;

#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct DynamicScopeFact {
    pub(crate) present: bool,
}

impl DynamicScopeFact {
    pub(crate) fn analyze(body: &[TopLevelItemView<'_>]) -> Self {
        let mut scanner = DynamicScopeScanner::default();
        for item in body {
            item.as_module_item().visit_with(&mut scanner);
        }
        Self {
            present: scanner.found,
        }
    }
}

#[derive(Default)]
struct DynamicScopeScanner {
    found: bool,
}

impl Visit for DynamicScopeScanner {
    fn visit_with_stmt(&mut self, node: &WithStmt) {
        self.found = true;
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if matches!(&node.callee, Callee::Expr(callee)
            if matches!(callee.unwrap_parens(), Expr::Ident(ident) if ident.sym.as_ref() == "eval"))
        {
            self.found = true;
        }
        node.visit_children_with(self);
    }
}
