//! Per-module body facts. The raw collector walks an AST with `RefCollector`
//! (which respects function-level shadowing) and finds imported, provided,
//! and referenced Ids. At the naturalization boundary, each reference is
//! paired with its source binding Id before any import/export planner sees it.

use crate::chunk_ast::{binding_names, top_level_declaration_ids};
use std::collections::{BTreeMap, BTreeSet, HashSet};
use swc_common::SyntaxContext;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

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

/// One reference after naturalization. `source_id` is present only when the
/// reference has the source chunk's top-level binding context. Function-local
/// and unresolved references cannot be queried in source binding tables.
/// A free alias may leave some uses of a source under the old spelling while
/// changing others, so this relation belongs to references, not to a single
/// name per source binding.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(super) struct BindingUse {
    source_id: Option<Id>,
    emitted_id: Id,
}

impl BindingUse {
    pub(super) fn source_id(&self) -> Option<&Id> {
        self.source_id.as_ref()
    }

    pub(super) fn emitted_id(&self) -> &Id {
        &self.emitted_id
    }
}

/// The only reference facts accepted by export growth and import planning.
/// Top-level source references carry both source and emitted identities;
/// local or unresolved references have no source-table identity.
pub(super) struct NaturalizedBodyFacts {
    imported_locals: HashSet<Id>,
    provided_locals: HashSet<Id>,
    binding_uses: HashSet<BindingUse>,
}

impl NaturalizedBodyFacts {
    pub(super) fn from_raw(
        facts: ModuleBodyFacts,
        source_by_emitted: &BTreeMap<Id, Id>,
        source_top_level_ctxt: SyntaxContext,
    ) -> Self {
        let binding_uses = facts
            .referenced_idents
            .into_iter()
            .map(|emitted_id| BindingUse {
                source_id: (emitted_id.1 == source_top_level_ctxt).then(|| {
                    source_by_emitted
                        .get(&emitted_id)
                        .cloned()
                        .unwrap_or_else(|| emitted_id.clone())
                }),
                emitted_id,
            })
            .collect();
        Self {
            imported_locals: facts.imported_locals,
            provided_locals: facts.provided_locals,
            binding_uses,
        }
    }

    pub(super) fn binding_uses(&self) -> impl Iterator<Item = &BindingUse> {
        self.binding_uses.iter()
    }

    pub(super) fn provides(&self, emitted_id: &Id) -> bool {
        self.provided_locals.contains(emitted_id)
    }

    pub(super) fn imports(&self, emitted_id: &Id) -> bool {
        self.imported_locals.contains(emitted_id)
    }
}

#[derive(Default)]
pub(super) struct RefCollector {
    /// `Id`-keyed references collected from the body. The hygiene-aware
    /// `Id = (sym, ctxt)` distinguishes same-named bindings declared in
    /// different scopes (a function parameter `x` vs. a module-level `x`).
    pub(super) ids: HashSet<Id>,
    /// Sym-based shadow filter. When a function parameter shadows an outer
    /// name, the inner `Ident` already has a distinct `ctxt` from the
    /// outer binding, so the `ids` set wouldn't match the outer `Id` anyway.
    /// However, downstream code still compares collected ids against
    /// String-keyed tables (e.g. `declaration_by_name`) via sym-only
    /// matching, so suppressing the inner-scope reference here prevents
    /// false "this sym is referenced" hits when only the inner scope uses it.
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
