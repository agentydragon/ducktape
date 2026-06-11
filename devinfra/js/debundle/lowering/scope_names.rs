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
/// [`collect_nested_binding_names`]: records every name bound anywhere
/// under the visited node (declarations, params, patterns, fn/class expr
/// names, import specifier locals).
struct BindingNameCollector {
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

/// Names bound strictly below `body`'s top level — everything
/// [`collect_local_binding_names`] sees except the top-level declarations'
/// own root names (which [`collect_occupied_local_names`] reports). Seal
/// validation distinguishes the two: a rename target colliding with a
/// top-level name is a root collision (vacatable by renaming that binding
/// away); a target bound anywhere below the top level could capture
/// renamed references and is rejected outright. A name bound at BOTH
/// levels (`const a = () => { let a; }`) appears in both sets.
pub(super) fn collect_nested_binding_names(body: &[ModuleItem]) -> BTreeSet<String> {
    let mut collector = BindingNameCollector {
        names: BTreeSet::new(),
    };
    for item in body {
        match item {
            // Import locals are root bindings; imports contain nothing else.
            ModuleItem::ModuleDecl(ModuleDecl::Import(_)) => {}
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                collect_nested_from_decl(&export_decl.decl, &mut collector);
            }
            // The declared name is a root binding; only the body is nested.
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(default_decl)) => {
                match &default_decl.decl {
                    DefaultDecl::Fn(function) => function.function.visit_with(&mut collector),
                    DefaultDecl::Class(class) => class.class.visit_with(&mut collector),
                    DefaultDecl::TsInterfaceDecl(_) => {}
                }
            }
            ModuleItem::Stmt(Stmt::Decl(decl)) => collect_nested_from_decl(decl, &mut collector),
            // Non-declaration statements bind nothing at the root level;
            // anything they bind (for-heads, block-scoped decls, function
            // expressions) is nested. `var` hoisted out of a top-level
            // block is recorded as nested too — over-restriction, matching
            // the rename visitors' block-shadow over-suppression.
            other => other.visit_with(&mut collector),
        }
    }
    collector.names
}

fn collect_nested_from_decl(decl: &Decl, collector: &mut BindingNameCollector) {
    match decl {
        // The declared name is the root binding; params and body are nested.
        Decl::Fn(function) => function.function.visit_with(collector),
        Decl::Class(class) => class.class.visit_with(collector),
        Decl::Var(var) => {
            for declarator in &var.decls {
                collect_nested_from_root_pat(&declarator.name, collector);
                if let Some(init) = &declarator.init {
                    init.visit_with(collector);
                }
            }
        }
        _ => decl.visit_with(collector),
    }
}

/// Walk a top-level declarator pattern recording only bindings that are
/// NOT the pattern's own root binders: pattern defaults and computed keys
/// can contain function expressions whose params/locals are nested.
fn collect_nested_from_root_pat(pat: &Pat, collector: &mut BindingNameCollector) {
    match pat {
        Pat::Ident(_) => {}
        Pat::Rest(rest) => collect_nested_from_root_pat(&rest.arg, collector),
        Pat::Assign(assign) => {
            collect_nested_from_root_pat(&assign.left, collector);
            assign.right.visit_with(collector);
        }
        Pat::Array(array) => {
            for elem in array.elems.iter().flatten() {
                collect_nested_from_root_pat(elem, collector);
            }
        }
        Pat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(key_value) => {
                        if let PropName::Computed(computed) = &key_value.key {
                            computed.expr.visit_with(collector);
                        }
                        collect_nested_from_root_pat(&key_value.value, collector);
                    }
                    ObjectPatProp::Assign(assign) => {
                        if let Some(value) = &assign.value {
                            value.visit_with(collector);
                        }
                    }
                    ObjectPatProp::Rest(rest) => collect_nested_from_root_pat(&rest.arg, collector),
                }
            }
        }
        Pat::Invalid(_) | Pat::Expr(_) => {}
    }
}
