use super::*;

impl Executor<'_, '_> {
    /// The complete pool must be covered: another member could otherwise
    /// change a return binding or publish a callable between observations.
    pub(super) fn summarize_read_only_callback_pool(
        &mut self,
        callbacks: &Values,
        outcome: &mut Outcome,
    ) -> bool {
        if !self.inputs.index.dynamic_scope_owners.is_empty()
            || self.state.world.reachability.incomplete
            || self.state.array_iteration_depth > 0
        {
            return false;
        }
        let inputs = self.inputs;
        let mut members = BTreeSet::new();
        let mut reads: BTreeMap<SourceUnitOrdinal, BTreeSet<&Id>> = BTreeMap::new();
        let mut returned = BTreeSet::new();
        for callback in callbacks {
            let key = match callback {
                AbstractValue::Value(Value::Alloc(Kind::Function, key)) => *key,
                _ => return false,
            };
            if self.state.active.contains(&ActiveInvocation {
                callable: key,
                top_owner: self.state.current_top_owner,
            }) {
                return false;
            }
            // Function-surface taint cannot change this source body: the
            // certificate excludes property/native/receiver operations.
            let Some(summary) = inputs.flow.read_only_callback(key) else {
                return false;
            };
            let Some(callable) = inputs.index.callables.get(&key) else {
                return false;
            };
            if summary.reads.iter().any(|id| {
                id.0.as_ref() == "arguments"
                    || !inputs.top_bindings.contains(id)
                    || inputs.index.imported_bindings.contains(id)
            }) {
                return false;
            }
            members.insert(key);
            reads
                .entry(callable.owner)
                .or_default()
                .extend(&summary.reads);
            returned.extend(&summary.returned);
        }
        // Check each shared cell once against this prefix, including after a
        // later write or initialization. TDZ contributes a throwing read, not
        // a successful returned capability. No negative whole-program fact is
        // being inferred from an empty possible-value set.
        let env = Context::new();
        for binding in returned {
            let values = self.evaluate_binding(binding, &env);
            if values.is_empty()
                || values.iter().any(|value| {
                    !value.is_primitive()
                        && !matches!(value, AbstractValue::Value(Value::Unavailable))
                })
            {
                return false;
            }
        }
        // These are hypothetical effects of arbitrary skip/repeat/order,
        // never ordered invocation receipts or a completion certificate.
        for (owner, bindings) in reads {
            self.promote_top_effects(
                &bindings.into_iter().collect::<Vec<_>>(),
                &[],
                owner,
                outcome,
            );
            self.record_executed_owner(owner);
            outcome.executed_owners.insert(owner);
        }
        self.state.statement_invoked_callables.extend(members);
        true
    }
}
