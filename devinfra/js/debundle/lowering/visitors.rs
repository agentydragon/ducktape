//! AST helpers shared by the lowering pipeline's plan-aware
//! visitor (see `lowering_execute.rs`).
//!
//! The legacy `IdentifierRenamer`, `RenameAndShorthandNaturalizer`,
//! and `ShorthandNaturalizer` visitors retired with the executor
//! migration; the object-shorthand collapse helpers below survive
//! as re-usable functions called by `PlanRenameVisitor`.

use super::*;

pub(super) fn naturalize_object_pattern_shorthand(object: &mut ObjectPat) {
    for prop in &mut object.props {
        if let ObjectPatProp::KeyValue(key_value) = prop
            && let PropName::Ident(key) = &key_value.key
            && let Pat::Ident(value) = &*key_value.value
            && key.sym == value.id.sym
        {
            *prop = ObjectPatProp::Assign(AssignPatProp {
                span: DUMMY_SP,
                key: value.clone(),
                value: None,
            });
        }
    }
}

pub(super) fn naturalize_object_literal_shorthand(object: &mut ObjectLit) {
    for prop in &mut object.props {
        if let PropOrSpread::Prop(prop_box) = prop
            && let Prop::KeyValue(key_value) = &**prop_box
            && let PropName::Ident(key) = &key_value.key
            && let Expr::Ident(value) = &*key_value.value
            && key.sym == value.sym
        {
            *prop = PropOrSpread::Prop(Box::new(Prop::Shorthand(value.clone())));
        }
    }
}
