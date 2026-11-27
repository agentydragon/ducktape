//! Records assignment writes, reassignments and member stores.

use super::global_receiver::is_global_receiver;
use super::native_write_census::{IntrinsicWriteTarget, intrinsic_write_target};
use super::patterns::{
    collect_assign_target_assignments, pattern_iterates, pattern_iterates_part,
    pattern_loses_values,
};
use super::property_keys::PropertyKeyExpr;
use super::{CallKind, CallbackResultWrite, ExecutionIndex, MemberValueWrite, data_read_operand};
use analysis_model::PrimKinds;
use analysis_vocabulary::report_schema::NativeMutationOperation;
use std::sync::Arc;
use swc_ecma_ast::*;
use swc_ecma_visit::VisitWith;
use value_model::{CallSite, MemberKey, ValueExpr, static_prop};

impl ExecutionIndex {
    pub(super) fn index_assign_expr(&mut self, assignment: &AssignExpr) {
        // Record before visiting operands: an await inside a target or RHS
        // can only make this conservative. Reuse the complete target walker
        // used by raw facts, including patterns and parenthesized targets.
        binding_targets::record_assign_target(&assignment.left, self);
        let simple_target = binding_targets::simple_write_target(&assignment.left);
        self.record_assigned_bindings(assignment, &simple_target);
        self.record_global_guard_writes(&simple_target);
        self.record_member_target_write(assignment, &simple_target);
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
        self.record_compound_coercion(assignment, &simple_target);
        self.record_assignment_write(assignment, &simple_target);
    }

    fn record_assigned_bindings(
        &mut self,
        assignment: &AssignExpr,
        simple_target: &Option<binding_targets::SimpleWriteTarget<'_>>,
    ) {
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
    }

    fn record_global_guard_writes(
        &mut self,
        simple_target: &Option<binding_targets::SimpleWriteTarget<'_>>,
    ) {
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
    }

    fn record_member_target_write(
        &mut self,
        assignment: &AssignExpr,
        simple_target: &Option<binding_targets::SimpleWriteTarget<'_>>,
    ) {
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
    }

    fn record_compound_coercion(
        &mut self,
        assignment: &AssignExpr,
        simple_target: &Option<binding_targets::SimpleWriteTarget<'_>>,
    ) {
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
    }

    fn record_assignment_write(
        &mut self,
        assignment: &AssignExpr,
        simple_target: &Option<binding_targets::SimpleWriteTarget<'_>>,
    ) {
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
}
