//! Value facts collected from a class body.

use super::fresh_values::value_without_user_code;
use analysis::facts::collector::UnresolvedCallSourceCollector;
use std::collections::BTreeSet;
use swc_ecma_ast::*;
use swc_ecma_visit::VisitWith;
use value_model::{
    ClassValueFacts, DiscardedHeritageCall, LexicalClassHeritage, ValueExpr, static_property_name,
};

pub(super) fn add_class_field(
    facts: &mut ClassValueFacts,
    is_static: bool,
    name: String,
    value: ValueExpr,
) {
    let fields = if is_static {
        &mut facts.static_fields
    } else {
        &mut facts.instance_fields
    };
    if fields.insert(name.clone(), value).is_some() {
        fields.insert(name, ValueExpr::Unknown);
    }
}

pub(super) fn collect_class_value_facts(class: &Class) -> ClassValueFacts {
    let mut heritage_inputs = UnresolvedCallSourceCollector::default();
    if let Some(heritage) = class.super_class.as_deref() {
        heritage.visit_with(&mut heritage_inputs);
    }
    let mut facts = ClassValueFacts {
        super_class: class.super_class.as_deref().map(ValueExpr::from_expr),
        super_class_inputs: heritage_inputs.idents,
        super_class_inputs_incomplete: heritage_inputs.inline_fn,
        discarded_heritage_call: class
            .super_class
            .as_deref()
            .and_then(DiscardedHeritageCall::collect),
        lexical_heritage: class
            .super_class
            .as_deref()
            .and_then(LexicalClassHeritage::collect),
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
