use super::*;

#[derive(Default)]
pub(crate) struct AlphaCanonicalScope {
    names: BTreeMap<Atom, Atom>,
}

#[derive(Default)]
pub(crate) struct AlphaIdentCanonicalizer {
    next: usize,
    scopes: Vec<AlphaCanonicalScope>,
    reserved_idents: BTreeSet<String>,
}

impl AlphaIdentCanonicalizer {
    pub(crate) fn new(wildcard_idents: &WildcardIdents) -> Self {
        Self {
            scopes: vec![AlphaCanonicalScope::default()],
            reserved_idents: wildcard_idents
                .expressions
                .iter()
                .chain(&wildcard_idents.statements)
                .chain(&wildcard_idents.statement_lists)
                .chain(&wildcard_idents.declarator_lists)
                .chain(&wildcard_idents.argument_lists)
                .chain(&wildcard_idents.object_property_lists)
                .chain(&wildcard_idents.patterns)
                .cloned()
                .collect(),
            ..Self::default()
        }
    }
}

impl AlphaIdentCanonicalizer {
    fn with_scope(&mut self, f: impl FnOnce(&mut Self)) {
        self.scopes.push(AlphaCanonicalScope::default());
        f(self);
        self.scopes.pop();
    }

    fn visible_canonical(&self, sym: &Atom) -> Option<Atom> {
        self.scopes
            .iter()
            .rev()
            .find_map(|scope| scope.names.get(sym).cloned())
    }

    fn canonical_ref(&mut self, sym: &Atom) -> Atom {
        if let Some(existing) = self.visible_canonical(sym) {
            return existing;
        }
        self.canonical_binding(sym)
    }

    fn canonical_binding(&mut self, sym: &Atom) -> Atom {
        let scope = self
            .scopes
            .last_mut()
            .expect("alpha canonicalizer always has a root scope");
        if let Some(existing) = scope.names.get(sym) {
            return existing.clone();
        }
        let canonical = Atom::from(format!("__debundle_alpha_{}", self.next));
        self.next += 1;
        scope.names.insert(sym.clone(), canonical.clone());
        canonical
    }
}

impl VisitMut for AlphaIdentCanonicalizer {
    fn visit_mut_ident(&mut self, ident: &mut swc_ecma_ast::Ident) {
        if self.reserved_idents.contains(ident.sym.as_ref()) {
            return;
        }
        ident.sym = self.canonical_ref(&ident.sym);
    }

    fn visit_mut_binding_ident(&mut self, ident: &mut BindingIdent) {
        if !self.reserved_idents.contains(ident.id.sym.as_ref()) {
            ident.id.sym = self.canonical_binding(&ident.id.sym);
        }
        ident.type_ann.visit_mut_with(self);
    }

    fn visit_mut_function(&mut self, function: &mut Function) {
        self.with_scope(|visitor| function.visit_mut_children_with(visitor));
    }

    fn visit_mut_arrow_expr(&mut self, arrow: &mut ArrowExpr) {
        self.with_scope(|visitor| arrow.visit_mut_children_with(visitor));
    }

    fn visit_mut_catch_clause(&mut self, catch_clause: &mut CatchClause) {
        self.with_scope(|visitor| catch_clause.visit_mut_children_with(visitor));
    }

    fn visit_mut_member_expr(&mut self, member: &mut MemberExpr) {
        member.obj.visit_mut_with(self);
        match &mut member.prop {
            MemberProp::Ident(_) | MemberProp::PrivateName(_) => {}
            MemberProp::Computed(prop) => prop.expr.visit_mut_with(self),
        }
    }

    fn visit_mut_prop(&mut self, prop: &mut Prop) {
        match prop {
            Prop::Shorthand(_) => {}
            Prop::KeyValue(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.value.visit_mut_with(self);
            }
            Prop::Assign(prop) => {
                prop.value.visit_mut_with(self);
            }
            Prop::Getter(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.type_ann.visit_mut_with(self);
                prop.body.visit_mut_with(self);
            }
            Prop::Setter(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.this_param.visit_mut_with(self);
                prop.param.visit_mut_with(self);
                prop.body.visit_mut_with(self);
            }
            Prop::Method(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.function.visit_mut_with(self);
            }
        }
    }
}

pub(crate) fn visit_computed_prop_name(
    prop_name: &mut PropName,
    visitor: &mut AlphaIdentCanonicalizer,
) {
    if let PropName::Computed(prop_name) = prop_name {
        prop_name.expr.visit_mut_with(visitor);
    }
}
