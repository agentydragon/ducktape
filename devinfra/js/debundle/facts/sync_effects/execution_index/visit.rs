//! The AST visitor that fills the `ExecutionIndex`.

use super::class_value_facts::collect_class_value_facts;
use super::fresh_values::{map_entry_array_span, value_without_user_code};
use super::global_receiver::{
    DestructuringMemberWrite, GlobalMembershipProbe, builtin_global_root, is_global_receiver,
};
use super::local_constructors::local_constructor;
use super::native_write_census::{IntrinsicWriteTarget, intrinsic_write_target};
use super::patterns::{collect_pattern_assignments, pattern_iterates};
use super::property_keys::PropertyKeyExpr;
use super::{
    Call, CallKind, ExecutionIndex, FreshErrorWriteClaim, FreshErrorWriteSite, Parameter,
    sole_return_expression,
};
use crate::namespace_wrapper;
use analysis_domain::lazy_boundary::visit_eager_member_parts;
use analysis_model::PrimKinds;
use analysis_vocabulary::report_schema::NativeMutationOperation;
use analysis_vocabulary::value::Kind;
use std::collections::BTreeSet;
use std::sync::Arc;
use swc_common::Spanned;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};
use value_model::local_operations::{DeferredCallableCreation, LocalBinding, LocalBody};
use value_model::{ArrayElement, MemberKey, ValueExpr, static_prop, static_property_name};

impl Visit for ExecutionIndex {
    fn visit_named_export(&mut self, export: &NamedExport) {
        for specifier in &export.specifiers {
            if let ExportSpecifier::Named(named) = specifier
                && let ModuleExportName::Ident(ident) = &named.orig
                && self.namespace_privacy.members.contains_key(&ident.to_id())
            {
                self.namespace_privacy.invalid = true;
            }
        }
        // Linkage is captured by collect_export_roots. Specifiers do not run
        // at module initialization or expose a class in a closed module.
    }

    fn visit_class(&mut self, class: &Class) {
        // A derived constructor can receive a native object returned by super.
        // Its public fields therefore do not carry the fresh-this certificate.
        if class.super_class.is_some() {
            for member in &class.body {
                if let ClassMember::ClassProp(prop) = member
                    && !prop.is_static
                {
                    let key = match &prop.key {
                        PropName::Computed(key) => PropertyKeyExpr::from_expr(&key.expr),
                        key => static_property_name(key)
                            .map(PropertyKeyExpr::String)
                            .unwrap_or(PropertyKeyExpr::Unknown),
                    };
                    self.prototype_field_writes
                        .push((self.owner, self.current, prop.span, key));
                }
            }
        }
        self.class_owners.insert(class.span.lo.0, self.owner);
        self.class_spans.insert(class.span.lo.0, class.span);
        self.class_constructors
            .extend(class.body.iter().filter_map(|member| match member {
                ClassMember::Constructor(constructor) => Some(constructor.span.lo.0),
                _ => None,
            }));
        self.class_member_callables
            .extend(class.body.iter().filter_map(|member| match member {
                ClassMember::Constructor(constructor) => Some(constructor.span.lo.0),
                ClassMember::Method(method) => Some(method.function.span.lo.0),
                ClassMember::PrivateMethod(method) => Some(method.function.span.lo.0),
                _ => None,
            }));
        self.class_store_regions.insert(
            class.span.lo.0,
            class
                .body
                .iter()
                .filter_map(|member| match member {
                    ClassMember::Constructor(constructor) => Some(constructor.span),
                    ClassMember::ClassProp(prop) => prop.value.as_ref().map(|value| value.span()),
                    ClassMember::PrivateProp(prop) => prop.value.as_ref().map(|value| value.span()),
                    ClassMember::StaticBlock(block) => Some(block.span),
                    _ => None,
                })
                .collect(),
        );
        self.class_accessors.insert(
            class.span.lo.0,
            class
                .body
                .iter()
                .filter_map(|member| match member {
                    ClassMember::Method(method)
                        if matches!(method.kind, MethodKind::Getter | MethodKind::Setter) =>
                    {
                        Some(method.function.span.lo.0)
                    }
                    ClassMember::PrivateMethod(method)
                        if matches!(method.kind, MethodKind::Getter | MethodKind::Setter) =>
                    {
                        Some(method.function.span.lo.0)
                    }
                    _ => None,
                })
                .collect(),
        );
        if let Some(heritage) = &class.super_class {
            self.class_heritage_spans
                .insert(class.span.lo.0, heritage.span());
        }
        self.class_values
            .insert(class.span.lo.0, collect_class_value_facts(class));
        if let Some(body) = LocalBody::lower_class_constructor(class) {
            self.class_provider_bodies.insert(class.span.lo.0, body);
        }
        if let Some(constructor) = local_constructor(class, &self.local_class_construction_shapes) {
            self.local_constructors.insert(class.span.lo.0, constructor);
        }
        class.visit_children_with(self);
    }

    fn visit_with_stmt(&mut self, statement: &WithStmt) {
        self.dynamic_scope_owners.insert(self.owner);
        statement.obj.visit_with(self);
        self.record_source_protocol(
            CallKind::OpaqueExecution,
            ValueExpr::from_expr(&statement.obj),
            statement.span,
        );
        statement.body.visit_with(self);
        self.mark_opaque();
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        // Defining a class evaluates computed keys and static initializers,
        // but does not run its constructor or instance field initializers.
        // The ordinary SWC descent would put calls in those deferred bodies
        // into `top_calls`, falsely promoting their callees at class init.
        match member {
            ClassMember::ClassProp(prop) => prop.decorators.visit_with(self),
            ClassMember::PrivateProp(prop) => prop.decorators.visit_with(self),
            ClassMember::AutoAccessor(accessor) => accessor.decorators.visit_with(self),
            _ => {}
        }
        visit_eager_member_parts(self, member);
        match member {
            ClassMember::Method(method) => {
                if method.kind == MethodKind::Setter {
                    self.setter_owners
                        .entry(static_property_name(&method.key))
                        .or_default()
                        .insert(self.owner);
                }
                method.function.visit_with(self);
            }
            ClassMember::PrivateMethod(method) => {
                method.function.visit_with(self);
            }
            ClassMember::Constructor(ctor) => {
                // Keep inspecting the deferred body for nested accessors;
                // property-write promotion still needs their owner. Its
                // calls must stay out of the class owner's top-call list.
                let params = ctor
                    .params
                    .iter()
                    .map(|param| match param {
                        ParamOrTsParamProp::Param(param) => Parameter::from_pattern(&param.pat),
                        ParamOrTsParamProp::TsParamProp(_) => Parameter {
                            binding: None,
                            bindings: BTreeSet::new(),
                            default: None,
                            pattern_assignments: Vec::new(),
                            pattern_loses_values: false,
                            pattern_iterates: false,
                            pattern_iterates_part: false,
                            iterated_defaults: Vec::new(),
                        },
                    })
                    .collect();
                self.with_callable(ctor.span, params, |index| {
                    for parameter in &ctor.params {
                        if let ParamOrTsParamProp::Param(parameter) = parameter {
                            index.invalidate_property_key_pattern(&parameter.pat);
                        }
                    }
                    ctor.visit_children_with(index);
                });
            }
            ClassMember::ClassProp(prop) if !prop.is_static => {
                if let Some(value) = &prop.value {
                    self.with_callable(prop.span, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            ClassMember::PrivateProp(prop) if !prop.is_static => {
                if let Some(value) = &prop.value {
                    self.with_callable(prop.span, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            ClassMember::AutoAccessor(accessor) if !accessor.is_static => {
                if let Some(value) = &accessor.value {
                    self.with_callable(accessor.span, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            _ => {}
        }
    }

    fn visit_setter_prop(&mut self, setter: &SetterProp) {
        self.callable_spans.insert(setter.span.lo.0, setter.span);
        // The Function child has its own canonical callable key. Keep this
        // owner-wide marker conservative: exact descriptor-body activation
        // and owner-local captured-write settlement are separate proofs.
        self.uncovered_callable_effect_owners.insert(self.owner);
        self.setter_owners
            .entry(static_property_name(&setter.key))
            .or_default()
            .insert(self.owner);
        let previous_scope = self.variable_scope.take();
        setter.visit_children_with(self);
        self.variable_scope = previous_scope;
    }

    fn visit_getter_prop(&mut self, getter: &GetterProp) {
        self.callable_spans.insert(getter.span.lo.0, getter.span);
        // The Function child is indexed separately, while this owner marker
        // conservatively prevents unproved accessor-write settlement.
        self.uncovered_callable_effect_owners.insert(self.owner);
        let previous_scope = self.variable_scope.take();
        getter.visit_children_with(self);
        self.variable_scope = previous_scope;
    }

    fn visit_static_block(&mut self, block: &StaticBlock) {
        let previous_scope = self.variable_scope.take();
        block.visit_children_with(self);
        self.variable_scope = previous_scope;
    }

    fn visit_object_lit(&mut self, object: &ObjectLit) {
        self.index_object_lit(object);
    }

    fn visit_array_lit(&mut self, array: &ArrayLit) {
        if array.elems.iter().all(|element| {
            element
                .as_ref()
                .is_some_and(|element| element.spread.is_none())
        }) {
            self.dense_array_literals
                .insert(array.span.lo.0, array.elems.len());
        }
        self.array_elements.insert(
            array.span.lo.0,
            array
                .elems
                .iter()
                .map(|element| {
                    element.as_ref().map(|element| ArrayElement {
                        spread: element.spread,
                        value: ValueExpr::from_expr(&element.expr),
                    })
                })
                .collect(),
        );
        self.literal_depth += 1;
        for element in array.elems.iter().flatten() {
            element.expr.visit_with(self);
            if let Some(span) = element.spread {
                self.record_call(Call::without_argument_facts(
                    CallKind::ArraySpread,
                    ValueExpr::Unknown,
                    vec![ValueExpr::from_expr(&element.expr)],
                    span,
                    self.current,
                    self.owner,
                ));
            }
        }
        self.literal_depth -= 1;
    }

    fn visit_fn_decl(&mut self, declaration: &FnDecl) {
        self.property_key_declarations
            .insert(declaration.ident.to_id());
        self.property_key_assignments
            .entry(declaration.ident.to_id())
            .or_default()
            .push(PropertyKeyExpr::Unknown);
        if let Some(key) = self.current {
            // Declarations are instantiated when the enclosing activation
            // begins, even if their source text follows an await.
            self.hoisted_nested_functions
                .insert(declaration.function.span.lo.0);
            let callable = self.callables.get_mut(&key).unwrap();
            callable
                .local_functions
                .insert(declaration.ident.to_id(), declaration.function.span.lo.0);
            callable.local_assignments.push((
                declaration.ident.to_id(),
                ValueExpr::Alloc(Kind::Function, declaration.function.span.lo.0),
            ));
        } else {
            self.hoisted_bindings
                .insert(declaration.ident.to_id(), declaration.function.span.lo.0);
            if let Some(Expr::Call(call)) = declaration
                .function
                .body
                .as_ref()
                .and_then(sole_return_expression)
                .map(Expr::unwrap_parens)
            {
                self.direct_return_calls
                    .insert(declaration.ident.to_id(), call.span);
            }
        }
        declaration.visit_children_with(self);
    }

    fn visit_class_decl(&mut self, declaration: &ClassDecl) {
        self.property_key_declarations
            .insert(declaration.ident.to_id());
        self.property_key_assignments
            .entry(declaration.ident.to_id())
            .or_default()
            .push(PropertyKeyExpr::Unknown);
        declaration.class.visit_with(self);
        if self.current.is_none() {
            self.top_definite_initializers[self.owner.0].insert(declaration.ident.to_id());
            self.top_assignments[self.owner.0].push((
                declaration.ident.to_id(),
                ValueExpr::Alloc(Kind::Class, declaration.class.span.lo.0),
            ));
        } else if let Some(key) = self.current {
            let callable = self.callables.get_mut(&key).unwrap();
            callable
                .lexical_declarations
                .insert(declaration.ident.to_id(), LocalBinding::Let);
            callable.local_assignments.push((
                declaration.ident.to_id(),
                ValueExpr::Alloc(Kind::Class, declaration.class.span.lo.0),
            ));
        }
    }

    fn visit_if_stmt(&mut self, statement: &IfStmt) {
        statement.test.visit_with(self);
        let after_test = self.past_await;
        self.conditional_depth += 1;
        statement.cons.visit_with(self);
        self.conditional_depth -= 1;
        let after_then = self.past_await;
        self.past_await = after_test;
        if let Some(else_branch) = &statement.alt {
            self.conditional_depth += 1;
            else_branch.visit_with(self);
            self.conditional_depth -= 1;
            self.past_await &= after_then;
        } else {
            self.past_await = after_test;
        }
    }

    fn visit_cond_expr(&mut self, expression: &CondExpr) {
        self.record_guard(&expression.test, expression.cons.span(), true);
        self.record_guard(&expression.test, expression.alt.span(), false);
        expression.test.visit_with(self);
        let after_test = self.past_await;
        self.conditional_depth += 1;
        expression.cons.visit_with(self);
        self.conditional_depth -= 1;
        let after_then = self.past_await;
        self.past_await = after_test;
        self.conditional_depth += 1;
        expression.alt.visit_with(self);
        self.conditional_depth -= 1;
        self.past_await &= after_then;
    }

    fn visit_bin_expr(&mut self, expression: &BinExpr) {
        expression.left.visit_with(self);
        let after_left = self.past_await;
        if expression.op.may_short_circuit() {
            self.conditional_depth += 1;
        }
        let previous_target = self.safe_class_target.take();
        self.safe_class_target = if expression.op == BinaryOp::InstanceOf {
            match expression.right.unwrap_parens() {
                Expr::Ident(id) => Some(id.to_id()),
                _ => None,
            }
        } else {
            None
        };
        expression.right.visit_with(self);
        self.safe_class_target = previous_target;
        if expression.op.may_short_circuit() {
            self.conditional_depth -= 1;
            self.past_await = after_left;
        }
        match expression.op {
            // Strict comparisons, short-circuit operators and `instanceof`
            // do not apply ToPrimitive to their operands. `in` converts only
            // the left operand to a property key. The remaining eager binary
            // operators can convert either object operand.
            BinaryOp::EqEqEq
            | BinaryOp::NotEqEq
            | BinaryOp::InstanceOf
            | BinaryOp::LogicalAnd
            | BinaryOp::LogicalOr
            | BinaryOp::NullishCoalescing => {}
            BinaryOp::In => self.record_implicit_coercion(&expression.left, expression.span),
            _ => {
                self.record_implicit_coercion(&expression.left, expression.span);
                self.record_implicit_coercion(&expression.right, expression.span);
            }
        }
        if self.global_guards_requested
            && expression.op == BinaryOp::In
            && let (Expr::Lit(Lit::Str(property)), Expr::Ident(receiver)) = (
                expression.left.unwrap_parens(),
                expression.right.unwrap_parens(),
            )
            && self.global_object_names.contains(receiver.sym.as_ref())
            && let Some(root) = builtin_global_root(receiver)
            && let Some(property) = property.value.as_str()
        {
            self.global_membership_candidates.insert(
                expression.span.into(),
                GlobalMembershipProbe {
                    root,
                    receiver: receiver.to_id(),
                    property: property.to_owned(),
                    owner: self.owner,
                },
            );
        }
        if matches!(expression.op, BinaryOp::In | BinaryOp::InstanceOf) {
            self.record_call(Call::without_argument_facts(
                if expression.op == BinaryOp::In {
                    CallKind::InProtocol
                } else {
                    CallKind::InstanceOfProtocol
                },
                ValueExpr::Unknown,
                vec![
                    ValueExpr::from_expr(&expression.right),
                    ValueExpr::from_expr(&expression.left),
                ],
                expression.span,
                self.current,
                self.owner,
            ));
        }
    }

    fn visit_while_stmt(&mut self, node: &WhileStmt) {
        self.visit_may_skip(node);
    }
    fn visit_labeled_stmt(&mut self, node: &LabeledStmt) {
        node.visit_children_with(self);
    }
    fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_stmt(&mut self, node: &ForStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        // An array pattern in the head iterates each key, a string.
        let head_iterates = match &node.left {
            ForHead::VarDecl(declaration) => declaration
                .decls
                .iter()
                .any(|declarator| pattern_iterates(&declarator.name)),
            ForHead::Pat(pattern) => pattern_iterates(pattern),
            ForHead::UsingDecl(_) => false,
        };
        if head_iterates {
            self.record_iterated_value(ValueExpr::NonCallable(PrimKinds::STRING));
        }
        self.record_head_iterated_defaults(&node.left);
        self.visit_may_skip(node);
    }
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        self.record_published_source(&node.right);
        self.record_iterated_source(&node.right);
        // An array pattern in the head iterates each element, which no
        // source tracks; `Object.entries` yields fresh native pairs.
        let head_iterates = match &node.left {
            ForHead::VarDecl(declaration) => declaration
                .decls
                .iter()
                .any(|declarator| pattern_iterates(&declarator.name)),
            ForHead::Pat(pattern) => pattern_iterates(pattern),
            ForHead::UsingDecl(_) => false,
        };
        let entries = matches!(node.right.unwrap_parens(), Expr::Call(call)
            if matches!(&call.callee, Callee::Expr(callee)
                if matches!(callee.unwrap_parens(), Expr::Member(member)
                    if static_prop(&member.prop).as_deref() == Some("entries")
                        && matches!(member.obj.unwrap_parens(), Expr::Ident(object) if object.sym == "Object"))));
        if head_iterates && !entries {
            self.record_iterated_value(ValueExpr::Unknown);
        }
        self.record_head_iterated_defaults(&node.left);
        if !node.is_await
            && let ForHead::VarDecl(declaration) = &node.left
            && matches!(declaration.kind, VarDeclKind::Let | VarDeclKind::Const)
            && declaration.decls.len() == 1
            && declaration.decls[0].init.is_none()
            && let Pat::Array(pattern) = &declaration.decls[0].name
            && pattern.elems.len() == 2
            && let Some(Pat::Ident(binding)) = &pattern.elems[0]
            && matches!(&pattern.elems[1], Some(Pat::Ident(_)))
            && let Expr::Call(call) = node.right.unwrap_parens()
            && call.args.len() == 1
            && call.args[0].spread.is_none()
            && let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && static_prop(&member.prop).as_deref() == Some("entries")
            && let Expr::Ident(namespace) = member.obj.unwrap_parens()
            && namespace.sym == "Object"
            && let Expr::Object(object) = call.args[0].expr.unwrap_parens()
        {
            let keys: Option<BTreeSet<String>> = object
                .props
                .iter()
                .map(|property| match property {
                    PropOrSpread::Prop(property) => match property.as_ref() {
                        Prop::KeyValue(pair) if !matches!(pair.key, PropName::Computed(_)) => {
                            static_property_name(&pair.key).filter(|key| key != "__proto__")
                        }
                        Prop::Shorthand(ident) => Some(ident.sym.to_string()),
                        _ => None,
                    },
                    _ => None,
                })
                .collect();
            if let Some(keys) = keys {
                self.property_key_assignments
                    .entry(binding.id.to_id())
                    .or_default()
                    .push(PropertyKeyExpr::EntriesKeys(namespace.to_id(), keys));
            }
        }
        self.visit_may_skip(node);
    }
    fn visit_try_stmt(&mut self, node: &TryStmt) {
        self.visit_may_skip(node);
    }
    fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
        self.visit_may_skip(node);
    }

    fn visit_fn_expr(&mut self, expression: &FnExpr) {
        if let Some(binding) = &expression.ident {
            self.property_key_declarations.insert(binding.to_id());
            self.property_key_assignments
                .entry(binding.to_id())
                .or_default()
                .push(PropertyKeyExpr::Unknown);
        }
        expression.visit_children_with(self);
        if let Some(binding) = &expression.ident {
            let callable = self
                .callables
                .get_mut(&expression.function.span.lo.0)
                .unwrap();
            callable.self_binding = Some(binding.to_id());
            if let Some(body) = callable.body_facts.take_ordered_plan() {
                callable
                    .body_facts
                    .set_ordered_plan(Some(body.with_declarations(
                        std::iter::empty(),
                        [(
                            binding.to_id(),
                            LocalBinding::NamedSelf(expression.function.span.lo.0),
                        )],
                    )));
            }
        }
    }

    fn visit_class_expr(&mut self, expression: &ClassExpr) {
        if let Some(binding) = &expression.ident {
            self.property_key_declarations.insert(binding.to_id());
        }
        expression.visit_children_with(self);
    }

    fn visit_catch_clause(&mut self, clause: &CatchClause) {
        if let Some(parameter) = &clause.param {
            self.invalidate_property_key_pattern(parameter);
            // The thrown value is untracked; an array pattern iterates it.
            if pattern_iterates(parameter) {
                self.record_iterated_value(ValueExpr::Unknown);
            }
            self.record_iterated_defaults(parameter);
            if let Some(callable) = self.current {
                self.callables
                    .get_mut(&callable)
                    .unwrap()
                    .pattern_locals
                    .extend(Parameter::from_pattern(parameter).bindings);
            }
        }
        clause.visit_children_with(self);
    }

    fn visit_function(&mut self, function: &Function) {
        if !function.is_async
            && !function.is_generator
            && function.decorators.is_empty()
            && function.type_params.is_none()
            && function.return_type.is_none()
            && function
                .params
                .iter()
                .all(|param| param.decorators.is_empty() && matches!(param.pat, Pat::Ident(_)))
            && function
                .body
                .as_ref()
                .is_some_and(|body| match body.stmts.as_slice() {
                    [] => true,
                    [Stmt::Return(ret)] => ret
                        .arg
                        .as_deref()
                        .is_none_or(|expr| js_ast::is_primitive_literal(expr.unwrap_parens())),
                    _ => false,
                })
        {
            self.inert_argument_functions.insert(function.span.lo.0);
        }
        self.callable_creations
            .entry(function.span.lo.0)
            .or_insert(DeferredCallableCreation::Function(function.span.lo.0));
        if let Some(candidate) = namespace_wrapper::candidate(function) {
            self.namespace_wrapper_candidates
                .insert(function.span.lo.0, candidate);
        }
        if function.is_async {
            self.async_callables.insert(function.span.lo.0);
        }
        // Decorator expressions run when the method is defined. Defaults in
        // parameter patterns and the body run only when it is called.
        function.decorators.visit_with(self);
        for param in &function.params {
            param.decorators.visit_with(self);
        }
        let params = function
            .params
            .iter()
            .map(|param| Parameter::from_pattern(&param.pat))
            .collect();
        self.with_callable(function.span, params, |index| {
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .body_facts
                .set_ordered_plan(
                    (!function.is_async
                        && !function.is_generator
                        && function.decorators.is_empty()
                        && function.type_params.is_none()
                        && function.return_type.is_none()
                        && function
                            .params
                            .iter()
                            .all(|param| param.decorators.is_empty()))
                    .then(|| {
                        LocalBody::lower(
                            function.params.iter().map(|parameter| &parameter.pat),
                            function.body.as_ref()?,
                        )
                    })
                    .flatten(),
                );
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .body_facts
                .set_direct_return_candidate(
                    (!function.is_async
                        && !function.is_generator
                        && function.decorators.is_empty()
                        && function.params.iter().all(|param| {
                            param.decorators.is_empty() && matches!(param.pat, Pat::Ident(_))
                        }))
                    .then(|| {
                        function
                            .body
                            .as_ref()
                            .and_then(sole_return_expression)
                            .map(|expression| expression.span().into())
                    })
                    .flatten(),
                );
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .may_fall_through = !function.body.as_ref().is_some_and(|body| {
                matches!(body.stmts.last(), Some(Stmt::Return(_) | Stmt::Throw(_)))
            });
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .generator = function.is_generator;
            for param in &function.params {
                index.invalidate_property_key_pattern(&param.pat);
                param.pat.visit_with(index);
            }
            if let Some(body) = &function.body {
                body.visit_with(index);
            }
        });
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        self.callable_creations.insert(
            arrow.span.lo.0,
            DeferredCallableCreation::Arrow(arrow.span.lo.0),
        );
        if arrow.is_async {
            self.async_callables.insert(arrow.span.lo.0);
        }
        let params = arrow.params.iter().map(Parameter::from_pattern).collect();
        self.with_callable(arrow.span, params, |index| {
            index
                .callables
                .get_mut(&arrow.span.lo.0)
                .unwrap()
                .body_facts
                .set_ordered_plan(
                    (!arrow.is_async
                        && !arrow.is_generator
                        && arrow.type_params.is_none()
                        && arrow.return_type.is_none())
                    .then(|| match arrow.body.as_ref() {
                        ArrowFunctionBody::FunctionBody(body) => {
                            LocalBody::lower(arrow.params.iter(), body)
                        }
                        ArrowFunctionBody::Expr(expression) => {
                            LocalBody::lower_expression(arrow.params.iter(), expression)
                        }
                    })
                    .flatten(),
                );
            index
                .callables
                .get_mut(&arrow.span.lo.0)
                .unwrap()
                .body_facts
                .set_direct_return_candidate(
                    (!arrow.is_async
                        && !arrow.is_generator
                        && arrow
                            .params
                            .iter()
                            .all(|param| matches!(param, Pat::Ident(_))))
                    .then(|| match arrow.body.as_ref() {
                        ArrowFunctionBody::Expr(expression) => Some(expression.span().into()),
                        ArrowFunctionBody::FunctionBody(body) => {
                            sole_return_expression(body).map(|expression| expression.span().into())
                        }
                    })
                    .flatten(),
                );
            for parameter in &arrow.params {
                index.invalidate_property_key_pattern(parameter);
            }
            index
                .callables
                .get_mut(&arrow.span.lo.0)
                .unwrap()
                .may_fall_through = match arrow.body.as_ref() {
                ArrowFunctionBody::Expr(_) => false,
                ArrowFunctionBody::FunctionBody(body) => {
                    !matches!(body.stmts.last(), Some(Stmt::Return(_) | Stmt::Throw(_)))
                }
            };
            arrow.visit_children_with(index);
            if let ArrowFunctionBody::Expr(expr) = arrow.body.as_ref() {
                let callable = index.callables.get_mut(&arrow.span.lo.0).unwrap();
                callable.body_facts.record_return(expr);
            }
        });
    }

    fn visit_ident(&mut self, ident: &Ident) {
        let position = ident.span.lo.0;
        self.binding_use_bounds
            .entry(ident.to_id())
            .and_modify(|(first, last)| {
                *first = (*first).min(position);
                *last = (*last).max(position);
            })
            .or_insert((position, position));
        self.ident_references.push((position, ident.to_id()));
        // A published outer closure can expose a nested closure's capabilities.
        for key in &self.prototype_capture_stack {
            self.prototype_callable_reads
                .entry(*key)
                .or_default()
                .insert(ident.to_id());
        }
        self.namespace_privacy.record_ident(ident);
        let id = ident.to_id();
        if self.class_ids.contains(&id) && self.safe_class_target.as_ref() != Some(&id) {
            self.other_class_uses.insert(id);
        }
        if self.current.is_some() {
            self.captured_bindings.insert(ident.to_id());
        }
        if !self.past_await
            && let Some(key) = self.current
        {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.reads.insert(ident.to_id());
            if self.global_guards_requested
                && self.discarded_sync_iifes[self.owner.0] == Some(key)
                && self.allowed_local_member_receiver != Some(ident.span.lo.0)
            {
                callable.bare_reads.insert(ident.to_id());
            }
            if self.literal_depth > 0 {
                callable.literal_captures.insert(ident.to_id());
                if self.factory_member_value_depth == 0 {
                    callable.literal_exposures.insert(ident.to_id());
                }
            }
        }
    }

    fn visit_this_expr(&mut self, _: &ThisExpr) {
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().implicit_capture = true;
        }
    }

    fn visit_meta_prop_expr(&mut self, _: &MetaPropExpr) {
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().implicit_capture = true;
        }
    }

    fn visit_member_expr(&mut self, member: &MemberExpr) {
        self.member_owners.insert(member.span.into(), self.owner);
        if self.global_guards_requested
            && is_global_receiver(&member.obj, &self.global_object_names)
        {
            if let Some(property) = static_prop(&member.prop) {
                *self.global_slot_mentions.entry(property).or_default() += 1;
            } else {
                self.dynamic_global_member = true;
            }
        }
        if !self.member_write_sites.contains(&member.span.into())
            && let Some(key) = self.current
        {
            self.callable_member_reads
                .entry(key)
                .or_default()
                .push((ValueExpr::from_expr(&member.obj), static_prop(&member.prop)));
        }
        let previous = self.namespace_privacy.allowed_read;
        self.namespace_privacy.allowed_read = self.namespace_privacy.read_receiver(member);
        let previous_local_receiver = self.allowed_local_member_receiver;
        if self.global_guards_requested
            && self
                .current
                .is_some_and(|key| self.discarded_sync_iifes[self.owner.0] == Some(key))
        {
            self.allowed_local_member_receiver = match (member.obj.unwrap_parens(), &member.prop) {
                (Expr::Ident(id), MemberProp::Ident(_)) => Some(id.span.lo.0),
                _ => None,
            };
        }
        member.obj.visit_with(self);
        self.allowed_local_member_receiver = previous_local_receiver;
        self.namespace_privacy.allowed_read = previous;
        member.prop.visit_with(self);
        if !self.member_write_sites.contains(&member.span.into())
            && !self.direct_call_members.contains(&member.span.into())
        {
            self.record_call(Call::member_read(
                self.owner,
                self.current,
                ValueExpr::Member(
                    Arc::new(ValueExpr::from_expr(&member.obj)),
                    static_prop(&member.prop),
                    MemberKey::of(&member.prop),
                    member.span.into(),
                ),
                member.span,
            ));
        }
    }

    fn visit_binding_ident(&mut self, binding: &BindingIdent) {
        if !self.assignment_target {
            self.property_key_declarations.insert(binding.id.to_id());
        } else {
            self.record_nested_write(binding.id.to_id());
        }
        self.namespace_privacy.record_ident(&binding.id);
    }

    fn visit_assign_target(&mut self, target: &AssignTarget) {
        let saved = std::mem::replace(&mut self.assignment_target, true);
        target.visit_children_with(self);
        self.assignment_target = saved;
    }

    fn visit_var_decl(&mut self, declaration: &VarDecl) {
        let previous = self.variable_kind.replace(declaration.kind);
        declaration.visit_children_with(self);
        self.variable_kind = previous;
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        self.index_var_declarator(declarator);
    }

    fn visit_seq_expr(&mut self, sequence: &SeqExpr) {
        let mut fresh: Option<(Id, Id)> = None;
        for expression in &sequence.exprs {
            let mut continues = false;
            if let Expr::Assign(assignment) = expression.unwrap_parens()
                && assignment.op == AssignOp::Assign
            {
                if let Some(binding_targets::SimpleWriteTarget::Binding(receiver)) =
                    binding_targets::simple_write_target(&assignment.left)
                    && let Expr::Call(call) = assignment.right.unwrap_parens()
                    && let Callee::Expr(callee) = &call.callee
                    && let Expr::Ident(callee) = callee.unwrap_parens()
                    && callee.sym == "Error"
                {
                    fresh = Some((receiver, callee.to_id()));
                    continues = true;
                } else if let Some((receiver, callee)) = &fresh
                    && let Some(binding_targets::SimpleWriteTarget::Member(member)) =
                        binding_targets::simple_write_target(&assignment.left)
                    && matches!(binding_targets::transparent_target_expr(&member.obj), Expr::Ident(id) if id.to_id() == *receiver)
                    && let Some(property) = static_prop(&member.prop)
                    && property != "__proto__"
                    && property != "stack"
                {
                    let local_read = match assignment.right.unwrap_parens() {
                        Expr::Ident(id) => Some(id.to_id()),
                        _ => None,
                    };
                    if local_read.is_some() || value_without_user_code(&assignment.right) {
                        if !self.past_await {
                            self.fresh_error_claims.push(FreshErrorWriteClaim {
                                site: FreshErrorWriteSite {
                                    owner: self.owner,
                                    callable: self.current,
                                    receiver: receiver.clone(),
                                    property,
                                },
                                callee: callee.clone(),
                                local_read,
                            });
                        }
                        continues = true;
                    }
                }
            }
            if !continues {
                fresh = None;
            }
            expression.visit_with(self);
        }
    }

    fn visit_pat(&mut self, pattern: &Pat) {
        if self.assignment_target && matches!(pattern, Pat::Expr(_) | Pat::Invalid(_)) {
            // The shared pattern target walker only certifies binding
            // patterns. Preserve raw owner effects for expression targets.
            self.uncovered_callable_effect_owners.insert(self.owner);
        }
        if let Some(source) = &self.destructuring_source {
            let receiver = match pattern {
                Pat::Expr(expression) => Some(match expression.unwrap_parens() {
                    Expr::Member(member) => {
                        self.member_write_sites.insert(member.span.into());
                        self.intrinsic_writes.push(intrinsic_write_target(
                            member,
                            self.owner,
                            None,
                            self.current,
                            NativeMutationOperation::PropertyAssignment,
                        ));
                        ValueExpr::from_expr(&member.obj)
                    }
                    _ => ValueExpr::Unknown,
                }),
                Pat::Invalid(_) => Some(ValueExpr::Unknown),
                _ => None,
            };
            if let Some(receiver) = receiver {
                match self.current {
                    Some(key) => self
                        .callables
                        .get_mut(&key)
                        .unwrap()
                        .destructuring_member_receivers
                        .push(receiver.clone()),
                    None => {
                        self.top_destructuring_member_receivers[self.owner.0].push(receiver.clone())
                    }
                }
                self.destructuring_member_writes
                    .push(DestructuringMemberWrite {
                        receiver,
                        source: source.clone(),
                    });
            }
        }
        pattern.visit_children_with(self);
    }

    fn visit_assign_pat(&mut self, assignment: &AssignPat) {
        let Some(source) = self.destructuring_source.take() else {
            assignment.left.visit_with(self);
            self.may_skip_member_read_depth += 1;
            assignment.right.visit_with(self);
            self.may_skip_member_read_depth -= 1;
            return;
        };
        self.destructuring_source = Some(ValueExpr::Alternatives(
            [source.clone(), ValueExpr::from_expr(&assignment.right)].into(),
        ));
        self.namespace_privacy.target_depth += 1;
        assignment.left.visit_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.destructuring_source = Some(source);
        self.may_skip_member_read_depth += 1;
        assignment.right.visit_with(self);
        self.may_skip_member_read_depth -= 1;
    }

    fn visit_for_head(&mut self, head: &ForHead) {
        let mut assignments = Vec::new();
        match head {
            ForHead::Pat(pattern) => {
                binding_targets::record_pat_write(pattern, self);
                collect_pattern_assignments(pattern, &ValueExpr::Unknown, &mut assignments);
                self.reassigned_bindings
                    .extend(assignments.iter().map(|(binding, _)| binding.clone()));
            }
            ForHead::VarDecl(declaration) => {
                for declarator in &declaration.decls {
                    binding_targets::record_pat_write(&declarator.name, self);
                    collect_pattern_assignments(
                        &declarator.name,
                        &ValueExpr::Unknown,
                        &mut assignments,
                    );
                }
            }
            _ => {}
        }
        if let Some(key) = self.current {
            self.callables
                .get_mut(&key)
                .unwrap()
                .head_bindings
                .extend(assignments.iter().map(|(binding, _)| binding.clone()));
        }
        if let ForHead::Pat(pattern) = head {
            self.invalidate_property_key_pattern(pattern);
        }
        let previous_source = self.destructuring_source.take();
        if matches!(head, ForHead::Pat(_)) {
            self.destructuring_source = Some(ValueExpr::Unknown);
        }
        self.namespace_privacy.target_depth += 1;
        let saved = std::mem::replace(&mut self.assignment_target, matches!(head, ForHead::Pat(_)));
        let saved_declaration = std::mem::replace(
            &mut self.for_head_declaration,
            matches!(head, ForHead::VarDecl(_)),
        );
        head.visit_children_with(self);
        self.for_head_declaration = saved_declaration;
        self.assignment_target = saved;
        self.namespace_privacy.target_depth -= 1;
        self.destructuring_source = previous_source;
    }

    fn visit_simple_assign_target(&mut self, target: &SimpleAssignTarget) {
        if let Some(binding_targets::SimpleWriteTarget::Member(member)) =
            binding_targets::simple_assign_target(target)
        {
            self.member_write_sites.insert(member.span.into());
        }
        target.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, assignment: &AssignExpr) {
        self.index_assign_expr(assignment);
    }

    fn visit_update_expr(&mut self, expression: &UpdateExpr) {
        binding_targets::record_update_target(&expression.arg, self);
        let simple_target = binding_targets::simple_write_target_expr(&expression.arg);
        if let Some(binding_targets::SimpleWriteTarget::Binding(binding)) = simple_target.as_ref() {
            if let Some(key) = self.current {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .update_targets
                    .insert(binding.clone());
            }
            self.record_nested_write(binding.clone());
            self.reassigned_bindings.insert(binding.clone());
            self.property_key_assignments
                .entry(binding.clone())
                .or_default()
                .push(PropertyKeyExpr::Unknown);
        }
        if let Some(binding_targets::SimpleWriteTarget::Member(member)) = simple_target.as_ref() {
            self.member_write_sites.insert(member.span.into());
        }

        if matches!(
            simple_target.as_ref(),
            Some(binding_targets::SimpleWriteTarget::Binding(binding))
                if matches!(
                    binding.0.as_ref(),
                    "Error" | "Function" | "Object" | "Array" | "Math" | "RegExp" | "Symbol"
                        | "Promise" | "Map" | "Set" | "WeakMap" | "WeakSet" | "FinalizationRegistry"
                )
        ) {
            self.intrinsic_writes.push(IntrinsicWriteTarget {
                owner: self.owner,
                activation_callable: self.current,
                constructor_this: None,
                span: expression.span,
                operation: NativeMutationOperation::Update,
                receiver: ValueExpr::from_expr(binding_targets::transparent_target_expr(
                    &expression.arg,
                )),
                key: None,
                additional_keys: Vec::new(),
                value: None,
            });
        }

        if matches!(
            simple_target.as_ref(),
            Some(binding_targets::SimpleWriteTarget::Binding(binding))
                if binding.0.as_ref() == "Map"
        ) {
            self.reassigned_map = true;
        }
        self.namespace_privacy.target_depth += 1;
        expression.visit_children_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.record_implicit_coercion(&expression.arg, expression.span);
        if let Some(binding_targets::SimpleWriteTarget::Member(member)) = simple_target.as_ref() {
            self.intrinsic_writes.push(intrinsic_write_target(
                member,
                self.owner,
                self.current,
                self.current,
                NativeMutationOperation::Update,
            ));
            self.mark_dynamic_property_effect(Some(&member.obj));
        }
        // Keep the source protocol for conservative flat-summary fallback.
        // A complete ordered body instead represents this binding read,
        // ToNumeric and store at execution, where values decide coverage.
        self.record_source_protocol(
            CallKind::OpaqueExecution,
            ValueExpr::from_expr(binding_targets::transparent_target_expr(&expression.arg)),
            expression.span,
        );
        let ordered_binding_update = matches!(
            simple_target.as_ref(),
            Some(binding_targets::SimpleWriteTarget::Binding(_))
        ) && self
            .current
            .is_some_and(|key| self.callables[&key].body_facts.ordered_plan().is_some());
        if !ordered_binding_update {
            self.mark_opaque();
        }
    }

    fn visit_unary_expr(&mut self, expression: &UnaryExpr) {
        if expression.op == UnaryOp::Delete
            && let Expr::Member(member) = expression.arg.unwrap_parens()
        {
            self.member_write_sites.insert(member.span.into());
        }

        let target = usize::from(expression.op == UnaryOp::Delete);
        self.namespace_privacy.target_depth += target;
        expression.visit_children_with(self);
        self.namespace_privacy.target_depth -= target;
        if matches!(
            expression.op,
            UnaryOp::Plus | UnaryOp::Minus | UnaryOp::Tilde
        ) {
            self.record_implicit_coercion(&expression.arg, expression.span);
        }
        if expression.op == UnaryOp::Delete
            && let Expr::Member(member) = expression.arg.unwrap_parens()
        {
            self.intrinsic_writes.push(intrinsic_write_target(
                member,
                self.owner,
                self.current,
                self.current,
                NativeMutationOperation::Delete,
            ));
            self.mark_dynamic_property_effect(Some(&member.obj));
        }
    }

    fn visit_return_stmt(&mut self, ret: &ReturnStmt) {
        // A generator's return value reaches its consumer through `next()`
        // or `yield*`, as a yielded one does.
        if let (Some(key), Some(argument)) = (self.current, &ret.arg)
            && self.callables[&key].generator
        {
            self.record_published_source(argument);
        }
        ret.visit_children_with(self);
        if ret.arg.is_none()
            && let Some(key) = self.current
        {
            self.callables
                .get_mut(&key)
                .unwrap()
                .body_facts
                .record_undefined_return();
        }
        if let (Some(key), Some(arg)) = (self.current, &ret.arg) {
            let callable = self.callables.get_mut(&key).unwrap();
            let value = callable.body_facts.record_return(arg);
            if let ValueExpr::Binding(id) = &value {
                callable.escaped_bindings.insert(id.clone());
            }
        }
    }

    fn visit_yield_expr(&mut self, yielded: &YieldExpr) {
        if let Some(argument) = &yielded.arg {
            self.record_published_source(argument);
            if yielded.delegate {
                self.record_iterated_source(argument);
            }
        }
        yielded.visit_children_with(self);
    }

    fn visit_expr_or_spread(&mut self, element: &ExprOrSpread) {
        if element.spread.is_some() {
            self.record_iterated_source(&element.expr);
        }
        element.visit_children_with(self);
    }

    fn visit_throw_stmt(&mut self, thrown: &ThrowStmt) {
        self.record_published_source(&thrown.arg);
        thrown.visit_children_with(self);
        self.mark_possible_namespace_escape(&thrown.arg);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        self.index_call_expr(call);
    }

    fn visit_tpl(&mut self, template: &Tpl) {
        for expression in &template.exprs {
            expression.visit_with(self);
            self.record_implicit_coercion(expression, expression.span());
        }
    }

    fn visit_tagged_tpl(&mut self, template: &TaggedTpl) {
        template.tag.visit_with(self);
        for expression in &template.tpl.exprs {
            expression.visit_with(self);
        }
        template.type_params.visit_with(self);
        self.record_source_protocol(
            CallKind::OpaqueExecution,
            ValueExpr::Alternatives(
                std::iter::once(ValueExpr::from_expr(&template.tag))
                    .chain(
                        template
                            .tpl
                            .exprs
                            .iter()
                            .map(|expr| ValueExpr::from_expr(expr)),
                    )
                    .collect(),
            ),
            template.span,
        );
        self.mark_opaque();
    }

    fn visit_opt_chain_expr(&mut self, chain: &OptChainExpr) {
        // Only the optional operation's own lookup is unsupported. Namespace
        // reads in its argument callbacks still receive their ordinary context.
        let member = match chain.base.as_ref() {
            OptChainBase::Member(member) => Some(member),
            OptChainBase::Call(call) => match call.callee.unwrap_parens() {
                Expr::Member(member) => Some(member),
                _ => None,
            },
        };
        if !self.namespace_privacy.members.is_empty()
            && let Some(member) = member
        {
            self.namespace_privacy
                .optional_members
                .insert(member.span.lo.0);
        }
        let before = self.past_await;
        // Computed keys and call arguments after `?.` may never execute.
        // Conservatively mark the whole chain conditional for read receipts.
        self.may_skip_member_read_depth += 1;
        chain.visit_children_with(self);
        self.may_skip_member_read_depth -= 1;
        self.past_await = before;
        if let OptChainBase::Call(call) = chain.base.as_ref() {
            // Optional invocation can still execute arbitrary callee/receiver
            // protocols and invoke its arguments. Keep that conservative call
            // even though value/short-circuit inference stays opaque. Do not
            // admit the assertions indexed only for ordinary CallExpr syntax.
            self.record_call(Call::from_source_arguments(
                CallKind::Call,
                &call.callee,
                &call.args,
                call.span,
                self.current,
                self.owner,
            ));
            if call.args.iter().any(|arg| arg.spread.is_some()) {
                // Argument positions and the returned value are unmodeled for
                // spread calls. Preserve the callee and argument capabilities
                // through the opaque path, including callable return values.
                self.record_source_protocol(
                    CallKind::Call,
                    ValueExpr::Alternatives(
                        std::iter::once(ValueExpr::from_expr(&call.callee))
                            .chain(call.args.iter().map(|arg| ValueExpr::from_expr(&arg.expr)))
                            .collect(),
                    ),
                    call.span,
                );
            }
        }
        self.mark_opaque();
    }

    fn visit_new_expr(&mut self, new_expr: &NewExpr) {
        if matches!(new_expr.callee.unwrap_parens(), Expr::Ident(_)) {
            self.bare_constructor_sites.insert(new_expr.span.into());
        }
        if let Some(span) = map_entry_array_span(new_expr) {
            self.map_entry_arrays.insert(span);
        }
        let previous_target = self.safe_class_target.take();
        self.safe_class_target = match new_expr.callee.unwrap_parens() {
            Expr::Ident(id) => Some(id.to_id()),
            _ => None,
        };
        new_expr.callee.visit_with(self);
        self.safe_class_target = previous_target;
        let retained_call = self.retained_constructor_call(new_expr);
        let callback_arguments: BTreeSet<_> = retained_call
            .iter()
            .flat_map(|call| &call.signatures)
            .flat_map(|signature| signature.callback_arguments.iter().copied())
            .collect();
        if let Some(contract) = retained_call {
            self.retained_constructor_calls
                .insert(new_expr.span.into(), contract);
        }
        for (position, argument) in new_expr
            .args
            .as_deref()
            .unwrap_or_default()
            .iter()
            .enumerate()
        {
            if callback_arguments.contains(&position) {
                self.factory_callback_depth += 1;
                argument.visit_with(self);
                self.factory_callback_depth -= 1;
            } else {
                argument.visit_with(self);
            }
        }
        if new_expr
            .args
            .as_deref()
            .unwrap_or_default()
            .iter()
            .any(|arg| arg.spread.is_some())
        {
            self.mark_opaque();
        }
        self.record_call(Call::from_source_arguments(
            CallKind::New,
            &new_expr.callee,
            new_expr.args.as_deref().unwrap_or_default(),
            new_expr.span,
            self.current,
            self.owner,
        ));
    }

    fn visit_await_expr(&mut self, await_expr: &AwaitExpr) {
        await_expr.arg.visit_with(self);
        if self.may_skip_await_depth == 0 {
            self.past_await = true;
        }
    }
}
