//! Per-module post-naturalize body facts: which Ids are imported,
//! provided, or referenced inside the module body. Built by walking the
//! mutated AST with `RefCollector` (a `Visit` impl that respects
//! function-level shadowing) plus pulling locals out of import specifiers.

use super::*;

#[derive(Debug, Default)]
pub(super) struct ModuleBodyFacts {
    /// Local bindings declared by an `import` statement in this module body.
    /// Keyed by the binding's hygiene-aware `Id` so two same-named bindings
    /// from different scopes are distinct.
    pub(super) imported_locals: HashSet<Id>,
    /// All locals this module body provides — `imported_locals` plus
    /// top-level declarations. Keyed by `Id`.
    pub(super) provided_locals: HashSet<Id>,
    /// Every identifier the module body references (read or write).
    /// Post-naturalize: any binding the heuristic naturalizer renamed
    /// appears here under the post-rename `(sym, ctxt)` (the rename
    /// mutates `sym` in place; `ctxt` is preserved).
    pub(super) referenced_idents: HashSet<Id>,
}

#[derive(Default)]
pub(super) struct RefCollector {
    /// `Id`-keyed references collected from the body. The hygiene-aware
    /// `Id = (sym, ctxt)` distinguishes same-named bindings declared in
    /// different scopes (a function parameter `x` vs. a module-level `x`).
    pub(super) ids: HashSet<Id>,
    /// Shadowing tracked by `sym` — kept for backwards-compatible behavior
    /// with the pre-hygiene collector. With hygiene-correct contexts the
    /// shadowed-by-sym filter is mostly redundant (the inner-scope ident
    /// has its own `ctxt`, distinct from any outer reference), but the
    /// filter preserves the exact set of `referenced_idents` produced
    /// before the `Id` migration so downstream comparisons (e.g. against
    /// String-keyed `declaration_by_name` via `sym`) don't drift.
    shadowed_scopes: Vec<BTreeSet<String>>,
}

impl Visit for RefCollector {
    fn visit_ident(&mut self, node: &Ident) {
        let name = node.sym.as_ref();
        if !self.is_shadowed(name) {
            self.ids.insert(node.to_id());
        }
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    fn visit_import_decl(&mut self, _node: &ImportDecl) {}

    fn visit_function(&mut self, node: &Function) {
        let shadowed = node
            .params
            .iter()
            .flat_map(|param| binding_names(&param.pat))
            .collect::<BTreeSet<_>>();
        self.with_shadowed_scope(shadowed, |collector| node.visit_children_with(collector));
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let shadowed = node
            .params
            .iter()
            .flat_map(binding_names)
            .collect::<BTreeSet<_>>();
        self.with_shadowed_scope(shadowed, |collector| node.visit_children_with(collector));
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        node.obj.visit_with(self);
        if let MemberProp::Computed(computed) = &node.prop {
            computed.expr.visit_with(self);
        }
    }

    fn visit_prop_name(&mut self, node: &PropName) {
        if let PropName::Computed(computed) = node {
            computed.expr.visit_with(self);
        }
    }

    fn visit_jsx_element_name(&mut self, _node: &JSXElementName) {}

    fn visit_jsx_attr_name(&mut self, _node: &JSXAttrName) {}
}

impl RefCollector {
    fn is_shadowed(&self, name: &str) -> bool {
        self.shadowed_scopes
            .iter()
            .rev()
            .any(|scope| scope.contains(name))
    }

    fn with_shadowed_scope<F: FnOnce(&mut Self)>(&mut self, names: BTreeSet<String>, f: F) {
        self.shadowed_scopes.push(names);
        f(self);
        self.shadowed_scopes.pop();
    }
}

pub(super) fn collect_module_body_facts(body: &[ModuleItem]) -> ModuleBodyFacts {
    let mut facts = ModuleBodyFacts::default();
    let mut ref_collector = RefCollector::default();
    for item in body {
        item.visit_with(&mut ref_collector);
        facts
            .provided_locals
            .extend(top_level_declaration_ids(item));
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
            for specifier in &import.specifiers {
                let local = match specifier {
                    ImportSpecifier::Named(named) => named.local.to_id(),
                    ImportSpecifier::Default(default) => default.local.to_id(),
                    ImportSpecifier::Namespace(namespace) => namespace.local.to_id(),
                };
                facts.imported_locals.insert(local.clone());
                facts.provided_locals.insert(local);
            }
        }
    }
    facts.referenced_idents = ref_collector.ids;
    facts
}
