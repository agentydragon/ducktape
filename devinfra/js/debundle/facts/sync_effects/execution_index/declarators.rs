//! Records variable declarator bindings and initializers.

use super::patterns::{
    collect_pattern_assignments, pattern_iterates, pattern_iterates_part, pattern_loses_values,
};
use super::property_keys::PropertyKeyExpr;
use super::{
    BindingDeclarationWrite, CallKind, DescriptorCopyCallCandidate, ExecutionIndex,
    empty_map_object_sites, set_from_primitive_array_aliases, set_from_set_spread_source,
    shallow_data_pattern,
};
use swc_common::Spanned;
use swc_ecma_ast::*;
use swc_ecma_visit::VisitWith;
use value_model::ValueExpr;
use value_model::local_operations::LocalBinding;

impl ExecutionIndex {
    pub(super) fn index_var_declarator(&mut self, declarator: &VarDeclarator) {
        self.record_declared_binding_scope(declarator);
        self.record_declarator_binding(declarator);
        self.record_undefined_before_assignment(declarator);
        declarator.visit_children_with(self);
        self.record_declarator_init_protocol(declarator);
    }

    fn record_declared_binding_scope(&mut self, declarator: &VarDeclarator) {
        if self.direct_top_level_var_declaration
            && self.current.is_none()
            && self.conditional_depth == 0
            && !self.past_await
            && matches!(declarator.name, Pat::Ident(_))
            && let Some(Expr::Call(call)) = declarator.init.as_deref().map(Expr::unwrap_parens)
            && call.args.len() == 1
            && call.args.iter().all(|arg| arg.spread.is_none())
            && let Callee::Expr(callee) = &call.callee
            && let Expr::Ident(imported) = callee.unwrap_parens()
            && let Expr::Object(object) = call.args[0].expr.unwrap_parens()
            && let Some(summary) = self.descriptor_copy_factories.get(&imported.to_id())
            && let Some(signature) = summary
                .signatures
                .iter()
                .find(|signature| signature.argument_count == 1 && signature.object_argument == 0)
        {
            self.descriptor_copy_calls.insert(
                call.span.into(),
                DescriptorCopyCallCandidate {
                    source_object: object.span.lo.0,
                    signature: signature.clone(),
                    provider_binding: imported.to_id(),
                },
            );
        }
        if self.variable_kind == Some(VarDeclKind::Var)
            && let Some(callable) = self.variable_scope
            && let Pat::Ident(binding) = &declarator.name
        {
            self.callables
                .get_mut(&callable)
                .unwrap()
                .hoisted_vars
                .insert(binding.id.to_id());
        }
        if let Some(callable) = self.current
            && let Pat::Ident(binding) = &declarator.name
            && let Some(kind @ (VarDeclKind::Let | VarDeclKind::Const)) = self.variable_kind
        {
            self.callables
                .get_mut(&callable)
                .unwrap()
                .lexical_declarations
                .insert(
                    binding.id.to_id(),
                    if kind == VarDeclKind::Let {
                        LocalBinding::Let
                    } else {
                        LocalBinding::Const
                    },
                );
        }
    }

    fn record_declarator_binding(&mut self, declarator: &VarDeclarator) {
        let initializer = declarator
            .init
            .as_ref()
            .map(|value| ValueExpr::from_expr(value));
        let direct_top_level = self.direct_top_level_var_declaration && self.current.is_none();
        if let Pat::Ident(binding) = &declarator.name {
            if self.direct_top_level_const_declaration && direct_top_level {
                self.top_const_declarations.insert(binding.id.to_id());
            }
            if self.direct_top_level_const_declaration
                && direct_top_level
                && declarator
                    .init
                    .as_deref()
                    .is_some_and(|expr| js_ast::is_primitive_literal(expr.unwrap_parens()))
            {
                self.direct_primitive_const_bindings
                    .insert(binding.id.to_id());
            }
            self.binding_declaration_writes
                .entry(binding.id.to_id())
                .or_insert_with(|| Vec::with_capacity(1))
                .push(BindingDeclarationWrite {
                    owner: self.owner,
                    private_var_scope: self
                        .variable_scope
                        .filter(|_| self.variable_kind == Some(VarDeclKind::Var)),
                    binding_span: binding.id.span,
                    initializer_span: declarator.init.as_ref().map(|value| value.span()),
                    initializer: initializer.clone(),
                    direct_top_level,
                    simple_identifier: true,
                    primitive_literal_initializer: declarator
                        .init
                        .as_deref()
                        .is_some_and(|expr| js_ast::is_primitive_literal(expr.unwrap_parens())),
                    set_from_primitive_array_aliases: declarator
                        .init
                        .as_deref()
                        .and_then(set_from_primitive_array_aliases),
                    set_from_set_spread_source: declarator
                        .init
                        .as_deref()
                        .and_then(set_from_set_spread_source),
                    empty_map_object_sites: declarator
                        .init
                        .as_deref()
                        .and_then(empty_map_object_sites),
                });
            self.property_key_assignments
                .entry(binding.id.to_id())
                .or_default()
                .push(
                    declarator
                        .init
                        .as_ref()
                        .map_or(PropertyKeyExpr::Unknown, |value| {
                            PropertyKeyExpr::from_expr(value)
                        }),
                );
        } else {
            if let Some(init) = &declarator.init
                && pattern_loses_values(&declarator.name)
            {
                self.record_published_source(init);
            }
            if let Some(init) = &declarator.init
                && pattern_iterates(&declarator.name)
            {
                self.record_iterated_source(init);
            }
            if pattern_iterates_part(&declarator.name) {
                self.record_iterated_value(ValueExpr::Unknown);
            }
            self.record_iterated_defaults(&declarator.name);
            let mut bindings = Vec::new();
            let pattern_value = initializer.clone().unwrap_or(ValueExpr::Unknown);
            collect_pattern_assignments(&declarator.name, &pattern_value, &mut bindings);
            if let Some(callable) = self.current {
                self.callables
                    .get_mut(&callable)
                    .unwrap()
                    .pattern_locals
                    .extend(bindings.iter().map(|(binding, _)| binding.clone()));
            }
            if self.variable_kind == Some(VarDeclKind::Var)
                && let Some(callable) = self.variable_scope
            {
                self.callables
                    .get_mut(&callable)
                    .unwrap()
                    .hoisted_vars
                    .extend(bindings.iter().map(|(binding, _)| binding.clone()));
            } else if let Some(callable) = self.current
                && let Some(kind @ (VarDeclKind::Let | VarDeclKind::Const)) = self.variable_kind
            {
                let kind = if kind == VarDeclKind::Let {
                    LocalBinding::Let
                } else {
                    LocalBinding::Const
                };
                self.callables
                    .get_mut(&callable)
                    .unwrap()
                    .lexical_declarations
                    .extend(bindings.iter().map(|(binding, _)| (binding.clone(), kind)));
            }
            for (id, _) in bindings {
                self.binding_declaration_writes
                    .entry(id.clone())
                    .or_insert_with(|| Vec::with_capacity(1))
                    .push(BindingDeclarationWrite {
                        owner: self.owner,
                        private_var_scope: None,
                        binding_span: declarator.name.span(),
                        initializer_span: declarator.init.as_ref().map(|value| value.span()),
                        initializer: initializer.clone(),
                        direct_top_level,
                        simple_identifier: false,
                        primitive_literal_initializer: false,
                        set_from_primitive_array_aliases: None,
                        set_from_set_spread_source: None,
                        empty_map_object_sites: None,
                    });
                // A for-of declaration has no initializer: its key fact was
                // supplied by the loop protocol, rather than an unknown RHS.
                if declarator.init.is_some() {
                    self.property_key_assignments
                        .entry(id)
                        .or_default()
                        .push(PropertyKeyExpr::Unknown);
                }
            }
        }
    }

    fn record_undefined_before_assignment(&mut self, declarator: &VarDeclarator) {
        if let Some(key) = self.current
            && let Pat::Ident(binding) = &declarator.name
        {
            self.callables
                .get_mut(&key)
                .unwrap()
                .declared_locals
                .insert(binding.id.to_id());
        }
        // A loop head's binding takes the loop's values, which no entry in
        // `local_assignments` records; leave it to the unknown fallback.
        if let Some(key) = self.current
            && !self.for_head_declaration
            && (declarator.init.is_none() || self.variable_kind == Some(VarDeclKind::Var))
        {
            let mut bindings = Vec::new();
            collect_pattern_assignments(&declarator.name, &ValueExpr::Undefined, &mut bindings);
            self.callables
                .get_mut(&key)
                .unwrap()
                .undefined_before_assignment
                .extend(bindings.into_iter().map(|(binding, _)| binding));
        }
    }

    fn record_declarator_init_protocol(&mut self, declarator: &VarDeclarator) {
        if let Some(init) = &declarator.init {
            let kind = match &declarator.name {
                Pat::Object(_) | Pat::Array(_) if !shallow_data_pattern(&declarator.name) => {
                    Some(CallKind::DeepDestructuringProtocol)
                }
                Pat::Object(_) => Some(CallKind::ObjectSpread),
                Pat::Array(_) => Some(CallKind::ArraySpread),
                _ => None,
            };
            if let Some(kind) = kind {
                self.record_source_protocol(kind, ValueExpr::from_expr(init), declarator.span);
            }
            let mut assignments = Vec::new();
            collect_pattern_assignments(
                &declarator.name,
                &ValueExpr::from_expr(init),
                &mut assignments,
            );
            if let Some(key) = self.current {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .local_assignments
                    .extend(assignments);
            } else {
                self.top_assignments[self.owner.0].extend(assignments);
            }
        }
    }
}
