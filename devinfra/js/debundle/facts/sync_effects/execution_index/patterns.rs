//! Destructuring-pattern analysis helpers.

use std::sync::Arc;
use swc_ecma_ast::*;
use value_model::{MemberKey, ValueExpr, static_property_name};

pub(super) fn collect_object_pattern_assignments(
    object: &ObjectPat,
    value: &ValueExpr,
    assignments: &mut Vec<(Id, ValueExpr)>,
) {
    for property in &object.props {
        match property {
            ObjectPatProp::KeyValue(property) => {
                let projected =
                    static_property_name(&property.key).map_or(ValueExpr::Unknown, |name| {
                        ValueExpr::Member(
                            Arc::new(value.clone()),
                            Some(name),
                            MemberKey::Computed(None),
                            swc_common::DUMMY_SP.into(),
                        )
                    });
                collect_pattern_assignments(&property.value, &projected, assignments);
            }
            ObjectPatProp::Assign(property) => {
                let projected = ValueExpr::Member(
                    Arc::new(value.clone()),
                    Some(property.key.id.sym.to_string()),
                    MemberKey::Computed(None),
                    swc_common::DUMMY_SP.into(),
                );
                let projected = property
                    .value
                    .as_deref()
                    .map_or(projected.clone(), |default| {
                        ValueExpr::Alternatives([projected, ValueExpr::from_expr(default)].into())
                    });
                assignments.push((property.key.id.to_id(), projected));
            }
            ObjectPatProp::Rest(rest) => {
                collect_pattern_assignments(&rest.arg, &ValueExpr::Unknown, assignments);
            }
        }
    }
}

pub(super) fn collect_pattern_assignments(
    pattern: &Pat,
    value: &ValueExpr,
    assignments: &mut Vec<(Id, ValueExpr)>,
) {
    match pattern {
        Pat::Ident(binding) => assignments.push((binding.id.to_id(), value.clone())),
        Pat::Assign(assign) => {
            let value = ValueExpr::Alternatives(
                [value.clone(), ValueExpr::from_expr(&assign.right)].into(),
            );
            collect_pattern_assignments(&assign.left, &value, assignments);
        }
        Pat::Rest(rest) => {
            collect_pattern_assignments(&rest.arg, &ValueExpr::Unknown, assignments);
        }
        Pat::Object(object) => collect_object_pattern_assignments(object, value, assignments),
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

pub(super) fn collect_assign_target_assignments(
    target: &AssignTargetPat,
    value: &ValueExpr,
    assignments: &mut Vec<(Id, ValueExpr)>,
) {
    match target {
        AssignTargetPat::Object(object) => {
            collect_object_pattern_assignments(object, value, assignments);
        }
        AssignTargetPat::Array(array) => {
            for element in array.elems.iter().flatten() {
                collect_pattern_assignments(element, &ValueExpr::Unknown, assignments);
            }
        }
        AssignTargetPat::Invalid(_) => {}
    }
}

/// The binding a pattern parameter's projections read its incoming value
/// from. No source identifier can spell it.
pub(in super::super) fn pattern_source() -> Id {
    ("\u{0}pattern_source".into(), Default::default())
}

/// Whether destructuring `pattern` gives some binding a part of the value
/// only as unknown: an array element, a rest, or a computed key.
pub(super) fn pattern_loses_values(pattern: &Pat) -> bool {
    match pattern {
        Pat::Ident(_) => false,
        Pat::Assign(assign) => pattern_loses_values(&assign.left),
        Pat::Object(object) => object.props.iter().any(|property| match property {
            ObjectPatProp::KeyValue(property) => {
                static_property_name(&property.key).is_none()
                    || pattern_loses_values(&property.value)
            }
            ObjectPatProp::Assign(_) => false,
            ObjectPatProp::Rest(_) => true,
        }),
        Pat::Array(_) | Pat::Rest(_) | Pat::Expr(_) | Pat::Invalid(_) => true,
    }
}

/// Whether destructuring `pattern` runs the iterator protocol on the value
/// or on a part of it.
pub(super) fn pattern_iterates(pattern: &Pat) -> bool {
    match pattern {
        Pat::Ident(_) | Pat::Expr(_) | Pat::Invalid(_) => false,
        Pat::Assign(assign) => pattern_iterates(&assign.left),
        Pat::Rest(rest) => pattern_iterates(&rest.arg),
        Pat::Object(object) => object.props.iter().any(|property| match property {
            ObjectPatProp::KeyValue(property) => pattern_iterates(&property.value),
            ObjectPatProp::Assign(_) => false,
            ObjectPatProp::Rest(rest) => pattern_iterates(&rest.arg),
        }),
        Pat::Array(_) => true,
    }
}

/// Whether destructuring `pattern` iterates a part of the value, or a
/// default, rather than the value itself: an array pattern below the root.
pub(super) fn pattern_iterates_part(pattern: &Pat) -> bool {
    match pattern {
        Pat::Array(array) => array.elems.iter().flatten().any(pattern_iterates),
        Pat::Assign(assign) => pattern_iterates_part(&assign.left),
        Pat::Object(_) | Pat::Rest(_) => pattern_iterates(pattern),
        Pat::Ident(_) | Pat::Expr(_) | Pat::Invalid(_) => false,
    }
}

/// The defaults below the root of `pattern` that supply a value an array
/// pattern iterates: `[[x] = mk()]`, `{ a: [x] = mk() }`.
pub(super) fn iterated_defaults<'a>(pattern: &'a Pat, defaults: &mut Vec<&'a Expr>) {
    match pattern {
        Pat::Array(array) => {
            for element in array.elems.iter().flatten() {
                collect_iterated_default(element, defaults);
            }
        }
        Pat::Object(object) => {
            for property in &object.props {
                match property {
                    ObjectPatProp::KeyValue(property) => {
                        collect_iterated_default(&property.value, defaults)
                    }
                    ObjectPatProp::Rest(rest) => collect_iterated_default(&rest.arg, defaults),
                    ObjectPatProp::Assign(_) => {}
                }
            }
        }
        Pat::Assign(assign) => iterated_defaults(&assign.left, defaults),
        Pat::Rest(rest) => iterated_defaults(&rest.arg, defaults),
        Pat::Ident(_) | Pat::Expr(_) | Pat::Invalid(_) => {}
    }
}

pub(super) fn collect_iterated_default<'a>(pattern: &'a Pat, defaults: &mut Vec<&'a Expr>) {
    if let Pat::Assign(assign) = pattern
        && pattern_iterates(&assign.left)
    {
        defaults.push(&assign.right);
    }
    iterated_defaults(pattern, defaults);
}
