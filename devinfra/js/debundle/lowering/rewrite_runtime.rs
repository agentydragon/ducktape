//! Rewrite runtime source URLs in a lowered module body so that
//! `new URL("./foo.bin", import.meta.url)` and `import.meta.url`
//! still resolve correctly after the body is moved from the source
//! chunk to a per-module target file.

use artifact::paths::join_module_path;
use artifact::runtime_resources::{RuntimeResourceDestinations, RuntimeResourceKind};
use js_ast::{
    is_dotted_import_meta_property, is_worker_constructor, new_url_expr, set_str_value, str_value,
};
use swc_ecma_ast::*;
use swc_ecma_visit::{VisitMut, VisitMutWith};

pub(super) fn rewrite_runtime_sources_for_target(
    body: &mut [ModuleItem],
    source_chunk_id: &str,
    destinations: &RuntimeResourceDestinations,
    target_file: &str,
) {
    let mut rewriter = RuntimeSourceRewriter {
        destinations,
        target_file: join_module_path(&[source_chunk_id, target_file]),
    };
    for item in body {
        item.visit_mut_with(&mut rewriter);
    }
}

struct RuntimeSourceRewriter<'a> {
    destinations: &'a RuntimeResourceDestinations,
    target_file: String,
}

impl RuntimeSourceRewriter<'_> {
    fn rewrite(&self, source: &str, kind: RuntimeResourceKind) -> String {
        self.destinations
            .get(&(kind, source.to_string()))
            .and_then(|destination| destination.relative_url(&self.target_file))
            .unwrap_or_else(|| source.to_string())
    }
}

impl VisitMut for RuntimeSourceRewriter<'_> {
    fn visit_mut_call_expr(&mut self, call: &mut CallExpr) {
        call.visit_mut_children_with(self);
        if matches!(call.callee, Callee::Import(_))
            && let Some(first) = call.args.first_mut()
            && let Expr::Lit(Lit::Str(source)) = &mut *first.expr
        {
            set_str_value(
                source,
                self.rewrite(&str_value(source), RuntimeResourceKind::DynamicImport),
            );
        }
    }

    fn visit_mut_new_expr(&mut self, new_expr: &mut NewExpr) {
        new_expr.visit_mut_children_with(self);
        if let Some(source) = new_url_import_meta_str_mut(new_expr) {
            let rewritten = self.rewrite(&str_value(source), RuntimeResourceKind::Url);
            set_str_value(source, rewritten);
            return;
        }
        if !is_worker_constructor(new_expr) {
            return;
        }
        let Some(args) = new_expr.args.as_mut() else {
            return;
        };
        let Some(first) = args.first_mut() else {
            return;
        };
        let Expr::Lit(Lit::Str(source)) = &*first.expr else {
            return;
        };
        *first.expr = new_url_expr(&self.rewrite(&str_value(source), RuntimeResourceKind::Worker));
    }
}

fn new_url_import_meta_str_mut(node: &mut NewExpr) -> Option<&mut Str> {
    let Expr::Ident(callee) = &*node.callee else {
        return None;
    };
    if callee.sym != *"URL" {
        return None;
    }
    let args = node.args.as_mut()?;
    let (first, rest) = args.split_first_mut()?;
    let second = rest.first()?;
    if first.spread.is_some()
        || second.spread.is_some()
        || !is_dotted_import_meta_property(&second.expr, "url")
    {
        return None;
    }
    if let Expr::Lit(Lit::Str(source)) = &mut *first.expr {
        Some(source)
    } else {
        None
    }
}
