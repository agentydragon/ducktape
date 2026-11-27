//! Records object-literal shapes, property kinds and descriptor facts while traversing the literal.

use super::{Call, CallKind, ExecutionIndex};
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Spanned;
use swc_ecma_ast::*;
use swc_ecma_visit::VisitWith;
use value_model::local_operations::DeferredCallableCreation;
use value_model::{
    LiteralPropertyKind, OwnPropertyDescriptorSyntax, OwnPropertyShape, ValueExpr,
    object_data_property_name, static_property_name,
};

impl ExecutionIndex {
    pub(super) fn index_object_lit(&mut self, object: &ObjectLit) {
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
        self.record_literal_definitions(object);
        self.record_descriptor_shape(object);
        self.record_static_data(object);
        self.literal_depth += 1;
        for property in &object.props {
            match property {
                PropOrSpread::Spread(spread) => {
                    spread.expr.visit_with(self);
                    self.record_call(Call::without_argument_facts(
                        CallKind::ObjectSpread,
                        ValueExpr::Unknown,
                        vec![ValueExpr::from_expr(&spread.expr)],
                        spread.expr.span(),
                        self.current,
                        self.owner,
                    ));
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

    fn record_literal_definitions(&mut self, object: &ObjectLit) {
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
    }

    fn record_descriptor_shape(&mut self, object: &ObjectLit) {
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
    }

    fn record_static_data(&mut self, object: &ObjectLit) {
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
    }
}
