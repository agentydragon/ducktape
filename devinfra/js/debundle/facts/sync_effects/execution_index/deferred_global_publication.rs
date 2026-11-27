//! Deferred global publications and their prepublication aliasing proofs.

use super::global_receiver::{
    GlobalMembershipBindings, GlobalMutationPath, builtin_global_root, global_mutation_path,
    global_receiver_path, stored_global_alias,
};
use super::{CallKind, ExecutionIndex};
use analysis_domain::item_views::TopLevelItemView;
use analysis_vocabulary::inputs::{AnalysisInputs, NativeIntrinsic};
use inference::{FactContext, IterativeInference, IterativeRules};
use source_identity::SourceUnitOrdinal;
use spec::BuiltinGlobalRoot;
use std::collections::BTreeSet;
use swc_ecma_ast::*;
use swc_ecma_visit::VisitWith;
use value_model::ValueExpr;

#[derive(Clone)]
pub struct DeferredGlobalPublication {
    pub(super) root: BuiltinGlobalRoot,
    pub(super) receiver_id: Id,
    pub(super) property: String,
    pub value: ValueExpr,
}

pub(super) fn uses_any_binding(value: &ValueExpr, ids: &BTreeSet<Id>) -> bool {
    let mut bindings = BTreeSet::new();
    value.bindings(&mut bindings);
    !bindings.is_disjoint(ids)
}

pub(super) struct DerivedBindingRules {
    pub(super) dependencies: Vec<(Id, Vec<Id>)>,
}

impl IterativeRules for DerivedBindingRules {
    type Key = Id;
    type Fact = ();
    type Rule = usize;

    type MergeScratch = ();
    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        let (target, sources) = &self.dependencies[*rule];
        if sources
            .iter()
            .any(|source| context.read(source.clone()).is_some())
        {
            context.propose(target.clone(), ());
        }
    }

    fn merge(_: &Self::Key, _: &mut Self::Fact, _: Self::Fact) -> bool {
        false
    }
}

pub(super) fn close_prepublication_derived_bindings(
    alias: &Id,
    assignments: &[(Id, ValueExpr)],
) -> BTreeSet<Id> {
    let dependencies = assignments
        .iter()
        .map(|(target, value)| {
            let mut sources = BTreeSet::new();
            value.bindings(&mut sources);
            (target.clone(), sources.into_iter().collect())
        })
        .collect::<Vec<_>>();
    let mut inference = IterativeInference::new(DerivedBindingRules { dependencies });
    inference.seed(alias.clone(), ());
    inference
        .solve(0..assignments.len())
        .into_values()
        .into_keys()
        .collect()
}

pub(super) fn same_property_or_fresh(value: &ValueExpr, alias: &Id, property: &str) -> bool {
    match value {
        ValueExpr::Member(receiver, Some(read), _, _)
            if read == property
                && matches!(receiver.as_ref(), ValueExpr::Binding(id) if id == alias) =>
        {
            true
        }
        ValueExpr::ObjectLiteral(_, properties) => properties.is_empty(),
        ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
            value
                .branching_alternatives()
                .iter()
                .all(|value| same_property_or_fresh(value, alias, property))
        }
        ValueExpr::NonCallable(_)
        | ValueExpr::Undefined
        | ValueExpr::Primitive(_)
        | ValueExpr::StrictEquality(..)
        | ValueExpr::LogicalNot(_)
        | ValueExpr::Void(_) => true,
        _ => false,
    }
}

pub(super) fn confined_prepublication_alias(
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
        || !callable.body_facts.value_flow().returns().is_empty()
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
    let derived = close_prepublication_derived_bindings(alias, &callable.local_assignments);
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
                ValueExpr::Member(receiver, Some(property), _, _)
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
                ValueExpr::NonCallable(_)
                    | ValueExpr::Undefined
                    | ValueExpr::Primitive(_)
                    | ValueExpr::StrictEquality(..)
                    | ValueExpr::LogicalNot(_)
                    | ValueExpr::Void(_)
            )
        } else {
            !uses_any_binding(&write.value, &derived)
        }
    })
}

pub(super) fn deferred_global_assignment(expression: &Expr) -> Option<DeferredGlobalPublication> {
    let Expr::Assign(assignment) = expression.unwrap_parens() else {
        return None;
    };
    let Some(binding_targets::SimpleWriteTarget::Member(member)) =
        binding_targets::simple_write_target(&assignment.left)
    else {
        return None;
    };
    let (Expr::Ident(receiver), MemberProp::Ident(property)) = (
        binding_targets::transparent_target_expr(&member.obj),
        &member.prop,
    ) else {
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

pub(super) fn typeof_window_guard(expression: &Expr) -> bool {
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

pub(super) fn deferred_global_publication(item: &ModuleItem) -> Option<DeferredGlobalPublication> {
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

pub(super) fn discarded_synchronous_iife(item: &ModuleItem) -> Option<u32> {
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
    let Some(Expr::Fn(function)) = js_ast::callee_expr(call) else {
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

impl ExecutionIndex {
    pub(super) fn admit_deferred_global_publications(&mut self, hints: &AnalysisInputs) {
        if self.deferred_global_requested {
            let first_publication = self
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
            let certified_root_ids: BTreeSet<Id> = self
                .deferred_global_candidates
                .iter()
                .flatten()
                .filter(|candidate| {
                    let name = match candidate.root {
                        BuiltinGlobalRoot::GlobalThis => "globalThis",
                        BuiltinGlobalRoot::Window => "window",
                    };
                    self.global_object_names.contains(name)
                        && hints
                            .assumptions
                            .deferred_global_publications
                            .get(&candidate.root)
                            .is_some_and(|slots| slots.contains(&candidate.property))
                })
                .map(|candidate| candidate.receiver_id.clone())
                .collect();
            let global_root_exposed =
                self.global_receiver_exposed(first_publication, &protected, &certified_root_ids);
            if self.dynamic_scope_owners.is_empty()
                && !self.dynamic_global_member
                && !self.global_alias_written
                && !global_root_exposed
            {
                for (owner, candidate) in self.deferred_global_candidates.iter().enumerate() {
                    let Some(candidate) = candidate else { continue };
                    let root_name = match candidate.root {
                        BuiltinGlobalRoot::GlobalThis => "globalThis",
                        BuiltinGlobalRoot::Window => "window",
                    };
                    if !self.global_object_names.contains(root_name)
                        || !hints
                            .assumptions
                            .deferred_global_publications
                            .get(&candidate.root)
                            .is_some_and(|slots| slots.contains(&candidate.property))
                        || self.global_slot_mentions.get(&candidate.property) != Some(&1)
                        || candidate.property == "__proto__"
                        || !self.top_calls[owner].is_empty()
                        || self.top_property_writes[owner].len() != 1
                        || self.top_member_value_writes[owner].len() != 1
                        || self.top_callback_result_writes[owner].len() != 1
                    {
                        continue;
                    }
                    self.deferred_global_certificates
                        .insert(SourceUnitOrdinal(owner));
                }
            }
        }
    }

    pub(super) fn admit_non_reentrant_global_membership(
        &mut self,
        body: &[TopLevelItemView<'_>],
        hints: &AnalysisInputs,
    ) {
        if !hints.assumptions.non_reentrant_global_membership.is_empty() {
            let mut bindings = GlobalMembershipBindings::default();
            for item in body {
                item.as_module_item().visit_with(&mut bindings);
            }
            self.global_membership_candidates.retain(|_, probe| {
                !bindings.0.contains(&probe.receiver)
                    && hints
                        .assumptions
                        .non_reentrant_global_membership
                        .get(&probe.root)
                        .is_some_and(|keys| keys.contains(&probe.property))
            });
            let first_probe = self
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
            let roots = self
                .global_membership_candidates
                .values()
                .map(|probe| probe.receiver.clone())
                .collect();
            let prototype_write = self
                .top_callback_result_writes
                .iter()
                .flatten()
                .chain(
                    self.callables
                        .values()
                        .flat_map(|callable| &callable.callback_result_writes),
                )
                .any(|write| {
                    let path = global_mutation_path(
                        &write.receiver,
                        &self.global_object_names,
                        &bindings.0,
                    );
                    // A visible stored back-alias defeats the descendant
                    // distinction even when its receiver is an ordinary object.
                    // Prototype values held by unrelated private objects do
                    // not themselves expose or mutate a global-rooted path.
                    if stored_global_alias(
                        &write.value,
                        &self.global_object_names,
                        &bindings.0,
                        path != GlobalMutationPath::Unrelated,
                    ) {
                        return true;
                    }
                    match write.property.as_deref() {
                        Some("__proto__" | "prototype" | "constructor") => {
                            path != GlobalMutationPath::Unrelated
                                || global_receiver_path(&write.receiver, &self.global_object_names)
                        }
                        None => matches!(
                            path,
                            GlobalMutationPath::Identity | GlobalMutationPath::Prototype
                        ),
                        _ => false,
                    }
                });
            if self.dynamic_scope_owners.is_empty()
                && !self.dynamic_global_member
                && !self.global_alias_written
                && !prototype_write
                && !self
                    .mutated_intrinsic_roots
                    .contains(&NativeIntrinsic::Object)
                && !self.global_receiver_exposed(first_probe, &protected, &roots)
            {
                self.non_reentrant_global_membership
                    .extend(self.global_membership_candidates.keys().copied());
            }
        }
    }
}
