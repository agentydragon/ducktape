//! Records call sites and callback arguments.

use super::fresh_values::{from_entries_mapped_proof, primitive_constant, record_factory_argument};
use super::native_write_census::IntrinsicWriteTarget;
use super::property_keys::PropertyKeyExpr;
use super::{
    Call, CallKind, ExecutionIndex, IndexedRetainedCallbackCall, no_reentry_inert_argument,
    no_reentry_primitive_argument, object_literal_assign_keys, plain_assign_source_property,
};
use analysis::facts::at_init_fallback::{
    is_no_sync_callback_member_call, member_row_invokes_no_argument,
};
use analysis_domain::builtins::{ArgSel, iteration_row};
use analysis_vocabulary::report_schema::NativeMutationOperation;
use std::collections::BTreeSet;
use swc_ecma_ast::*;
use swc_ecma_visit::VisitWith;
use value_model::{ArrayIteration, CallSite, ValueExpr, static_prop};

impl ExecutionIndex {
    pub(super) fn index_call_expr(&mut self, call: &CallExpr) {
        self.record_call_entry_points(call);
        self.record_call_member_facts(call);
        self.record_intrinsic_mutation_call(call);

        self.record_callback_calls(call);
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

    fn record_call_entry_points(&mut self, call: &CallExpr) {
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
    }

    fn record_call_member_facts(&mut self, call: &CallExpr) {
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
    }

    fn record_intrinsic_mutation_call(&mut self, call: &CallExpr) {
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
    }

    fn record_callback_calls(&mut self, call: &CallExpr) {
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
    }
}
