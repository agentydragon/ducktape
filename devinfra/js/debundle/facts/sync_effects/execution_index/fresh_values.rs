//! Fresh-value and record-factory proofs: syntax whose evaluation allocates only fresh values.

use analysis_vocabulary::report_schema::RecordFactoryRejectionReason;
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::*;
use value_model::{
    ClassSurface, FromEntriesMappedProof, MemberKey, NamespaceValueFacts, ValueExpr,
    object_data_property_name, static_prop, static_property_name,
};

pub(super) fn map_entry_array_span(new_expr: &NewExpr) -> Option<u32> {
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

pub(super) fn from_entries_mapped_proof(
    call: &CallExpr,
    plain_array_bindings: &BTreeSet<String>,
) -> Option<FromEntriesMappedProof> {
    let Some(Expr::Member(from_entries)) = js_ast::callee_expr(call) else {
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

pub(super) enum FreshValueStep {
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

pub(super) enum FreshValue {
    Instance(u32),
    Factory,
    Literal(BTreeSet<String>),
}

pub(super) fn fresh_literal_own_fields(object: &ObjectLit) -> Option<BTreeSet<String>> {
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

pub(super) fn primitive_constant(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Lit(Lit::Str(_) | Lit::Bool(_) | Lit::Null(_) | Lit::Num(_) | Lit::BigInt(_)) => true,
        Expr::Unary(unary) if unary.op != UnaryOp::Delete => primitive_constant(&unary.arg),
        Expr::Bin(binary) if !matches!(binary.op, BinaryOp::In | BinaryOp::InstanceOf) => {
            primitive_constant(&binary.left) && primitive_constant(&binary.right)
        }
        _ => false,
    }
}

#[derive(Clone, Copy)]
pub(super) enum LiteralRecordPrimitive {
    BigInt,
    Other,
}

pub(super) fn literal_record_primitive(expression: &Expr) -> Option<LiteralRecordPrimitive> {
    match expression.unwrap_parens() {
        Expr::Lit(Lit::BigInt(_)) => Some(LiteralRecordPrimitive::BigInt),
        Expr::Lit(Lit::Str(_) | Lit::Bool(_) | Lit::Null(_) | Lit::Num(_)) => {
            Some(LiteralRecordPrimitive::Other)
        }
        Expr::Unary(unary) => {
            let operand = literal_record_primitive(&unary.arg)?;
            match unary.op {
                UnaryOp::Delete => None,
                UnaryOp::Plus if matches!(operand, LiteralRecordPrimitive::BigInt) => None,
                UnaryOp::Minus | UnaryOp::Tilde => Some(operand),
                UnaryOp::Bang | UnaryOp::Void | UnaryOp::TypeOf | UnaryOp::Plus => {
                    Some(LiteralRecordPrimitive::Other)
                }
            }
        }
        _ => None,
    }
}

/// Syntax of a direct annotated record-factory call's single argument. Binding
/// reads, member reads and template substitutions are only candidates here:
/// the interpreter proves their values at the call before consulting the
/// author premise.
pub(crate) type RecordFactoryArgument =
    Result<RecordFactoryRecord, (RecordFactoryRejectionReason, Option<String>)>;

#[derive(Debug, Clone, Default)]
pub struct RecordFactoryRecord {
    /// Template substitutions, by outermost record property, whose `ToString`
    /// must see a proven primitive.
    pub template_substitutions: Vec<(String, ValueExpr)>,
}

pub(super) fn record_factory_argument(arguments: &[ExprOrSpread]) -> RecordFactoryArgument {
    let [argument] = arguments else {
        return Err((RecordFactoryRejectionReason::NotObjectLiteral, None));
    };
    if argument.spread.is_some() || !matches!(argument.expr.unwrap_parens(), Expr::Object(_)) {
        return Err((RecordFactoryRejectionReason::NotObjectLiteral, None));
    }
    let mut record = RecordFactoryRecord::default();
    record_factory_value(&argument.expr, None, &mut record)?;
    Ok(record)
}

pub(super) fn record_factory_value(
    expression: &Expr,
    property: Option<&str>,
    record: &mut RecordFactoryRecord,
) -> Result<(), (RecordFactoryRejectionReason, Option<String>)> {
    let reject = |reason, name: Option<&str>| Err((reason, property.or(name).map(str::to_owned)));
    let expression = expression.unwrap_parens();
    if literal_record_primitive(expression).is_some() {
        return Ok(());
    }
    match expression {
        Expr::Ident(_) => Ok(()),
        Expr::Object(object) => {
            let mut names = BTreeSet::new();
            for entry in &object.props {
                let PropOrSpread::Prop(entry) = entry else {
                    return reject(RecordFactoryRejectionReason::Spread, None);
                };
                let (key, value) = match entry.as_ref() {
                    Prop::KeyValue(pair) => (&pair.key, Some(pair.value.as_ref())),
                    // Defining a method allocates a function; no code runs.
                    Prop::Method(method) => (&method.key, None),
                    Prop::Shorthand(binding) => {
                        let name = binding.sym.to_string();
                        if name == "__proto__" {
                            return reject(RecordFactoryRejectionReason::PrototypeKey, None);
                        }
                        if !names.insert(name.clone()) {
                            return reject(RecordFactoryRejectionReason::DuplicateKey, Some(&name));
                        }
                        continue;
                    }
                    Prop::Getter(_) | Prop::Setter(_) => {
                        return reject(RecordFactoryRejectionReason::Accessor, None);
                    }
                    Prop::Assign(_) => {
                        return reject(RecordFactoryRejectionReason::UnsupportedValue, None);
                    }
                };
                if matches!(key, PropName::Computed(_)) {
                    return reject(RecordFactoryRejectionReason::ComputedKey, None);
                }
                let Some(name) = object_data_property_name(key) else {
                    let reason = if static_property_name(key).is_some() {
                        RecordFactoryRejectionReason::PrototypeKey
                    } else {
                        RecordFactoryRejectionReason::ComputedKey
                    };
                    return reject(reason, None);
                };
                if !names.insert(name.clone()) {
                    return reject(RecordFactoryRejectionReason::DuplicateKey, Some(&name));
                }
                if let Some(value) = value {
                    record_factory_value(value, Some(property.unwrap_or(&name)), record)?;
                }
            }
            Ok(())
        }
        Expr::Array(array) => {
            for element in &array.elems {
                match element {
                    Some(element) if element.spread.is_none() => {
                        record_factory_value(&element.expr, property, record)?;
                    }
                    _ => return reject(RecordFactoryRejectionReason::ArrayShape, None),
                }
            }
            Ok(())
        }
        Expr::Arrow(_) => Ok(()),
        Expr::Fn(function)
            if function.function.decorators.is_empty()
                && function
                    .function
                    .params
                    .iter()
                    .all(|parameter| parameter.decorators.is_empty()) =>
        {
            Ok(())
        }
        Expr::Member(member)
            if matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                && static_prop(&member.prop).is_some() =>
        {
            Ok(())
        }
        Expr::Member(_) => reject(RecordFactoryRejectionReason::UnprovenMemberRead, None),
        Expr::Tpl(template) => {
            for substitution in &template.exprs {
                let substitution = substitution.unwrap_parens();
                if literal_record_primitive(substitution).is_some() {
                    continue;
                }
                let candidate = matches!(substitution, Expr::Ident(_))
                    || matches!(substitution, Expr::Member(member)
                        if matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                            && static_prop(&member.prop).is_some());
                if !candidate {
                    return reject(RecordFactoryRejectionReason::TemplateSubstitution, None);
                }
                record.template_substitutions.push((
                    property.unwrap_or_default().to_owned(),
                    ValueExpr::from_expr(substitution),
                ));
            }
            Ok(())
        }
        _ => reject(RecordFactoryRejectionReason::UnsupportedValue, None),
    }
}

pub(super) fn value_without_user_code(expression: &Expr) -> bool {
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

pub(super) fn fresh_value_step(item: &ModuleItem) -> Option<FreshValueStep> {
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
            let Some(binding_targets::SimpleWriteTarget::Member(member)) =
                binding_targets::simple_write_target(&assignment.left)
            else {
                return None;
            };
            let Expr::Ident(receiver) = binding_targets::transparent_target_expr(&member.obj)
            else {
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

pub(super) fn fresh_value_writes(
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
                    ValueExpr::Member(object, Some(property), MemberKey::Static, _) => {
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
