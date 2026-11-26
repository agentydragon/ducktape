//! Local class construction: constructor eligibility, ordered and refused
//! construction, and the reach a refused instance publishes.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    pub(super) fn eligible_local_constructor(
        &mut self,
        key: u32,
        arg_count: usize,
    ) -> Result<&LocalConstructor, ConstructionRejection> {
        let (Some(constructor), Some(binding)) = (
            self.inputs.index.local_constructors.get(&key),
            self.inputs.index.class_bindings.get(&key),
        ) else {
            return Err(ConstructionRejection::UncheckedConstructor);
        };
        // A constructorless, fieldless class only allocates an instance for a
        // direct zero-argument `new`. Even if its class object escaped, a
        // modified prototype or deferred method cannot run during allocation.
        // Any unknown/reassigned callee alternative still takes the ordinary
        // opaque path in execute_call. Other constructors need the class and
        // prototype to remain unexposed to avoid setters and method reentry.
        let inert_allocation = constructor.inert_allocation && arg_count == 0;
        let checked_default_fields = constructor.checked_default_fields && arg_count == 0;
        if constructor.checked_default_fields && arg_count != 0 {
            return Err(ConstructionRejection::ArgumentsToDefaultFields);
        }
        if !inert_allocation && !checked_default_fields {
            if self.inputs.index.other_class_uses.contains(binding) {
                return Err(ConstructionRejection::ClassUsedOutsideConstruction);
            }
            if self.inputs.export_reentry_enabled && self.inputs.reentry_bindings.contains(binding)
            {
                return Err(ConstructionRejection::ExportedClass);
            }
            if self.state.world.classes.tainted.contains(&key) {
                return Err(ConstructionRejection::ClassMutated);
            }
            if let Some(property) = constructor
                .own_fields
                .iter()
                .find(|property| !self.constructor_field_prototype_absent(key, property))
            {
                return Err(ConstructionRejection::FieldMayExistOnObjectPrototype {
                    property: property.clone(),
                });
            }
            if self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::ClassMembers(key))
            {
                return Err(ConstructionRejection::ClassMembersEscaped);
            }
        }
        if constructor
            .binding_reads
            .iter()
            .any(|id| !self.inputs.flow.is_declared(id))
        {
            return Err(ConstructionRejection::UndeclaredConstructorRead);
        }
        Ok(constructor)
    }

    pub(super) fn construct_local_class(
        &mut self,
        key: u32,
        args: &[Values],
    ) -> Result<(Outcome, AbstractValue), ConstructionRejection> {
        let result = self.request(RuntimeOperation::Model(
            super::super::runtime_models::ModelOperation::ConstructLocalClass {
                key,
                arguments: args.to_vec(),
            },
        ));
        let RuntimeValue::Model(super::super::runtime_models::ModelResult::LocalClassConstruction(
            result,
        )) = result
        else {
            unreachable!("local class models produce construction results");
        };
        *result
    }

    pub(crate) fn construct_local_class_transfer(
        &mut self,
        key: u32,
        args: &[Values],
    ) -> Result<(Outcome, AbstractValue), ConstructionRejection> {
        let constructor = self.begin_local_construction(key, args)?;
        let outcome = self.invoke_checked_constructor(&constructor, args);
        Ok((outcome, self.constructed_instance(key, args)))
    }

    /// A local construction the checked constructor refused still runs its
    /// instance fields and body at `new`: record the typed loss and expose the
    /// class. The caller invokes the arguments' possible callbacks.
    pub(super) fn reject_local_construction(
        &mut self,
        key: u32,
        args: &[Values],
        call: &Call,
        rejection: ConstructionRejection,
        outcome: &mut Outcome,
    ) {
        let reach = self.construction_reach(key, args);
        outcome.lose_precision(
            &self.inputs.metrics,
            ExecutionPrecisionLossReason::UnsupportedConstruction { rejection },
            reach,
            call.owner,
            call.span,
        );
        outcome.unknown_call = true;
        self.note_unknown_call(call);
        self.state
            .world_mut()
            .classes
            .escaped_capabilities
            .insert(key);
        // Unsupported constructors still execute their instance fields and
        // body at `new`, even without a callback argument to seed the opaque
        // fallback.
        self.promote_class_members(key, outcome);
        self.publish_refused_instance_reach(key, call.span);
    }

    /// The unknown instance of a refused construction holds whatever its
    /// fields and constructors stored on `this`. Kept out of line for
    /// `execute_call`'s frame size.
    #[inline(never)]
    pub(super) fn publish_refused_instance_reach(&mut self, key: u32, span: Span) {
        let reached = self.class_reach(key);
        self.publish_values(
            &reached,
            ClassMembersPublicationWriter::RefusedConstructionInstance,
            Some(span),
        );
    }

    /// `class` and the local classes it extends, named by binding or inline
    /// in an `extends` expression. That expression ran when the class was
    /// created; only the classes it names are followed.
    pub(super) fn class_chain(&self, class: u32) -> BTreeSet<u32> {
        let index = self.inputs.index;
        let mut pending = vec![class];
        let mut chain = BTreeSet::new();
        while let Some(class) = pending.pop() {
            if !chain.insert(class) {
                continue;
            }
            let Some(heritage) = index.class_heritage_spans.get(&class) else {
                continue;
            };
            let (start, end) = (heritage.lo.0, heritage.hi.0);
            pending.extend(index.class_spans.range(start..end).map(|(base, _)| *base));
            for binding in index.references_within(start, end) {
                if self.inputs.flow.is_declared(binding) {
                    pending.extend(
                        self.inputs
                            .flow
                            .values_of_binding(binding)
                            .into_iter()
                            .filter_map(|value| match value {
                                Value::Alloc(Kind::Class, base) => Some(base),
                                _ => None,
                            }),
                    );
                }
            }
        }
        chain
    }

    /// What holding an instance of `class`, or the class, exposes along its
    /// class chain: its methods, accessors and static methods, which unknown
    /// code can call or read as values (with an unknown receiver), and what
    /// constructing or creating it can store on an instance or the class:
    /// every function its constructors, field initializers and static blocks
    /// create, and every value of the bindings they read. Constructors stay
    /// out: they run only as constructions, and a free call of one would
    /// make its `super()` an unknown call.
    pub(super) fn class_reach(&self, class: u32) -> Values {
        let index = self.inputs.index;
        let mut reached = Values::new();
        for class in self.class_chain(class) {
            if let Some(span) = index.class_spans.get(&class) {
                let heritage = index.class_heritage_spans.get(&class);
                reached.extend(
                    index
                        .callable_spans
                        .range(span.lo.0..span.hi.0)
                        .filter(|(callable, _)| {
                            index.class_member_callables.contains(*callable)
                                && !index.class_constructors.contains(*callable)
                                && heritage.is_none_or(|heritage| {
                                    !(heritage.lo.0 <= **callable && **callable < heritage.hi.0)
                                })
                        })
                        .map(|(callable, _)| {
                            AbstractValue::Value(Value::Alloc(Kind::Function, *callable))
                        }),
                );
            }
            for region in index.class_store_regions.get(&class).into_iter().flatten() {
                let (lo, hi) = (region.lo.0, region.hi.0);
                reached.extend(
                    index
                        .callable_spans
                        .range(lo..hi)
                        .filter(|(callable, _)| !index.class_member_callables.contains(*callable))
                        .map(|(callable, _)| {
                            AbstractValue::Value(Value::Alloc(Kind::Function, *callable))
                        }),
                );
                for binding in index.references_within(lo, hi) {
                    if self.inputs.flow.is_declared(binding) {
                        reached.extend(from_flow(self.inputs.flow.values_of_binding(binding)));
                    }
                }
            }
        }
        reached
    }

    /// The value of a refused construction: unknown, but it may retain every
    /// callback its arguments reach.
    pub(super) fn refused_construction_values(&mut self, args: &[Values]) -> Values {
        let mut results = Values::from([AbstractValue::Value(Value::Top)]);
        let callbacks: Values = args
            .iter()
            .flat_map(|values| self.callback_values(values))
            .collect();
        if !callbacks.is_empty() {
            results.insert(self.deferred_callbacks(callbacks));
        }
        results
    }

    /// One `new` of a local class whose callee and arguments the ordered
    /// kernel evaluated. A checked class body without calls runs through the
    /// kernel's construct rule against the checked instance; a body the
    /// kernel cannot run keeps the flat lane's invocation and no coverage.
    pub(crate) fn construct_local_class_ordered(
        &mut self,
        key: u32,
        arguments: Vec<SelectedValue>,
        call: &Call,
        outcome: &mut Outcome,
    ) -> OrderedConstruction {
        let args: Vec<Values> = arguments
            .iter()
            .map(|argument| argument.values.clone())
            .collect();
        let constructor = match self.begin_local_construction(key, &args) {
            Ok(constructor) => constructor,
            Err(rejection) => {
                self.reject_local_construction(key, &args, call, rejection, outcome);
                self.invoke_possible_callbacks_at(&args, false, Some(call.span), outcome);
                return OrderedConstruction {
                    values: self.refused_construction_values(&args),
                    body: None,
                };
            }
        };
        let inputs = self.inputs;
        let Some((body, class_owner)) = kernel_constructor_body(inputs.index, key, &constructor)
        else {
            let mut nested = self.invoke_checked_constructor(&constructor, &args);
            self.absorb_construction(&mut nested, outcome);
            return OrderedConstruction {
                values: Values::from([self.constructed_instance(key, &args)]),
                body: None,
            };
        };
        let instance = Values::from([self.constructed_instance(key, &args)]);
        if let Some(constructor_key) = constructor.key {
            self.record_executed_owner(class_owner);
            self.state
                .statement_invoked_callables
                .insert(constructor_key);
            self.activate_native_effects(class_owner, Some(constructor_key));
        }
        let mut nested = self.execute_local_body(
            body,
            Context::new(),
            Some(arguments),
            class_owner,
            LocalActivation::Constructor {
                class: key,
                instance: instance.clone(),
            },
        );
        let effects =
            (nested.complete && !nested.unknown_call).then(|| nested.effect_coverage.clone());
        let values = construct::construct_result(&nested.returns, &instance);
        self.absorb_construction(&mut nested, outcome);
        match values {
            Some(values) => OrderedConstruction {
                values,
                body: effects.map(|effects| {
                    (
                        LocalBodyIdentity::Constructor {
                            class: key,
                            owner: class_owner,
                        },
                        effects,
                    )
                }),
            },
            // The checked constructor admits no return statement; an object
            // result would replace the instance outside this rule.
            None => OrderedConstruction {
                values: Values::from([AbstractValue::Value(Value::Top)]),
                body: None,
            },
        }
    }

    pub(super) fn absorb_construction(&mut self, nested: &mut Outcome, outcome: &mut Outcome) {
        self.apply_rebind_effects(&nested.effects.rebinds);
        outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
        outcome
            .executed_owners
            .extend(std::mem::take(&mut nested.executed_owners));
        outcome.unknown_call |= nested.unknown_call;
        outcome.inherit_completeness(
            nested.complete,
            std::mem::take(&mut nested.effects.precision_losses),
        );
    }

    /// The application identity of a checked instance: it retains every
    /// argument capability.
    pub(super) fn constructed_instance(&mut self, key: u32, args: &[Values]) -> AbstractValue {
        let values: Values = args.iter().flatten().cloned().collect();
        AbstractValue::Instance(
            key,
            self.intern_callback_set(values),
            InstanceOrigin::Constructed,
        )
    }

    /// One local function or closure target of an ordinary call: invoke
    /// it, keep its receipt when it is the call's covered target, and absorb
    /// its effects. Kept out of line for `execute_call`'s frame size, which
    /// bounds the supported call depth.
    #[inline(never)]
    pub(super) fn execute_local_call_target(
        &mut self,
        target: LocalCallTarget<'_>,
        result_values: &mut Values,
        result_selection: &mut Option<CallableSelection>,
        outcome: &mut Outcome,
    ) {
        let LocalCallTarget {
            key,
            captured,
            args,
            argument_selections,
            coverage_target,
            single_target,
            needs_value,
            call_span,
        } = target;
        let mut nested = self.invoke_selected(
            key,
            args.to_vec(),
            argument_selections.to_vec(),
            captured,
            false,
        );
        if single_target && coverage_target.is_some() {
            *result_selection = nested.return_selection.clone();
        }
        if let Some((expected, caller, target, prerequisites)) = coverage_target
            && *expected == key
        {
            let mut effect_coverage = nested.effect_coverage.clone();
            effect_coverage.depend_on(prerequisites);
            effect_coverage.selected_local_call(LocalCallReceipt {
                caller: caller.clone(),
                call_span,
                target: target.clone(),
                dependencies: prerequisites.clone(),
            });
            outcome.effect_coverage = effect_coverage;
        }
        if needs_value {
            result_values.extend(nested.returns.iter().cloned());
        }
        self.apply_rebind_effects(&nested.effects.rebinds);
        outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
        outcome.executed_owners.extend(nested.executed_owners);
        outcome.unknown_call |= nested.unknown_call;
        outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
    }

    /// Runs each local plain function among `targets` for `new`, and
    /// absorbs its effects into `outcome`.
    #[inline(never)]
    pub(super) fn construct_plain_functions(
        &mut self,
        targets: &Values,
        args: &[Values],
        outcome: &mut Outcome,
    ) {
        for target in targets {
            let (key, captured) = match target {
                AbstractValue::Value(Value::Alloc(Kind::Function, key)) => (*key, None),
                AbstractValue::Closure(HeapAddress { callable: key, .. }, captured) => {
                    (*key, Some(captured.clone()))
                }
                _ => continue,
            };
            if !self.inputs.index.callables.contains_key(&key) {
                continue;
            }
            let mut nested = self.invoke(key, args.to_vec(), captured, false);
            self.apply_rebind_effects(&nested.effects.rebinds);
            outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
            outcome.executed_owners.extend(nested.executed_owners);
            outcome.unknown_call |= nested.unknown_call;
            outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
        }
    }

    /// The flat lane's invocation of an admitted constructor body.
    pub(super) fn invoke_checked_constructor(
        &mut self,
        constructor: &LocalConstructor,
        args: &[Values],
    ) -> Outcome {
        let previous_inputs =
            std::mem::take(&mut self.state.world_mut().classes.checked_constructor_inputs);
        self.state.world_mut().classes.checked_constructor_inputs = args
            .iter()
            .flatten()
            .filter_map(|value| match value {
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => Some(*object),
                _ => None,
            })
            .collect();
        let outcome = constructor.key.map_or_else(
            || Outcome {
                complete: true,
                ..Outcome::default()
            },
            |constructor_key| self.invoke(constructor_key, args.to_vec(), None, true),
        );
        self.state.world_mut().classes.checked_constructor_inputs = previous_inputs;
        outcome
    }

    /// Admit one local construction: the checked constructor's eligibility
    /// and argument sources, with the export and inference bookkeeping every
    /// admitted construction records.
    pub(super) fn begin_local_construction(
        &mut self,
        key: u32,
        args: &[Values],
    ) -> Result<LocalConstructor, ConstructionRejection> {
        let constructor = self.eligible_local_constructor(key, args.len())?.clone();
        if self.inputs.export_reentry_enabled {
            let exported: Values = self
                .inputs
                .reentry_bindings
                .iter()
                .flat_map(|id| {
                    self.state
                        .world
                        .module
                        .prefix_values
                        .get(id)
                        .into_iter()
                        .flatten()
                        .cloned()
                })
                .collect();
            self.taint_objects(&exported);
        }
        for (index, _) in &constructor.property_reads {
            let unchecked = ConstructionRejection::UncheckedPropertySource { parameter: *index };
            let values = args.get(*index).ok_or_else(|| unchecked.clone())?;
            if values.is_empty()
                || !values.iter().all(|value| {
                    matches!(value,
                    AbstractValue::Value(Value::Alloc(Kind::Object, object))
                        if self.inputs.index.plain_data_objects.contains(object)
                            && self.state.world.object_properties_are_settled(*object))
                })
            {
                return Err(unchecked);
            }
        }
        if let Some(proof) = self
            .inputs
            .index
            .local_class_construction_proof
            .inference_use_for_class(key)
        {
            let dependencies = self
                .inputs
                .index
                .local_class_construction_proof
                .dependencies_for_class(key)
                .expect("a class certificate retains its typed dependencies");
            self.record_inference_use(
                NativeIntegrityGuard::LocalClassConstruction,
                proof,
                dependencies,
            );
        }
        Ok(constructor)
    }
}
