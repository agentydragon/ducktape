//! The AST visitor that fills the `ExecutionIndex`.

use super::class_value_facts::collect_class_value_facts;
use super::fresh_values::{
    from_entries_mapped_proof, map_entry_array_span, primitive_constant, record_factory_argument,
    value_without_user_code,
};
use super::global_receiver::{
    DestructuringMemberWrite, GlobalMembershipProbe, builtin_global_root, is_global_receiver,
};
use super::local_constructors::local_constructor;
use super::native_write_census::{IntrinsicWriteTarget, intrinsic_write_target};
use super::patterns::{
    collect_assign_target_assignments, collect_pattern_assignments, pattern_iterates,
    pattern_iterates_part, pattern_loses_values,
};
use super::property_keys::PropertyKeyExpr;
use super::{
    BindingDeclarationWrite, Call, CallKind, CallbackResultWrite, DescriptorCopyCallCandidate,
    ExecutionIndex, FreshErrorWriteClaim, FreshErrorWriteSite, IndexedRetainedCallbackCall,
    MemberValueWrite, Parameter, data_read_operand, empty_map_object_sites,
    no_reentry_inert_argument, no_reentry_primitive_argument, object_literal_assign_keys,
    plain_assign_source_property, set_from_primitive_array_aliases, set_from_set_spread_source,
    shallow_data_pattern, sole_return_expression,
};
use crate::namespace_wrapper;
use analysis::facts::at_init_fallback::{
    is_no_sync_callback_member_call, member_row_invokes_no_argument,
};
use analysis_domain::builtins::{ArgSel, iteration_row};
use analysis_domain::lazy_boundary::visit_eager_member_parts;
use analysis_model::PrimKinds;
use analysis_vocabulary::report_schema::NativeMutationOperation;
use analysis_vocabulary::value::Kind;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_common::Spanned;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};
use value_model::local_operations::{DeferredCallableCreation, LocalBinding, LocalBody};
use value_model::{
    ArrayElement, ArrayIteration, CallSite, LiteralPropertyKind, MemberKey,
    OwnPropertyDescriptorSyntax, OwnPropertyShape, ValueExpr, object_data_property_name,
    static_prop, static_property_name,
};

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
        if let Some(prototype) = object.props.iter().find_map(|property| match property {
            PropOrSpread::Prop(property) => match property.as_ref() {
                Prop::KeyValue(pair)
                    if matches!(pair.key, PropName::Ident(_) | PropName::Str(_))
                        && static_property_name(&pair.key).as_deref() == Some("__proto__")
                        && !matches!(pair.value.unwrap_parens(), Expr::Lit(Lit::Null(_))) =>
                {
                    Some(ValueExpr::from_expr(&pair.value))
                }
                _ => None,
            },
            _ => None,
        }) {
            self.literal_prototypes.insert(object.span.lo.0, prototype);
        }
        if matches!(object.props.as_slice(), [PropOrSpread::Spread(_)]) {
            self.exact_object_spread_copies.insert(object.span.lo.0);
        }
        let mut definition_kinds = Vec::new();
        let mut record_members = Some(Vec::new());
        let mut member_names = BTreeSet::new();
        for property in &object.props {
            let member = match property {
                PropOrSpread::Prop(property) => match property.as_ref() {
                    Prop::Method(method) if method.function.decorators.is_empty() => {
                        Some((&method.key, method.function.span.lo.0))
                    }
                    Prop::Getter(getter) => Some((&getter.key, getter.function.span.lo.0)),
                    _ => None,
                },
                _ => None,
            };
            if let Some(members) = record_members.as_mut() {
                if let Some((key, function_start)) = member
                    && matches!(
                        key,
                        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
                    )
                    && let Some(name) = object_data_property_name(key)
                    && member_names.insert(name.clone())
                {
                    members.push(crate::foreign_call::RecordMember {
                        name,
                        function_start,
                    });
                } else {
                    record_members = None;
                }
            }

            let (kind, callable) = match property {
                PropOrSpread::Prop(property) => match property.as_ref() {
                    Prop::KeyValue(_) | Prop::Shorthand(_) => (LiteralPropertyKind::Data, None),
                    Prop::Method(method) => {
                        (LiteralPropertyKind::Method, Some(method.function.span.lo.0))
                    }
                    Prop::Getter(getter) => {
                        (LiteralPropertyKind::Getter, Some(getter.function.span.lo.0))
                    }
                    Prop::Setter(setter) => {
                        (LiteralPropertyKind::Setter, Some(setter.function.span.lo.0))
                    }
                    _ => (LiteralPropertyKind::Opaque, None),
                },
                _ => (LiteralPropertyKind::Opaque, None),
            };
            definition_kinds.push(kind);
            if let Some(callable) = callable {
                let home_object = object.span.lo.0;
                let creation = match kind {
                    LiteralPropertyKind::Method => DeferredCallableCreation::Method {
                        callable,
                        home_object,
                    },
                    LiteralPropertyKind::Getter => DeferredCallableCreation::Getter {
                        callable,
                        home_object,
                    },
                    LiteralPropertyKind::Setter => DeferredCallableCreation::Setter {
                        callable,
                        home_object,
                    },
                    _ => unreachable!("only deferred definitions have a callable"),
                };
                self.callable_creations.insert(callable, creation);
            }
        }
        self.literal_property_kinds
            .insert(object.span.lo.0, definition_kinds);
        if let Some(members) = record_members.filter(|members| !members.is_empty()) {
            self.method_record_members.insert(object.span.lo.0, members);
        }
        let mut descriptor_shape = BTreeMap::new();
        let mut descriptor_creation_order = Vec::new();
        let descriptorable = object.props.iter().all(|property| {
            let PropOrSpread::Prop(property) = property else {
                return false;
            };
            match property.as_ref() {
                Prop::KeyValue(pair) => {
                    let Some(name) = static_property_name(&pair.key) else {
                        return false;
                    };
                    if name == "__proto__" || descriptor_shape.contains_key(&name) {
                        return false;
                    }
                    descriptor_creation_order.push(name.clone());
                    descriptor_shape.insert(name, OwnPropertyDescriptorSyntax::Data);
                    true
                }
                Prop::Shorthand(ident) if ident.sym != "__proto__" => {
                    let name = ident.sym.to_string();
                    if descriptor_shape.contains_key(&name) {
                        return false;
                    }
                    descriptor_creation_order.push(name.clone());
                    descriptor_shape.insert(name, OwnPropertyDescriptorSyntax::Data);
                    true
                }
                Prop::Method(method)
                    if method.function.decorators.is_empty()
                        && !matches!(method.key, PropName::Computed(_)) =>
                {
                    let Some(name) = static_property_name(&method.key) else {
                        return false;
                    };
                    if name == "__proto__" || descriptor_shape.contains_key(&name) {
                        return false;
                    }
                    descriptor_creation_order.push(name.clone());
                    descriptor_shape.insert(name, OwnPropertyDescriptorSyntax::Data);
                    true
                }
                Prop::Getter(getter)
                    if getter.function.decorators.is_empty()
                        && !matches!(getter.key, PropName::Computed(_)) =>
                {
                    let Some(name) =
                        static_property_name(&getter.key).filter(|name| name != "__proto__")
                    else {
                        return false;
                    };
                    match descriptor_shape.get_mut(&name) {
                        Some(OwnPropertyDescriptorSyntax::Accessor {
                            getter: current_getter,
                            ..
                        }) if current_getter.is_none() => {
                            *current_getter = Some(getter.function.span.lo.0);
                            true
                        }
                        Some(_) => false,
                        None => {
                            descriptor_creation_order.push(name.clone());
                            descriptor_shape.insert(
                                name,
                                OwnPropertyDescriptorSyntax::Accessor {
                                    getter: Some(getter.function.span.lo.0),
                                    setter: None,
                                },
                            );
                            true
                        }
                    }
                }
                Prop::Setter(setter)
                    if setter.function.decorators.is_empty()
                        && !matches!(setter.key, PropName::Computed(_)) =>
                {
                    let Some(name) =
                        static_property_name(&setter.key).filter(|name| name != "__proto__")
                    else {
                        return false;
                    };
                    match descriptor_shape.get_mut(&name) {
                        Some(OwnPropertyDescriptorSyntax::Accessor {
                            setter: current_setter,
                            ..
                        }) if current_setter.is_none() => {
                            *current_setter = Some(setter.function.span.lo.0);
                            true
                        }
                        Some(_) => false,
                        None => {
                            descriptor_creation_order.push(name.clone());
                            descriptor_shape.insert(
                                name,
                                OwnPropertyDescriptorSyntax::Accessor {
                                    getter: None,
                                    setter: Some(setter.function.span.lo.0),
                                },
                            );
                            true
                        }
                    }
                }
                _ => false,
            }
        });
        if descriptorable {
            self.own_descriptor_shapes.insert(
                object.span.lo.0,
                OwnPropertyShape {
                    properties: descriptor_shape,
                    creation_order: descriptor_creation_order,
                },
            );
        }
        let mut null_prototype = 0;
        let static_data = object.props.iter().all(|property| match property {
            PropOrSpread::Spread(_) => false,
            PropOrSpread::Prop(property) => match property.as_ref() {
                Prop::KeyValue(pair) => match static_property_name(&pair.key).as_deref() {
                    Some("__proto__")
                        if matches!(pair.value.unwrap_parens(), Expr::Lit(Lit::Null(_))) =>
                    {
                        null_prototype += 1;
                        true
                    }
                    Some("__proto__") | None => false,
                    Some(_) => true,
                },
                Prop::Shorthand(id) => id.sym.as_ref() != "__proto__",
                // Ordinary method syntax installs an own data property. Its
                // body is only stored here, so parameter property reads by a
                // local constructor cannot invoke it. Keep computed keys and
                // the special prototype name outside this narrow proof.
                Prop::Method(method) => {
                    !matches!(method.key, PropName::Computed(_))
                        && static_property_name(&method.key).is_some_and(|name| name != "__proto__")
                }
                _ => false,
            },
        });
        self.object_literal_owners
            .insert(object.span.lo.0, self.owner);
        if static_data {
            if null_prototype == 0 {
                self.plain_data_objects.insert(object.span.lo.0);
            } else if null_prototype == 1 {
                self.null_proto_data_objects.insert(object.span.lo.0);
            }
        }
        let mut primitive_property_counts = BTreeMap::<String, usize>::new();
        let mut primitive_property_values = BTreeSet::<String>::new();
        for property in &object.props {
            let PropOrSpread::Prop(property) = property else {
                continue;
            };
            let (name, primitive_value) = match property.as_ref() {
                Prop::KeyValue(pair) => (
                    static_property_name(&pair.key),
                    js_ast::is_primitive_literal(pair.value.unwrap_parens()),
                ),
                Prop::Shorthand(ident) => (Some(ident.sym.to_string()), false),
                Prop::Assign(assign) => (Some(assign.key.sym.to_string()), false),
                Prop::Method(method) => (static_property_name(&method.key), false),
                Prop::Getter(getter) => (static_property_name(&getter.key), false),
                Prop::Setter(setter) => (static_property_name(&setter.key), false),
            };
            if let Some(name) = name {
                *primitive_property_counts.entry(name.clone()).or_default() += 1;
                if primitive_value {
                    primitive_property_values.insert(name);
                }
            }
        }
        if static_data && null_prototype == 0 {
            for (name, count) in primitive_property_counts {
                if count == 1 && primitive_property_values.contains(&name) {
                    self.primitive_data_properties
                        .insert((object.span.lo.0, name));
                }
            }
        }
        self.literal_depth += 1;
        for property in &object.props {
            match property {
                PropOrSpread::Spread(spread) => {
                    spread.expr.visit_with(self);
                    self.record_call(Call {
                        kind: CallKind::ObjectSpread,
                        callee: ValueExpr::Unknown,
                        args: vec![ValueExpr::from_expr(&spread.expr)],
                        span: spread.expr.span(),
                        caller: self.current,
                        owner: self.owner,
                        plain_object_sources: false,
                        no_sync_arguments: false,
                        no_reentry_candidate: false,
                        no_reentry_record_candidate: false,
                        has_spread: false,
                    });
                }
                PropOrSpread::Prop(property)
                    if let Prop::KeyValue(pair) = property.as_ref()
                        && self.is_factory_member_data_value(&pair.key, &pair.value) =>
                {
                    pair.key.visit_with(self);
                    // This one-hop member read is a deferred data value, not
                    // the namespace object itself. The factory contract's
                    // author must establish that it cannot alias the namespace
                    // or mutate a listed factory member; syntax alone cannot
                    // prove the imported member's runtime identity.
                    self.factory_member_value_depth += 1;
                    pair.value.visit_with(self);
                    self.factory_member_value_depth -= 1;
                }
                _ => property.visit_with(self),
            }
        }
        self.literal_depth -= 1;
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
                self.record_call(Call {
                    kind: CallKind::ArraySpread,
                    callee: ValueExpr::Unknown,
                    args: vec![ValueExpr::from_expr(&element.expr)],
                    span,
                    caller: self.current,
                    owner: self.owner,
                    plain_object_sources: false,
                    no_sync_arguments: false,
                    no_reentry_candidate: false,
                    no_reentry_record_candidate: false,
                    has_spread: false,
                });
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
            self.record_call(Call {
                kind: if expression.op == BinaryOp::In {
                    CallKind::InProtocol
                } else {
                    CallKind::InstanceOfProtocol
                },
                callee: ValueExpr::Unknown,
                args: vec![
                    ValueExpr::from_expr(&expression.right),
                    ValueExpr::from_expr(&expression.left),
                ],
                span: expression.span,
                caller: self.current,
                owner: self.owner,
                plain_object_sources: false,
                no_sync_arguments: false,
                no_reentry_candidate: false,
                no_reentry_record_candidate: false,
                has_spread: false,
            });
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
        declarator.visit_children_with(self);
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
        // Record before visiting operands: an await inside a target or RHS
        // can only make this conservative. Reuse the complete target walker
        // used by raw facts, including patterns and parenthesized targets.
        binding_targets::record_assign_target(&assignment.left, self);
        let simple_target = binding_targets::simple_write_target(&assignment.left);
        if let Some(binding_targets::SimpleWriteTarget::Member(member)) = simple_target.as_ref() {
            self.member_write_sites.insert(member.span.into());
        }
        let mut assigned = Vec::new();
        match simple_target.as_ref() {
            Some(binding_targets::SimpleWriteTarget::Binding(binding)) => {
                assigned.push((binding.clone(), ValueExpr::Unknown));
            }
            Some(binding_targets::SimpleWriteTarget::Member(_)) => {}
            None => {
                if let AssignTarget::Pat(pattern) = &assignment.left {
                    collect_assign_target_assignments(pattern, &ValueExpr::Unknown, &mut assigned);
                }
            }
        }
        self.reassigned_bindings
            .extend(assigned.iter().map(|(binding, _)| binding.clone()));
        match simple_target.as_ref() {
            Some(binding_targets::SimpleWriteTarget::Binding(binding)) => self
                .property_key_assignments
                .entry(binding.clone())
                .or_default()
                .push(if assignment.op == AssignOp::Assign {
                    PropertyKeyExpr::from_expr(&assignment.right)
                } else {
                    PropertyKeyExpr::Unknown
                }),
            None if matches!(&assignment.left, AssignTarget::Pat(_)) => {
                for (id, _) in &assigned {
                    self.property_key_assignments
                        .entry(id.clone())
                        .or_default()
                        .push(PropertyKeyExpr::Unknown);
                }
            }
            _ => {}
        }
        if self.global_guards_requested {
            self.global_alias_written |= match simple_target.as_ref() {
                Some(binding_targets::SimpleWriteTarget::Binding(binding)) => {
                    self.global_object_names.contains(binding.0.as_ref())
                }
                Some(binding_targets::SimpleWriteTarget::Member(member)) => {
                    is_global_receiver(&member.obj, &self.global_object_names)
                        && static_prop(&member.prop).is_some_and(|property| {
                            matches!(
                                property.as_str(),
                                "globalThis" | "window" | "self" | "frames" | "top"
                            )
                        })
                }
                None => false,
            };
        }
        if matches!(
            simple_target.as_ref(),
            Some(binding_targets::SimpleWriteTarget::Binding(binding))
                if binding.0.as_ref() == "Map"
        ) {
            self.reassigned_map = true;
        }
        if let Some(binding_targets::SimpleWriteTarget::Member(member)) = simple_target.as_ref() {
            let right = ValueExpr::from_expr(&assignment.right);
            let value = match assignment.op {
                AssignOp::Assign => right,
                // A logical assignment stores the right side or keeps the
                // property's current value.
                AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign => {
                    ValueExpr::Alternatives(
                        [
                            ValueExpr::Member(
                                Arc::new(ValueExpr::from_expr(
                                    binding_targets::transparent_target_expr(&member.obj),
                                )),
                                static_prop(&member.prop),
                                MemberKey::of(&member.prop),
                                member.span.into(),
                            ),
                            right,
                        ]
                        .into(),
                    )
                }
                _ => ValueExpr::Primitive([right].into()),
            };
            self.intrinsic_writes.push(IntrinsicWriteTarget {
                value: Some(value),
                ..intrinsic_write_target(
                    member,
                    self.owner,
                    self.current,
                    self.current,
                    NativeMutationOperation::PropertyAssignment,
                )
            });
        }
        let before_assignment = self.past_await;
        let previous_source = self.destructuring_source.take();
        if matches!(assignment.left, AssignTarget::Pat(_)) {
            self.destructuring_source = Some(ValueExpr::from_expr(&assignment.right));
        }
        self.namespace_privacy.target_depth += 1;
        assignment.left.visit_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.destructuring_source = previous_source;
        if assignment.op.may_short_circuit() {
            self.may_skip_member_read_depth += 1;
        }
        assignment.right.visit_with(self);
        if assignment.op.may_short_circuit() {
            self.may_skip_member_read_depth -= 1;
            self.past_await = before_assignment;
        }
        if !matches!(
            assignment.op,
            AssignOp::Assign | AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            let left = match simple_target.as_ref() {
                Some(binding_targets::SimpleWriteTarget::Binding(binding)) => {
                    ValueExpr::Binding(binding.clone())
                }
                Some(binding_targets::SimpleWriteTarget::Member(member)) => ValueExpr::Member(
                    Arc::new(ValueExpr::from_expr(
                        binding_targets::transparent_target_expr(&member.obj),
                    )),
                    static_prop(&member.prop),
                    MemberKey::of(&member.prop),
                    member.span.into(),
                ),
                None => ValueExpr::Unknown,
            };
            self.record_implicit_coercion_value(left, assignment.span);
            self.record_implicit_coercion(&assignment.right, assignment.span);
        }
        if let (Some(key), Some(binding_targets::SimpleWriteTarget::Binding(binding))) =
            (self.current, simple_target.as_ref())
        {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.rebinds.insert(binding.clone());
            callable
                .local_assignments
                .push((binding.clone(), ValueExpr::from_expr(&assignment.right)));
        } else if let Some(binding_targets::SimpleWriteTarget::Binding(binding)) =
            simple_target.as_ref()
        {
            self.top_assignments[self.owner.0]
                .push((binding.clone(), ValueExpr::from_expr(&assignment.right)));
            if self.conditional_depth == 0 {
                self.top_definite_initializers[self.owner.0].insert(binding.clone());
            }
        } else if let Some(binding_targets::SimpleWriteTarget::Member(member)) =
            simple_target.as_ref()
        {
            let write = CallbackResultWrite {
                receiver: ValueExpr::from_expr(binding_targets::transparent_target_expr(
                    &member.obj,
                )),
                property_key: match &member.prop {
                    MemberProp::Computed(key) => ValueExpr::from_expr(&key.expr),
                    _ => ValueExpr::NonCallable(PrimKinds::STRING),
                },
                simple_assignment: assignment.op == AssignOp::Assign,
                data_read_operands: data_read_operand(&assignment.right)
                    && match &member.prop {
                        MemberProp::Computed(key) => data_read_operand(&key.expr),
                        _ => true,
                    },
                property: static_prop(&member.prop),
                value: ValueExpr::from_expr(&assignment.right),
                site: CallSite::from(assignment.span),
            };
            if let Some(key) = self.current {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .callback_result_writes
                    .push(write);
            } else {
                self.top_callback_result_writes[self.owner.0].push(write);
            }
            if let Expr::Ident(receiver) = binding_targets::transparent_target_expr(&member.obj)
                && let Some(key) = static_prop(&member.prop)
            {
                let write = MemberValueWrite {
                    receiver: receiver.to_id(),
                    property: Some(key),
                    value: ValueExpr::from_expr(&assignment.right),
                    site: assignment.span.into(),
                    owner: self.owner,
                };
                if let Some(callable) = self.current {
                    self.callables
                        .get_mut(&callable)
                        .unwrap()
                        .member_value_writes
                        .push(write);
                } else {
                    self.top_member_value_writes[self.owner.0].push(write);
                }
            } else if let Expr::Ident(receiver) =
                binding_targets::transparent_target_expr(&member.obj)
            {
                let write = MemberValueWrite {
                    receiver: receiver.to_id(),
                    property: None,
                    value: ValueExpr::from_expr(&assignment.right),
                    site: assignment.span.into(),
                    owner: self.owner,
                };
                if let Some(callable) = self.current {
                    self.callables
                        .get_mut(&callable)
                        .unwrap()
                        .member_value_writes
                        .push(write);
                } else {
                    self.top_member_value_writes[self.owner.0].push(write);
                }
            }
            self.mark_possible_namespace_escape(&assignment.right);
            if assignment.op == AssignOp::Assign {
                self.record_member_write(member);
            } else {
                self.mark_dynamic_property_effect(Some(&member.obj));
            }
        } else if let AssignTarget::Pat(pattern) = &assignment.left {
            let kind = match pattern {
                AssignTargetPat::Array(_) | AssignTargetPat::Object(_) => {
                    Some(CallKind::DeepDestructuringProtocol)
                }
                _ => None,
            };
            if let Some(kind) = kind {
                self.record_source_protocol(
                    kind,
                    ValueExpr::from_expr(&assignment.right),
                    assignment.span,
                );
            }
            let loses_values = match pattern {
                AssignTargetPat::Array(_) | AssignTargetPat::Invalid(_) => true,
                AssignTargetPat::Object(object) => {
                    pattern_loses_values(&Pat::Object(object.clone()))
                }
            };
            if loses_values {
                self.record_published_source(&assignment.right);
            }
            let (iterates, iterates_part) = match pattern {
                AssignTargetPat::Array(array) => {
                    (true, pattern_iterates_part(&Pat::Array(array.clone())))
                }
                AssignTargetPat::Object(object) => {
                    let object = Pat::Object(object.clone());
                    (pattern_iterates(&object), pattern_iterates_part(&object))
                }
                AssignTargetPat::Invalid(_) => (false, false),
            };
            if iterates {
                self.record_iterated_source(&assignment.right);
            }
            if iterates_part {
                self.record_iterated_value(ValueExpr::Unknown);
            }
            match pattern {
                AssignTargetPat::Array(array) => {
                    self.record_iterated_defaults(&Pat::Array(array.clone()))
                }
                AssignTargetPat::Object(object) => {
                    self.record_iterated_defaults(&Pat::Object(object.clone()))
                }
                AssignTargetPat::Invalid(_) => {}
            }
            let mut assignments = Vec::new();
            collect_assign_target_assignments(
                pattern,
                &ValueExpr::from_expr(&assignment.right),
                &mut assignments,
            );
            if let Some(key) = self.current {
                let callable = self.callables.get_mut(&key).unwrap();
                for (binding, value) in assignments {
                    callable.rebinds.insert(binding.clone());
                    callable.local_assignments.push((binding, value));
                }
            } else {
                if self.conditional_depth == 0 {
                    self.top_definite_initializers[self.owner.0]
                        .extend(assignments.iter().map(|(binding, _)| binding.clone()));
                }
                self.top_assignments[self.owner.0].extend(assignments);
            }
            self.mark_dynamic_property_effect(None);
        } else {
            self.mark_dynamic_property_effect(None);
        }
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
        if self.current.is_none()
            && call.args.len() <= 1
            && call
                .args
                .iter()
                .all(|argument| argument.spread.is_none() && primitive_constant(&argument.expr))
            && matches!(&call.callee, Callee::Expr(callee)
                if matches!(callee.unwrap_parens(), Expr::Ident(identifier) if identifier.sym == "Symbol"))
        {
            self.primitive_native_symbol_calls
                .insert(CallSite::from(call.span));
        }
        if let Callee::Expr(callee) = &call.callee {
            if let Expr::Fn(function) = callee.unwrap_parens()
                && function.ident.is_none()
                && !function.function.is_async
                && !function.function.is_generator
            {
                self.immediate_anonymous_callers
                    .insert(function.function.span.lo.0, self.current);
            }
            if let Expr::Ident(import) = callee.unwrap_parens()
                && let [argument] = call.args.as_slice()
                && argument.spread.is_none()
                && let Expr::Fn(function) = argument.expr.unwrap_parens()
                && function.ident.is_none()
            {
                self.inline_callback_entries.insert(
                    function.function.span.lo.0,
                    super::callback_arguments::InlineCallbackEntry {
                        import: import.to_id(),
                        function: function.function.span,
                        call: call.span,
                    },
                );
            }
        }
        if self.current.is_none() {
            self.ordinary_top_call_sites.insert(call.span.into());
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Ident(factory) = callee.unwrap_parens()
            && self
                .retained_record_factories
                .contains(factory.sym.as_ref())
        {
            self.record_factory_calls
                .insert(call.span.into(), record_factory_argument(&call.args));
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
        {
            self.direct_call_members.insert(member.span.into());
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && let MemberProp::Ident(property) = &member.prop
            && let Some(row) = iteration_row(property.sym.as_ref(), &call.args)
            && let ArgSel::Arg(callback) = row.invokes
        {
            self.array_iterations.insert(
                call.span.into(),
                ArrayIteration {
                    row,
                    source: ValueExpr::from_expr(&member.obj),
                    callback: ValueExpr::from_expr(&call.args[callback].expr),
                    initial: call
                        .args
                        .get(1)
                        .map(|initial| ValueExpr::from_expr(&initial.expr)),
                    result: None,
                },
            );
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && matches!(member.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Object" || id.sym == "Reflect")
            && static_prop(&member.prop).as_deref().is_none_or(|method| {
                matches!(
                    method,
                    "set"
                        | "defineProperty"
                        | "defineProperties"
                        | "assign"
                        | "setPrototypeOf"
                        | "deleteProperty"
                )
            })
            && let Some(argument) = call.args.first()
        {
            let receiver = ValueExpr::from_expr(&argument.expr);
            let method = static_prop(&member.prop);
            let mut target = if matches!(
                method.as_deref(),
                Some("set" | "defineProperty" | "deleteProperty")
            ) {
                IntrinsicWriteTarget {
                    owner: self.owner,
                    activation_callable: self.current,
                    constructor_this: None,
                    span: call.span,
                    operation: NativeMutationOperation::MutationCall,
                    receiver,
                    key: Some(
                        call.args
                            .get(1)
                            .map_or(PropertyKeyExpr::Unknown, |argument| {
                                PropertyKeyExpr::from_expr(&argument.expr)
                            }),
                    ),
                    additional_keys: Vec::new(),
                    value: None,
                }
            } else {
                IntrinsicWriteTarget {
                    owner: self.owner,
                    activation_callable: self.current,
                    constructor_this: None,
                    span: call.span,
                    operation: NativeMutationOperation::MutationCall,
                    receiver,
                    key: None,
                    additional_keys: Vec::new(),
                    value: None,
                }
            };
            let literal_keys = (method.as_deref() == Some("assign"))
                .then(|| object_literal_assign_keys(call.args.get(1..).unwrap_or_default()))
                .flatten();
            if let Some((first, rest)) = literal_keys.as_ref().and_then(|keys| keys.split_first()) {
                target.key = Some(PropertyKeyExpr::String(first.clone()));
                target
                    .additional_keys
                    .extend(rest.iter().cloned().map(PropertyKeyExpr::String));
            } else if matches!(method.as_deref(), Some("assign" | "defineProperties")) {
                let sources = if method.as_deref() == Some("assign") {
                    call.args.get(1..).unwrap_or_default()
                } else {
                    call.args.get(1..2).unwrap_or_default()
                };
                for source in sources {
                    if source.spread.is_some() {
                        continue;
                    }
                    if let ValueExpr::ObjectLiteral(_, properties) =
                        ValueExpr::from_expr(&source.expr)
                    {
                        for (key, _) in properties.iter() {
                            if let Some(key) = key.as_ref().filter(|key| *key != "__proto__") {
                                target
                                    .additional_keys
                                    .push(PropertyKeyExpr::String(key.clone()));
                            }
                        }
                    }
                }
            }
            self.intrinsic_writes.push(target);
        }

        let callback_argument = self.inline_factory_callback_argument(call);
        if self.current.is_none()
            && let Some(Expr::Ident(callee)) = Self::inline_callback_callee(call)
        {
            self.direct_inline_callback_calls
                .insert(call.span.into(), callee.to_id());
        }
        let retained_call = self.retained_callback_call(call);
        let no_reentry_call = self.no_reentry_callback_call(call);
        let no_reentry_accessor_call = no_reentry_call
            .as_ref()
            .is_some_and(|contract| contract.require_single_callback_identity)
            .then(|| CallSite::from(call.span));
        let callback_arguments: BTreeSet<_> = callback_argument
            .into_iter()
            .chain(
                retained_call
                    .iter()
                    .chain(no_reentry_call.iter())
                    .flat_map(|call| call.signatures.iter())
                    .flat_map(|signature| signature.callback_arguments.iter().copied()),
            )
            .collect();
        if let Some(retained_call) = retained_call {
            self.retained_callback_calls.insert(
                call.span.into(),
                IndexedRetainedCallbackCall {
                    call: retained_call,
                    owner: self.owner,
                    caller: self.current,
                },
            );
        }
        if let Some(no_reentry_call) = no_reentry_call {
            self.no_reentry_callback_calls
                .insert(call.span.into(), no_reentry_call);
        }
        let is_factory_member_callee = self.literal_depth > 0
            && self.factory_callback_depth > 0
            && matches!(&call.callee, Callee::Expr(callee)
                if self.is_callback_factory_member_callee(callee));
        if !callback_arguments.is_empty() || is_factory_member_callee {
            match &call.callee {
                Callee::Expr(callee) if is_factory_member_callee => {
                    self.factory_member_value_depth += 1;
                    callee.visit_with(self);
                    self.factory_member_value_depth -= 1;
                }
                callee => callee.visit_with(self),
            }
            if let Callee::Expr(callee) = &call.callee
                && let Expr::Member(member) = callee.unwrap_parens()
            {
                self.record_accessor_callee_lookup(member, no_reentry_accessor_call);
            }
            for (index, argument) in call.args.iter().enumerate() {
                if callback_arguments.contains(&index) {
                    self.factory_callback_depth += 1;
                    argument.visit_with(self);
                    self.factory_callback_depth -= 1;
                } else {
                    argument.visit_with(self);
                }
            }
            call.type_args.visit_with(self);
        } else if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
        {
            // A member callee is evaluated before any arguments. Keep a
            // typed event at that point so an applied getter can invalidate
            // receiver state before nested argument calls are analyzed.
            callee.visit_with(self);
            self.record_accessor_callee_lookup(member, no_reentry_accessor_call);
            for argument in &call.args {
                argument.visit_with(self);
            }
            call.type_args.visit_with(self);
        } else {
            call.visit_children_with(self);
        }
        if call.args.iter().any(|arg| arg.spread.is_some()) {
            self.mark_opaque();
        }
        if let Some(proof) = from_entries_mapped_proof(call, &self.plain_array_bindings) {
            self.mapped_array_map_calls
                .insert(proof.map_call_site, proof.clone());
            self.from_entries_map_calls.insert(call.span.into(), proof);
        }
        if let Callee::Expr(callee) = &call.callee {
            if matches!(call.args.as_slice(), [first, second]
                if first.spread.is_none()
                    && second.spread.is_none()
                    && matches!(second.expr.unwrap_parens(), Expr::Lit(Lit::Bool(boolean)) if boolean.value))
            {
                self.literal_true_second_arguments.insert(call.span.into());
            }
            let indexed_call = Call {
                callee: ValueExpr::from_expr(callee),
                args: call
                    .args
                    .iter()
                    .map(|arg| ValueExpr::from_expr(&arg.expr))
                    .collect(),
                kind: CallKind::Call,
                span: call.span,
                has_spread: call.args.iter().any(|arg| arg.spread.is_some()),
                no_reentry_candidate: call
                    .args
                    .iter()
                    .all(|arg| arg.spread.is_none() && no_reentry_primitive_argument(&arg.expr)),
                no_reentry_record_candidate: call
                    .args
                    .iter()
                    .all(|arg| arg.spread.is_none() && no_reentry_inert_argument(&arg.expr))
                    && call
                        .args
                        .iter()
                        .any(|arg| matches!(arg.expr.unwrap_parens(), Expr::Object(_))),
                no_sync_arguments: is_no_sync_callback_member_call(
                    callee,
                    &self.no_sync_callback_members,
                ) || member_row_invokes_no_argument(callee, &call.args),
                caller: self.current,
                owner: self.owner,
                plain_object_sources: call.args.len() > 1
                    && call.args.iter().skip(1).all(|arg| {
                        matches!(arg.expr.unwrap_parens(), Expr::Object(object)
                            if object.props.iter().all(plain_assign_source_property))
                    }),
            };
            if let Expr::Member(member) = callee.unwrap_parens() {
                self.accessor_callee_calls
                    .insert(member.span.into(), indexed_call.clone());
            }
            self.record_call(indexed_call);
        } else {
            self.record_source_protocol(CallKind::OpaqueExecution, ValueExpr::Unknown, call.span);
            self.mark_opaque();
        }
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
            self.record_call(Call {
                callee: ValueExpr::from_expr(&call.callee),
                args: call
                    .args
                    .iter()
                    .map(|arg| ValueExpr::from_expr(&arg.expr))
                    .collect(),
                kind: CallKind::Call,
                span: call.span,
                has_spread: call.args.iter().any(|arg| arg.spread.is_some()),
                no_reentry_candidate: false,
                no_reentry_record_candidate: false,
                no_sync_arguments: false,
                caller: self.current,
                owner: self.owner,
                plain_object_sources: false,
            });
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
        self.record_call(Call {
            callee: ValueExpr::from_expr(&new_expr.callee),
            args: new_expr
                .args
                .as_deref()
                .unwrap_or_default()
                .iter()
                .map(|arg| ValueExpr::from_expr(&arg.expr))
                .collect(),
            kind: CallKind::New,
            span: new_expr.span,
            has_spread: new_expr
                .args
                .as_deref()
                .unwrap_or_default()
                .iter()
                .any(|arg| arg.spread.is_some()),
            no_sync_arguments: false,
            no_reentry_candidate: false,
            no_reentry_record_candidate: false,
            caller: self.current,
            owner: self.owner,
            plain_object_sources: false,
        });
    }

    fn visit_await_expr(&mut self, await_expr: &AwaitExpr) {
        await_expr.arg.visit_with(self);
        if self.may_skip_await_depth == 0 {
            self.past_await = true;
        }
    }
}
