//! A provider source program uses the shared continuation machine in the caller
//! world. Its lexical namespace never becomes the caller's active-cell map.

use super::super::provider_program::ProviderProgramSource;
use super::*;
use crate::facts::sync_effects::allocation_transfer::AllocationJudgment;
use crate::facts::sync_effects::effect_coverage::OperationSupportReceipt;
use crate::facts::sync_effects::value_model::LiteralPropertyKind;
use local_transfer::{LocalCallRequest, LocalContinuation, LocalSuspension};

#[derive(Clone)]
struct ProviderActivation {
    index: Arc<ExecutionIndex>,
    source: ProviderProgramSource,
    callable: u32,
    cells: OrdMap<Id, LexicalCellId>,
    regions: Vec<Vec<(Id, Option<LexicalCellId>)>>,
    owner: SourceUnitOrdinal,
    invocation_span: Span,
}

struct ProviderExecutionContext<'a, 'source, 'rule> {
    executor: &'a mut Executor<'source, 'rule>,
    activation: &'a mut ProviderActivation,
    outcome: &'a mut Outcome,
    body: &'a LocalBody,
}

fn normal_value(selected: SelectedValue) -> LocalValueResult {
    LocalValueResult {
        ledger: TransferLedger::new(
            Completion::Normal(selected),
            EffectCoverageFragment::no_effects(),
            Box::default(),
            AllocationJudgment::Opaque,
        ),
    }
}

impl Executor<'_, '_> {
    /// None is exclusively a pre-entry refusal. Once entered, each completed
    /// prefix belongs to the returned family, including an opaque remainder.
    pub(super) fn invoke_provider_closure(
        &mut self,
        callee: &AbstractValue,
        arguments: &[SelectedValue],
        receiver: Option<Values>,
        span: Span,
    ) -> Option<RuntimeTransferFamily> {
        let AbstractValue::ProviderClosure(_, closure) = callee else {
            return None;
        };
        if self.state.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH
            || self.state.invocation_count() >= MAX_SYNCHRONOUS_INVOCATIONS
        {
            return None;
        }
        let (program, key) = self.inputs.provider_programs.callable_program(
            closure.values.module,
            &closure.values.source_sha256,
            closure.values.source_origin,
        )?;
        let index = Arc::clone(&program.executable.index);
        let callable = index.callables.get(&key)?;
        if callable.generator
            || index.async_callables.contains(&key)
            || !index.dynamic_scope_owners.is_empty()
            || !closure.values.captures.snapshots.is_empty()
            || closure.values.captures.home_object.is_some()
        {
            return None;
        }
        let body = callable.body_facts.ordered_plan()?.clone();
        if body.activation_bindings.values().any(|kind| {
            matches!(
                kind,
                LocalBinding::NamedSelf(_) | LocalBinding::HoistedFunction(_)
            )
        }) {
            return None;
        }
        if body.free_reads().iter().any(|binding| {
            !closure.values.captures.cells.contains_key(binding)
                && callable.self_binding.as_ref() != Some(binding)
        }) {
            // Missing module/global values require a qualified live-cell or
            // native selection proof, never an Id lookup in caller top_env.
            return None;
        }
        let mut environment = Context::new();
        for (binding, cell) in &closure.values.captures.cells {
            environment.insert(
                binding.clone(),
                self.state.world.get().lexical_cells[cell.0].values.clone(),
            );
        }
        if let Some(binding) = &callable.self_binding {
            environment.insert(binding.clone(), Values::from([callee.clone()]));
        }
        let mut frame = LocalFrame::new(environment);
        frame.inputs.insert(callee.clone());
        frame
            .inputs
            .extend(arguments.iter().flat_map(SelectedValue::retained_values));
        frame.this_values = closure.values.captures.receiver.clone().or(receiver);
        frame
            .inputs
            .extend(frame.this_values.iter().flatten().cloned());
        frame.parameter_inputs = Some(arguments.to_vec());
        frame.borrowed_records = self.reachable_objects(&frame.inputs);
        let activation = ProviderActivation {
            source: program.source.clone(),
            index,
            callable: key,
            cells: closure.values.captures.cells.clone(),
            regions: Vec::new(),
            owner: self.state.current_top_owner?,
            invocation_span: span,
        };
        self.state.record_invocation();
        self.state.invocation_depth += 1;
        let mut outcome = Outcome {
            complete: true,
            body_invoked: true,
            ..Outcome::default()
        };
        outcome.begin_entered_sequence(SelectedValue::raw(Values::new()));
        let source_index = Arc::clone(&activation.index);
        let body_identity = LocalBodyIdentity::Callable {
            key,
            owner: source_index.callables[&key].owner,
        };
        let expected_calls = source_index.call_operation_sites(
            &body_identity,
            source_index.callables[&key]
                .calls
                .iter()
                .filter(|call| matches!(call.kind, CallKind::Call | CallKind::New)),
        );
        let family = self.resume_provider_continuation(
            &body,
            LocalContinuation::new(frame),
            activation,
            outcome,
        );
        Some(family.map(self, |executor, mut outcome| {
            executor.state.invocation_depth -= 1;
            outcome.ledger.effects = expected_calls
                .as_ref()
                .and_then(|expected| {
                    outcome.complete.then_some(())?;
                    if let Completion::Abrupt(thrown) = &outcome.ledger.completion
                        && !executor.throw_origin_is_current(&thrown.origin)
                    {
                        return None;
                    }
                    outcome.ledger.effects.project_checked_body(
                        &body_identity,
                        expected,
                        &source_index.source_operation_sites,
                    )
                })
                .unwrap_or_default();
            outcome
        }))
    }

    fn resume_provider_continuation(
        &mut self,
        body: &LocalBody,
        mut continuation: LocalContinuation,
        mut activation: ProviderActivation,
        mut outcome: Outcome,
    ) -> RuntimeTransferFamily {
        loop {
            let suspension = continuation.poll(
                &mut ProviderExecutionContext {
                    executor: self,
                    activation: &mut activation,
                    outcome: &mut outcome,
                    body,
                },
                body,
            );
            match suspension {
                LocalSuspension::Complete(attempt) => {
                    let (transfer, refused) = match attempt {
                        LocalExecutionAttempt::Completed(result) => (result, false),
                        LocalExecutionAttempt::Refused { result, .. } => (result, true),
                    };
                    outcome.adopt_local_transfer(
                        transfer,
                        LocalExecutionScope::Callable(activation.callable),
                        !refused,
                    );
                    continuation
                        .frame
                        .activation
                        .apply_effect_proof(&mut outcome.ledger.effects);
                    outcome.reached.extend(continuation.retained_values());
                    if refused && !continuation.frame.remainder_summarized.get() {
                        ProviderExecutionContext {
                            executor: self,
                            activation: &mut activation,
                            outcome: &mut outcome,
                            body,
                        }
                        .summarize(&continuation.frame, Values::new());
                    }
                    return RuntimeTransferFamily::capture(self, outcome);
                }
                LocalSuspension::Fork(branches) => {
                    let mut branches = branches.into_iter();
                    return RuntimeTransferFamily::fork(self, outcome).bind(
                        self,
                        |executor, prefix| {
                            executor.resume_provider_continuation(
                                body,
                                branches.next().expect("one continuation per branch"),
                                activation.clone(),
                                prefix,
                            )
                        },
                    );
                }
                LocalSuspension::Call(call) => {
                    continuation
                        .frame
                        .inputs
                        .extend(call.retained_operands.iter().cloned());
                    continuation
                        .frame
                        .inputs
                        .extend(continuation.retained_values());
                    let child = self.provider_evaluated_call(&call, activation.invocation_span);
                    if let Some(mut family) = child {
                        family.reanchor(self);
                        return family.bind(self, |executor, mut child| {
                            executor.project_provider_child_coverage(&call, &mut child);
                            let mut caller = continuation.clone();
                            let mut prefix = outcome.clone();
                            let mut activation = activation.clone();
                            if let Completion::Abrupt(thrown) = &mut child.ledger.completion {
                                thrown.frontier = call.source_site.clone();
                            }
                            caller.frame.inputs.extend(child.reached.iter().cloned());
                            if let Some(values) = child.normal_values() {
                                caller.frame.inputs.extend(values.iter().cloned());
                                caller
                                    .frame
                                    .borrowed_records
                                    .extend(executor.reachable_objects(values));
                            }
                            prefix.absorb_call_effects(
                                &mut child.effects,
                                executor.state.assumption.as_ref(),
                                executor.state.heap_access_origin,
                            );
                            prefix.executed_owners.append(&mut child.executed_owners);
                            prefix.unknown_call |= child.unknown_call;
                            prefix.inherit_completeness(
                                child.complete,
                                std::mem::take(&mut child.effects.precision_losses),
                            );
                            caller.resume_call(
                                &mut ProviderExecutionContext {
                                    executor,
                                    activation: &mut activation,
                                    outcome: &mut prefix,
                                    body,
                                },
                                Some(LocalValueResult {
                                    ledger: child.ledger,
                                }),
                            );
                            executor.resume_provider_continuation(body, caller, activation, prefix)
                        });
                    }
                    let mut context = ProviderExecutionContext {
                        executor: self,
                        activation: &mut activation,
                        outcome: &mut outcome,
                        body,
                    };
                    let result = context.call(
                        &mut continuation.frame,
                        call.invocation,
                        &call.callee,
                        &call.arguments,
                        &call.source_site,
                        call.span,
                    );
                    continuation.resume_call(&mut context, result);
                }
            }
        }
    }

    pub(super) fn provider_call_selection(callee: &EvaluatedCallee) -> Option<&ProofDependencies> {
        match callee {
            EvaluatedCallee::Direct(selected) => selected
                .selection
                .as_ref()
                .filter(|selection| selection.matches(&selected.values))
                .map(|selection| selection.dependencies.as_ref()),
            EvaluatedCallee::Member {
                selection_dependencies,
                array_method: None,
                ..
            } => selection_dependencies.as_deref(),
            EvaluatedCallee::FunctionCallMethod {
                target,
                dependencies,
                ..
            } if target
                .selection
                .as_ref()
                .is_some_and(|selection| selection.matches(&target.values)) =>
            {
                Some(dependencies)
            }
            _ => None,
        }
    }

    pub(super) fn provider_closure_identity(
        &self,
        callee: &AbstractValue,
    ) -> Option<LocalBodyIdentity> {
        let AbstractValue::ProviderClosure(_, closure) = callee else {
            return None;
        };
        let (program, _) = self.inputs.provider_programs.callable_program(
            closure.values.module,
            &closure.values.source_sha256,
            closure.values.source_origin,
        )?;
        Some(LocalBodyIdentity::ProviderCallable {
            defining_module: program.source.module.clone(),
            source_sha256: program.source.sha256.clone(),
            source_origin: closure.values.source_origin,
        })
    }

    fn project_provider_child_coverage(&self, call: &LocalCallRequest, child: &mut Outcome) {
        let checked = (|| {
            if !child.complete {
                return None;
            }
            let callee = call.callee.targets().first()?;
            let (target, mut effects) = if let Some(target) = self.provider_closure_identity(callee)
            {
                child.ledger.effects.closed_domain_dependencies()?;
                (target, child.ledger.effects.clone())
            } else {
                let address = match callee {
                    AbstractValue::Closure(address, _)
                    | AbstractValue::Value(Value::Alloc(Kind::Function, address)) => *address,
                    _ => return None,
                };
                let key = self.local_callable_origin(address)?;
                if let Completion::Abrupt(thrown) = &child.ledger.completion
                    && !self.throw_origin_is_current(&thrown.origin)
                {
                    return None;
                }
                let callable = self.inputs.index.callables.get(&key)?;
                let target = LocalBodyIdentity::Callable {
                    key,
                    owner: callable.owner,
                };
                let expected = self.inputs.index.call_operation_sites(
                    &target,
                    callable
                        .calls
                        .iter()
                        .filter(|call| matches!(call.kind, CallKind::Call | CallKind::New)),
                )?;
                let effects = child.ledger.effects.project_checked_body(
                    &target,
                    &expected,
                    &self.inputs.index.source_operation_sites,
                )?;
                (target, effects)
            };
            let mut dependencies = effects.closed_domain_dependencies()?;
            dependencies.extend(Self::provider_call_selection(&call.callee)?);
            effects.selected_local_call(LocalCallReceipt {
                caller: call.source_site.body.clone(),
                site: call.source_site.clone(),
                target,
                dependencies: Box::new(dependencies),
            });
            Some(effects)
        })();
        if checked.is_some()
            && let Completion::Abrupt(thrown) = &mut child.ledger.completion
        {
            // The child body is discharged above. Only the continuation
            // frontier moves; the qualified throwing source stays unchanged.
            thrown.frontier = call.source_site.clone();
        }
        child.ledger.effects = checked.unwrap_or_default();
    }

    fn provider_evaluated_call(
        &mut self,
        call: &LocalCallRequest,
        span: Span,
    ) -> Option<RuntimeTransferFamily> {
        if call.invocation != ForeignInvocation::Call || call.callee.targets().len() != 1 {
            return None;
        }
        let callee = call.callee.targets().first()?;
        if matches!(callee, AbstractValue::ProviderClosure(_, _)) {
            return self.invoke_provider_closure(
                callee,
                &call.arguments,
                call.callee.receiver().cloned(),
                span,
            );
        }
        let (address, captured) = match callee {
            AbstractValue::Closure(address, captured) => (*address, Some(captured.clone())),
            AbstractValue::Value(Value::Alloc(Kind::Function, address)) => (*address, None),
            _ => return None,
        };
        // This is an actual caller-owned function value passed into the
        // provider, not a provider key interpreted in the caller program.
        let key = self.local_callable_origin(address)?;
        Some(
            self.invoke_selected(InvocationInput {
                key,
                args: call
                    .arguments
                    .iter()
                    .map(|argument| argument.values.clone())
                    .collect(),
                argument_selections: call
                    .arguments
                    .iter()
                    .map(|argument| argument.selection.clone())
                    .collect(),
                captured,
                fresh_checked_constructor: false,
                receiver: call.callee.receiver().cloned(),
                continuation: None,
            }),
        )
    }
}

impl ProviderExecutionContext<'_, '_, '_> {
    fn summarize(&mut self, frame: &LocalFrame, mut reached: Values) {
        if frame.remainder_summarized.get() {
            return;
        }
        reached.extend(self.activation.cells.values().flat_map(|cell| {
            self.executor.state.world.get().lexical_cells[cell.0]
                .values
                .iter()
                .cloned()
        }));
        self.executor
            .havoc_cells(self.activation.cells.values().copied().collect::<Vec<_>>());
        self.executor.local_operation_fallback(
            frame,
            reached,
            self.activation.owner,
            ExecutionPrecisionLossReason::UnsupportedOperation {
                operation: ExecutionOperation::OpaqueExecution,
                ordered_field_read_refusal: None,
                ordered_call_admission_refusal: None,
                object_spread_refusal: None,
            },
            self.activation.invocation_span,
            self.outcome,
        );
        frame.remainder_summarized.set(true);
    }

    fn allocate_cell(&mut self, binding: &Id, kind: LocalBinding, frame: &LocalFrame) {
        let initialized = frame.initialized_bindings.contains(binding) || kind == LocalBinding::Var;
        let id = self
            .executor
            .state
            .world
            .make_mut()
            .lexical_cells
            .insert(LexicalCell {
                values: frame.environment.get(binding).cloned().unwrap_or_default(),
                selection: None,
                kind,
                initialization: if initialized {
                    CellInitialization::Initialized
                } else {
                    CellInitialization::Uninitialized
                },
            });
        self.activation.cells.insert(binding.clone(), id);
        self.executor.state.world.bump_prefix_revision();
    }

    fn cell_effect(&self, binding: &Id) -> Option<EffectCell> {
        Some(EffectCell::LexicalCell {
            origin: *self.activation.index.lexical_cell_origins.get(binding)?,
            instance: *self.activation.cells.get(binding)?,
        })
    }
}

impl OwnDataContext for ProviderExecutionContext<'_, '_, '_> {
    fn heap_resolver(&self) -> SourceHeapResolver {
        self.activation.index.heap_resolver
    }
    fn permits_heap_address(&self, address: HeapAddress) -> bool {
        address.module() == Some(self.heap_resolver().scope())
            || address.module() == Some(self.executor.heap_resolver().scope())
    }
    fn object_surface(&self) -> &super::super::super::object_surface::ObjectSurfaceStore {
        self.executor.object_surface()
    }
    fn ordinary_record_shape(&self, address: HeapAddress) -> Option<&OwnPropertyShape> {
        if let Some(origin) = self.heap_resolver().source_origin(address) {
            self.activation.index.ordinary_record_shape(origin)
        } else {
            self.executor.ordinary_record_shape(address)
        }
    }
    fn own_properties(&self, address: HeapAddress) -> Option<&BTreeMap<Option<String>, Values>> {
        self.executor.own_properties(address)
    }
    fn object_properties_are_settled(&self, address: HeapAddress) -> bool {
        self.executor.object_properties_are_settled(address)
    }
    fn instance_properties_are_settled(&self, class: ClassDefinitionId) -> bool {
        self.executor.instance_properties_are_settled(class)
    }
    fn property_values(&self, address: HeapAddress, property: &str) -> Option<&Values> {
        self.executor.property_values(address, property)
    }
    fn property_is_accessor(&self, address: HeapAddress, property: &str) -> bool {
        self.executor.property_is_accessor(address, property)
    }
    fn property_is_writable(&self, address: HeapAddress, property: &str) -> bool {
        self.executor.property_is_writable(address, property)
    }
}

impl LocalContext for ProviderExecutionContext<'_, '_, '_> {
    fn function_call_method_callee(
        &mut self,
        frame: &mut LocalFrame,
        receivers: &Values,
        property: &str,
        span: Span,
        site: &SourceOperationSite,
    ) -> Option<(FunctionCallMethodSelection, EffectCoverageFragment)> {
        let identity = LocalBodyIdentity::Callable {
            key: self.activation.callable,
            owner: self.activation.owner,
        };
        if site.step != OperationStep::CallCalleeGetValue
            || site.body != identity
            || site.span != span
            || !self.body.has_source_operation_site(site)
            || !self.activation.index.source_operation_sites.contains(site)
            || !self.activation.index.callables[&self.activation.callable].calls.iter().any(|call| {
                call.kind == CallKind::Call && call.span == span
                    && call.source_site_for_body(&identity).is_some()
                    && matches!(&call.callee, ValueExpr::Member(_, Some(name), MemberKey::Static, _) if name == property)
            })
        {
            return None;
        }
        let (selection, mut effects) = self
            .executor
            .function_call_method_value_selection(receivers, property)?;
        let receipt = self.operation_support_receipt(
            frame,
            OperationSupportOperation::OwnDataRead,
            selection.receiver_address,
            &selection.dependencies,
        )?;
        effects.operation_support(receipt);
        self.record_property_read(EffectCell::ObjectShape(selection.receiver_address));
        self.record_property_read(EffectCell::OwnProperty {
            origin: selection.receiver_address,
            property: property.to_owned(),
        });
        Some((selection, effects))
    }

    fn function_apply_arguments(
        &mut self,
        frame: &mut LocalFrame,
        argument_list: &Values,
        _span: Span,
    ) -> Option<FunctionApplyArguments> {
        let expanded = self.executor.function_apply_arguments(argument_list)?;
        if let Some(object) = expanded.object {
            let receipt = self.operation_support_receipt(
                frame,
                OperationSupportOperation::OwnDataRead,
                object,
                &expanded.dependencies,
            )?;
            let mut effects = EffectCoverageFragment::modeled_own_data_access();
            effects.operation_support(receipt);
            effects.depend_on(&expanded.dependencies);
            frame.ledger.effects.compose(&effects);
            self.record_property_read(EffectCell::ObjectShape(object));
            self.record_property_read(EffectCell::ArrayContents(object));
            for key in std::iter::once("length".to_owned())
                .chain((0..expanded.arguments.len()).map(|index| index.to_string()))
            {
                self.record_property_read(EffectCell::OwnProperty {
                    origin: object,
                    property: key,
                });
            }
        }
        frame.ledger.dependencies.extend(&expanded.dependencies);
        frame.inputs.extend(
            expanded
                .arguments
                .iter()
                .flat_map(SelectedValue::retained_values),
        );
        Some(expanded)
    }

    fn rest_parameter_allocation(
        &self,
        site: &SourceOperationSite,
        allocation: u32,
    ) -> Option<HeapAddress> {
        (site.step == OperationStep::Allocate
            && site.owner == self.activation.owner
            && self.body.has_source_operation_site(site)
            && self.activation.index.source_operation_sites.contains(site)
            && self.body.parameters.iter().any(|parameter| {
                parameter.rest_allocation == Some(allocation)
                    && parameter.rest_site.as_ref() == Some(site)
            }))
        .then(|| self.heap_resolver().allocation(Kind::Object, allocation))
    }

    fn array_creation_started(&mut self, allocation: u32) {
        let address = self.heap_resolver().allocation(Kind::Object, allocation);
        if self
            .executor
            .state
            .world
            .make_mut()
            .primitive_arrays
            .remove(&address)
            .is_some()
        {
            self.executor.state.world.bump_prefix_revision();
        }
    }

    fn array_created(&mut self, allocation: u32, sparse: bool) {
        if sparse {
            let address = self.heap_resolver().allocation(Kind::Object, allocation);
            self.executor.invalidate_object_heap_proofs(address);
        }
    }

    fn array_spread(
        &mut self,
        frame: &mut LocalFrame,
        span: Span,
        operand: &Values,
    ) -> ChildTransfer<allocation_transfer::ArraySpreadResult> {
        let supported_site = self
            .activation
            .index
            .source_operation_sites
            .iter()
            .any(|site| {
                site.step == OperationStep::IterateSpread
                    && site.span == span
                    && self.body.has_source_operation_site(site)
            });
        let receipt = supported_site
            .then(|| {
                self.executor
                    .native_iterable(NativeIterableUse::CurrentArraySpread, operand)
            })
            .flatten();
        let Some(receipt) = receipt else {
            self.summarize(frame, operand.clone());
            return ChildTransfer::RemainderAfterPrefix { summarized: true };
        };
        let Some(dependencies) = receipt.coverage.initialization_domain_dependencies() else {
            self.summarize(frame, operand.clone());
            return ChildTransfer::RemainderAfterPrefix { summarized: true };
        };
        let contents = receipt.external_heap_contents(self.heap_resolver());
        self.outcome
            .effects
            .record_heap_contents(&contents, self.executor.state.heap_access_origin);
        let NativeIterableYields::ExactSlots(slots) = receipt.yields else {
            self.summarize(frame, operand.clone());
            return ChildTransfer::RemainderAfterPrefix { summarized: true };
        };
        ChildTransfer::Value(allocation_transfer::ArraySpreadResult {
            yields: allocation_transfer::ArraySpreadYields::ExactSlots(slots),
            dependencies,
        })
    }

    fn selection_prerequisites(&self) -> Box<ProofDependencies> {
        self.executor.boxed_settled_prefix_dependencies()
    }
    fn instantiate_parameter_bindings(&mut self, frame: &mut LocalFrame) {
        for (binding, kind) in &self.body.activation_bindings.clone() {
            if *kind == LocalBinding::Parameter {
                self.allocate_cell(binding, *kind, frame);
            }
        }
    }
    fn instantiate_bindings(&mut self, frame: &mut LocalFrame) {
        for (binding, kind) in &self.body.activation_bindings.clone() {
            if *kind != LocalBinding::Parameter && *kind != LocalBinding::CatchParameter {
                self.allocate_cell(binding, *kind, frame);
            }
        }
    }
    fn enter_region_bindings(
        &mut self,
        bindings: &BTreeMap<Id, LocalBinding>,
        frame: &mut LocalFrame,
    ) -> bool {
        let saved = bindings
            .keys()
            .map(|binding| (binding.clone(), self.activation.cells.get(binding).copied()))
            .collect();
        for (binding, kind) in bindings {
            self.allocate_cell(binding, *kind, frame);
        }
        self.activation.regions.push(saved);
        true
    }
    fn leave_region_bindings(&mut self, _frame: &mut LocalFrame, _refused: bool) {
        for (binding, previous) in self
            .activation
            .regions
            .pop()
            .expect("entered provider region")
        {
            if let Some(cell) = previous {
                self.activation.cells.insert(binding, cell);
            } else {
                self.activation.cells.remove(&binding);
            }
        }
    }
    fn instantiate_catch_binding(&mut self, binding: &Id, frame: &LocalFrame) {
        let previous = self.activation.cells.get(binding).copied();
        self.activation
            .regions
            .push(vec![(binding.clone(), previous)]);
        self.allocate_cell(binding, LocalBinding::CatchParameter, frame);
    }
    fn leave_catch_binding(&mut self, _binding: &Id) {
        for (binding, previous) in self
            .activation
            .regions
            .pop()
            .expect("entered provider catch")
        {
            if let Some(cell) = previous {
                self.activation.cells.insert(binding, cell);
            } else {
                self.activation.cells.remove(&binding);
            }
        }
    }
    fn local_binding_stored(&mut self, binding: &Id, frame: &LocalFrame) {
        if let Some(id) = self.activation.cells.get(binding) {
            let cell = &mut self.executor.state.world.make_mut().lexical_cells[id.0];
            cell.values = frame.environment[binding].clone();
            cell.selection = frame.binding_selections.get(binding).cloned();
            cell.initialization = CellInitialization::Initialized;
            self.executor.state.world.bump_prefix_revision();
        }
    }
    fn declarative_binding_operation(
        &mut self,
        binding: &Id,
        _frame: &LocalFrame,
        operation: BindingOperation,
    ) -> EffectCoverageFragment {
        let Some(cell) = self.cell_effect(binding) else {
            return EffectCoverageFragment::default();
        };
        match operation {
            BindingOperation::Create => {
                self.outcome.effects.heap_contents.allocations.insert(cell);
            }
            BindingOperation::Initialize | BindingOperation::Store => self
                .outcome
                .effects
                .record_heap_write(cell, self.executor.state.heap_access_origin),
        }
        EffectCoverageFragment::no_effects()
    }
    fn read_binding(
        &mut self,
        binding: &Id,
        frame: &mut LocalFrame,
        _site: Option<&SourceOperationSite>,
    ) -> Option<LocalValueResult> {
        let id = self.activation.cells.get(binding)?;
        let cell = &self.executor.state.world.get().lexical_cells[id.0];
        if cell.initialization != CellInitialization::Initialized {
            return None;
        }
        let values = cell.values.clone();
        let selection = cell
            .selection
            .as_ref()
            .filter(|selection| selection.matches(&values))
            .cloned();
        if values.is_empty() || values.contains(&AbstractValue::Value(Value::Unavailable)) {
            return None;
        }
        let effect = self.cell_effect(binding)?;
        self.outcome
            .effects
            .record_heap_read(effect, self.executor.state.heap_access_origin);
        frame.inputs.extend(values.iter().cloned());
        let mut result = normal_value(SelectedValue {
            values,
            selection,
            postcondition: None,
        });
        result
            .ledger
            .effects
            .depend_on(&self.executor.settled_prefix_dependencies());
        Some(result)
    }
    fn assign_capture(
        &mut self,
        frame: &mut LocalFrame,
        binding: &Id,
        selected: &SelectedValue,
        _span: Span,
    ) -> Option<LocalValueResult> {
        let id = *self.activation.cells.get(binding)?;
        let cell = &self.executor.state.world.get().lexical_cells[id.0];
        if cell.initialization != CellInitialization::Initialized
            || matches!(cell.kind, LocalBinding::Const | LocalBinding::NamedSelf(_))
        {
            return None;
        }
        let effect = self.cell_effect(binding)?;
        let cell = &mut self.executor.state.world.make_mut().lexical_cells[id.0];
        cell.values = selected.values.clone();
        cell.selection = selected.selection.clone();
        self.executor.state.world.bump_prefix_revision();
        self.outcome
            .effects
            .record_heap_write(effect, self.executor.state.heap_access_origin);
        frame.inputs.extend(selected.values.iter().cloned());
        Some(normal_value(selected.clone()))
    }
    fn closure(
        &mut self,
        frame: &mut LocalFrame,
        creation: DeferredCallableCreation,
    ) -> Option<LocalValueResult> {
        if creation.home_object().is_some()
            || self.activation.index.callable_creation(creation.callable()) != Some(creation)
        {
            return None;
        }
        let callable = &self.activation.index.callables[&creation.callable()];
        let reads = self
            .activation
            .index
            .prototype_callable_reads
            .get(&creation.callable())
            .unwrap_or(&callable.reads);
        let mut captures = LexicalCaptures::default();
        for binding in reads.iter().chain(callable.rebinds.iter()) {
            if callable.declared_locals.contains(binding)
                || callable.local_functions.contains_key(binding)
                || callable
                    .params
                    .iter()
                    .any(|parameter| parameter.binding.as_ref() == Some(binding))
            {
                continue;
            }
            captures
                .cells
                .insert(binding.clone(), *self.activation.cells.get(binding)?);
        }
        if matches!(creation, DeferredCallableCreation::Arrow(_)) && callable.lexical_this {
            captures.receiver = Some(frame.this_values.clone()?);
        }
        let address = self
            .heap_resolver()
            .allocation(Kind::Function, creation.callable());
        let value = self.executor.construct_provider_closure(
            address,
            InstantiatedProviderClosure {
                module: self.activation.source.scope,
                source_origin: self.activation.source.source_offset(creation.callable())?,
                source_sha256: self.activation.source.sha256.clone(),
                captures,
                retained: Values::new(),
            },
        );
        self.outcome
            .effects
            .heap_contents
            .allocations
            .insert(EffectCell::ObjectShape(address));
        frame.inputs.insert(value.clone());
        let values = Values::from([value]);
        let selection = CallableSelection::observed(&values, self.selection_prerequisites());
        Some(normal_value(SelectedValue {
            values,
            selection,
            postcondition: None,
        }))
    }
    fn call(
        &mut self,
        frame: &mut LocalFrame,
        _invocation: ForeignInvocation,
        callee: &EvaluatedCallee,
        arguments: &[SelectedValue],
        _site: &SourceOperationSite,
        _span: Span,
    ) -> Option<LocalValueResult> {
        let mut reached = callee.targets().clone();
        reached.extend(callee.receiver().into_iter().flatten().cloned());
        reached.extend(arguments.iter().flat_map(SelectedValue::retained_values));
        self.summarize(frame, reached);
        None
    }
    fn literal_layout(&self, _allocation: u32) -> allocation_transfer::LiteralLayout {
        allocation_transfer::LiteralLayout::Unsupported
    }
    fn literal_property_kind(&self, _allocation: u32, _position: usize) -> LiteralPropertyKind {
        LiteralPropertyKind::Data
    }
    fn heap(&mut self) -> allocation_transfer::LiteralHeap<'_> {
        self.executor.literal_heap()
    }
    fn record_property_read(&mut self, cell: EffectCell) {
        self.outcome
            .effects
            .record_heap_read(cell, self.executor.state.heap_access_origin);
    }
    fn record_property_write(&mut self, cell: EffectCell) {
        self.outcome
            .effects
            .record_heap_write(cell, self.executor.state.heap_access_origin);
    }
    fn operation_support_receipt(
        &self,
        _frame: &LocalFrame,
        operation: OperationSupportOperation,
        origin: HeapAddress,
        dependencies: &ProofDependencies,
    ) -> Option<OperationSupportReceipt> {
        Some(OperationSupportReceipt::pooled_object_shape(
            operation,
            origin,
            dependencies,
        ))
    }
    fn unproved_field_read(
        &mut self,
        frame: &mut LocalFrame,
        _receiver: Option<&Id>,
        receivers: &Values,
        _property: &str,
        _span: Span,
        _refusal: OrderedFieldReadRefusal,
    ) -> Option<AllocationValue> {
        self.summarize(frame, receivers.clone());
        None
    }
    fn unproved_field_write(
        &mut self,
        frame: &mut LocalFrame,
        receivers: &Values,
        values: &Values,
        _span: Span,
    ) {
        self.summarize(frame, receivers.iter().chain(values).cloned().collect());
    }
    fn unproved_operator(&mut self, frame: &mut LocalFrame, operands: &Values, _span: Span) {
        self.summarize(frame, operands.clone());
    }
    fn unproved_control_flow(&mut self, frame: &LocalFrame, _span: Span) {
        self.summarize(frame, Values::new());
    }
    fn skipped_region(
        &mut self,
        frame: &mut LocalFrame,
        region: &SourceOperationSite,
        frontier: &SourceOperationSite,
        reason: SkippedCallReason,
    ) -> bool {
        let callable = &self.activation.index.callables[&self.activation.callable];
        self.activation.index.record_skipped_region(
            callable.calls.iter(),
            &LocalBodyIdentity::Callable {
                key: self.activation.callable,
                owner: callable.owner,
            },
            frame,
            region,
            frontier,
            reason,
        )
    }
    fn publish_throw(&mut self, values: &Values) {
        self.outcome.reached.extend(values.iter().cloned());
    }
}
