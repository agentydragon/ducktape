//! Recording helpers the index visitor calls per syntax node: calls, accessor
//! and protocol lookups, opacity marks, retained callbacks, iterated sources
//! and member writes.

use super::patterns::iterated_defaults;
use super::*;

impl ExecutionIndex {
    pub(super) fn with_callable(
        &mut self,
        span: Span,
        params: Vec<Parameter>,
        visit: impl FnOnce(&mut Self),
    ) {
        let key = span.lo.0;
        self.callable_spans.insert(key, span);
        let created_after_parent_await = self
            .current
            .and_then(|parent| self.callables.get(&parent))
            .is_some_and(|parent| parent.created_after_parent_await)
            || (self.past_await && !self.hoisted_nested_functions.contains(&key));
        let saved_current = self.current.replace(key);
        let saved_variable_scope = self.variable_scope.replace(key);
        let saved_await = std::mem::replace(&mut self.past_await, false);
        let saved_may_skip = std::mem::replace(&mut self.may_skip_await_depth, 0);
        let saved_conditional = std::mem::replace(&mut self.conditional_depth, 0);
        let saved_assignment_target = std::mem::replace(&mut self.assignment_target, false);
        let opaque_params = params.iter().any(|param| param.binding.is_none());
        self.callables.entry(key).or_insert_with(|| Callable {
            owner: self.owner,
            created_after_parent_await,
            params,
            opaque: opaque_params,
            ..Callable::default()
        });
        self.prototype_capture_stack.push(key);
        visit(self);
        self.prototype_capture_stack.pop();
        let callable = self.callables.get_mut(&key).unwrap();
        if let Some(body) = callable.body_facts.take_ordered_plan()
            // Defaults run outside the body var environment. Use the full
            // declaration census, including dead/nested var declarations.
            && ((callable.hoisted_vars.is_empty() && callable.local_functions.is_empty())
                || body.parameters.iter().all(|parameter| parameter.default.is_none()))
        {
            callable.body_facts.set_ordered_plan(Some(
                body.with_declarations(
                    callable.hoisted_vars.iter().cloned(),
                    callable
                        .lexical_declarations
                        .iter()
                        .map(|(binding, kind)| (binding.clone(), *kind))
                        .chain(callable.local_functions.iter().map(|(binding, function)| {
                            (binding.clone(), LocalBinding::HoistedFunction(*function))
                        })),
                ),
            ));
        }
        self.current = saved_current;
        self.variable_scope = saved_variable_scope;
        self.past_await = saved_await;
        self.may_skip_await_depth = saved_may_skip;
        self.conditional_depth = saved_conditional;
        self.assignment_target = saved_assignment_target;
    }

    pub(super) fn record_call(&mut self, call: Call) {
        // Native property integrity includes deferred and post-await bodies.
        let call = Arc::new(call);
        self.prototype_calls.push(Arc::clone(&call));
        if matches!(&call.callee, ValueExpr::Binding(id) if id.0.as_ref() == "eval") {
            self.dynamic_scope_owners.insert(self.owner);
        }
        if self.current.is_some() && self.past_await {
            return;
        }
        if self.current.is_none()
            && (self.conditional_depth > 0 || self.may_skip_member_read_depth > 0)
            && call.kind == CallKind::LocalMemberRead
        {
            self.conditional_top_member_reads.insert(call.span.into());
        }
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().calls.push(call);
        } else {
            self.top_calls[self.owner.0].push(call);
        }
    }

    pub(super) fn record_accessor_callee_lookup(
        &mut self,
        member: &MemberExpr,
        no_reentry_call: Option<CallSite>,
    ) {
        if let Some(no_reentry_call) = no_reentry_call {
            self.no_reentry_accessor_calls
                .insert(CallSite::from(member.span), no_reentry_call);
        }
        self.record_call(Call::without_argument_facts(
            CallKind::AccessorCalleeLookup,
            ValueExpr::Member(
                Arc::new(ValueExpr::from_expr(&member.obj)),
                static_prop(&member.prop),
                MemberKey::of(&member.prop),
                member.span.into(),
            ),
            Vec::new(),
            member.span,
            self.current,
            self.owner,
        ));
    }

    pub(super) fn record_source_protocol(&mut self, kind: CallKind, source: ValueExpr, span: Span) {
        self.record_call(Call::without_argument_facts(
            kind,
            ValueExpr::Unknown,
            vec![source],
            span,
            self.current,
            self.owner,
        ));
    }

    pub(super) fn record_implicit_coercion(&mut self, expression: &Expr, span: Span) {
        if (self.current.is_some() && self.past_await)
            || js_ast::has_definite_primitive_result(expression)
        {
            return;
        }
        self.record_implicit_coercion_value(ValueExpr::from_expr(expression), span);
    }

    pub(super) fn record_implicit_coercion_value(&mut self, argument: ValueExpr, span: Span) {
        if self.current.is_some() && self.past_await {
            return;
        }
        self.record_call(Call::without_argument_facts(
            CallKind::ImplicitCoercion,
            ValueExpr::Unknown,
            vec![argument],
            span,
            self.current,
            self.owner,
        ));
    }

    pub(super) fn mark_opaque(&mut self) {
        if self.current.is_some() && self.past_await {
            return;
        }
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().opaque = true;
        } else {
            self.opaque_top.insert(self.owner);
        }
    }

    pub(super) fn mark_dynamic_property_effect(&mut self, receiver: Option<&Expr>) {
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

    pub(super) fn mark_possible_namespace_escape(&mut self, expression: &Expr) {
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

    pub(super) fn is_factory_member_data_value(&self, key: &PropName, expression: &Expr) -> bool {
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

    pub(super) fn is_callback_factory_member_callee(&self, expression: &Expr) -> bool {
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

    pub(super) fn inline_callback_callee(call: &CallExpr) -> Option<&Expr> {
        if call.args.len() != 1
            || call.args[0].spread.is_some()
            || !matches!(
                call.args[0].expr.unwrap_parens(),
                Expr::Fn(_) | Expr::Arrow(_)
            )
        {
            return None;
        }
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        Some(callee.unwrap_parens())
    }

    pub(super) fn inline_factory_callback_argument(&self, call: &CallExpr) -> Option<usize> {
        let is_factory = match Self::inline_callback_callee(call)? {
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
        is_factory.then_some(0)
    }

    pub(super) fn retained_callback_call(&self, call: &CallExpr) -> Option<RetainedCallbackCall> {
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

    pub(super) fn no_reentry_callback_call(&self, call: &CallExpr) -> Option<RetainedCallbackCall> {
        let imported = if let Callee::Expr(callee) = &call.callee
            && let Expr::Ident(binding) = callee.unwrap_parens()
        {
            self.no_reentry_callback_imports
                .get(&binding.to_id())
                .and_then(|signatures| {
                    Self::retained_callback_invocation(signatures, &call.args, None)
                })
        } else {
            None
        };
        imported.or_else(|| self.source_no_reentry_callback_call(call))
    }

    pub(super) fn source_no_reentry_callback_call(
        &self,
        call: &CallExpr,
    ) -> Option<RetainedCallbackCall> {
        if call.args.iter().any(|argument| argument.spread.is_some()) {
            return None;
        }
        let signature = self
            .no_reentry_statement_calls
            .get(&(call.span.lo.0, call.span.hi.0))?;
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        let simple_callee = match callee.unwrap_parens() {
            Expr::Ident(_) => true,
            Expr::Member(member) => {
                matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                    && matches!(member.prop, MemberProp::Ident(_))
            }
            _ => false,
        };
        if !simple_callee
            || signature.callback_arguments.iter().any(|position| {
                !call.args.get(*position).is_some_and(|argument| {
                    match argument.expr.unwrap_parens() {
                        Expr::Ident(_) => true,
                        Expr::Fn(function) => {
                            !function.function.is_async && !function.function.is_generator
                        }
                        Expr::Arrow(arrow) => !arrow.is_async,
                        _ => false,
                    }
                })
            })
        {
            return None;
        }
        let mut invocation =
            Self::retained_callback_invocation(std::slice::from_ref(signature), &call.args, None)?;
        invocation.require_single_callback_identity = true;
        Some(invocation)
    }

    pub(super) fn retained_constructor_call(
        &self,
        allocation: &NewExpr,
    ) -> Option<RetainedCallbackCall> {
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

    pub(super) fn retained_callback_invocation(
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
            require_single_callback_identity: false,
        })
    }

    pub(super) fn record_published_source(&mut self, value: &Expr) {
        let value = ValueExpr::from_expr(value);
        match self.current {
            Some(key) => self
                .callables
                .get_mut(&key)
                .unwrap()
                .published_sources
                .push(value),
            None => self
                .top_published_sources
                .entry(self.owner)
                .or_default()
                .push(value),
        }
    }

    /// `arguments` is recorded as unknown: an own `[Symbol.iterator]` write
    /// to it is not tracked.
    pub(super) fn record_iterated_source(&mut self, value: &Expr) {
        let value = match value.unwrap_parens() {
            Expr::Ident(ident) if ident.sym == "arguments" => ValueExpr::Unknown,
            value => ValueExpr::from_expr(value),
        };
        self.record_iterated_value(value);
    }

    pub(super) fn record_head_iterated_defaults(&mut self, head: &ForHead) {
        match head {
            ForHead::VarDecl(declaration) => {
                for declarator in &declaration.decls {
                    self.record_iterated_defaults(&declarator.name);
                }
            }
            ForHead::Pat(pattern) => self.record_iterated_defaults(pattern),
            ForHead::UsingDecl(_) => {}
        }
    }

    pub(super) fn record_iterated_defaults(&mut self, pattern: &Pat) {
        let mut defaults = Vec::new();
        iterated_defaults(pattern, &mut defaults);
        for default in defaults {
            self.record_iterated_source(default);
        }
    }

    pub(super) fn record_iterated_value(&mut self, value: ValueExpr) {
        match self.current {
            Some(key) => self
                .callables
                .get_mut(&key)
                .unwrap()
                .iterated_sources
                .push(value),
            None => self
                .top_iterated_sources
                .entry(self.owner)
                .or_default()
                .push(value),
        }
    }

    pub(super) fn record_nested_write(&mut self, binding: Id) {
        for key in &self.prototype_capture_stack {
            self.nested_callable_writes
                .entry(*key)
                .or_default()
                .insert(binding.clone());
        }
    }

    pub(super) fn record_member_write(&mut self, member: &MemberExpr) {
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
}
