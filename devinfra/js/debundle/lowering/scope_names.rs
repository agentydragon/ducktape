use super::*;

/// Names occupying the file-scope binding namespace of `body`.
///
/// Used to disambiguate consumer-side `import { exportedName as localName }`
/// emissions whose `localName` would collide with another binding in the
/// same scope (e.g. a surviving import or top-level declaration that
/// already uses the input-bundle name). `export { name }` re-exports without
/// `from` are references, not bindings, so they aren't tracked here; the
/// IdentifierRenamer pass that follows the disambiguation rewrites their
/// `orig` ident along with every other body reference.
pub(super) fn collect_occupied_local_names(body: &[ModuleItem]) -> BTreeSet<String> {
    let mut occupied = BTreeSet::new();
    for item in body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => {
                for specifier in &import.specifiers {
                    match specifier {
                        ImportSpecifier::Named(named) => {
                            occupied.insert(named.local.sym.to_string());
                        }
                        ImportSpecifier::Default(default) => {
                            occupied.insert(default.local.sym.to_string());
                        }
                        ImportSpecifier::Namespace(namespace) => {
                            occupied.insert(namespace.local.sym.to_string());
                        }
                    }
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                for name in declaration_names(&export_decl.decl) {
                    occupied.insert(name);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(default_decl)) => {
                if let DefaultDecl::Class(class) = &default_decl.decl
                    && let Some(ident) = &class.ident
                {
                    occupied.insert(ident.sym.to_string());
                }
                if let DefaultDecl::Fn(function) = &default_decl.decl
                    && let Some(ident) = &function.ident
                {
                    occupied.insert(ident.sym.to_string());
                }
            }
            ModuleItem::Stmt(Stmt::Decl(decl)) => {
                for name in declaration_names(decl) {
                    occupied.insert(name);
                }
            }
            _ => {}
        }
    }
    occupied
}

/// Visitor behind [`collect_local_binding_names`] /
/// [`collect_binding_names_in`]: records every name bound anywhere under
/// the visited node (declarations, params, patterns, fn/class expr names,
/// import specifier locals).
pub(super) struct BindingNameCollector {
    names: BTreeSet<String>,
}

impl Visit for BindingNameCollector {
    fn visit_binding_ident(&mut self, ident: &BindingIdent) {
        self.names.insert(ident.id.sym.to_string());
    }

    fn visit_class_decl(&mut self, decl: &ClassDecl) {
        self.names.insert(decl.ident.sym.to_string());
        decl.class.visit_with(self);
    }

    fn visit_class_expr(&mut self, expr: &ClassExpr) {
        if let Some(ident) = &expr.ident {
            self.names.insert(ident.sym.to_string());
        }
        expr.class.visit_with(self);
    }

    fn visit_fn_decl(&mut self, decl: &FnDecl) {
        self.names.insert(decl.ident.sym.to_string());
        decl.function.visit_with(self);
    }

    fn visit_fn_expr(&mut self, expr: &FnExpr) {
        if let Some(ident) = &expr.ident {
            self.names.insert(ident.sym.to_string());
        }
        expr.function.visit_with(self);
    }

    fn visit_import_default_specifier(&mut self, specifier: &ImportDefaultSpecifier) {
        self.names.insert(specifier.local.sym.to_string());
    }

    fn visit_import_named_specifier(&mut self, specifier: &ImportNamedSpecifier) {
        self.names.insert(specifier.local.sym.to_string());
    }

    fn visit_import_star_as_specifier(&mut self, specifier: &ImportStarAsSpecifier) {
        self.names.insert(specifier.local.sym.to_string());
    }
}

/// Names bound anywhere under one AST node (a function, arrow,
/// constructor, …). Used to suppress renames whose target a node's
/// subtree binds — applying such a rename would capture the rewritten
/// references.
pub(super) fn collect_binding_names_in<N>(node: &N) -> BTreeSet<String>
where
    N: VisitWith<BindingNameCollector>,
{
    let mut collector = BindingNameCollector {
        names: BTreeSet::new(),
    };
    node.visit_with(&mut collector);
    collector.names
}

/// Names bound anywhere under `body`. This is stricter than file-scope
/// occupancy: readable import locals must avoid nested bindings too, or the
/// follow-up body rewrite can accidentally capture references that were
/// supposed to resolve to the import.
pub(super) fn collect_local_binding_names(body: &[ModuleItem]) -> BTreeSet<String> {
    let mut collector = BindingNameCollector {
        names: BTreeSet::new(),
    };
    for item in body {
        item.visit_with(&mut collector);
    }
    collector.names
}
