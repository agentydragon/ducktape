use super::*;

pub(crate) fn validate_anything_holes(items: &[ModuleItem]) -> Result<()> {
    let mut collector = UnsupportedAnythingCollector::default();
    for item in items {
        item.visit_with(&mut collector);
    }
    if collector.positions.is_empty() {
        return Ok(());
    }
    collector.positions.sort();
    collector.positions.dedup();
    bail!(
        "source_match `ANYTHING` is unsupported in {}. `ANYTHING` is anonymous \
         sugar only for expression (`EXPR`), statement (`STMT`), pattern, \
         variable-declarator-list (`DECLARATORS`), object-property-list, and \
         class-member-list holes. \
         Use typed holes when the position needs stronger diagnostics. \
         Object property keys still need exact keys; use `key: ANYTHING` to wildcard a \
         property value or `{{ ANYTHING }}` to skip arbitrary properties.",
        collector.positions.join(", ")
    )
}

#[derive(Default)]
pub(crate) struct UnsupportedAnythingCollector {
    positions: Vec<&'static str>,
}

impl UnsupportedAnythingCollector {
    fn push(&mut self, position: &'static str) {
        self.positions.push(position);
    }
}

impl Visit for UnsupportedAnythingCollector {
    fn visit_expr(&mut self, expr: &Expr) {
        if is_anything_expr_hole(expr) {
            return;
        }
        expr.visit_children_with(self);
    }

    fn visit_stmt(&mut self, stmt: &Stmt) {
        if is_anything_stmt_hole(stmt) {
            return;
        }
        stmt.visit_children_with(self);
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if is_anything_declarator_list_hole(declarator) {
            declarator.init.visit_with(self);
            return;
        }
        declarator.visit_children_with(self);
    }

    fn visit_pat(&mut self, pat: &Pat) {
        if is_anything_pat_hole(pat) {
            return;
        }
        pat.visit_children_with(self);
    }

    fn visit_object_pat_prop(&mut self, prop: &ObjectPatProp) {
        // `{ ANYTHING }` is the destructure-pattern property-list hole (the
        // pattern analog of the object-literal shorthand `ANYTHING`), so the
        // shorthand here is a supported run hole, not a stray `ANYTHING`
        // binding identifier.
        if is_anything_object_pat_prop_hole(prop) {
            return;
        }
        prop.visit_children_with(self);
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        if is_anything_class_rest_hole(member) {
            return;
        }
        if let ClassMember::ClassProp(prop) = member
            && prop_name_is_anything(&prop.key)
        {
            self.push("a class member with an initializer");
            prop.value.visit_with(self);
            return;
        }
        member.visit_children_with(self);
    }

    fn visit_prop(&mut self, prop: &Prop) {
        match prop {
            Prop::Shorthand(_) => {}
            Prop::KeyValue(prop) => {
                if prop_name_is_anything(&prop.key) {
                    self.push("an object property key");
                }
                prop.value.visit_with(self);
            }
            Prop::Assign(prop) => {
                if ident_is_anything(&prop.key) {
                    self.push("an object assignment property key");
                }
                prop.value.visit_with(self);
            }
            Prop::Getter(prop) => {
                if prop_name_is_anything(&prop.key) {
                    self.push("an object getter key");
                }
                prop.body.visit_with(self);
            }
            Prop::Setter(prop) => {
                if prop_name_is_anything(&prop.key) {
                    self.push("an object setter key");
                }
                prop.param.visit_with(self);
                prop.body.visit_with(self);
            }
            Prop::Method(prop) => {
                if prop_name_is_anything(&prop.key) {
                    self.push("an object method key");
                }
                prop.function.visit_with(self);
            }
        }
    }

    fn visit_binding_ident(&mut self, ident: &BindingIdent) {
        if binding_ident_is_anything(ident) {
            self.push("a binding identifier");
            ident.type_ann.visit_with(self);
            return;
        }
        ident.visit_children_with(self);
    }

    fn visit_ident(&mut self, ident: &Ident) {
        if ident_is_anything(ident) {
            self.push("an identifier");
        }
    }
}

pub fn parse_selector_module_with_capability_check(
    request_id: &str,
    selector_kind: &str,
    file_label: String,
    match_source: &str,
    // Field path used only in the "did not parse as JS" message; usually
    // equals `selector_kind`, but a few callers name the narrower `.match`
    // sub-field whose source actually failed to parse.
    parse_label: &str,
) -> Result<Module> {
    let parsed = js_ast::parse_js_module_ast(&file_label, match_source).with_context(|| {
        format!("logical_module {request_id}: {parse_label} did not parse as JS:\n{match_source}")
    })?;
    validate_anything_holes(&parsed.body)?;
    validate_selector_capabilities(request_id, selector_kind, match_source, &parsed)?;
    Ok(parsed)
}

impl ParsedSourceMatchSelector {
    pub fn parse(
        request_id: &str,
        selector_kind: &str,
        file_label: String,
        selector: &AnonymousStatementSelector,
        parse_label: &str,
    ) -> Result<Self> {
        let parsed = parse_selector_module_with_capability_check(
            request_id,
            selector_kind,
            file_label,
            &selector.match_source,
            parse_label,
        )?;
        Ok(Self::new(selector.clone(), parsed))
    }
}

pub(crate) fn validate_selector_capabilities(
    request_id: &str,
    selector_kind: &str,
    match_source: &str,
    parsed: &Module,
) -> Result<()> {
    let mut collector = UnsupportedSelectorCapabilityCollector::default();
    parsed.visit_with(&mut collector);
    if collector.unsupported_holes.is_empty() {
        return Ok(());
    }

    let holes = collector
        .unsupported_holes
        .iter()
        .map(|hole| format!("`{hole}`"))
        .collect::<Vec<_>>()
        .join(", ");
    bail!(
        "logical_module {request_id}: unsupported selector capability in {selector_kind}: \
         hole name(s) {holes} are not supported by this debundler; upgrade the debundler or \
         rewrite the selector with supported holes. Selector:\n{match_source}"
    );
}

#[derive(Default)]
pub(crate) struct UnsupportedSelectorCapabilityCollector {
    unsupported_holes: BTreeSet<String>,
}

impl UnsupportedSelectorCapabilityCollector {
    fn record_identifier(&mut self, name: &str) {
        if unsupported_selector_hole_name(name).is_some() {
            self.unsupported_holes.insert(name.to_string());
        }
    }
}

impl Visit for UnsupportedSelectorCapabilityCollector {
    fn visit_ident(&mut self, ident: &Ident) {
        self.record_identifier(ident.sym.as_ref());
    }

    fn visit_binding_ident(&mut self, ident: &BindingIdent) {
        self.record_identifier(ident.id.sym.as_ref());
    }

    fn visit_prop_name(&mut self, name: &PropName) {
        if let PropName::Ident(ident) = name {
            self.record_identifier(ident.sym.as_ref());
        }
        name.visit_children_with(self);
    }
}
