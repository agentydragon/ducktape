//! AST collection for synchronous execution: callables, roots, writes, and source order.

use super::value_model::{
    ArrayElement, ArrayIteration, ArrayIterationMethod, CallSite, ClassSurface, ClassValueFacts,
    FromEntriesMappedProof, NamespaceValueFacts, ValueExpr, infer_namespace_aliases,
    object_data_property_name, static_prop, static_property_name,
};
use crate::analysis_inputs::AnalysisInputs;
use crate::facts::LocalClassConstructionProof;
use crate::facts::at_init_fallback::{
    is_no_sync_callback_member_call, is_static_event_listener_registration,
};
use crate::facts::global_object::unshadowed_global_object_aliases;
use crate::facts::item_views::{TopLevelItemView, collect_declared_names, var_decl_of_item};
use crate::facts::lazy_boundary::visit_eager_member_parts;
use crate::facts::local_effect_targets::{MetadataDecorateCall, metadata_decorate_call};
use crate::facts::statement_facts::{
    CallbackFactoryCallArgumentWitness, CallbackFactoryWriteWitness, StatementFacts,
};
use crate::facts::value_flow::{ChunkValueFlow, ValueOrigin};
use crate::purity::ChunkCodeGraph;
use source_identity::SourceUnitOrdinal;
use spec::{BuiltinGlobalRoot, HostNoReentryOperation, HostNoReentryRuntime};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

fn map_entry_array_span(new_expr: &NewExpr) -> Option<u32> {
    if !matches!(new_expr.callee.unwrap_parens(), Expr::Ident(ident) if ident.sym == "Map") {
        return None;
    }
    let [argument] = new_expr.args.as_deref().unwrap_or_default() else {
        return None;
    };
    if argument.spread.is_some() {
        return None;
    }
    let Expr::Array(entries) = argument.expr.unwrap_parens() else {
        return None;
    };
    for entry in &entries.elems {
        let Some(entry) = entry else {
            return None;
        };
        if entry.spread.is_some() {
            return None;
        }
        let Expr::Array(pair) = entry.expr.unwrap_parens() else {
            return None;
        };
        if pair.elems.len() != 2
            || pair.elems.iter().any(|element| {
                element
                    .as_ref()
                    .is_none_or(|element| element.spread.is_some())
            })
        {
            return None;
        }
    }
    Some(entries.span.lo.0)
}

fn from_entries_mapped_proof(
    call: &CallExpr,
    plain_array_bindings: &BTreeSet<String>,
) -> Option<FromEntriesMappedProof> {
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Member(from_entries) = callee.unwrap_parens() else {
        return None;
    };
    if !matches!(from_entries.obj.unwrap_parens(), Expr::Ident(object) if object.sym == "Object")
        || !matches!(&from_entries.prop, MemberProp::Ident(property) if property.sym == "fromEntries")
    {
        return None;
    }
    let [argument] = call.args.as_slice() else {
        return None;
    };
    if argument.spread.is_some() {
        return None;
    }
    let Expr::Call(map_call) = argument.expr.unwrap_parens() else {
        return None;
    };
    let Callee::Expr(map_callee) = &map_call.callee else {
        return None;
    };
    let Expr::Member(map_member) = map_callee.unwrap_parens() else {
        return None;
    };
    if !matches!(&map_member.prop, MemberProp::Ident(method) if method.sym == "map")
        || map_call.args.len() != 1
        || map_call.args[0].spread.is_some()
    {
        return None;
    }
    let Expr::Ident(source) = map_member.obj.unwrap_parens() else {
        return None;
    };
    if !plain_array_bindings.contains(source.sym.as_ref()) {
        return None;
    }
    let Expr::Arrow(mapper) = map_call.args[0].expr.unwrap_parens() else {
        return None;
    };
    if mapper.is_async
        || mapper.params.len() != 1
        || mapper.type_params.is_some()
        || mapper.return_type.is_some()
    {
        return None;
    }
    let Pat::Ident(parameter) = &mapper.params[0] else {
        return None;
    };
    let ArrowFunctionBody::Expr(body) = mapper.body.as_ref() else {
        return None;
    };
    let Expr::Array(pair) = body.unwrap_parens() else {
        return None;
    };
    if pair.elems.len() != 2
        || pair.elems.iter().any(|element| {
            element
                .as_ref()
                .is_none_or(|element| element.spread.is_some())
        })
    {
        return None;
    }
    let [Some(key), Some(value)] = pair.elems.as_slice() else {
        return None;
    };
    let Expr::Member(key_member) = key.expr.unwrap_parens() else {
        return None;
    };
    let MemberProp::Ident(key_property) = &key_member.prop else {
        return None;
    };
    if !matches!(key_member.obj.unwrap_parens(), Expr::Ident(id) if id.to_id() == parameter.id.to_id())
        || !matches!(value.expr.unwrap_parens(), Expr::Ident(id) if id.to_id() == parameter.id.to_id())
    {
        return None;
    }
    Some(FromEntriesMappedProof {
        source: ValueExpr::Binding(source.to_id()),
        key: key_property.sym.to_string(),
        map_call_site: map_call.span.into(),
        object_intrinsic: match from_entries.obj.unwrap_parens() {
            Expr::Ident(object) => object.to_id(),
            _ => unreachable!("checked Object.fromEntries receiver"),
        },
    })
}

enum FreshValueStep {
    Allocate {
        binding: Id,
        constructor: Id,
    },
    Factory {
        binding: Id,
        callee: ValueExpr,
    },
    Literal {
        binding: Id,
        span: u32,
        own_fields: BTreeSet<String>,
    },
    Write {
        receiver: Id,
        property: String,
    },
}

enum FreshValue {
    Instance(u32),
    Factory,
    Literal(BTreeSet<String>),
}

fn fresh_literal_own_fields(object: &ObjectLit) -> Option<BTreeSet<String>> {
    let mut fields = BTreeSet::new();
    for property in &object.props {
        let (name, value) = match property {
            PropOrSpread::Prop(property) => match property.as_ref() {
                Prop::KeyValue(pair) if !matches!(pair.key, PropName::Computed(_)) => {
                    (object_data_property_name(&pair.key)?, pair.value.as_ref())
                }
                Prop::Shorthand(ident) => {
                    if ident.sym == "__proto__" || !fields.insert(ident.sym.to_string()) {
                        return None;
                    }
                    continue;
                }
                _ => return None,
            },
            PropOrSpread::Spread(_) => return None,
        };
        if name == "__proto__"
            || !matches!(value.unwrap_parens(), Expr::Ident(_)) && !value_without_user_code(value)
            || !fields.insert(name)
        {
            return None;
        }
    }
    Some(fields)
}

fn primitive_constant(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Lit(Lit::Str(_) | Lit::Bool(_) | Lit::Null(_) | Lit::Num(_) | Lit::BigInt(_)) => true,
        Expr::Unary(unary) if unary.op != UnaryOp::Delete => primitive_constant(&unary.arg),
        Expr::Bin(binary) if !matches!(binary.op, BinaryOp::In | BinaryOp::InstanceOf) => {
            primitive_constant(&binary.left) && primitive_constant(&binary.right)
        }
        _ => false,
    }
}

fn primitive_literal(expression: &Expr) -> bool {
    matches!(
        expression.unwrap_parens(),
        Expr::Lit(Lit::Str(_) | Lit::Bool(_) | Lit::Null(_) | Lit::Num(_) | Lit::BigInt(_))
    )
}

fn value_without_user_code(expression: &Expr) -> bool {
    if primitive_constant(expression) {
        return true;
    }
    match expression.unwrap_parens() {
        Expr::Arrow(_) => true,
        Expr::Fn(function) => {
            function.function.decorators.is_empty()
                && function
                    .function
                    .params
                    .iter()
                    .all(|parameter| parameter.decorators.is_empty())
        }
        Expr::Array(array) => array.elems.is_empty(),
        // A11 fixes Array.prototype.values as an ordinary data property.
        // The empty receiver cannot publish the instance being initialized.
        Expr::Member(member) => {
            matches!(&member.prop, MemberProp::Ident(property) if property.sym == "values")
                && matches!(member.obj.unwrap_parens(), Expr::Array(array) if array.elems.is_empty())
        }
        _ => false,
    }
}

fn fresh_value_step(item: &ModuleItem) -> Option<FreshValueStep> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration)))
            if declaration.kind == VarDeclKind::Const && declaration.decls.len() == 1 =>
        {
            let declaration = &declaration.decls[0];
            let Pat::Ident(binding) = &declaration.name else {
                return None;
            };
            let initializer = declaration.init.as_deref()?.unwrap_parens();
            if let Expr::Object(object) = initializer {
                return fresh_literal_own_fields(object).map(|own_fields| {
                    FreshValueStep::Literal {
                        binding: binding.id.to_id(),
                        span: object.span.lo.0,
                        own_fields,
                    }
                });
            }
            if let Expr::Call(call) = initializer {
                let [argument] = call.args.as_slice() else {
                    return None;
                };
                let Callee::Expr(callee) = &call.callee else {
                    return None;
                };
                match callee.unwrap_parens() {
                    Expr::Ident(_) => {}
                    Expr::Member(member)
                        if matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                            && matches!(member.prop, MemberProp::Ident(_)) => {}
                    _ => return None,
                }
                return (argument.spread.is_none()
                    && matches!(argument.expr.unwrap_parens(), Expr::Fn(_) | Expr::Arrow(_))
                    && value_without_user_code(&argument.expr))
                .then(|| FreshValueStep::Factory {
                    binding: binding.id.to_id(),
                    callee: ValueExpr::from_expr(callee),
                });
            }
            let Expr::New(allocation) = initializer else {
                return None;
            };
            let Expr::Ident(constructor) = allocation.callee.unwrap_parens() else {
                return None;
            };
            allocation
                .args
                .as_ref()
                .is_none_or(Vec::is_empty)
                .then(|| FreshValueStep::Allocate {
                    binding: binding.id.to_id(),
                    constructor: constructor.to_id(),
                })
        }
        ModuleItem::Stmt(Stmt::Expr(statement)) => {
            let Expr::Assign(assignment) = statement.expr.unwrap_parens() else {
                return None;
            };
            if assignment.op != AssignOp::Assign || !value_without_user_code(&assignment.right) {
                return None;
            }
            let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assignment.left else {
                return None;
            };
            let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
                return None;
            };
            let MemberProp::Ident(property) = &member.prop else {
                return None;
            };
            (property.sym != "__proto__").then(|| FreshValueStep::Write {
                receiver: receiver.to_id(),
                property: property.sym.to_string(),
            })
        }
        _ => None,
    }
}

fn fresh_value_writes(
    steps: &[Option<FreshValueStep>],
    facts: &NamespaceValueFacts,
    factory_imports: &BTreeSet<Id>,
    factory_members: &BTreeMap<Id, BTreeSet<String>>,
) -> (BTreeSet<SourceUnitOrdinal>, BTreeSet<SourceUnitOrdinal>) {
    let mut active: Option<(Id, FreshValue)> = None;
    let mut writes = BTreeSet::new();
    let mut instance_field_writes = BTreeSet::new();
    for (ordinal, step) in steps.iter().enumerate() {
        match step {
            Some(FreshValueStep::Allocate {
                binding,
                constructor,
            }) => {
                active = facts
                    .resolve_class(
                        &ValueExpr::Binding(constructor.clone()),
                        &mut BTreeSet::new(),
                    )
                    .filter(|(key, surface)| {
                        *surface == ClassSurface::Constructor
                            && facts.class_allocation_without_user_code(*key)
                    })
                    .map(|(key, _)| (binding.clone(), FreshValue::Instance(key)));
            }
            Some(FreshValueStep::Factory { binding, callee }) => {
                let is_factory = match callee {
                    ValueExpr::Binding(callee) => factory_imports.contains(callee),
                    ValueExpr::Member(object, Some(property), true) => {
                        matches!(object.as_ref(), ValueExpr::Binding(namespace)
                            if factory_members.get(namespace)
                                .is_some_and(|members| members.contains(property)))
                    }
                    _ => false,
                };
                active = is_factory.then(|| (binding.clone(), FreshValue::Factory));
            }
            Some(FreshValueStep::Literal {
                binding,
                span: _,
                own_fields,
            }) => {
                active = Some((binding.clone(), FreshValue::Literal(own_fields.clone())));
            }
            Some(FreshValueStep::Write { receiver, property })
                if active.as_ref().is_some_and(|(binding, value)| {
                    binding == receiver
                        && match value {
                            FreshValue::Instance(key) => facts.has_instance_field(*key, property),
                            FreshValue::Factory => property != "prototype",
                            FreshValue::Literal(own_fields) => own_fields.contains(property),
                        }
                }) =>
            {
                writes.insert(SourceUnitOrdinal(ordinal));
                if active
                    .as_ref()
                    .is_some_and(|(_, value)| matches!(value, FreshValue::Instance(_)))
                {
                    // A constructorless local class defined this own data
                    // field before the adjacent write. An inherited setter
                    // cannot observe the assignment.
                    instance_field_writes.insert(SourceUnitOrdinal(ordinal));
                }
            }
            _ => active = None,
        }
    }
    (writes, instance_field_writes)
}

fn add_class_field(facts: &mut ClassValueFacts, is_static: bool, name: String, value: ValueExpr) {
    let fields = if is_static {
        &mut facts.static_fields
    } else {
        &mut facts.instance_fields
    };
    if fields.insert(name.clone(), value).is_some() {
        fields.insert(name, ValueExpr::Unknown);
    }
}

fn collect_class_value_facts(class: &Class) -> ClassValueFacts {
    let mut facts = ClassValueFacts {
        super_class: class.super_class.as_deref().map(ValueExpr::from_expr),
        allocation_without_user_code: class.super_class.is_none() && class.decorators.is_empty(),
        default_has_instance: class.super_class.is_none() && class.decorators.is_empty(),
        ..ClassValueFacts::default()
    };
    if class.super_class.is_some() {
        facts.opaque_instance = true;
    }
    if !class.decorators.is_empty() {
        facts.opaque_instance = true;
        facts.opaque_static = true;
    }
    let mut accessor_names = BTreeSet::new();
    let mut dynamic_instance_keys = !class.decorators.is_empty();
    for member in &class.body {
        facts.default_has_instance &= match member {
            ClassMember::Method(method) => {
                !method.is_static && method.function.decorators.is_empty()
            }
            ClassMember::PrivateMethod(method) => {
                !method.is_static && method.function.decorators.is_empty()
            }
            ClassMember::ClassProp(property) => {
                !property.is_static && property.decorators.is_empty()
            }
            ClassMember::PrivateProp(property) => {
                !property.is_static && property.decorators.is_empty()
            }
            ClassMember::Constructor(_) | ClassMember::Empty(_) => true,
            _ => false,
        };
        match member {
            ClassMember::Method(method) if !method.is_static => {
                if let Some(name) = static_property_name(&method.key) {
                    if method.kind == MethodKind::Method && method.function.decorators.is_empty() {
                        facts.ordinary_instance_methods.insert(name);
                    } else {
                        accessor_names.insert(name);
                    }
                } else {
                    dynamic_instance_keys = true;
                }
            }
            ClassMember::ClassProp(property) if !property.is_static => {
                dynamic_instance_keys |= static_property_name(&property.key).is_none()
                    || !property.decorators.is_empty();
            }
            ClassMember::AutoAccessor(_) => dynamic_instance_keys = true,
            _ => {}
        }
        facts.allocation_without_user_code &= match member {
            ClassMember::ClassProp(property) => {
                !property.is_static
                    && property.decorators.is_empty()
                    && static_property_name(&property.key).is_some_and(|name| name != "__proto__")
                    && property
                        .value
                        .as_deref()
                        .is_none_or(value_without_user_code)
            }
            ClassMember::Method(method) => {
                !method.is_static
                    && matches!(method.kind, MethodKind::Method)
                    && method.function.decorators.is_empty()
                    && method
                        .function
                        .params
                        .iter()
                        .all(|parameter| parameter.decorators.is_empty())
            }
            ClassMember::Empty(_) => true,
            _ => false,
        };
        match member {
            ClassMember::ClassProp(prop) => {
                if !prop.decorators.is_empty() {
                    if prop.is_static {
                        facts.opaque_static = true;
                    } else {
                        facts.opaque_instance = true;
                    }
                    continue;
                }
                let Some(name) = static_property_name(&prop.key) else {
                    if prop.is_static {
                        facts.opaque_static = true;
                    } else {
                        facts.opaque_instance = true;
                    }
                    continue;
                };
                add_class_field(
                    &mut facts,
                    prop.is_static,
                    name,
                    prop.value
                        .as_deref()
                        .map_or(ValueExpr::Undefined, ValueExpr::from_expr),
                );
            }
            ClassMember::PrivateProp(prop) => {
                if !prop.decorators.is_empty() {
                    if prop.is_static {
                        facts.opaque_static = true;
                    } else {
                        facts.opaque_instance = true;
                    }
                    continue;
                }
                add_class_field(
                    &mut facts,
                    prop.is_static,
                    format!("#{}", prop.key.name),
                    prop.value
                        .as_deref()
                        .map_or(ValueExpr::Undefined, ValueExpr::from_expr),
                );
            }
            ClassMember::Method(method)
                if matches!(method.kind, MethodKind::Getter | MethodKind::Setter) =>
            {
                if method.is_static {
                    facts.opaque_static = true;
                } else {
                    facts.opaque_instance = true;
                }
            }
            ClassMember::PrivateMethod(method)
                if matches!(method.kind, MethodKind::Getter | MethodKind::Setter) =>
            {
                if method.is_static {
                    facts.opaque_static = true;
                } else {
                    facts.opaque_instance = true;
                }
            }
            ClassMember::Constructor(_) => facts.opaque_instance = true,
            ClassMember::AutoAccessor(accessor) => {
                if accessor.is_static {
                    facts.opaque_static = true;
                } else {
                    facts.opaque_instance = true;
                }
            }
            ClassMember::StaticBlock(_) => facts.opaque_static = true,
            ClassMember::TsIndexSignature(_) => {
                facts.opaque_instance = true;
                facts.opaque_static = true;
            }
            _ => {}
        }
    }
    if dynamic_instance_keys {
        facts.ordinary_instance_methods.clear();
    } else {
        facts
            .ordinary_instance_methods
            .retain(|name| !accessor_names.contains(name));
    }
    facts
}

fn collect_pattern_assignments(
    pattern: &Pat,
    value: &ValueExpr,
    assignments: &mut Vec<(Id, ValueExpr)>,
) {
    match pattern {
        Pat::Ident(binding) => assignments.push((binding.id.to_id(), value.clone())),
        Pat::Assign(assign) => {
            let value =
                ValueExpr::Alternatives(vec![value.clone(), ValueExpr::from_expr(&assign.right)]);
            collect_pattern_assignments(&assign.left, &value, assignments);
        }
        Pat::Rest(rest) => {
            collect_pattern_assignments(&rest.arg, &ValueExpr::Unknown, assignments);
        }
        Pat::Object(object) => {
            for property in &object.props {
                match property {
                    ObjectPatProp::KeyValue(property) => {
                        let projected = static_property_name(&property.key)
                            .map_or(ValueExpr::Unknown, |name| {
                                ValueExpr::Member(Box::new(value.clone()), Some(name), false)
                            });
                        collect_pattern_assignments(&property.value, &projected, assignments);
                    }
                    ObjectPatProp::Assign(property) => {
                        let projected = ValueExpr::Member(
                            Box::new(value.clone()),
                            Some(property.key.id.sym.to_string()),
                            false,
                        );
                        let projected =
                            property
                                .value
                                .as_deref()
                                .map_or(projected.clone(), |default| {
                                    ValueExpr::Alternatives(vec![
                                        projected,
                                        ValueExpr::from_expr(default),
                                    ])
                                });
                        assignments.push((property.key.id.to_id(), projected));
                    }
                    ObjectPatProp::Rest(rest) => {
                        collect_pattern_assignments(&rest.arg, &ValueExpr::Unknown, assignments);
                    }
                }
            }
        }
        // Array destructuring invokes the iterator protocol. Retain its bound
        // names but do not infer element identity from a syntax-only array
        // shape; unknown iteration results must reach later publication sinks.
        Pat::Array(array) => {
            for element in array.elems.iter().flatten() {
                collect_pattern_assignments(element, &ValueExpr::Unknown, assignments);
            }
        }
        Pat::Invalid(_) | Pat::Expr(_) => {}
    }
}

fn collect_assign_target_assignments(
    target: &AssignTargetPat,
    value: &ValueExpr,
    assignments: &mut Vec<(Id, ValueExpr)>,
) {
    match target {
        AssignTargetPat::Object(object) => {
            for property in &object.props {
                match property {
                    ObjectPatProp::KeyValue(property) => {
                        let projected = static_property_name(&property.key)
                            .map_or(ValueExpr::Unknown, |name| {
                                ValueExpr::Member(Box::new(value.clone()), Some(name), false)
                            });
                        collect_pattern_assignments(&property.value, &projected, assignments);
                    }
                    ObjectPatProp::Assign(property) => {
                        let projected = ValueExpr::Member(
                            Box::new(value.clone()),
                            Some(property.key.id.sym.to_string()),
                            false,
                        );
                        let projected =
                            property
                                .value
                                .as_deref()
                                .map_or(projected.clone(), |default| {
                                    ValueExpr::Alternatives(vec![
                                        projected,
                                        ValueExpr::from_expr(default),
                                    ])
                                });
                        assignments.push((property.key.id.to_id(), projected));
                    }
                    ObjectPatProp::Rest(rest) => {
                        collect_pattern_assignments(&rest.arg, &ValueExpr::Unknown, assignments);
                    }
                }
            }
        }
        AssignTargetPat::Array(array) => {
            for element in array.elems.iter().flatten() {
                collect_pattern_assignments(element, &ValueExpr::Unknown, assignments);
            }
        }
        AssignTargetPat::Invalid(_) => {}
    }
}

fn infer_mutation_receiver_aliases(
    namespace: &Id,
    assignments: &[&(Id, ValueExpr)],
    aliases: &BTreeSet<Id>,
    facts: &NamespaceValueFacts,
) -> BTreeSet<Id> {
    let mut receiver_aliases = BTreeSet::new();
    loop {
        let mut changed = false;
        for assignment in assignments {
            let (target, value) = *assignment;
            if target != namespace
                && value.may_be_namespace_mutation_receiver(
                    namespace,
                    aliases,
                    &receiver_aliases,
                    facts,
                )
                && receiver_aliases.insert(target.clone())
            {
                changed = true;
            }
        }
        if !changed {
            return receiver_aliases;
        }
    }
}

fn has_definite_primitive_result(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Lit(Lit::Regex(_)) => false,
        Expr::Lit(_) | Expr::Tpl(_) | Expr::Unary(_) | Expr::Update(_) => true,
        Expr::Bin(binary) => !matches!(
            binary.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ),
        _ => false,
    }
}

/// Admit only arguments whose evaluation cannot itself trigger caller code.
/// The provider must separately be audited for every admitted argument shape.
/// The call result stays opaque and the ordinary visitor records the effects.
fn no_reentry_primitive_argument(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Lit(literal) => !matches!(literal, Lit::Regex(_)),
        Expr::Unary(unary) if matches!(unary.op, UnaryOp::Void | UnaryOp::Bang) => {
            matches!(unary.arg.unwrap_parens(), Expr::Lit(literal) if !matches!(literal, Lit::Regex(_)))
        }
        Expr::Tpl(template) => template.exprs.is_empty(),
        _ => false,
    }
}

fn no_reentry_inert_argument(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Object(object) => object.props.iter().all(|property| {
            let PropOrSpread::Prop(property) = property else {
                return false;
            };
            let Prop::KeyValue(pair) = property.as_ref() else {
                return false;
            };
            let static_key = match &pair.key {
                PropName::Ident(ident) => ident.sym.as_ref() != "__proto__",
                PropName::Str(string) => string.value.to_string_lossy() != "__proto__",
                PropName::Num(_) | PropName::BigInt(_) => true,
                PropName::Computed(_) => false,
            };
            static_key && no_reentry_inert_argument(&pair.value)
        }),
        _ => no_reentry_primitive_argument(expression),
    }
}

#[derive(Debug, Clone)]
struct Parameter {
    binding: Option<Id>,
    default: Option<ValueExpr>,
}

impl Parameter {
    fn from_pattern(pattern: &Pat) -> Self {
        match pattern {
            Pat::Ident(binding) => Self {
                binding: Some(binding.id.to_id()),
                default: None,
            },
            Pat::Assign(assign) => Self {
                binding: match assign.left.as_ref() {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                },
                default: Some(ValueExpr::from_expr(&assign.right)),
            },
            _ => Self {
                binding: None,
                default: None,
            },
        }
    }
}

#[derive(Debug, Clone)]
struct Call {
    kind: CallKind,
    callee: ValueExpr,
    args: Vec<ValueExpr>,
    span: Span,
    caller: Option<u32>,
    owner: SourceUnitOrdinal,
    plain_object_sources: bool,
    no_sync_arguments: bool,
    no_reentry_candidate: bool,
    no_reentry_record_candidate: bool,
    has_spread: bool,
}

#[derive(Debug, Clone)]
struct RetainedCallbackCall {
    namespace_member: Option<(Id, String)>,
    signatures: Vec<spec::RetainedCallbackSignature>,
    argument_count: usize,
    callback_argument_exprs: BTreeMap<usize, ValueExpr>,
}

#[derive(Debug, Clone)]
struct IndexedRetainedCallbackCall {
    call: RetainedCallbackCall,
    owner: SourceUnitOrdinal,
    caller: Option<u32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CallKind {
    Call,
    New,
    ImplicitCoercion,
    ArraySpread,
    ObjectSpread,
    LocalMemberRead,
    InProtocol,
    InstanceOfProtocol,
    DeepDestructuringProtocol,
}

fn shallow_data_pattern(pattern: &Pat) -> bool {
    fn leaf(pattern: &Pat) -> bool {
        matches!(pattern, Pat::Ident(_))
            || matches!(pattern, Pat::Assign(assign) if matches!(assign.left.as_ref(), Pat::Ident(_)))
    }
    match pattern {
        Pat::Object(object) => object.props.iter().all(|property| match property {
            ObjectPatProp::KeyValue(property) => leaf(&property.value),
            ObjectPatProp::Assign(_) => true,
            ObjectPatProp::Rest(rest) => matches!(rest.arg.as_ref(), Pat::Ident(_)),
        }),
        Pat::Array(array) => array.elems.iter().flatten().all(|element| match element {
            Pat::Rest(rest) => matches!(rest.arg.as_ref(), Pat::Ident(_)),
            other => leaf(other),
        }),
        _ => false,
    }
}

fn data_read_operand(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Ident(_)
        | Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::BigInt(_) | Lit::Bool(_) | Lit::Null(_)) => {
            true
        }
        Expr::Member(member) if matches!(member.prop, MemberProp::Ident(_)) => {
            data_read_operand(&member.obj)
        }
        _ => false,
    }
}

fn plain_assign_source_property(property: &PropOrSpread) -> bool {
    let PropOrSpread::Prop(property) = property else {
        return false;
    };
    let name = match property.as_ref() {
        Prop::KeyValue(pair) => &pair.key,
        Prop::Shorthand(ident) => return ident.sym.as_ref() != "__proto__",
        _ => return false,
    };
    match name {
        PropName::Ident(ident) => ident.sym.as_ref() != "__proto__",
        PropName::Str(string) => string.value.to_string_lossy() != "__proto__",
        PropName::Num(_) | PropName::BigInt(_) => true,
        PropName::Computed(computed) => match computed.expr.unwrap_parens() {
            Expr::Lit(Lit::Str(string)) => string.value.to_string_lossy() != "__proto__",
            Expr::Lit(Lit::Num(_)) | Expr::Lit(Lit::BigInt(_)) => true,
            _ => false,
        },
    }
}

#[derive(Debug, Clone)]
struct CallbackResultWrite {
    receiver: ValueExpr,
    property_key: ValueExpr,
    simple_assignment: bool,
    data_read_operands: bool,
    property: Option<String>,
    value: ValueExpr,
}

#[derive(Debug, Clone)]
struct Callable {
    owner: SourceUnitOrdinal,
    created_after_parent_await: bool,
    params: Vec<Parameter>,
    reads: BTreeSet<Id>,
    bare_reads: BTreeSet<Id>,
    rebinds: BTreeSet<Id>,
    calls: Vec<Call>,
    local_assignments: Vec<(Id, ValueExpr)>,
    declared_locals: BTreeSet<Id>,
    returns: Vec<ValueExpr>,
    local_functions: BTreeSet<Id>,
    literal_captures: BTreeSet<Id>,
    literal_exposures: BTreeSet<Id>,
    escaped_bindings: BTreeSet<Id>,
    namespace_escape_values: Vec<ValueExpr>,
    member_writes: Vec<(Id, String)>,
    member_value_writes: Vec<(Id, Option<String>, ValueExpr)>,
    callback_result_writes: Vec<CallbackResultWrite>,
    dynamic_property_effect: bool,
    dynamic_property_receivers: Vec<ValueExpr>,
    implicit_capture: bool,
    opaque: bool,
    generator: bool,
    may_fall_through: bool,
}

#[derive(Debug, Clone)]
struct LocalConstructor {
    key: Option<u32>,
    /// No constructor body or instance fields run during direct allocation.
    inert_allocation: bool,
    /// Implicit constructor whose instance fields are checked to initialize
    /// without user code. Unlike inert allocation, the fields still run.
    checked_default_fields: bool,
    /// Own data fields written by the checked constructor body.
    own_fields: BTreeSet<String>,
    /// Parameter property reads must come from fresh plain data objects.
    property_reads: BTreeSet<(usize, String)>,
    /// Unqualified reads must resolve to local bindings, not host getters.
    binding_reads: BTreeSet<Id>,
}

fn safe_constructor_value(
    expression: &Expr,
    params: &[Option<Id>],
    property_reads: &mut BTreeSet<(usize, String)>,
    binding_reads: &mut BTreeSet<Id>,
) -> bool {
    match expression.unwrap_parens() {
        Expr::Ident(id) => {
            if !params
                .iter()
                .any(|param| param.as_ref() == Some(&id.to_id()))
            {
                binding_reads.insert(id.to_id());
            }
            true
        }
        Expr::Lit(_) => true,
        Expr::Unary(unary) if unary.op == UnaryOp::Bang => {
            safe_constructor_value(&unary.arg, params, property_reads, binding_reads)
        }
        Expr::Member(member) if matches!(&member.prop, MemberProp::Ident(_)) => {
            let Expr::Ident(id) = member.obj.unwrap_parens() else {
                return false;
            };
            let Some(index) = params
                .iter()
                .position(|param| param.as_ref() == Some(&id.to_id()))
            else {
                return false;
            };
            let MemberProp::Ident(property) = &member.prop else {
                unreachable!()
            };
            property_reads.insert((index, property.sym.to_string()));
            true
        }
        Expr::Bin(bin) if matches!(bin.op, BinaryOp::NullishCoalescing | BinaryOp::EqEqEq) => {
            safe_constructor_value(&bin.left, params, property_reads, binding_reads)
                && safe_constructor_value(&bin.right, params, property_reads, binding_reads)
        }
        _ => false,
    }
}

fn safe_constructor_assignment(
    expression: &Expr,
    params: &[Option<Id>],
    property_reads: &mut BTreeSet<(usize, String)>,
    binding_reads: &mut BTreeSet<Id>,
    written_properties: &mut BTreeSet<String>,
) -> bool {
    match expression.unwrap_parens() {
        Expr::Seq(seq) => seq.exprs.iter().all(|expr| {
            safe_constructor_assignment(
                expr,
                params,
                property_reads,
                binding_reads,
                written_properties,
            )
        }),
        Expr::Assign(assign) if assign.op == AssignOp::Assign => {
            let AssignTarget::Simple(SimpleAssignTarget::Member(target)) = &assign.left else {
                return false;
            };
            let MemberProp::Ident(property) = &target.prop else {
                return false;
            };
            if !matches!(target.obj.unwrap_parens(), Expr::This(_))
                || property.sym.as_ref() == "__proto__"
                || !safe_constructor_value(&assign.right, params, property_reads, binding_reads)
            {
                return false;
            }
            written_properties.insert(property.sym.to_string());
            true
        }
        _ => false,
    }
}

fn local_constructor(
    class: &Class,
    construction_proof: &LocalClassConstructionProof,
) -> Option<LocalConstructor> {
    if class.super_class.is_some() || !class.decorators.is_empty() {
        return None;
    }
    let checked_default_fields = construction_proof.contains_class(class.span);
    let mut constructor = None;
    let mut has_instance_fields = false;
    for member in &class.body {
        match member {
            ClassMember::Constructor(ctor) if constructor.is_none() => constructor = Some(ctor),
            ClassMember::Method(method)
                if method.kind != MethodKind::Setter
                    && !matches!(method.key, PropName::Computed(_))
                    && method.function.decorators.is_empty() => {}
            ClassMember::PrivateMethod(method) if method.function.decorators.is_empty() => {}
            ClassMember::ClassProp(prop)
                if !prop.is_static
                    && prop.decorators.is_empty()
                    && !matches!(prop.key, PropName::Computed(_))
                    && (checked_default_fields
                        || prop.value.as_deref().is_none_or(|value| {
                            matches!(value.unwrap_parens(), Expr::Fn(_) | Expr::Arrow(_))
                        })) =>
            {
                has_instance_fields = true
            }
            ClassMember::PrivateProp(prop)
                if !prop.is_static
                    && prop.decorators.is_empty()
                    && (checked_default_fields
                        || prop.value.as_deref().is_none_or(|value| {
                            matches!(value.unwrap_parens(), Expr::Fn(_) | Expr::Arrow(_))
                        })) =>
            {
                has_instance_fields = true
            }
            ClassMember::Empty(_) => {}
            _ => return None,
        }
    }
    let Some(ctor) = constructor else {
        return Some(LocalConstructor {
            key: None,
            inert_allocation: !has_instance_fields,
            checked_default_fields,
            own_fields: BTreeSet::new(),
            property_reads: BTreeSet::new(),
            binding_reads: BTreeSet::new(),
        });
    };
    let params: Vec<_> = ctor
        .params
        .iter()
        .map(|param| match param {
            ParamOrTsParamProp::Param(param) if param.decorators.is_empty() => match &param.pat {
                Pat::Ident(binding) => Some(binding.id.to_id()),
                _ => None,
            },
            _ => None,
        })
        .collect();
    if params.iter().any(Option::is_none) {
        return None;
    }
    let mut property_reads = BTreeSet::new();
    let mut binding_reads = BTreeSet::new();
    let mut written_properties = BTreeSet::new();
    for statement in &ctor.body.as_ref()?.stmts {
        let Stmt::Expr(expr) = statement else {
            return None;
        };
        if !safe_constructor_assignment(
            &expr.expr,
            &params,
            &mut property_reads,
            &mut binding_reads,
            &mut written_properties,
        ) {
            return None;
        }
    }
    // A getter with the same name on this class's prototype makes assignment
    // to the fresh instance throw. All other accepted writes create ordinary
    // own data properties under the unmodified built-in prototype premise.
    if class.body.iter().any(|member| {
        matches!(member, ClassMember::Method(method)
            if !method.is_static && method.kind == MethodKind::Getter
                && static_property_name(&method.key)
                    .is_some_and(|name| written_properties.contains(&name)))
    }) {
        return None;
    }
    Some(LocalConstructor {
        key: Some(ctor.span.lo.0),
        inert_allocation: false,
        checked_default_fields: false,
        own_fields: written_properties,
        property_reads,
        binding_reads,
    })
}

impl Default for Callable {
    fn default() -> Self {
        Self {
            owner: SourceUnitOrdinal(0),
            created_after_parent_await: false,
            params: Vec::new(),
            reads: BTreeSet::new(),
            bare_reads: BTreeSet::new(),
            rebinds: BTreeSet::new(),
            calls: Vec::new(),
            local_assignments: Vec::new(),
            declared_locals: BTreeSet::new(),
            returns: Vec::new(),
            local_functions: BTreeSet::new(),
            literal_captures: BTreeSet::new(),
            literal_exposures: BTreeSet::new(),
            escaped_bindings: BTreeSet::new(),
            namespace_escape_values: Vec::new(),
            member_writes: Vec::new(),
            member_value_writes: Vec::new(),
            callback_result_writes: Vec::new(),
            dynamic_property_effect: false,
            dynamic_property_receivers: Vec::new(),
            implicit_capture: false,
            opaque: false,
            generator: false,
            may_fall_through: true,
        }
    }
}

fn sole_return_expression(body: &FunctionBody) -> Option<&Expr> {
    match body.stmts.as_slice() {
        [Stmt::Return(ret)] => ret.arg.as_deref(),
        _ => None,
    }
}

fn returned_function_span(expr: &Expr) -> Option<Span> {
    match expr.unwrap_parens() {
        Expr::Arrow(arrow) if !arrow.is_async && arrow.params.is_empty() => Some(arrow.span),
        Expr::Fn(function)
            if !function.function.is_async
                && !function.function.is_generator
                && function.function.params.is_empty()
                && function.function.decorators.is_empty() =>
        {
            Some(function.function.span)
        }
        _ => None,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum IntrinsicRoot {
    GlobalObject,
    Error,
    Object,
    Array,
    Math,
    RegExp,
    Symbol,
    Promise,
    Map,
    Set,
    WeakMap,
    WeakSet,
}

type IntrinsicRoots = BTreeSet<IntrinsicRoot>;

fn intrinsic_roots(
    value: &ValueExpr,
    roots: &BTreeMap<Id, IntrinsicRoots>,
    globals: &BTreeSet<&str>,
) -> IntrinsicRoots {
    match value {
        ValueExpr::Binding(id) if id.0.as_ref() == "Error" => {
            BTreeSet::from([IntrinsicRoot::Error])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Object" => {
            BTreeSet::from([IntrinsicRoot::Object])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Array" => {
            BTreeSet::from([IntrinsicRoot::Array])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Math" => BTreeSet::from([IntrinsicRoot::Math]),
        ValueExpr::Binding(id) if id.0.as_ref() == "RegExp" => {
            BTreeSet::from([IntrinsicRoot::RegExp])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Symbol" => {
            BTreeSet::from([IntrinsicRoot::Symbol])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Promise" => {
            BTreeSet::from([IntrinsicRoot::Promise])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Map" => BTreeSet::from([IntrinsicRoot::Map]),
        ValueExpr::Binding(id) if id.0.as_ref() == "Set" => BTreeSet::from([IntrinsicRoot::Set]),
        ValueExpr::Binding(id) if id.0.as_ref() == "WeakMap" => {
            BTreeSet::from([IntrinsicRoot::WeakMap])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "WeakSet" => {
            BTreeSet::from([IntrinsicRoot::WeakSet])
        }
        ValueExpr::Binding(id) if globals.contains(id.0.as_ref()) => {
            BTreeSet::from([IntrinsicRoot::GlobalObject])
        }
        ValueExpr::Binding(id) => roots.get(id).cloned().unwrap_or_default(),
        ValueExpr::Alternatives(options) => {
            options
                .iter()
                .fold(IntrinsicRoots::new(), |mut combined, option| {
                    combined.extend(intrinsic_roots(option, roots, globals));
                    combined
                })
        }
        ValueExpr::Member(base, property, _) => {
            let base_roots = intrinsic_roots(base, roots, globals);
            let mut out = IntrinsicRoots::new();
            for root in base_roots {
                match root {
                    IntrinsicRoot::GlobalObject => match property.as_deref() {
                        Some("Error") => {
                            out.insert(IntrinsicRoot::Error);
                        }
                        Some("Object") => {
                            out.insert(IntrinsicRoot::Object);
                        }
                        Some("Array") => {
                            out.insert(IntrinsicRoot::Array);
                        }
                        Some("Math") => {
                            out.insert(IntrinsicRoot::Math);
                        }
                        Some("RegExp") => {
                            out.insert(IntrinsicRoot::RegExp);
                        }
                        Some("Symbol") => {
                            out.insert(IntrinsicRoot::Symbol);
                        }
                        Some("Promise") => {
                            out.insert(IntrinsicRoot::Promise);
                        }
                        Some("Map") => {
                            out.insert(IntrinsicRoot::Map);
                        }
                        Some("Set") => {
                            out.insert(IntrinsicRoot::Set);
                        }
                        Some("WeakMap") => {
                            out.insert(IntrinsicRoot::WeakMap);
                        }
                        Some("WeakSet") => {
                            out.insert(IntrinsicRoot::WeakSet);
                        }
                        Some("globalThis" | "window" | "self" | "frames" | "top") | None => {
                            out.insert(IntrinsicRoot::GlobalObject);
                        }
                        _ => {}
                    },
                    // A member of a builtin constructor remains rooted in that
                    // constructor: Array.prototype.map, Array[Symbol.species],
                    // and Object.fromEntries are mutations of their respective
                    // intrinsic families.
                    IntrinsicRoot::Error
                    | IntrinsicRoot::Object
                    | IntrinsicRoot::Array
                    | IntrinsicRoot::Math
                    | IntrinsicRoot::RegExp
                    | IntrinsicRoot::Symbol
                    | IntrinsicRoot::Promise
                    | IntrinsicRoot::Map
                    | IntrinsicRoot::Set
                    | IntrinsicRoot::WeakMap
                    | IntrinsicRoot::WeakSet => {
                        out.insert(root);
                    }
                }
            }
            out
        }
        _ => IntrinsicRoots::new(),
    }
}

fn intrinsic_write_target(member: &MemberExpr) -> ValueExpr {
    ValueExpr::Member(
        Box::new(ValueExpr::from_expr(&member.obj)),
        static_prop(&member.prop),
        matches!(member.prop, MemberProp::Ident(_)),
    )
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct FreshErrorWriteSite {
    owner: SourceUnitOrdinal,
    callable: Option<u32>,
    receiver: Id,
    property: String,
}

struct FreshErrorWriteClaim {
    site: FreshErrorWriteSite,
    callee: Id,
    local_read: Option<Id>,
}

#[derive(Clone)]
struct DeferredGlobalPublication {
    root: BuiltinGlobalRoot,
    receiver_id: Id,
    property: String,
    value: ValueExpr,
}

fn builtin_global_root(ident: &Ident) -> Option<BuiltinGlobalRoot> {
    match ident.sym.as_ref() {
        "globalThis" => Some(BuiltinGlobalRoot::GlobalThis),
        "window" => Some(BuiltinGlobalRoot::Window),
        _ => None,
    }
}

fn is_global_receiver(expression: &Expr, names: &BTreeSet<&str>) -> bool {
    match expression.unwrap_parens() {
        Expr::Ident(ident) => names.contains(ident.sym.as_ref()),
        Expr::Member(member) => {
            matches!(
                static_prop(&member.prop).as_deref(),
                Some("globalThis" | "window" | "self" | "frames" | "top")
            ) && is_global_receiver(&member.obj, names)
        }
        _ => false,
    }
}

fn exposes_global_receiver(value: &ValueExpr, names: &BTreeSet<&str>) -> bool {
    match value {
        ValueExpr::Binding(id) => names.contains(id.0.as_ref()),
        ValueExpr::Member(receiver, property, _) => {
            // A direct global property read is not publication of the global
            // receiver. Projection through any other value can carry one:
            // `({root: globalThis}).root` must stay an escape.
            if matches!(receiver.as_ref(), ValueExpr::Binding(id) if names.contains(id.0.as_ref()))
            {
                property.as_deref().is_some_and(|property| {
                    matches!(
                        property,
                        "globalThis" | "window" | "self" | "frames" | "top"
                    )
                })
            } else {
                exposes_global_receiver(receiver, names)
            }
        }
        ValueExpr::Alternatives(values) => values
            .iter()
            .any(|value| exposes_global_receiver(value, names)),
        ValueExpr::ObjectLiteral(_, properties) => properties
            .iter()
            .any(|(_, value)| exposes_global_receiver(value, names)),
        ValueExpr::Call(_, args, _) | ValueExpr::New(_, args, _) => args
            .iter()
            .any(|value| exposes_global_receiver(value, names)),
        // Operators can inspect or coerce their operands, but their result
        // cannot be the global object. Calls arising from those operands are
        // checked separately below.
        ValueExpr::Primitive(_) => false,
        _ => false,
    }
}

fn call_mutates_global_slot(call: &Call, names: &BTreeSet<&str>) -> bool {
    matches!(&call.callee, ValueExpr::Member(receiver, Some(property), _)
        if matches!(property.as_str(), "__defineSetter__" | "__defineGetter__")
            && exposes_global_receiver(receiver, names))
}

fn call_exposes_global_receiver(
    call: &Call,
    names: &BTreeSet<&str>,
    certified_root_ids: &BTreeSet<Id>,
) -> bool {
    if call_mutates_global_slot(call, names) {
        return true;
    }
    // The retained `in` protocol call models possible proxy/host effects;
    // it does not pass its RHS to a user function. A literal key against a
    // direct, unshadowed builtin global root cannot publish that root. Keep
    // the protocol call itself for ordinary effect and reentry analysis.
    if call.kind == CallKind::InProtocol
        && matches!(call.args.as_slice(),
            [ValueExpr::Binding(receiver), ValueExpr::NonCallable]
                if names.contains(receiver.0.as_ref())
                    && certified_root_ids.contains(receiver))
    {
        return false;
    }
    call.args
        .iter()
        .any(|value| exposes_global_receiver(value, names))
}

fn uses_any_binding(value: &ValueExpr, ids: &BTreeSet<Id>) -> bool {
    let mut bindings = BTreeSet::new();
    value.bindings(&mut bindings);
    !bindings.is_disjoint(ids)
}

fn same_property_or_fresh(value: &ValueExpr, alias: &Id, property: &str) -> bool {
    match value {
        ValueExpr::Member(receiver, Some(read), _)
            if read == property
                && matches!(receiver.as_ref(), ValueExpr::Binding(id) if id == alias) =>
        {
            true
        }
        ValueExpr::ObjectLiteral(_, properties) => properties.is_empty(),
        ValueExpr::Alternatives(options) => options
            .iter()
            .all(|value| same_property_or_fresh(value, alias, property)),
        ValueExpr::NonCallable | ValueExpr::Undefined | ValueExpr::Primitive(_) => true,
        _ => false,
    }
}

fn confined_prepublication_alias(
    index: &ExecutionIndex,
    key: u32,
    alias: &Id,
    first_publication: SourceUnitOrdinal,
    protected: &BTreeSet<String>,
) -> bool {
    let Some(callable) = index.callables.get(&key) else {
        return false;
    };
    let owner = callable.owner;
    if owner >= first_publication
        || index.discarded_sync_iifes[owner.0] != Some(key)
        || callable.opaque
        || !callable.params.is_empty()
        || !callable.returns.is_empty()
        || callable.reads.iter().any(|id| id.0 == "arguments")
        || callable.bare_reads.contains(alias)
        || callable.rebinds.contains(alias)
        || index.callables.iter().any(|(other_key, other)| {
            other.owner == owner && *other_key != key
                || other.owner != owner && other.reads.contains(alias)
        })
        || callable
            .local_assignments
            .iter()
            .filter(|(id, _)| id == alias)
            .count()
            != 1
    {
        return false;
    }
    let forbidden = |property: &str| {
        protected.contains(property)
            || matches!(
                property,
                "__proto__"
                    | "prototype"
                    | "constructor"
                    | "globalThis"
                    | "window"
                    | "self"
                    | "frames"
                    | "top"
                    | "__defineSetter__"
                    | "__defineGetter__"
            )
    };
    if index.callable_member_reads.get(&key).is_some_and(|reads| {
        reads.iter().any(|(receiver, property)| {
            matches!(receiver, ValueExpr::Binding(id) if id == alias)
                && property.as_deref().is_none_or(&forbidden)
        })
    }) {
        return false;
    }
    let mut derived = BTreeSet::from([alias.clone()]);
    loop {
        let before = derived.len();
        for (id, value) in &callable.local_assignments {
            if uses_any_binding(value, &derived) {
                derived.insert(id.clone());
            }
        }
        if derived.len() == before {
            break;
        }
    }
    for call in &callable.calls {
        if call.kind == CallKind::Call && uses_any_binding(&call.callee, &derived)
            || matches!(call.kind, CallKind::Call | CallKind::New)
                && call.args.iter().any(|arg| uses_any_binding(arg, &derived))
        {
            return false;
        }
        if call.kind == CallKind::New
            && uses_any_binding(&call.callee, &derived)
            && !matches!(&call.callee,
                ValueExpr::Member(receiver, Some(property), _)
                    if property == "Error"
                        && matches!(receiver.as_ref(), ValueExpr::Binding(id) if id == alias)
                        && call.args.is_empty())
        {
            return false;
        }
    }
    callable.callback_result_writes.iter().all(|write| {
        if let ValueExpr::Binding(id) = &write.receiver
            && id == alias
        {
            let Some(property) = write.property.as_deref() else {
                return false;
            };
            !forbidden(property) && same_property_or_fresh(&write.value, alias, property)
        } else if uses_any_binding(&write.receiver, &derived) {
            // A derived receiver may be an alias, but this entire invocation
            // finishes before publication. Only primitive writes can survive.
            matches!(
                write.value,
                ValueExpr::NonCallable | ValueExpr::Undefined | ValueExpr::Primitive(_)
            )
        } else {
            !uses_any_binding(&write.value, &derived)
        }
    })
}

fn deferred_global_assignment(expression: &Expr) -> Option<DeferredGlobalPublication> {
    let Expr::Assign(assignment) = expression.unwrap_parens() else {
        return None;
    };
    let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assignment.left else {
        return None;
    };
    let (Expr::Ident(receiver), MemberProp::Ident(property)) =
        (member.obj.unwrap_parens(), &member.prop)
    else {
        return None;
    };
    if assignment.op != AssignOp::Assign
        || !matches!(
            assignment.right.unwrap_parens(),
            Expr::Arrow(_) | Expr::Fn(_) | Expr::Ident(_)
        )
    {
        return None;
    }
    Some(DeferredGlobalPublication {
        root: builtin_global_root(receiver)?,
        receiver_id: receiver.to_id(),
        property: property.sym.to_string(),
        value: ValueExpr::from_expr(&assignment.right),
    })
}

fn typeof_window_guard(expression: &Expr) -> bool {
    let Expr::Bin(guard) = expression.unwrap_parens() else {
        return false;
    };
    let Expr::Unary(left) = guard.left.unwrap_parens() else {
        return false;
    };
    if left.op != UnaryOp::TypeOf
        || !matches!(left.arg.unwrap_parens(), Expr::Ident(id) if id.sym == "window")
    {
        return false;
    }
    matches!((guard.op, guard.right.unwrap_parens()),
        (BinaryOp::Lt, Expr::Lit(Lit::Str(value))) if value.value == "u"
    ) || matches!((guard.op, guard.right.unwrap_parens()),
        (BinaryOp::NotEq | BinaryOp::NotEqEq, Expr::Lit(Lit::Str(value))) if value.value == "undefined"
    )
}

fn deferred_global_publication(item: &ModuleItem) -> Option<DeferredGlobalPublication> {
    let ModuleItem::Stmt(Stmt::Expr(statement)) = item else {
        return None;
    };
    let expression = statement.expr.unwrap_parens();
    if let Expr::Bin(binary) = expression
        && binary.op == BinaryOp::LogicalAnd
        && typeof_window_guard(&binary.left)
    {
        return deferred_global_assignment(&binary.right)
            .filter(|publication| publication.root == BuiltinGlobalRoot::Window);
    }
    deferred_global_assignment(expression)
        .filter(|publication| publication.root == BuiltinGlobalRoot::GlobalThis)
}

fn discarded_synchronous_iife(item: &ModuleItem) -> Option<u32> {
    let ModuleItem::Stmt(Stmt::Expr(statement)) = item else {
        return None;
    };
    let value = match statement.expr.unwrap_parens() {
        Expr::Unary(unary) if matches!(unary.op, UnaryOp::Bang | UnaryOp::Void) => {
            unary.arg.unwrap_parens()
        }
        value => value,
    };
    let Expr::Call(call) = value else {
        return None;
    };
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Fn(function) = callee.unwrap_parens() else {
        return None;
    };
    (function.ident.is_none()
        && call.args.is_empty()
        && !function.function.is_async
        && !function.function.is_generator
        && function.function.decorators.is_empty()
        && function.function.params.is_empty())
    .then_some(function.function.span.lo.0)
}

struct DestructuringMemberWrite {
    receiver: ValueExpr,
    /// The whole source (including defaults) over-approximates the target value.
    source: ValueExpr,
}

/// A sufficient source-privacy proof, independent of execution/value-flow facts.
/// Every configured root belongs to one potentially aliasing namespace family.
#[derive(Default)]
struct NamespacePrivacy {
    members: BTreeMap<Id, BTreeSet<String>>,
    import_sites: BTreeSet<u32>,
    allowed_read: Option<u32>,
    optional_members: BTreeSet<u32>,
    target_depth: usize,
    invalid: bool,
}

impl NamespacePrivacy {
    fn record_ident(&mut self, ident: &Ident) {
        if self.members.contains_key(&ident.to_id())
            && !self.import_sites.contains(&ident.span.lo.0)
            && self.allowed_read != Some(ident.span.lo.0)
        {
            self.invalid = true;
        }
    }

    fn read_receiver(&self, member: &MemberExpr) -> Option<u32> {
        if self.target_depth == 0
            && !self.optional_members.contains(&member.span.lo.0)
            && let Expr::Ident(receiver) = member.obj.unwrap_parens()
            && let MemberProp::Ident(property) = &member.prop
            && self
                .members
                .get(&receiver.to_id())
                .is_some_and(|members| members.contains(property.sym.as_ref()))
        {
            Some(receiver.span.lo.0)
        } else {
            None
        }
    }
}

#[derive(Default)]
struct GlobalMembershipBindings(BTreeSet<Id>);

impl Visit for GlobalMembershipBindings {
    fn visit_binding_ident(&mut self, binding: &BindingIdent) {
        self.0.insert(binding.id.to_id());
    }

    fn visit_fn_decl(&mut self, declaration: &FnDecl) {
        self.0.insert(declaration.ident.to_id());
        declaration.visit_children_with(self);
    }

    fn visit_class_decl(&mut self, declaration: &ClassDecl) {
        self.0.insert(declaration.ident.to_id());
        declaration.visit_children_with(self);
    }

    fn visit_fn_expr(&mut self, expression: &FnExpr) {
        if let Some(ident) = &expression.ident {
            self.0.insert(ident.to_id());
        }
        expression.visit_children_with(self);
    }

    fn visit_class_expr(&mut self, expression: &ClassExpr) {
        if let Some(ident) = &expression.ident {
            self.0.insert(ident.to_id());
        }
        expression.visit_children_with(self);
    }
}

#[derive(Clone)]
struct GlobalMembershipProbe {
    root: BuiltinGlobalRoot,
    receiver: Id,
    property: String,
    owner: SourceUnitOrdinal,
}

fn global_receiver_path(value: &ValueExpr, names: &BTreeSet<&str>) -> bool {
    match value {
        ValueExpr::Binding(id) => names.contains(id.0.as_ref()),
        ValueExpr::Member(receiver, _, _) => global_receiver_path(receiver, names),
        _ => false,
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum GlobalMutationPath {
    Unrelated,
    Descendant,
    Identity,
    Prototype,
}

// This classifies syntactic evidence only. A descendant is not proven
// disjoint: its complete alias integrity remains the author's premise.
fn global_mutation_path(
    value: &ValueExpr,
    names: &BTreeSet<&str>,
    bindings: &BTreeSet<Id>,
) -> GlobalMutationPath {
    use GlobalMutationPath::*;
    match value {
        ValueExpr::Binding(id) if !bindings.contains(id) => match id.0.as_ref() {
            name if names.contains(name) => Identity,
            "document" => Descendant,
            "Window" | "EventTarget" | "Object" | "Document" | "HTMLDocument" => Prototype,
            _ => Unrelated,
        },
        ValueExpr::Member(receiver, property, _) => {
            match global_mutation_path(receiver, names, bindings) {
                Unrelated => Unrelated,
                Prototype => Prototype,
                Identity | Descendant => match property.as_deref() {
                    Some(
                        "__proto__" | "prototype" | "constructor" | "Window" | "EventTarget"
                        | "Object" | "Document" | "HTMLDocument",
                    ) => Prototype,
                    Some(
                        "globalThis" | "global" | "window" | "self" | "frames" | "top" | "parent"
                        | "defaultView",
                    ) => Identity,
                    _ => Descendant,
                },
            }
        }
        _ => Unrelated,
    }
}

fn stored_global_alias(
    value: &ValueExpr,
    names: &BTreeSet<&str>,
    bindings: &BTreeSet<Id>,
    include_prototypes: bool,
) -> bool {
    match global_mutation_path(value, names, bindings) {
        GlobalMutationPath::Identity => true,
        GlobalMutationPath::Prototype => include_prototypes,
        GlobalMutationPath::Descendant => false,
        GlobalMutationPath::Unrelated => match value {
            ValueExpr::Alternatives(values) => values
                .iter()
                .any(|value| stored_global_alias(value, names, bindings, include_prototypes)),
            ValueExpr::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| stored_global_alias(value, names, bindings, include_prototypes)),
            ValueExpr::Member(receiver, _, _) => {
                stored_global_alias(receiver, names, bindings, include_prototypes)
            }
            ValueExpr::Call(_, args, _) | ValueExpr::New(_, args, _) => args
                .iter()
                .any(|value| stored_global_alias(value, names, bindings, include_prototypes)),
            _ => false,
        },
    }
}

struct ExecutionIndex {
    owner: SourceUnitOrdinal,
    current: Option<u32>,
    allowed_local_member_receiver: Option<u32>,
    past_await: bool,
    may_skip_await_depth: u32,
    conditional_depth: u32,
    literal_depth: u32,
    factory_member_value_depth: u32,
    factory_callback_depth: u32,
    callables: BTreeMap<u32, Callable>,
    local_constructors: BTreeMap<u32, LocalConstructor>,
    local_class_construction_proof: LocalClassConstructionProof,
    class_values: BTreeMap<u32, ClassValueFacts>,
    class_owners: BTreeMap<u32, SourceUnitOrdinal>,
    class_bindings: BTreeMap<u32, Id>,
    class_ids: BTreeSet<Id>,
    other_class_uses: BTreeSet<Id>,
    safe_class_target: Option<Id>,
    plain_data_objects: BTreeSet<u32>,
    null_proto_data_objects: BTreeSet<u32>,
    exact_object_spread_copies: BTreeSet<u32>,
    map_entry_arrays: BTreeSet<u32>,
    dense_array_literals: BTreeMap<u32, usize>,
    array_elements: BTreeMap<u32, Vec<Option<ArrayElement>>>,
    array_iterations: BTreeMap<CallSite, ArrayIteration>,
    async_callables: BTreeSet<u32>,
    callable_member_reads: BTreeMap<u32, Vec<(ValueExpr, Option<String>)>>,
    direct_call_members: BTreeSet<CallSite>,
    member_write_sites: BTreeSet<CallSite>,
    primitive_data_properties: BTreeSet<(u32, String)>,
    from_entries_map_calls: BTreeMap<CallSite, FromEntriesMappedProof>,
    mapped_array_map_calls: BTreeMap<CallSite, FromEntriesMappedProof>,
    plain_array_bindings: BTreeSet<String>,
    mutated_intrinsic_roots: IntrinsicRoots,
    reassigned_map: bool,
    namespace_privacy: NamespacePrivacy,
    fresh_error_claims: Vec<FreshErrorWriteClaim>,
    intrinsic_mutation_receivers: Vec<ValueExpr>,
    destructuring_source: Option<ValueExpr>,
    destructuring_member_writes: Vec<DestructuringMemberWrite>,
    setter_owners: BTreeMap<Option<String>, BTreeSet<SourceUnitOrdinal>>,
    hoisted_nested_functions: BTreeSet<u32>,
    owner_callable_prefix_reads: Vec<Option<BTreeSet<Id>>>,
    unindexed_accessor_owners: BTreeSet<SourceUnitOrdinal>,
    top_calls: Vec<Vec<Call>>,
    /// Ordinary CallExpr syntax at lexical top level; optional calls use a
    /// conservative Call record too, but do not satisfy this witness shape.
    ordinary_top_call_sites: BTreeSet<CallSite>,
    top_property_writes: Vec<Vec<(Option<Id>, Option<String>)>>,
    fresh_instance_field_writes: BTreeSet<SourceUnitOrdinal>,
    top_dynamic_property_receivers: Vec<Vec<ValueExpr>>,
    top_assignments: Vec<Vec<(Id, ValueExpr)>>,
    top_definite_initializers: Vec<BTreeSet<Id>>,
    const_call_owners: BTreeSet<SourceUnitOrdinal>,
    top_member_value_writes: Vec<Vec<(Id, Option<String>, ValueExpr)>>,
    top_callback_result_writes: Vec<Vec<CallbackResultWrite>>,
    top_exported_values: Vec<Vec<ValueExpr>>,
    top_metadata_decorations: Vec<Option<MetadataDecorateCall>>,
    deferred_global_candidates: Vec<Option<DeferredGlobalPublication>>,
    discarded_sync_iifes: Vec<Option<u32>>,
    deferred_global_certificates: BTreeSet<SourceUnitOrdinal>,
    deferred_global_requested: bool,
    global_guards_requested: bool,
    global_membership_candidates: BTreeMap<CallSite, GlobalMembershipProbe>,
    non_reentrant_global_membership: BTreeSet<CallSite>,
    global_slot_mentions: BTreeMap<String, usize>,
    dynamic_global_member: bool,
    global_alias_written: bool,
    imported_bindings: BTreeSet<Id>,
    hoisted_bindings: BTreeMap<Id, u32>,
    exported_hoisted_functions: BTreeSet<u32>,
    global_object_names: BTreeSet<&'static str>,
    hoisted_function_owners: BTreeSet<SourceUnitOrdinal>,
    exported_declaration_owners: BTreeSet<SourceUnitOrdinal>,
    exported_bindings: BTreeSet<Id>,
    linkage_owners: BTreeSet<SourceUnitOrdinal>,
    dynamic_scope_owners: BTreeSet<SourceUnitOrdinal>,
    opaque_top: BTreeSet<SourceUnitOrdinal>,
    no_sync_callback_members: BTreeMap<String, BTreeSet<String>>,
    callback_factory_imports: BTreeSet<Id>,
    callback_factory_summaries: BTreeMap<Id, crate::analysis_inputs::CallableSummary>,
    inline_callback_factory_imports: BTreeSet<Id>,
    callback_factory_members: BTreeMap<Id, BTreeSet<String>>,
    /// Union used only for namespace privacy/mutation guards. Admission of the
    /// old and stronger contracts remains separate.
    audited_factory_members: BTreeMap<Id, BTreeSet<String>>,
    rejected_namespace_member_writes: BTreeMap<(Id, String), CallbackFactoryWriteWitness>,
    rejected_namespace_dynamic_property_writes: BTreeMap<(Id, String), CallbackFactoryWriteWitness>,
    rejected_namespace_call_arguments: BTreeMap<Id, CallbackFactoryCallArgumentWitness>,
    retained_callback_factory_imports: BTreeMap<Id, Vec<spec::RetainedCallbackSignature>>,
    retained_callback_constructor_imports: BTreeMap<Id, Vec<spec::RetainedCallbackSignature>>,
    no_reentry_callback_imports: BTreeMap<Id, Vec<spec::RetainedCallbackSignature>>,
    retained_callback_factory_members:
        BTreeMap<Id, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
    retained_callback_calls: BTreeMap<CallSite, IndexedRetainedCallbackCall>,
    retained_constructor_calls: BTreeMap<CallSite, RetainedCallbackCall>,
    no_reentry_callback_calls: BTreeMap<CallSite, RetainedCallbackCall>,
    no_reentry_exports: BTreeSet<Id>,
    host_async_local_storage_imports: BTreeSet<Id>,
    host_promise_resolve: bool,
    no_reentry_members: BTreeMap<Id, BTreeSet<String>>,
    no_reentry_record_members: BTreeMap<Id, BTreeSet<String>>,
    returned_factory_candidates: BTreeMap<u32, Span>,
}

impl Default for ExecutionIndex {
    fn default() -> Self {
        Self {
            owner: SourceUnitOrdinal(0),
            current: None,
            allowed_local_member_receiver: None,
            past_await: false,
            may_skip_await_depth: 0,
            conditional_depth: 0,
            literal_depth: 0,
            factory_member_value_depth: 0,
            factory_callback_depth: 0,
            callables: BTreeMap::new(),
            local_constructors: BTreeMap::new(),
            local_class_construction_proof: LocalClassConstructionProof::default(),
            class_values: BTreeMap::new(),
            class_owners: BTreeMap::new(),
            class_bindings: BTreeMap::new(),
            class_ids: BTreeSet::new(),
            other_class_uses: BTreeSet::new(),
            safe_class_target: None,
            plain_data_objects: BTreeSet::new(),
            null_proto_data_objects: BTreeSet::new(),
            exact_object_spread_copies: BTreeSet::new(),
            map_entry_arrays: BTreeSet::new(),
            dense_array_literals: BTreeMap::new(),
            array_elements: BTreeMap::new(),
            array_iterations: BTreeMap::new(),
            async_callables: BTreeSet::new(),
            callable_member_reads: BTreeMap::new(),
            direct_call_members: BTreeSet::new(),
            member_write_sites: BTreeSet::new(),
            primitive_data_properties: BTreeSet::new(),
            from_entries_map_calls: BTreeMap::new(),
            mapped_array_map_calls: BTreeMap::new(),
            plain_array_bindings: BTreeSet::new(),
            mutated_intrinsic_roots: IntrinsicRoots::new(),
            reassigned_map: false,
            namespace_privacy: NamespacePrivacy::default(),
            fresh_error_claims: Vec::new(),
            intrinsic_mutation_receivers: Vec::new(),
            destructuring_source: None,
            destructuring_member_writes: Vec::new(),
            setter_owners: BTreeMap::new(),
            hoisted_nested_functions: BTreeSet::new(),
            owner_callable_prefix_reads: Vec::new(),
            unindexed_accessor_owners: BTreeSet::new(),
            top_calls: Vec::new(),
            ordinary_top_call_sites: BTreeSet::new(),
            top_property_writes: Vec::new(),
            fresh_instance_field_writes: BTreeSet::new(),
            top_dynamic_property_receivers: Vec::new(),
            top_assignments: Vec::new(),
            top_definite_initializers: Vec::new(),
            const_call_owners: BTreeSet::new(),
            top_member_value_writes: Vec::new(),
            top_callback_result_writes: Vec::new(),
            top_exported_values: Vec::new(),
            top_metadata_decorations: Vec::new(),
            deferred_global_candidates: Vec::new(),
            discarded_sync_iifes: Vec::new(),
            deferred_global_certificates: BTreeSet::new(),
            deferred_global_requested: false,
            global_guards_requested: false,
            global_membership_candidates: BTreeMap::new(),
            non_reentrant_global_membership: BTreeSet::new(),
            global_slot_mentions: BTreeMap::new(),
            dynamic_global_member: false,
            global_alias_written: false,
            imported_bindings: BTreeSet::new(),
            hoisted_bindings: BTreeMap::new(),
            exported_hoisted_functions: BTreeSet::new(),
            global_object_names: BTreeSet::new(),
            hoisted_function_owners: BTreeSet::new(),
            exported_declaration_owners: BTreeSet::new(),
            exported_bindings: BTreeSet::new(),
            linkage_owners: BTreeSet::new(),
            dynamic_scope_owners: BTreeSet::new(),
            opaque_top: BTreeSet::new(),
            no_sync_callback_members: BTreeMap::new(),
            callback_factory_imports: BTreeSet::new(),
            callback_factory_summaries: BTreeMap::new(),
            inline_callback_factory_imports: BTreeSet::new(),
            callback_factory_members: BTreeMap::new(),
            audited_factory_members: BTreeMap::new(),
            rejected_namespace_member_writes: BTreeMap::new(),
            rejected_namespace_dynamic_property_writes: BTreeMap::new(),
            rejected_namespace_call_arguments: BTreeMap::new(),
            retained_callback_factory_imports: BTreeMap::new(),
            retained_callback_constructor_imports: BTreeMap::new(),
            no_reentry_callback_imports: BTreeMap::new(),
            retained_callback_factory_members: BTreeMap::new(),
            retained_callback_calls: BTreeMap::new(),
            retained_constructor_calls: BTreeMap::new(),
            no_reentry_callback_calls: BTreeMap::new(),
            no_reentry_exports: BTreeSet::new(),
            host_async_local_storage_imports: BTreeSet::new(),
            host_promise_resolve: false,
            no_reentry_members: BTreeMap::new(),
            no_reentry_record_members: BTreeMap::new(),
            returned_factory_candidates: BTreeMap::new(),
        }
    }
}

impl ExecutionIndex {
    fn collect(
        body: &[TopLevelItemView<'_>],
        hints: &AnalysisInputs,
        flow: Option<&ChunkValueFlow>,
        plain_array_bindings: &BTreeSet<String>,
        construction_proof: &LocalClassConstructionProof,
    ) -> Self {
        let host_operations = hints
            .assumptions
            .host_no_reentry
            .as_ref()
            .map(|policy| match policy.runtime {
                HostNoReentryRuntime::BunStandalone1_3_10 => &policy.operations,
            });
        let global_guards_requested = !hints.assumptions.deferred_global_publications.is_empty()
            || !hints.assumptions.non_reentrant_global_membership.is_empty();
        let mut index = Self {
            global_guards_requested,
            local_class_construction_proof: construction_proof.clone(),
            top_calls: vec![Vec::new(); body.len()],
            owner_callable_prefix_reads: vec![None; body.len()],
            top_property_writes: vec![Vec::new(); body.len()],
            top_dynamic_property_receivers: vec![Vec::new(); body.len()],
            top_assignments: vec![Vec::new(); body.len()],
            top_definite_initializers: vec![BTreeSet::new(); body.len()],
            top_member_value_writes: vec![Vec::new(); body.len()],
            top_callback_result_writes: vec![Vec::new(); body.len()],
            top_exported_values: vec![Vec::new(); body.len()],
            top_metadata_decorations: body
                .iter()
                .map(|item| {
                    metadata_decorate_call(item.as_module_item(), &hints.assumptions.known_effects)
                })
                .collect(),
            deferred_global_candidates: vec![None; body.len()],
            discarded_sync_iifes: if !global_guards_requested {
                Vec::new()
            } else {
                vec![None; body.len()]
            },
            deferred_global_requested: !hints.assumptions.deferred_global_publications.is_empty(),
            global_object_names: unshadowed_global_object_aliases(body),
            plain_array_bindings: plain_array_bindings.clone(),
            no_sync_callback_members: hints.assumptions.no_sync_callback_members.clone(),
            host_promise_resolve: host_operations.is_some_and(|operations| {
                operations.contains(&HostNoReentryOperation::PromiseResolve)
            }),
            ..Self::default()
        };
        let mut fresh_steps = Vec::with_capacity(body.len());
        let mut max_source_position = 0u32;
        for item in body {
            max_source_position = max_source_position.max(item.as_module_item().span().hi.0);
            // Seed all candidate roots before visiting any item, including imports
            // textually after a use. Other imported aliases remain an author audit.
            if (!hints.assumptions.namespace_independent_members.is_empty()
                || !hints.assumptions.no_reentry_members.is_empty()
                || !hints.assumptions.no_reentry_record_members.is_empty())
                && let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item.as_module_item()
            {
                for specifier in &import.specifiers {
                    let local = specifier.local();
                    let name = local.sym.as_ref();
                    if hints
                        .assumptions
                        .callback_factory_members
                        .contains_key(name)
                        || hints
                            .assumptions
                            .retained_callback_factory_members
                            .contains_key(name)
                        || hints.assumptions.no_reentry_members.contains_key(name)
                        || hints
                            .assumptions
                            .no_reentry_record_members
                            .contains_key(name)
                        || hints
                            .assumptions
                            .namespace_independent_members
                            .contains_key(name)
                    {
                        index.namespace_privacy.members.insert(
                            local.to_id(),
                            hints
                                .assumptions
                                .namespace_independent_members
                                .get(name)
                                .cloned()
                                .unwrap_or_default(),
                        );
                        index.namespace_privacy.import_sites.insert(local.span.lo.0);
                    }
                }
            }
            let declaration = match item.as_module_item() {
                ModuleItem::Stmt(Stmt::Decl(Decl::Class(declaration))) => Some(declaration),
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    if let Decl::Class(declaration) = &export.decl {
                        Some(declaration)
                    } else {
                        None
                    }
                }
                _ => None,
            };
            if let Some(declaration) = declaration {
                let id = declaration.ident.to_id();
                index
                    .class_bindings
                    .insert(declaration.class.span.lo.0, id.clone());
                index.class_ids.insert(id);
            }
        }
        for (ordinal, item) in body.iter().enumerate() {
            fresh_steps.push(fresh_value_step(item.as_module_item()));
            if index.global_guards_requested {
                index.deferred_global_candidates[ordinal] =
                    deferred_global_publication(item.as_module_item());
                index.discarded_sync_iifes[ordinal] =
                    discarded_synchronous_iife(item.as_module_item());
            }
            index.owner = SourceUnitOrdinal(ordinal);
            index.past_await = false;
            index.may_skip_await_depth = 0;
            index.conditional_depth = 0;
            index.literal_depth = 0;
            index.factory_member_value_depth = 0;
            index.factory_callback_depth = 0;
            index.collect_uninitialized_lexical_bindings(item.as_module_item());
            index.collect_export_roots(item.as_module_item());
            if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item.as_module_item() {
                let host_async_local_storage = host_operations.is_some_and(|operations| {
                    operations.contains(&HostNoReentryOperation::AsyncLocalStorageConstruct)
                }) && !import.type_only
                    && matches!(&import.phase, ImportPhase::Evaluation)
                    && import.with.is_none()
                    && matches!(
                        import.src.value.as_str(),
                        Some("async_hooks" | "node:async_hooks")
                    );
                for specifier in &import.specifiers {
                    let local = specifier.local();
                    index.imported_bindings.insert(local.to_id());
                    if host_async_local_storage
                        && let ImportSpecifier::Named(named) = specifier
                        && !named.is_type_only
                        && named
                            .imported
                            .as_ref()
                            .map(|name| match name {
                                ModuleExportName::Ident(id) => id.sym.as_ref(),
                                ModuleExportName::Str(string) => {
                                    string.value.as_str().unwrap_or("")
                                }
                            })
                            .unwrap_or(named.local.sym.as_ref())
                            == "AsyncLocalStorage"
                    {
                        index.host_async_local_storage_imports.insert(local.to_id());
                    }
                    if let Some(summary) = hints.imported_callables.get(local.sym.as_ref())
                        && summary.supports_callback_factory()
                    {
                        index.callback_factory_imports.insert(local.to_id());
                        index
                            .callback_factory_summaries
                            .insert(local.to_id(), summary.clone());
                    }
                    if hints
                        .assumptions
                        .inline_callback_factories
                        .contains(local.sym.as_ref())
                    {
                        index.inline_callback_factory_imports.insert(local.to_id());
                    }
                    if hints
                        .assumptions
                        .no_reentry_exports
                        .contains(local.sym.as_ref())
                    {
                        index.no_reentry_exports.insert(local.to_id());
                    }
                    if let Some(signatures) = hints
                        .assumptions
                        .no_reentry_callback_exports
                        .get(local.sym.as_ref())
                    {
                        index
                            .no_reentry_callback_imports
                            .insert(local.to_id(), signatures.clone());
                    }
                    if let Some(signatures) = hints
                        .assumptions
                        .retained_callback_factories
                        .get(local.sym.as_ref())
                    {
                        index
                            .retained_callback_factory_imports
                            .insert(local.to_id(), signatures.clone());
                    }
                    if let Some(signatures) = hints
                        .assumptions
                        .retained_callback_constructors
                        .get(local.sym.as_ref())
                    {
                        index
                            .retained_callback_constructor_imports
                            .insert(local.to_id(), signatures.clone());
                    }
                    if let Some(members) = hints
                        .assumptions
                        .retained_callback_factory_members
                        .get(local.sym.as_ref())
                    {
                        index
                            .retained_callback_factory_members
                            .insert(local.to_id(), members.clone());
                        index
                            .audited_factory_members
                            .entry(local.to_id())
                            .or_default()
                            .extend(members.keys().cloned());
                    }
                    if let Some(members) = hints
                        .assumptions
                        .callback_factory_members
                        .get(local.sym.as_ref())
                    {
                        index
                            .callback_factory_members
                            .insert(local.to_id(), members.clone());
                        index
                            .audited_factory_members
                            .entry(local.to_id())
                            .or_default()
                            .extend(members.iter().cloned());
                    }
                    if let Some(members) =
                        hints.assumptions.no_reentry_members.get(local.sym.as_ref())
                    {
                        index
                            .no_reentry_members
                            .insert(local.to_id(), members.clone());
                    }
                    if let Some(members) = hints
                        .assumptions
                        .no_reentry_record_members
                        .get(local.sym.as_ref())
                    {
                        index
                            .no_reentry_record_members
                            .insert(local.to_id(), members.clone());
                    }
                }
            }
            item.as_module_item().visit_with(&mut index);
        }
        for (offset, proof) in index.array_iterations.values_mut().enumerate() {
            proof.result = u32::try_from(offset)
                .ok()
                .and_then(|offset| max_source_position.checked_add(offset)?.checked_add(1));
        }
        // An accessor fallback may run any callable owned by this item,
        // including a nested function installed as the setter. Union each
        // retained synchronous prefix instead of using whole lazy bodies.
        for callable in index.callables.values() {
            let reads = index.owner_callable_prefix_reads[callable.owner.0]
                .get_or_insert_with(BTreeSet::new);
            if !callable.created_after_parent_await {
                reads.extend(callable.reads.iter().cloned());
            }
        }
        let namespace_assignments = index
            .top_assignments
            .iter()
            .flatten()
            .chain(
                index
                    .callables
                    .values()
                    .flat_map(|callable| callable.local_assignments.iter()),
            )
            .collect::<Vec<_>>();
        let namespace_value_facts = NamespaceValueFacts::from_assignments(
            index.class_values.clone(),
            fresh_steps
                .iter()
                .filter_map(|step| match step {
                    Some(FreshValueStep::Literal { span, .. }) => Some(*span),
                    _ => None,
                })
                .collect(),
            &namespace_assignments,
        );
        // Track aliases to the built-in objects used by narrow construction
        // proofs. A value may retain multiple roots (for example, a
        // conditional alias of Object and Array), so merge sets rather than
        // imposing an arbitrary ordering on them.
        let mut intrinsic_aliases = BTreeMap::<Id, IntrinsicRoots>::new();
        loop {
            let mut changed = false;
            for (id, value) in &namespace_assignments {
                let found = intrinsic_roots(value, &intrinsic_aliases, &index.global_object_names);
                if !found.is_empty() {
                    let roots = intrinsic_aliases.entry((*id).clone()).or_default();
                    let before = roots.len();
                    roots.extend(found);
                    changed |= roots.len() != before;
                }
            }
            if !changed {
                break;
            }
        }
        let mut mutated_intrinsics = IntrinsicRoots::new();
        for (id, _) in &namespace_assignments {
            match id.0.as_ref() {
                "Error" => {
                    mutated_intrinsics.insert(IntrinsicRoot::Error);
                }
                "Object" => {
                    mutated_intrinsics.insert(IntrinsicRoot::Object);
                }
                "Array" => {
                    mutated_intrinsics.insert(IntrinsicRoot::Array);
                }
                "Math" => {
                    mutated_intrinsics.insert(IntrinsicRoot::Math);
                }
                "RegExp" => {
                    mutated_intrinsics.insert(IntrinsicRoot::RegExp);
                }
                "Symbol" => {
                    mutated_intrinsics.insert(IntrinsicRoot::Symbol);
                }
                "Promise" => {
                    mutated_intrinsics.insert(IntrinsicRoot::Promise);
                }
                "Map" => {
                    mutated_intrinsics.insert(IntrinsicRoot::Map);
                }
                "Set" => {
                    mutated_intrinsics.insert(IntrinsicRoot::Set);
                }
                "WeakMap" => {
                    mutated_intrinsics.insert(IntrinsicRoot::WeakMap);
                }
                "WeakSet" => {
                    mutated_intrinsics.insert(IntrinsicRoot::WeakSet);
                }
                _ => {}
            }
        }
        for receiver in &index.intrinsic_mutation_receivers {
            mutated_intrinsics.extend(intrinsic_roots(
                receiver,
                &intrinsic_aliases,
                &index.global_object_names,
            ));
        }
        index.mutated_intrinsic_roots = mutated_intrinsics.clone();
        let mut claim_counts = BTreeMap::<FreshErrorWriteSite, usize>::new();
        if !index.fresh_error_claims.is_empty()
            && !index.audited_factory_members.is_empty()
            && flow.is_some()
        {
            // A11 supplies the standard Error allocation/prototype behavior. Reject
            // visible replacement of that intrinsic, including writes through local
            // aliases of a known global object. This is not a general effect proof.
            let intrinsic_mutated = mutated_intrinsics.contains(&IntrinsicRoot::Error)
                || mutated_intrinsics.contains(&IntrinsicRoot::GlobalObject);
            if !intrinsic_mutated
                && index.dynamic_scope_owners.is_empty()
                && let Some(flow) = flow
            {
                for claim in &index.fresh_error_claims {
                    if !flow.is_declared(&claim.callee)
                        && claim
                            .local_read
                            .as_ref()
                            .is_none_or(|id| flow.is_declared(id))
                    {
                        *claim_counts.entry(claim.site.clone()).or_default() += 1;
                    }
                }
            }
            let mut write_counts = BTreeMap::<FreshErrorWriteSite, usize>::new();
            let mut count_writes = |owner, callable, writes: &[CallbackResultWrite]| {
                for write in writes {
                    if let (ValueExpr::Binding(receiver), Some(property)) =
                        (&write.receiver, &write.property)
                    {
                        *write_counts
                            .entry(FreshErrorWriteSite {
                                owner,
                                callable,
                                receiver: receiver.clone(),
                                property: property.clone(),
                            })
                            .or_default() += 1;
                    }
                }
            };
            for (owner, writes) in index.top_callback_result_writes.iter().enumerate() {
                count_writes(SourceUnitOrdinal(owner), None, writes);
            }
            for (key, callable) in &index.callables {
                count_writes(callable.owner, Some(*key), &callable.callback_result_writes);
            }
            claim_counts.retain(|site, count| write_counts.get(site) == Some(count));
        }
        let certified = |owner, callable, receiver: &Id, property: &str| {
            claim_counts.contains_key(&FreshErrorWriteSite {
                owner,
                callable,
                receiver: receiver.clone(),
                property: property.to_owned(),
            })
        };
        let certified_callback_write = |owner, callable, write: &CallbackResultWrite| matches!((&write.receiver, &write.property), (ValueExpr::Binding(receiver), Some(property)) if certified(owner, callable, receiver, property));
        // A namespace member is an audited target only while its binding is
        // unchanged. Any local write could replace a member with an eager
        // callback invoker before the apparent factory call.
        // Total source privacy proves that this family cannot reach any unrelated
        // Unknown/This slot. It grants no execution or callback timing effects.
        let private_namespace_family = !index.namespace_privacy.members.is_empty()
            && !index.namespace_privacy.invalid
            && index.dynamic_scope_owners.is_empty();
        let namespace_member_write_witnesses =
            RefCell::new(BTreeMap::<(Id, String), CallbackFactoryWriteWitness>::new());
        let namespace_dynamic_property_write_witnesses =
            RefCell::new(BTreeMap::<(Id, String), CallbackFactoryWriteWitness>::new());
        let namespace_call_argument_witnesses =
            RefCell::new(BTreeMap::<Id, CallbackFactoryCallArgumentWitness>::new());
        let keep_namespace = |id: &Id, members: &BTreeSet<String>, writes: &BTreeSet<_>| {
            if private_namespace_family {
                return true;
            }
            let aliases =
                infer_namespace_aliases(id, &namespace_assignments, &namespace_value_facts);
            let receiver_aliases = infer_mutation_receiver_aliases(
                id,
                &namespace_assignments,
                &aliases,
                &namespace_value_facts,
            );
            let is_namespace_or_alias = |binding: &Id| binding == id || aliases.contains(binding);
            let is_mutation_receiver = |receiver: &ValueExpr| {
                receiver.may_be_namespace_mutation_receiver(
                    id,
                    &aliases,
                    &receiver_aliases,
                    &namespace_value_facts,
                )
            };
            // Keep the original conservative admission predicate independent
            // of diagnostic witness extraction below.
            let top_member_write_rejected = index
                .top_property_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| {
                    writes
                        .iter()
                        .map(move |write| (SourceUnitOrdinal(owner), write))
                })
                .any(|(owner, (receiver, property))| {
                    receiver.as_ref().is_some_and(|receiver| {
                        !property
                            .as_ref()
                            .is_some_and(|property| certified(owner, None, receiver, property))
                            && ((receiver == id
                                && property
                                    .as_ref()
                                    .is_none_or(|property| members.contains(property)))
                                || aliases.contains(receiver)
                                || receiver_aliases.contains(receiver))
                    })
                });
            let top_member_write_witness = index
                .top_property_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| {
                    writes
                        .iter()
                        .map(move |write| (SourceUnitOrdinal(owner), write))
                })
                .find_map(|(owner, (receiver, property))| {
                    let receiver = receiver.as_ref()?;
                    let property = property.as_ref()?;
                    (members.contains(property)
                        && !certified(owner, None, receiver, property)
                        && (receiver == id
                            || aliases.contains(receiver)
                            || receiver_aliases.contains(receiver)))
                    .then_some((owner, receiver, property))
                });
            if let Some((statement_ordinal, receiver_binding, property)) = &top_member_write_witness
            {
                let witness = CallbackFactoryWriteWitness {
                    statement_ordinal: *statement_ordinal,
                    receiver_binding: receiver_binding.0.to_string(),
                    property: Some((*property).clone()),
                };
                let mut recorded = namespace_member_write_witnesses.borrow_mut();
                for member in members {
                    recorded
                        .entry((id.clone(), member.clone()))
                        .or_insert_with(|| witness.clone());
                }
            }
            let destructuring_write = index.destructuring_member_writes.iter().any(|write| {
                is_mutation_receiver(&write.receiver)
                    || write
                        .source
                        .may_publish_namespace(id, &aliases, &namespace_value_facts)
            });
            let top_dynamic_write = index
                .top_dynamic_property_receivers
                .iter()
                .flatten()
                .any(is_mutation_receiver);
            // This is a diagnostic for the already rejected guard, not an
            // admission rule. Only a concrete binding receiver supplies an
            // attributable source witness; opaque receiver shapes stay unknown.
            let dynamic_write_witness = index
                .top_property_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| writes.iter().map(move |write| (owner, write)))
                .find_map(|(owner, (receiver, property))| match (receiver, property) {
                    (Some(binding), None)
                        if binding == id
                            || aliases.contains(binding)
                            || receiver_aliases.contains(binding) =>
                    {
                        Some(CallbackFactoryWriteWitness {
                            statement_ordinal: SourceUnitOrdinal(owner),
                            receiver_binding: binding.0.to_string(),
                            property: None,
                        })
                    }
                    _ => None,
                });
            if let Some(witness) = dynamic_write_witness {
                let mut recorded = namespace_dynamic_property_write_witnesses.borrow_mut();
                for member in members {
                    recorded
                        .entry((id.clone(), member.clone()))
                        .or_insert_with(|| witness.clone());
                }
            }
            let top_callback_result_write = index
                .top_callback_result_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| {
                    writes
                        .iter()
                        .map(move |write| (SourceUnitOrdinal(owner), write))
                })
                .any(|(owner, write)| {
                    (is_mutation_receiver(&write.receiver)
                        && !certified_callback_write(owner, None, write))
                        || write
                            .value
                            .may_publish_namespace(id, &aliases, &namespace_value_facts)
                });
            let callable_write = index.callables.iter().any(|(key, callable)| {
                callable.member_writes.iter().any(|(receiver, property)| {
                    !certified(callable.owner, Some(*key), receiver, property)
                        && ((receiver == id && members.contains(property))
                            || aliases.contains(receiver)
                            || receiver_aliases.contains(receiver))
                }) || callable
                    .dynamic_property_receivers
                    .iter()
                    .any(is_mutation_receiver)
                    || callable.literal_exposures.iter().any(is_namespace_or_alias)
                    || callable.escaped_bindings.iter().any(is_namespace_or_alias)
                    || callable.namespace_escape_values.iter().any(|value| {
                        value.may_publish_namespace(id, &aliases, &namespace_value_facts)
                    })
                    || callable.member_value_writes.iter().any(|(_, _, value)| {
                        value.may_publish_namespace(id, &aliases, &namespace_value_facts)
                    })
                    || callable.callback_result_writes.iter().any(|write| {
                        (is_mutation_receiver(&write.receiver)
                            && !certified_callback_write(callable.owner, Some(*key), write))
                            || write.value.may_publish_namespace(
                                id,
                                &aliases,
                                &namespace_value_facts,
                            )
                    })
                    || callable.returns.iter().any(|value| {
                        value.may_publish_namespace_in_factory_result(
                            id,
                            &aliases,
                            &namespace_value_facts,
                        )
                    })
                    || callable.calls.iter().any(|call| {
                        call.args.iter().any(|value| {
                            value.may_publish_namespace(id, &aliases, &namespace_value_facts)
                        })
                    })
            });
            let top_member_value =
                index
                    .top_member_value_writes
                    .iter()
                    .flatten()
                    .any(|(_, _, value)| {
                        value.may_publish_namespace(id, &aliases, &namespace_value_facts)
                    });
            let top_call_argument = index.top_calls.iter().flatten().any(|call| {
                call.args
                    .iter()
                    .any(|value| value.may_publish_namespace(id, &aliases, &namespace_value_facts))
            });
            if top_call_argument {
                let direct = index
                    .top_calls
                    .iter()
                    .flatten()
                    .filter(|call| {
                        call.kind == CallKind::Call
                            && call.caller.is_none()
                            && !call.has_spread
                            && index.ordinary_top_call_sites.contains(&call.span.into())
                    })
                    .flat_map(|call| {
                        call.args
                                .iter()
                                .enumerate()
                                .filter(|(_, value)| {
                                    matches!(*value, ValueExpr::Binding(binding) if binding == id)
                                })
                                .map(move |(argument_index, _)| CallbackFactoryCallArgumentWitness {
                                    statement_ordinal: call.owner,
                                    call_span: call.span,
                                    argument_index,
                                    namespace_binding: id.0.to_string(),
                                })
                    })
                    .min_by_key(|witness| {
                        (
                            witness.statement_ordinal,
                            witness.call_span.lo.0,
                            witness.argument_index,
                        )
                    });
                if let Some(witness) = direct {
                    namespace_call_argument_witnesses
                        .borrow_mut()
                        .insert(id.clone(), witness);
                }
            }
            let exported_value = index
                .top_exported_values
                .iter()
                .flatten()
                .any(|value| value.may_publish_namespace(id, &aliases, &namespace_value_facts))
                || index.exported_bindings.iter().any(is_namespace_or_alias);
            !(top_member_write_rejected
                || destructuring_write
                || top_dynamic_write
                || top_callback_result_write
                || callable_write
                || top_member_value
                || top_call_argument
                || exported_value)
        };
        let namespace_family: Vec<_> = index.namespace_privacy.members.keys().cloned().collect();
        let family_audited_members: BTreeSet<_> = if namespace_family.is_empty() {
            BTreeSet::new()
        } else {
            index
                .audited_factory_members
                .values()
                .chain(index.no_reentry_members.values())
                .chain(index.no_reentry_record_members.values())
                .flat_map(|members| members.iter().cloned())
                .collect()
        };
        let factory_imports = index
            .callback_factory_imports
            .union(&index.inline_callback_factory_imports)
            .cloned()
            .collect();
        loop {
            let factory_members = index.audited_factory_members.clone();
            let legacy_factory_members = index.callback_factory_members.clone();
            let no_reentry_members = index.no_reentry_members.clone();
            let no_reentry_record_members = index.no_reentry_record_members.clone();
            // The fresh result is private until these adjacent writes finish.
            // Only currently admitted factories can establish that provenance;
            // removing a producer must invalidate proofs depending on its result.
            let (fresh_writes, instance_field_writes) = if index.dynamic_scope_owners.is_empty() {
                fresh_value_writes(
                    &fresh_steps,
                    &namespace_value_facts,
                    &factory_imports,
                    &legacy_factory_members,
                )
            } else {
                (BTreeSet::new(), BTreeSet::new())
            };
            index.fresh_instance_field_writes = instance_field_writes;
            let keep_audited_members = |id: &Id, members: &BTreeSet<String>| {
                if namespace_family.is_empty() {
                    keep_namespace(id, members, &fresh_writes)
                } else {
                    // Import names do not establish distinct object identities.
                    // Fallback must also certify every potentially aliasing root.
                    namespace_family
                        .iter()
                        .all(|root| keep_namespace(root, &family_audited_members, &fresh_writes))
                }
            };
            index
                .audited_factory_members
                .retain(|id, members| keep_audited_members(id, members));
            index
                .callback_factory_members
                .retain(|id, _| index.audited_factory_members.contains_key(id));
            index.no_reentry_members.retain(|id, members| {
                index
                    .namespace_privacy
                    .members
                    .get(id)
                    .is_some_and(|independent| members.is_subset(independent))
                    && keep_audited_members(id, members)
            });
            index.no_reentry_record_members.retain(|id, members| {
                index
                    .namespace_privacy
                    .members
                    .get(id)
                    .is_some_and(|independent| members.is_subset(independent))
                    && keep_audited_members(id, members)
            });
            if index.audited_factory_members.len() == factory_members.len()
                && index.no_reentry_members.len() == no_reentry_members.len()
                && index.no_reentry_record_members.len() == no_reentry_record_members.len()
            {
                break;
            }
        }
        index.rejected_namespace_member_writes = namespace_member_write_witnesses.into_inner();
        index.rejected_namespace_dynamic_property_writes =
            namespace_dynamic_property_write_witnesses.into_inner();
        index.rejected_namespace_call_arguments = namespace_call_argument_witnesses.into_inner();
        if index.deferred_global_requested {
            let first_publication = index
                .deferred_global_candidates
                .iter()
                .position(|candidate| {
                    candidate.as_ref().is_some_and(|candidate| {
                        hints
                            .assumptions
                            .deferred_global_publications
                            .get(&candidate.root)
                            .is_some_and(|slots| slots.contains(&candidate.property))
                    })
                })
                .map(SourceUnitOrdinal)
                .unwrap_or(SourceUnitOrdinal(0));
            let protected: BTreeSet<String> = hints
                .assumptions
                .deferred_global_publications
                .values()
                .flat_map(|slots| slots.iter().cloned())
                .collect();
            let certified_root_ids: BTreeSet<Id> = index
                .deferred_global_candidates
                .iter()
                .flatten()
                .filter(|candidate| {
                    let name = match candidate.root {
                        BuiltinGlobalRoot::GlobalThis => "globalThis",
                        BuiltinGlobalRoot::Window => "window",
                    };
                    index.global_object_names.contains(name)
                        && hints
                            .assumptions
                            .deferred_global_publications
                            .get(&candidate.root)
                            .is_some_and(|slots| slots.contains(&candidate.property))
                })
                .map(|candidate| candidate.receiver_id.clone())
                .collect();
            let global_root_exposed =
                index.global_receiver_exposed(first_publication, &protected, &certified_root_ids);
            if index.dynamic_scope_owners.is_empty()
                && !index.dynamic_global_member
                && !index.global_alias_written
                && !global_root_exposed
            {
                for (owner, candidate) in index.deferred_global_candidates.iter().enumerate() {
                    let Some(candidate) = candidate else { continue };
                    let root_name = match candidate.root {
                        BuiltinGlobalRoot::GlobalThis => "globalThis",
                        BuiltinGlobalRoot::Window => "window",
                    };
                    if !index.global_object_names.contains(root_name)
                        || !hints
                            .assumptions
                            .deferred_global_publications
                            .get(&candidate.root)
                            .is_some_and(|slots| slots.contains(&candidate.property))
                        || index.global_slot_mentions.get(&candidate.property) != Some(&1)
                        || candidate.property == "__proto__"
                        || !index.top_calls[owner].is_empty()
                        || index.top_property_writes[owner].len() != 1
                        || index.top_member_value_writes[owner].len() != 1
                        || index.top_callback_result_writes[owner].len() != 1
                    {
                        continue;
                    }
                    index
                        .deferred_global_certificates
                        .insert(SourceUnitOrdinal(owner));
                }
            }
        }
        if !hints.assumptions.non_reentrant_global_membership.is_empty() {
            let mut bindings = GlobalMembershipBindings::default();
            for item in body {
                item.as_module_item().visit_with(&mut bindings);
            }
            index.global_membership_candidates.retain(|_, probe| {
                !bindings.0.contains(&probe.receiver)
                    && hints
                        .assumptions
                        .non_reentrant_global_membership
                        .get(&probe.root)
                        .is_some_and(|keys| keys.contains(&probe.property))
            });
            let first_probe = index
                .global_membership_candidates
                .values()
                .map(|probe| probe.owner)
                .min()
                .unwrap_or(SourceUnitOrdinal(0));
            let protected = hints
                .assumptions
                .non_reentrant_global_membership
                .values()
                .flat_map(|keys| keys.iter().cloned())
                .collect();
            let roots = index
                .global_membership_candidates
                .values()
                .map(|probe| probe.receiver.clone())
                .collect();
            let prototype_write = index
                .top_callback_result_writes
                .iter()
                .flatten()
                .chain(
                    index
                        .callables
                        .values()
                        .flat_map(|callable| &callable.callback_result_writes),
                )
                .any(|write| {
                    let path = global_mutation_path(
                        &write.receiver,
                        &index.global_object_names,
                        &bindings.0,
                    );
                    // A visible stored back-alias defeats the descendant
                    // distinction even when its receiver is an ordinary object.
                    // Prototype values held by unrelated private objects do
                    // not themselves expose or mutate a global-rooted path.
                    if stored_global_alias(
                        &write.value,
                        &index.global_object_names,
                        &bindings.0,
                        path != GlobalMutationPath::Unrelated,
                    ) {
                        return true;
                    }
                    match write.property.as_deref() {
                        Some("__proto__" | "prototype" | "constructor") => {
                            path != GlobalMutationPath::Unrelated
                                || global_receiver_path(&write.receiver, &index.global_object_names)
                        }
                        None => matches!(
                            path,
                            GlobalMutationPath::Identity | GlobalMutationPath::Prototype
                        ),
                        _ => false,
                    }
                });
            if index.dynamic_scope_owners.is_empty()
                && !index.dynamic_global_member
                && !index.global_alias_written
                && !prototype_write
                && !index
                    .mutated_intrinsic_roots
                    .contains(&IntrinsicRoot::Object)
                && !index.global_receiver_exposed(first_probe, &protected, &roots)
            {
                index
                    .non_reentrant_global_membership
                    .extend(index.global_membership_candidates.keys().copied());
            }
        }
        index
    }

    fn collection_intrinsic_is_unmodified(&self, constructor: &str) -> bool {
        let root = match constructor {
            "Map" => IntrinsicRoot::Map,
            "Set" => IntrinsicRoot::Set,
            "WeakMap" => IntrinsicRoot::WeakMap,
            "WeakSet" => IntrinsicRoot::WeakSet,
            _ => return false,
        };
        self.dynamic_scope_owners.is_empty()
            && !self
                .mutated_intrinsic_roots
                .contains(&IntrinsicRoot::GlobalObject)
            && !self.mutated_intrinsic_roots.contains(&root)
    }

    fn set_iterable_intrinsics_are_unmodified(&self) -> bool {
        self.collection_intrinsic_is_unmodified("Set")
            && !self.mutated_intrinsic_roots.contains(&IntrinsicRoot::Array)
    }

    fn global_receiver_exposed(
        &self,
        first_publication: SourceUnitOrdinal,
        protected: &BTreeSet<String>,
        certified_root_ids: &BTreeSet<Id>,
    ) -> bool {
        self.top_assignments
            .iter()
            .flatten()
            .any(|(_, value)| exposes_global_receiver(value, &self.global_object_names))
            || self
                .top_exported_values
                .iter()
                .flatten()
                .any(|value| exposes_global_receiver(value, &self.global_object_names))
            || self.top_calls.iter().flatten().any(|call| {
                call_exposes_global_receiver(call, &self.global_object_names, certified_root_ids)
            })
            || self.callables.iter().any(|(key, callable)| {
                callable.local_assignments.iter().any(|(id, value)| {
                    exposes_global_receiver(value, &self.global_object_names)
                        && !confined_prepublication_alias(
                            self,
                            *key,
                            id,
                            first_publication,
                            protected,
                        )
                }) || callable
                    .returns
                    .iter()
                    .any(|value| exposes_global_receiver(value, &self.global_object_names))
                    || callable.calls.iter().any(|call| {
                        call_exposes_global_receiver(
                            call,
                            &self.global_object_names,
                            certified_root_ids,
                        )
                    })
            })
    }

    fn collect_uninitialized_lexical_bindings(&mut self, item: &ModuleItem) {
        let Some(declaration) = var_decl_of_item(item) else {
            return;
        };
        if declaration.kind == VarDeclKind::Const
            && matches!(declaration.decls.as_slice(), [declarator]
                if matches!(declarator.init.as_deref().map(Expr::unwrap_parens), Some(Expr::Call(call))
                    if matches!(&call.callee, Callee::Expr(callee)
                        if matches!(callee.unwrap_parens(), Expr::Member(member)
                            if matches!(member.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Object")
                                && matches!(&member.prop, MemberProp::Ident(prop) if prop.sym == "freeze")))))
        {
            self.const_call_owners.insert(self.owner);
        }
        if declaration.kind == VarDeclKind::Var {
            for declarator in &declaration.decls {
                if declarator.init.is_some()
                    && let Pat::Ident(binding) = &declarator.name
                {
                    self.top_definite_initializers[self.owner.0].insert(binding.id.to_id());
                }
            }
            return;
        }
        for declarator in &declaration.decls {
            if let Pat::Ident(binding) = &declarator.name {
                let id = binding.id.to_id();
                self.top_definite_initializers[self.owner.0].insert(id.clone());
                if declarator.init.is_none() {
                    self.top_assignments[self.owner.0].push((id, ValueExpr::Undefined));
                }
            }
        }
    }

    fn collect_export_roots(&mut self, item: &ModuleItem) {
        match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_))) => {
                self.hoisted_function_owners.insert(self.owner);
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                self.exported_declaration_owners.insert(self.owner);
                self.exported_bindings.extend(collect_declared_names(item));
                if matches!(export.decl, Decl::Fn(_)) {
                    self.hoisted_function_owners.insert(self.owner);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(export)) => {
                self.exported_declaration_owners.insert(self.owner);
                if let DefaultDecl::Fn(function) = &export.decl {
                    self.hoisted_function_owners.insert(self.owner);
                    self.exported_hoisted_functions
                        .insert(function.function.span.lo.0);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(export)) => {
                self.exported_declaration_owners.insert(self.owner);
                self.top_exported_values[self.owner.0].push(ValueExpr::from_expr(&export.expr));
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) => {
                self.linkage_owners.insert(self.owner);
                if export.src.is_none() {
                    for specifier in &export.specifiers {
                        if let ExportSpecifier::Named(named) = specifier
                            && let ModuleExportName::Ident(local) = &named.orig
                        {
                            self.exported_bindings.insert(local.to_id());
                        }
                    }
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_)) => {
                self.linkage_owners.insert(self.owner);
            }
            _ => {}
        }
    }

    fn with_callable(&mut self, key: u32, params: Vec<Parameter>, visit: impl FnOnce(&mut Self)) {
        let created_after_parent_await = self
            .current
            .and_then(|parent| self.callables.get(&parent))
            .is_some_and(|parent| parent.created_after_parent_await)
            || (self.past_await && !self.hoisted_nested_functions.contains(&key));
        let saved_current = self.current.replace(key);
        let saved_await = std::mem::replace(&mut self.past_await, false);
        let saved_may_skip = std::mem::replace(&mut self.may_skip_await_depth, 0);
        let saved_conditional = std::mem::replace(&mut self.conditional_depth, 0);
        let opaque_params = params.iter().any(|param| param.binding.is_none());
        self.callables.entry(key).or_insert_with(|| Callable {
            owner: self.owner,
            created_after_parent_await,
            params,
            opaque: opaque_params,
            ..Callable::default()
        });
        visit(self);
        self.current = saved_current;
        self.past_await = saved_await;
        self.may_skip_await_depth = saved_may_skip;
        self.conditional_depth = saved_conditional;
    }

    fn record_call(&mut self, call: Call) {
        if matches!(&call.callee, ValueExpr::Binding(id) if id.0.as_ref() == "eval") {
            self.dynamic_scope_owners.insert(self.owner);
        }
        if self.current.is_some() && self.past_await {
            return;
        }
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().calls.push(call);
        } else {
            self.top_calls[self.owner.0].push(call);
        }
    }

    fn record_source_protocol(&mut self, kind: CallKind, source: ValueExpr, span: Span) {
        self.record_call(Call {
            kind,
            callee: ValueExpr::Unknown,
            args: vec![source],
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

    fn record_implicit_coercion(&mut self, expression: &Expr, span: Span) {
        if (self.current.is_some() && self.past_await) || has_definite_primitive_result(expression)
        {
            return;
        }
        self.record_implicit_coercion_value(ValueExpr::from_expr(expression), span);
    }

    fn record_implicit_coercion_value(&mut self, argument: ValueExpr, span: Span) {
        if self.current.is_some() && self.past_await {
            return;
        }
        self.record_call(Call {
            kind: CallKind::ImplicitCoercion,
            callee: ValueExpr::Unknown,
            args: vec![argument],
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

    fn mark_opaque(&mut self) {
        if self.current.is_some() && self.past_await {
            return;
        }
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().opaque = true;
        } else {
            self.opaque_top.insert(self.owner);
        }
    }

    fn mark_dynamic_property_effect(&mut self, receiver: Option<&Expr>) {
        if self.past_await {
            return;
        }
        if let Some(key) = self.current {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.dynamic_property_effect = true;
            callable
                .dynamic_property_receivers
                .push(receiver.map_or(ValueExpr::Unknown, ValueExpr::from_expr));
        } else {
            self.top_property_writes[self.owner.0].push((None, None));
            if let Some(receiver) = receiver {
                self.top_dynamic_property_receivers[self.owner.0]
                    .push(ValueExpr::from_expr(receiver));
            }
        }
    }

    fn mark_possible_namespace_escape(&mut self, expression: &Expr) {
        let Some(key) = self.current else {
            return;
        };
        let value = ValueExpr::from_expr(expression);
        let exposed = self
            .audited_factory_members
            .keys()
            .filter(|id| value.carries_namespace(id, &BTreeSet::new()))
            .cloned()
            .collect::<Vec<_>>();
        let callable = self.callables.get_mut(&key).unwrap();
        callable.escaped_bindings.extend(exposed);
        callable.namespace_escape_values.push(value);
    }

    fn is_factory_member_data_value(&self, key: &PropName, expression: &Expr) -> bool {
        if object_data_property_name(key).is_none() {
            return false;
        }
        let Expr::Member(member) = expression.unwrap_parens() else {
            return false;
        };
        matches!(member.obj.unwrap_parens(), Expr::Ident(namespace)
            if self.audited_factory_members.contains_key(&namespace.to_id()))
            && matches!(member.prop, MemberProp::Ident(_))
    }

    fn is_callback_factory_member_callee(&self, expression: &Expr) -> bool {
        let Expr::Member(member) = expression.unwrap_parens() else {
            return false;
        };
        let Expr::Ident(namespace) = member.obj.unwrap_parens() else {
            return false;
        };
        self.audited_factory_members
            .contains_key(&namespace.to_id())
            && matches!(member.prop, MemberProp::Ident(_))
    }

    fn inline_factory_callback_argument(&self, call: &CallExpr) -> Option<usize> {
        if call.args.len() != 1 || call.args[0].spread.is_some() {
            return None;
        }
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        let is_factory = match callee.unwrap_parens() {
            Expr::Ident(callee) => self.callback_factory_imports.contains(&callee.to_id()),
            Expr::Member(member) => {
                let (Expr::Ident(namespace), MemberProp::Ident(property)) =
                    (member.obj.unwrap_parens(), &member.prop)
                else {
                    return None;
                };
                self.callback_factory_members
                    .get(&namespace.to_id())
                    .is_some_and(|members| members.contains(property.sym.as_ref()))
            }
            _ => false,
        };
        (is_factory
            && matches!(
                call.args[0].expr.unwrap_parens(),
                Expr::Fn(_) | Expr::Arrow(_)
            ))
        .then_some(0)
    }

    fn retained_callback_call(&self, call: &CallExpr) -> Option<RetainedCallbackCall> {
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        let (signatures, namespace_member) = match callee.unwrap_parens() {
            Expr::Ident(binding) => (
                self.retained_callback_factory_imports
                    .get(&binding.to_id())?,
                None,
            ),
            Expr::Member(member) => {
                let (Expr::Ident(namespace), MemberProp::Ident(property)) =
                    (member.obj.unwrap_parens(), &member.prop)
                else {
                    return None;
                };
                (
                    self.retained_callback_factory_members
                        .get(&namespace.to_id())?
                        .get(property.sym.as_ref())?,
                    Some((namespace.to_id(), property.sym.to_string())),
                )
            }
            _ => return None,
        };
        Self::retained_callback_invocation(signatures, &call.args, namespace_member)
    }

    fn no_reentry_callback_call(&self, call: &CallExpr) -> Option<RetainedCallbackCall> {
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        let Expr::Ident(binding) = callee.unwrap_parens() else {
            return None;
        };
        Self::retained_callback_invocation(
            self.no_reentry_callback_imports.get(&binding.to_id())?,
            &call.args,
            None,
        )
    }

    fn retained_constructor_call(&self, allocation: &NewExpr) -> Option<RetainedCallbackCall> {
        let Expr::Ident(binding) = allocation.callee.unwrap_parens() else {
            return None;
        };
        Self::retained_callback_invocation(
            self.retained_callback_constructor_imports
                .get(&binding.to_id())?,
            allocation.args.as_deref().unwrap_or_default(),
            None,
        )
    }

    fn retained_callback_invocation(
        signatures: &[spec::RetainedCallbackSignature],
        args: &[ExprOrSpread],
        namespace_member: Option<(Id, String)>,
    ) -> Option<RetainedCallbackCall> {
        if args.iter().any(|argument| argument.spread.is_some()) {
            return None;
        }
        let signatures: Vec<_> = signatures
            .iter()
            .filter(|signature| {
                signature.argument_count == args.len()
                    && signature
                        .callback_arguments
                        .iter()
                        .all(|position| *position < args.len())
                    && args.iter().enumerate().all(|(position, argument)| {
                        if signature.callback_arguments.contains(&position) {
                            matches!(
                                argument.expr.unwrap_parens(),
                                Expr::Ident(_) | Expr::Fn(_) | Expr::Arrow(_) | Expr::Call(_)
                            )
                        } else {
                            no_reentry_primitive_argument(&argument.expr)
                        }
                    })
            })
            .cloned()
            .collect();
        let callback_argument_positions: BTreeSet<_> = signatures
            .iter()
            .flat_map(|signature| signature.callback_arguments.iter().copied())
            .collect();
        let callback_argument_exprs = callback_argument_positions
            .into_iter()
            .map(|position| (position, ValueExpr::from_expr(&args[position].expr)))
            .collect();
        (!signatures.is_empty()).then_some(RetainedCallbackCall {
            namespace_member,
            signatures,
            argument_count: args.len(),
            callback_argument_exprs,
        })
    }

    fn record_member_write(&mut self, member: &MemberExpr) {
        if self.past_await {
            return;
        }
        let receiver = match member.obj.unwrap_parens() {
            Expr::Ident(object) => Some(object.to_id()),
            _ => None,
        };
        let property = static_prop(&member.prop);
        match (self.current, receiver, property) {
            (Some(key), Some(receiver), Some(property)) => self
                .callables
                .get_mut(&key)
                .unwrap()
                .member_writes
                .push((receiver, property)),
            (None, receiver, property) => {
                self.top_property_writes[self.owner.0].push((receiver, property))
            }
            _ => self.mark_dynamic_property_effect(Some(&member.obj)),
        }
    }

    fn visit_may_skip<N: VisitWith<Self>>(&mut self, node: &N) {
        let before = self.past_await;
        self.may_skip_await_depth += 1;
        self.conditional_depth += 1;
        node.visit_children_with(self);
        self.conditional_depth -= 1;
        self.may_skip_await_depth -= 1;
        self.past_await = before;
    }
}

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
        self.class_owners.insert(class.span.lo.0, self.owner);
        self.class_values
            .insert(class.span.lo.0, collect_class_value_facts(class));
        if let Some(constructor) = local_constructor(class, &self.local_class_construction_proof) {
            self.local_constructors.insert(class.span.lo.0, constructor);
        }
        class.visit_children_with(self);
    }

    fn visit_with_stmt(&mut self, statement: &WithStmt) {
        self.dynamic_scope_owners.insert(self.owner);
        statement.visit_children_with(self);
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
                            default: None,
                        },
                    })
                    .collect();
                self.with_callable(ctor.span.lo.0, params, |index| {
                    ctor.visit_children_with(index);
                });
            }
            ClassMember::ClassProp(prop) if !prop.is_static => {
                if let Some(value) = &prop.value {
                    self.with_callable(prop.span.lo.0, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            ClassMember::PrivateProp(prop) if !prop.is_static => {
                if let Some(value) = &prop.value {
                    self.with_callable(prop.span.lo.0, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            ClassMember::AutoAccessor(accessor) if !accessor.is_static => {
                if let Some(value) = &accessor.value {
                    self.with_callable(accessor.span.lo.0, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            _ => {}
        }
    }

    fn visit_setter_prop(&mut self, setter: &SetterProp) {
        // Object-literal setters are not represented by a `Function` node in
        // SWC, so their bodies lack a per-Callable prefix summary.
        self.unindexed_accessor_owners.insert(self.owner);
        self.setter_owners
            .entry(static_property_name(&setter.key))
            .or_default()
            .insert(self.owner);
        setter.visit_children_with(self);
    }

    fn visit_getter_prop(&mut self, getter: &GetterProp) {
        self.unindexed_accessor_owners.insert(self.owner);
        getter.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, object: &ObjectLit) {
        if matches!(object.props.as_slice(), [PropOrSpread::Spread(_)]) {
            self.exact_object_spread_copies.insert(object.span.lo.0);
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
                    primitive_literal(&pair.value),
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
                        spread: element.spread.is_some(),
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
        if let Some(key) = self.current {
            // Declarations are instantiated when the enclosing activation
            // begins, even if their source text follows an await.
            self.hoisted_nested_functions
                .insert(declaration.function.span.lo.0);
            let callable = self.callables.get_mut(&key).unwrap();
            callable.local_functions.insert(declaration.ident.to_id());
            callable.local_assignments.push((
                declaration.ident.to_id(),
                ValueExpr::Origin(ValueOrigin::Function(declaration.function.span.lo.0)),
            ));
        } else {
            self.hoisted_bindings
                .insert(declaration.ident.to_id(), declaration.function.span.lo.0);
        }
        declaration.visit_children_with(self);
    }

    fn visit_class_decl(&mut self, declaration: &ClassDecl) {
        declaration.class.visit_with(self);
        if self.current.is_none() {
            self.top_definite_initializers[self.owner.0].insert(declaration.ident.to_id());
            self.top_assignments[self.owner.0].push((
                declaration.ident.to_id(),
                ValueExpr::Origin(ValueOrigin::Class(declaration.class.span.lo.0)),
            ));
        } else if let Some(key) = self.current {
            self.callables
                .get_mut(&key)
                .unwrap()
                .local_assignments
                .push((
                    declaration.ident.to_id(),
                    ValueExpr::Origin(ValueOrigin::Class(declaration.class.span.lo.0)),
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
        if matches!(
            expression.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ) {
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
        if matches!(
            expression.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ) {
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
    fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_stmt(&mut self, node: &ForStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        self.visit_may_skip(node);
    }
    fn visit_try_stmt(&mut self, node: &TryStmt) {
        self.visit_may_skip(node);
    }
    fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
        self.visit_may_skip(node);
    }

    fn visit_function(&mut self, function: &Function) {
        if function.is_async {
            self.async_callables.insert(function.span.lo.0);
        }
        if !function.params.is_empty()
            && function
                .params
                .iter()
                .all(|param| matches!(param.pat, Pat::Ident(_)) && param.decorators.is_empty())
            && !function.is_async
            && !function.is_generator
            && function.decorators.is_empty()
            && let Some(returned) = function
                .body
                .as_ref()
                .and_then(sole_return_expression)
                .and_then(returned_function_span)
        {
            self.returned_factory_candidates
                .insert(function.span.lo.0, returned);
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
        self.with_callable(function.span.lo.0, params, |index| {
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .may_fall_through = !function
                .body
                .as_ref()
                .is_some_and(|body| matches!(body.stmts.last(), Some(Stmt::Return(_))));
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .generator = function.is_generator;
            for param in &function.params {
                param.pat.visit_with(index);
            }
            if let Some(body) = &function.body {
                body.visit_with(index);
            }
        });
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        if arrow.is_async {
            self.async_callables.insert(arrow.span.lo.0);
        }
        let returned = match arrow.body.as_ref() {
            ArrowFunctionBody::Expr(expr) => returned_function_span(expr),
            ArrowFunctionBody::FunctionBody(body) => {
                sole_return_expression(body).and_then(returned_function_span)
            }
        };
        if !arrow.is_async
            && !arrow.params.is_empty()
            && arrow
                .params
                .iter()
                .all(|param| matches!(param, Pat::Ident(_)))
            && let Some(returned) = returned
        {
            self.returned_factory_candidates
                .insert(arrow.span.lo.0, returned);
        }
        let params = arrow.params.iter().map(Parameter::from_pattern).collect();
        self.with_callable(arrow.span.lo.0, params, |index| {
            index
                .callables
                .get_mut(&arrow.span.lo.0)
                .unwrap()
                .may_fall_through = match arrow.body.as_ref() {
                ArrowFunctionBody::Expr(_) => false,
                ArrowFunctionBody::FunctionBody(body) => {
                    !matches!(body.stmts.last(), Some(Stmt::Return(_)))
                }
            };
            arrow.visit_children_with(index);
            if let ArrowFunctionBody::Expr(expr) = arrow.body.as_ref() {
                index
                    .callables
                    .get_mut(&arrow.span.lo.0)
                    .unwrap()
                    .returns
                    .push(ValueExpr::from_expr(expr));
            }
        });
    }

    fn visit_ident(&mut self, ident: &Ident) {
        self.namespace_privacy.record_ident(ident);
        let id = ident.to_id();
        if self.class_ids.contains(&id) && self.safe_class_target.as_ref() != Some(&id) {
            self.other_class_uses.insert(id);
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
            self.record_call(Call {
                kind: CallKind::LocalMemberRead,
                callee: ValueExpr::Member(
                    Box::new(ValueExpr::from_expr(&member.obj)),
                    static_prop(&member.prop),
                    matches!(member.prop, MemberProp::Ident(_)),
                ),
                args: Vec::new(),
                span: member.span,
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

    fn visit_binding_ident(&mut self, binding: &BindingIdent) {
        self.namespace_privacy.record_ident(&binding.id);
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if let Some(key) = self.current
            && let Pat::Ident(binding) = &declarator.name
        {
            self.callables
                .get_mut(&key)
                .unwrap()
                .declared_locals
                .insert(binding.id.to_id());
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
                if let AssignTarget::Simple(SimpleAssignTarget::Ident(receiver)) = &assignment.left
                    && let Expr::Call(call) = assignment.right.unwrap_parens()
                    && let Callee::Expr(callee) = &call.callee
                    && let Expr::Ident(callee) = callee.unwrap_parens()
                    && callee.sym == "Error"
                {
                    fresh = Some((receiver.id.to_id(), callee.to_id()));
                    continues = true;
                } else if let Some((receiver, callee)) = &fresh
                    && let AssignTarget::Simple(SimpleAssignTarget::Member(member)) =
                        &assignment.left
                    && matches!(member.obj.unwrap_parens(), Expr::Ident(id) if id.to_id() == *receiver)
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
        if let Some(source) = &self.destructuring_source {
            let receiver = match pattern {
                Pat::Expr(expression) => Some(match expression.unwrap_parens() {
                    Expr::Member(member) => {
                        self.member_write_sites.insert(member.span.into());
                        self.intrinsic_mutation_receivers
                            .push(intrinsic_write_target(member));
                        ValueExpr::from_expr(&member.obj)
                    }
                    _ => ValueExpr::Unknown,
                }),
                Pat::Invalid(_) => Some(ValueExpr::Unknown),
                _ => None,
            };
            if let Some(receiver) = receiver {
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
            assignment.visit_children_with(self);
            return;
        };
        self.destructuring_source = Some(ValueExpr::Alternatives(vec![
            source.clone(),
            ValueExpr::from_expr(&assignment.right),
        ]));
        self.namespace_privacy.target_depth += 1;
        assignment.left.visit_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.destructuring_source = Some(source);
        assignment.right.visit_with(self);
    }

    fn visit_for_head(&mut self, head: &ForHead) {
        let previous_source = self.destructuring_source.take();
        if matches!(head, ForHead::Pat(_)) {
            self.destructuring_source = Some(ValueExpr::Unknown);
        }
        self.namespace_privacy.target_depth += 1;
        head.visit_children_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.destructuring_source = previous_source;
    }

    fn visit_simple_assign_target(&mut self, target: &SimpleAssignTarget) {
        if let SimpleAssignTarget::Member(member) = target {
            self.member_write_sites.insert(member.span.into());
        }
        target.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, assignment: &AssignExpr) {
        if self.global_guards_requested {
            self.global_alias_written |= match &assignment.left {
                AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                    self.global_object_names.contains(binding.id.sym.as_ref())
                }
                AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                    is_global_receiver(&member.obj, &self.global_object_names)
                        && static_prop(&member.prop).is_some_and(|property| {
                            matches!(
                                property.as_str(),
                                "globalThis" | "window" | "self" | "frames" | "top"
                            )
                        })
                }
                _ => false,
            };
        }
        if matches!(&assignment.left,
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding))
                if binding.id.sym == "Map")
        {
            self.reassigned_map = true;
        }
        if let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assignment.left {
            self.intrinsic_mutation_receivers
                .push(intrinsic_write_target(member));
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
        assignment.right.visit_with(self);
        if matches!(
            assignment.op,
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            self.past_await = before_assignment;
        }
        if !matches!(
            assignment.op,
            AssignOp::Assign | AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            let left = match &assignment.left {
                AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                    ValueExpr::Binding(binding.id.to_id())
                }
                AssignTarget::Simple(SimpleAssignTarget::Member(member)) => ValueExpr::Member(
                    Box::new(ValueExpr::from_expr(&member.obj)),
                    static_prop(&member.prop),
                    matches!(member.prop, MemberProp::Ident(_)),
                ),
                _ => ValueExpr::Unknown,
            };
            self.record_implicit_coercion_value(left, assignment.span);
            self.record_implicit_coercion(&assignment.right, assignment.span);
        }
        if let (Some(key), AssignTarget::Simple(SimpleAssignTarget::Ident(binding))) =
            (self.current, &assignment.left)
        {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.rebinds.insert(binding.id.to_id());
            callable
                .local_assignments
                .push((binding.id.to_id(), ValueExpr::from_expr(&assignment.right)));
        } else if let AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) = &assignment.left {
            self.top_assignments[self.owner.0]
                .push((binding.id.to_id(), ValueExpr::from_expr(&assignment.right)));
            if self.conditional_depth == 0 {
                self.top_definite_initializers[self.owner.0].insert(binding.id.to_id());
            }
        } else if let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assignment.left {
            let write = CallbackResultWrite {
                receiver: ValueExpr::from_expr(&member.obj),
                property_key: match &member.prop {
                    MemberProp::Computed(key) => ValueExpr::from_expr(&key.expr),
                    _ => ValueExpr::NonCallable,
                },
                simple_assignment: assignment.op == AssignOp::Assign,
                data_read_operands: data_read_operand(&assignment.right)
                    && match &member.prop {
                        MemberProp::Computed(key) => data_read_operand(&key.expr),
                        _ => true,
                    },
                property: static_prop(&member.prop),
                value: ValueExpr::from_expr(&assignment.right),
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
            if let Expr::Ident(receiver) = member.obj.unwrap_parens()
                && let Some(key) = static_prop(&member.prop)
            {
                let write = (
                    receiver.to_id(),
                    Some(key),
                    ValueExpr::from_expr(&assignment.right),
                );
                if let Some(callable) = self.current {
                    self.callables
                        .get_mut(&callable)
                        .unwrap()
                        .member_value_writes
                        .push(write);
                } else {
                    self.top_member_value_writes[self.owner.0].push(write);
                }
            } else if let Expr::Ident(receiver) = member.obj.unwrap_parens() {
                let write = (
                    receiver.to_id(),
                    None,
                    ValueExpr::from_expr(&assignment.right),
                );
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
        if let Expr::Member(member) = expression.arg.unwrap_parens() {
            self.member_write_sites.insert(member.span.into());
        }

        if matches!(expression.arg.unwrap_parens(), Expr::Ident(id) if matches!(id.sym.as_ref(), "Error" | "Object" | "Array"))
        {
            self.intrinsic_mutation_receivers
                .push(ValueExpr::from_expr(&expression.arg));
        }

        if matches!(expression.arg.unwrap_parens(), Expr::Ident(ident) if ident.sym == "Map") {
            self.reassigned_map = true;
        }
        self.namespace_privacy.target_depth += 1;
        expression.visit_children_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.record_implicit_coercion(&expression.arg, expression.span);
        if let Expr::Member(member) = expression.arg.unwrap_parens() {
            self.intrinsic_mutation_receivers
                .push(intrinsic_write_target(member));
            self.mark_dynamic_property_effect(Some(&member.obj));
        }
        self.mark_opaque();
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
            self.intrinsic_mutation_receivers
                .push(intrinsic_write_target(member));
            self.mark_dynamic_property_effect(Some(&member.obj));
        }
    }

    fn visit_return_stmt(&mut self, ret: &ReturnStmt) {
        ret.visit_children_with(self);
        if ret.arg.is_none()
            && let Some(key) = self.current
        {
            self.callables
                .get_mut(&key)
                .unwrap()
                .returns
                .push(ValueExpr::Undefined);
        }
        if let (Some(key), Some(arg)) = (self.current, &ret.arg) {
            if let ValueExpr::Binding(id) = ValueExpr::from_expr(arg) {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .escaped_bindings
                    .insert(id);
            }
            self.callables
                .get_mut(&key)
                .unwrap()
                .returns
                .push(ValueExpr::from_expr(arg));
        }
    }

    fn visit_throw_stmt(&mut self, thrown: &ThrowStmt) {
        thrown.visit_children_with(self);
        self.mark_possible_namespace_escape(&thrown.arg);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if self.current.is_none() {
            self.ordinary_top_call_sites.insert(call.span.into());
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
        {
            self.direct_call_members.insert(member.span.into());
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && let MemberProp::Ident(property) = &member.prop
            && call.args.len() == 1
            && call.args[0].spread.is_none()
        {
            let method = match property.sym.as_ref() {
                "forEach" => Some(ArrayIterationMethod::ForEach),
                "filter" => Some(ArrayIterationMethod::Filter),
                "map" => Some(ArrayIterationMethod::Map),
                _ => None,
            };
            if let Some(method) = method {
                self.array_iterations.insert(
                    call.span.into(),
                    ArrayIteration {
                        method,
                        source: ValueExpr::from_expr(&member.obj),
                        callback: ValueExpr::from_expr(&call.args[0].expr),
                        result: None,
                    },
                );
            }
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
            let target = if matches!(
                method.as_deref(),
                Some("set" | "defineProperty" | "deleteProperty")
            ) {
                let property =
                    call.args
                        .get(1)
                        .and_then(|argument| match argument.expr.unwrap_parens() {
                            Expr::Lit(Lit::Str(value)) => {
                                Some(value.value.to_string_lossy().into_owned())
                            }
                            _ => None,
                        });
                ValueExpr::Member(Box::new(receiver), property, false)
            } else {
                receiver
            };
            self.intrinsic_mutation_receivers.push(target);
        }

        let callback_argument = self.inline_factory_callback_argument(call);
        let retained_call = self.retained_callback_call(call);
        let no_reentry_call = self.no_reentry_callback_call(call);
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
            self.record_call(Call {
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
                ) || is_static_event_listener_registration(callee, &call.args),
                caller: self.current,
                owner: self.owner,
                plain_object_sources: call.args.len() > 1
                    && call.args.iter().skip(1).all(|arg| {
                        matches!(arg.expr.unwrap_parens(), Expr::Object(object)
                            if object.props.iter().all(plain_assign_source_property))
                    }),
            });
        } else {
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
        chain.visit_children_with(self);
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
/// An imported one-argument call supplies only the first parameter. A returned
/// function can therefore retain that callback and omitted parameters, but it
/// must not capture another source/host capability that the abstract returned
/// callback graph would omit when the result is invoked later.
fn returned_callback_factory(index: &ExecutionIndex, key: u32) -> bool {
    let Some(returned) = index.returned_factory_candidates.get(&key) else {
        return false;
    };
    let (Some(outer), Some(inner)) = (
        index.callables.get(&key),
        index.callables.get(&returned.lo.0),
    ) else {
        return false;
    };
    let Some(callback) = outer
        .params
        .first()
        .and_then(|param| param.binding.as_ref())
    else {
        return false;
    };
    if outer.opaque
        || outer.generator
        || outer.implicit_capture
        || index.async_callables.contains(&key)
        || outer.may_fall_through
        || outer
            .params
            .iter()
            .any(|param| param.binding.is_none() || param.default.is_some())
        || !outer.calls.is_empty()
        || !outer.reads.is_empty()
        || !outer.rebinds.is_empty()
        || !outer.local_assignments.is_empty()
        || !outer.member_writes.is_empty()
        || !outer.member_value_writes.is_empty()
        || outer.dynamic_property_effect
        || outer.returns.len() != 1
        || !matches!(outer.returns[0], ValueExpr::Origin(ValueOrigin::Function(inner_key)) if inner_key == returned.lo.0)
        || inner.opaque
        || inner.generator
        || inner.implicit_capture
        || index.async_callables.contains(&returned.lo.0)
        || !inner.params.is_empty()
        || inner.dynamic_property_effect
        || !inner.member_writes.is_empty()
        || !inner.member_value_writes.is_empty()
        || !inner.callback_result_writes.is_empty()
        || index
            .callables
            .range((returned.lo.0 + 1)..returned.hi.0)
            .next()
            .is_some()
    {
        return false;
    }
    let allowed: BTreeSet<_> = outer
        .params
        .iter()
        .filter_map(|param| param.binding.clone())
        .chain(inner.declared_locals.iter().cloned())
        .collect();
    let local_receivers: BTreeSet<_> = allowed
        .iter()
        .filter(|id| *id != callback)
        .cloned()
        .collect();
    let has_fresh_own_data_property = |value: &ValueExpr, property: &str| {
        matches!(value, ValueExpr::ObjectLiteral(object, fields)
            if index.plain_data_objects.contains(object)
                && fields.iter().any(|(name, _)| name.as_deref() == Some(property)))
    };
    inner.reads.is_subset(&allowed)
        && inner.rebinds.is_subset(&local_receivers)
        && inner
            .local_assignments
            .iter()
            .all(|(id, _)| local_receivers.contains(id))
        && inner
            .calls
            .iter()
            .all(|call| match (&call.kind, &call.callee) {
                (CallKind::Call, ValueExpr::Binding(id)) => id == callback && !call.has_spread,
                (CallKind::LocalMemberRead, ValueExpr::Member(receiver, Some(property), true)) => {
                    match receiver.as_ref() {
                        ValueExpr::Binding(id) if local_receivers.contains(id) => {
                            inner.local_assignments.iter().any(|(assigned, value)| {
                                assigned == id && has_fresh_own_data_property(value, property)
                            }) && inner
                                .local_assignments
                                .iter()
                                .filter(|(assigned, _)| assigned == id)
                                .all(|(_, value)| has_fresh_own_data_property(value, property))
                        }
                        // ValueExpr reduces an assignment expression to its RHS.
                        // The indexed member receiver is still this fresh literal.
                        value @ ValueExpr::ObjectLiteral(..) => {
                            has_fresh_own_data_property(value, property)
                        }
                        _ => false,
                    }
                }
                _ => false,
            })
}

pub(crate) fn infer_returned_callback_factories(
    body: &[TopLevelItemView<'_>],
    exported_locals: &BTreeSet<String>,
) -> BTreeMap<String, u32> {
    let index = ExecutionIndex::collect(
        body,
        &AnalysisInputs::default(),
        None,
        &BTreeSet::new(),
        &LocalClassConstructionProof::default(),
    );
    if !index.dynamic_scope_owners.is_empty() {
        return BTreeMap::new();
    }
    let mut assignments: BTreeMap<Id, Vec<(SourceUnitOrdinal, &ValueExpr)>> = BTreeMap::new();
    for (ordinal, writes) in index.top_assignments.iter().enumerate() {
        for (id, value) in writes {
            assignments
                .entry(id.clone())
                .or_default()
                .push((SourceUnitOrdinal(ordinal), value));
        }
    }
    let mut candidates: Vec<_> = index
        .hoisted_bindings
        .iter()
        .filter(|(id, key)| {
            exported_locals.contains(id.0.as_ref()) && returned_callback_factory(&index, **key)
        })
        .map(|(id, key)| (id.clone(), *key))
        .collect();
    candidates.extend(assignments.into_iter().filter_map(|(id, writes)| {
        let [(owner, ValueExpr::Origin(ValueOrigin::Function(key)))] = writes.as_slice() else {
            return None;
        };
        (exported_locals.contains(id.0.as_ref())
            && index.top_definite_initializers[owner.0].contains(&id)
            && returned_callback_factory(&index, *key))
        .then_some((id, *key))
    }));
    if candidates.is_empty() {
        return BTreeMap::new();
    }
    let flow = ChunkValueFlow::analyze(body);
    candidates
        .into_iter()
        .filter_map(|(id, key)| {
            let values = flow.values_of_binding(&id);
            values
                .iter()
                .eq([ValueOrigin::Function(key)])
                .then(|| (id.0.to_string(), key))
        })
        .collect()
}
pub(crate) fn stable_ts_enum_named_data_members(
    graph: &ChunkCodeGraph,
    facts: &[StatementFacts],
) -> BTreeMap<Id, BTreeSet<String>> {
    let candidates = graph.ts_enum_named_data_candidates();
    if candidates.is_empty() {
        return BTreeMap::new();
    }
    let mut unstable = BTreeSet::new();
    for fact in facts {
        for binding in fact.rebinds.eager.iter().chain(&fact.rebinds.lazy) {
            if candidates
                .get(binding)
                .is_some_and(|candidate| candidate.initializer_source_unit != fact.ordinal)
            {
                unstable.insert(binding.clone());
            }
        }
    }
    candidates
        .iter()
        .filter(|(binding, _)| !unstable.contains(*binding))
        .map(|(binding, candidate)| (binding.clone(), candidate.own_primitive_keys.clone()))
        .collect()
}

#[path = "interpreter.rs"]
pub(crate) mod interpreter;

#[path = "native_calls.rs"]
mod native_calls;
