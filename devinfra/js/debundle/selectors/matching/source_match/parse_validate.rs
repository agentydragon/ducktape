use super::*;

pub(crate) fn validate_anything_holes(items: &[ModuleItem]) -> MatchResult<()> {
    let mut collector = UnsupportedAnythingCollector::default();
    for item in items {
        item.visit_with(&mut collector);
    }
    if collector.positions.is_empty() {
        return Ok(());
    }
    collector.positions.sort();
    collector.positions.dedup();
    Err(SourceMatchError::Anything {
        positions: collector.positions,
    })
}

#[derive(Default)]
pub(crate) struct UnsupportedAnythingCollector {
    positions: Vec<AnythingPosition>,
}

impl UnsupportedAnythingCollector {
    fn push(&mut self, position: AnythingPosition) {
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
            self.push(AnythingPosition::ClassMemberInitializer);
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
                    self.push(AnythingPosition::ObjectPropertyKey);
                }
                prop.value.visit_with(self);
            }
            Prop::Assign(prop) => {
                if ident_is_anything(&prop.key) {
                    self.push(AnythingPosition::ObjectAssignmentKey);
                }
                prop.value.visit_with(self);
            }
            Prop::Getter(prop) => {
                if prop_name_is_anything(&prop.key) {
                    self.push(AnythingPosition::ObjectGetterKey);
                }
                prop.function.body.visit_with(self);
            }
            Prop::Setter(prop) => {
                if prop_name_is_anything(&prop.key) {
                    self.push(AnythingPosition::ObjectSetterKey);
                }
                for param in &prop.function.params {
                    param.pat.visit_with(self);
                }
                prop.function.body.visit_with(self);
            }
            Prop::Method(prop) => {
                if prop_name_is_anything(&prop.key) {
                    self.push(AnythingPosition::ObjectMethodKey);
                }
                prop.function.visit_with(self);
            }
        }
    }

    fn visit_binding_ident(&mut self, ident: &BindingIdent) {
        if binding_ident_is_anything(ident) {
            self.push(AnythingPosition::BindingIdentifier);
            ident.type_ann.visit_with(self);
            return;
        }
        ident.visit_children_with(self);
    }

    fn visit_ident(&mut self, ident: &Ident) {
        if ident_is_anything(ident) {
            self.push(AnythingPosition::Identifier);
        }
    }
}

pub fn parse_selector_module(
    request_id: &str,
    file_label: String,
    match_source: &str,
    // Field path used only in the "did not parse as JS" message.
    parse_label: SelectorField,
) -> MatchResult<Module> {
    let parsed = js_ast::parse_js_module_ast(&file_label, match_source).map_err(|source| {
        SourceMatchError::Parse {
            logical_module: request_id.to_string(),
            field: parse_label,
            match_source: match_source.to_string(),
            source: std::sync::Arc::new(source),
        }
    })?;
    validate_anything_holes(&parsed.body)?;
    Ok(parsed)
}

impl ParsedSourceMatchSelector {
    pub fn parse(
        request_id: &str,
        file_label: String,
        selector: &AnonymousStatementSelector,
        parse_label: SelectorField,
    ) -> MatchResult<Self> {
        let parsed =
            parse_selector_module(request_id, file_label, &selector.match_source, parse_label)?;
        Ok(Self::new(selector.clone(), parsed))
    }
}
