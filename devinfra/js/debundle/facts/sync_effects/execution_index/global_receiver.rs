//! Global-receiver and global-mutation path checks.

use super::{Call, CallKind};
use source_identity::SourceUnitOrdinal;
use spec::BuiltinGlobalRoot;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};
use value_model::{ValueExpr, static_prop};

pub(super) fn builtin_global_root(ident: &Ident) -> Option<BuiltinGlobalRoot> {
    match ident.sym.as_ref() {
        "globalThis" => Some(BuiltinGlobalRoot::GlobalThis),
        "window" => Some(BuiltinGlobalRoot::Window),
        _ => None,
    }
}

pub(super) fn is_global_receiver(expression: &Expr, names: &BTreeSet<&str>) -> bool {
    match binding_targets::transparent_target_expr(expression) {
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

pub(super) fn exposes_global_receiver(value: &ValueExpr, names: &BTreeSet<&str>) -> bool {
    match value {
        ValueExpr::Binding(id) => names.contains(id.0.as_ref()),
        ValueExpr::Member(receiver, property, _, _) => {
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
        ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
            value
                .branching_alternatives()
                .iter()
                .any(|value| exposes_global_receiver(value, names))
        }
        ValueExpr::ObjectLiteral(_, properties) => properties
            .iter()
            .any(|(_, value)| exposes_global_receiver(value, names)),
        ValueExpr::Call(_, args, _) | ValueExpr::New(_, args, _) => args
            .iter()
            .any(|value| exposes_global_receiver(value, names)),
        // Operators can inspect or coerce their operands, but their result
        // cannot be the global object. Calls arising from those operands are
        // checked separately below.
        ValueExpr::Primitive(_)
        | ValueExpr::StrictEquality(..)
        | ValueExpr::LogicalNot(_)
        | ValueExpr::Void(_) => false,
        _ => false,
    }
}

pub(super) fn call_mutates_global_slot(call: &Call, names: &BTreeSet<&str>) -> bool {
    matches!(&call.callee, ValueExpr::Member(receiver, Some(property), _, _)
        if matches!(property.as_str(), "__defineSetter__" | "__defineGetter__")
            && exposes_global_receiver(receiver, names))
}

pub(super) fn call_exposes_global_receiver(
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
            [ValueExpr::Binding(receiver), ValueExpr::NonCallable(_)]
                if names.contains(receiver.0.as_ref())
                    && certified_root_ids.contains(receiver))
    {
        return false;
    }
    call.args
        .iter()
        .any(|value| exposes_global_receiver(value, names))
}

pub(super) struct DestructuringMemberWrite {
    pub(super) receiver: ValueExpr,
    /// The whole source (including defaults) over-approximates the target value.
    pub(super) source: ValueExpr,
}

/// A sufficient source-privacy proof, independent of execution/value-flow facts.
/// Every configured root belongs to one potentially aliasing namespace family.
#[derive(Default)]
pub(super) struct NamespacePrivacy {
    pub(super) members: BTreeMap<Id, BTreeSet<String>>,
    pub(super) import_sites: BTreeSet<u32>,
    pub(super) allowed_read: Option<u32>,
    pub(super) optional_members: BTreeSet<u32>,
    pub(super) target_depth: usize,
    pub(super) invalid: bool,
}

impl NamespacePrivacy {
    pub(super) fn record_ident(&mut self, ident: &Ident) {
        if self.members.contains_key(&ident.to_id())
            && !self.import_sites.contains(&ident.span.lo.0)
            && self.allowed_read != Some(ident.span.lo.0)
        {
            self.invalid = true;
        }
    }

    pub(super) fn read_receiver(&self, member: &MemberExpr) -> Option<u32> {
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
pub(super) struct GlobalMembershipBindings(pub(super) BTreeSet<Id>);

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
pub(super) struct GlobalMembershipProbe {
    pub(super) root: BuiltinGlobalRoot,
    pub(super) receiver: Id,
    pub(super) property: String,
    pub(super) owner: SourceUnitOrdinal,
}

pub(super) fn global_receiver_path(value: &ValueExpr, names: &BTreeSet<&str>) -> bool {
    match value {
        ValueExpr::Binding(id) => names.contains(id.0.as_ref()),
        ValueExpr::Member(receiver, _, _, _) => global_receiver_path(receiver, names),
        _ => false,
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum GlobalMutationPath {
    Unrelated,
    Descendant,
    Identity,
    Prototype,
}

// This classifies syntactic evidence only. A descendant is not proven
// disjoint: its complete alias integrity remains the author's premise.
pub(super) fn global_mutation_path(
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
        ValueExpr::Member(receiver, property, _, _) => {
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

pub(super) fn stored_global_alias(
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
            ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
                value
                    .branching_alternatives()
                    .iter()
                    .any(|value| stored_global_alias(value, names, bindings, include_prototypes))
            }
            ValueExpr::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| stored_global_alias(value, names, bindings, include_prototypes)),
            ValueExpr::Member(receiver, _, _, _) => {
                stored_global_alias(receiver, names, bindings, include_prototypes)
            }
            ValueExpr::Call(_, args, _) | ValueExpr::New(_, args, _) => args
                .iter()
                .any(|value| stored_global_alias(value, names, bindings, include_prototypes)),
            _ => false,
        },
    }
}
