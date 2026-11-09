//! Defunctionalized source continuation. A call yields after its operands have
//! run; the same unevaluated stack can then be resumed in each returned world.

use super::super::allocation_transfer::ArraySpreadYields;
use super::*;
use std::sync::Arc;
use swc_ecma_ast::BinaryOp;

#[derive(Clone)]
enum AggregateTransfer {
    Record {
        allocation: u32,
        address: HeapAddress,
        transfer: Box<allocation_transfer::RecordTransfer>,
        was_new: bool,
    },
    Array(Box<allocation_transfer::ArrayTransfer>),
}

impl AggregateTransfer {
    fn retained_values(&self) -> Values {
        match self {
            Self::Record { transfer, .. } => transfer.retained_values(),
            Self::Array(transfer) => transfer.retained_values(),
        }
    }

    fn abort(self, context: &mut LocalValueContext<'_, impl LocalContext>) {
        match self {
            Self::Record { transfer, .. } => transfer.abort(context),
            Self::Array(transfer) => transfer.abort(context),
        }
    }
}

#[derive(Clone)]
pub(in crate::facts::sync_effects) struct LocalCallRequest {
    pub(in crate::facts::sync_effects) invocation: ForeignInvocation,
    pub(in crate::facts::sync_effects) callee: EvaluatedCallee,
    pub(in crate::facts::sync_effects) arguments: Vec<SelectedValue>,
    /// Exact argument-spread operations whose operands were expanded by the
    /// shared iterator transfer. The roots in `retained_operands` remain
    /// non-executable evidence for any later conservative refusal.
    pub(in crate::facts::sync_effects) spread_sites: BTreeMap<usize, SourceOperationSite>,
    pub(in crate::facts::sync_effects) spread_expanded: bool,
    pub(in crate::facts::sync_effects) retained_operands: Values,
    pub(in crate::facts::sync_effects) source_site: SourceOperationSite,
    pub(in crate::facts::sync_effects) span: Span,
}

pub(in crate::facts::sync_effects) enum LocalSuspension {
    Call(LocalCallRequest),
    Complete(LocalExecutionAttempt),
    Fork(Vec<LocalContinuation>),
}

#[derive(Clone)]
struct CallSites {
    invocation: SourceOperationSite,
    getvalue: SourceOperationSite,
    receiver: Option<SourceOperationSite>,
    receiver_check: Option<SourceOperationSite>,
    callee_check: Option<SourceOperationSite>,
    spread_sites: BTreeMap<usize, SourceOperationSite>,
}

impl CallSites {
    fn from_expression(node: &LocalExpression, call: &LocalCall) -> Result<Self, ()> {
        let required = |step| node.source_site_for_step(step).cloned().ok_or(());
        let (receiver, receiver_check) = match &call.callee {
            LocalCallee::Member {
                optional_receiver, ..
            } => (
                Some(required(OperationStep::CalleeReceiver)?),
                optional_receiver
                    .then(|| required(OperationStep::OptionalReceiverCheck))
                    .transpose()?,
            ),
            _ => (None, None),
        };
        let mut spread_sites = BTreeMap::new();
        for index in call.spread_arguments.keys() {
            let site = call.spread_sites.get(index).cloned().ok_or(())?;
            if site.step != OperationStep::IterateSpread {
                return Err(());
            }
            spread_sites.insert(*index, site);
        }
        Ok(Self {
            invocation: required(OperationStep::CallInvocation)?,
            getvalue: required(OperationStep::CallCalleeGetValue)?,
            receiver,
            receiver_check,
            callee_check: matches!(call.invocation, LocalInvocation::OptionalCall)
                .then(|| required(OperationStep::OptionalCalleeCheck))
                .transpose()?,
            spread_sites,
        })
    }
}

#[derive(Clone)]
enum Step {
    Prepare,
    Parameters(usize),
    BindParameter(Id),
    PrepareBody,
    Operations(Arc<[LocalOperation]>, usize),
    Expression(LocalExpression),
    RecordNext {
        aggregate: u64,
        properties: Arc<[PropertyDefinition<LocalExpression>]>,
        index: usize,
    },
    RecordValue {
        aggregate: u64,
        index: usize,
    },
    ArrayNext {
        aggregate: u64,
        elements: Arc<[LocalArrayElement]>,
        index: usize,
    },
    ArrayValue(u64),
    ArraySpread {
        aggregate: u64,
        span: Span,
    },
    FinishAggregate(u64),
    SetConstruction {
        source: ValueExpr,
        site: CallSite,
        source_site: SourceOperationSite,
    },
    DefineThisField(ClassFieldKey),
    AssignPublicThisField {
        property: String,
        class: ClassDefinitionId,
    },
    AssignPrivateThisField(String),
    Selected(SelectedValue),
    ClassSteps(Arc<[ClassStaticStep]>, usize),
    ClassBlock(Arc<[LocalExpression]>, usize),
    ClassValue {
        property: Option<String>,
        may_escape: bool,
    },
    LeaveClass {
        selected: SelectedValue,
        previous_this: Option<Values>,
    },
    ConditionalSelect {
        consequent: LocalExpression,
        alternate: LocalExpression,
    },
    LogicalSelect {
        operator: LocalLogicalOperator,
        right: LocalExpression,
    },
    Initialize(Id),
    InitializeObject(ObjectBindingPattern),
    StoreField {
        receivers: Values,
        module_prefix: bool,
        property: String,
        span: Span,
        source_site: Option<SourceOperationSite>,
    },
    Return(SourceOperationSite),
    Throw(SourceOperationSite),
    Discard,
    Sequence(Arc<[LocalExpression]>, usize),
    Template {
        substitutions: Arc<[LocalExpression]>,
        index: usize,
        span: Span,
        source_site: Option<SourceOperationSite>,
    },
    TemplateConvert {
        span: Span,
        source_site: Option<SourceOperationSite>,
    },
    Assign(Id, Span),
    BinaryLeft {
        operator: BinaryOp,
        right: LocalExpression,
        span: Span,
        frontier: Option<SourceOperationSite>,
    },
    BinaryRight {
        operator: BinaryOp,
        left: SelectedValue,
        span: Span,
        frontier: Option<SourceOperationSite>,
    },
    MemberReceiver {
        call: LocalCall,
        sites: Box<CallSites>,
    },
    MemberGet {
        call: LocalCall,
        receivers: Values,
        receiver_selection: Option<CallableSelection>,
        sites: Box<CallSites>,
    },
    CallCallee {
        call: LocalCall,
        callee: EvaluatedCallee,
        sites: Box<CallSites>,
    },
    InvokeArguments {
        call: LocalCall,
        callee: EvaluatedCallee,
        sites: Box<CallSites>,
    },
    ReadReceiver {
        property: String,
        optional_receiver: bool,
        receiver_check: Option<SourceOperationSite>,
        getvalue_site: SourceOperationSite,
        span: Span,
    },
    ReadMember {
        getvalue_site: SourceOperationSite,
        receivers: Values,
        property: String,
        span: Span,
    },
    Arguments {
        call: LocalCall,
        callee: EvaluatedCallee,
        arguments: Vec<SelectedValue>,
        retained_arguments: Values,
        sites: Box<CallSites>,
        source_index: usize,
    },
    IfSelect {
        consequent: LocalRegion,
        alternate: Option<LocalRegion>,
        frontier: SourceOperationSite,
    },
    EnterBranch(Option<LocalRegion>),
    LeaveRegion(LocalRegionSnapshot),
    Catch {
        region: LocalRegion,
        binding: Option<Id>,
        handler: LocalRegion,
    },
    LeaveCatch(Option<Id>, LocalRegion),
}

#[derive(Clone)]
pub(in crate::facts::sync_effects) struct LocalContinuation {
    pub(in crate::facts::sync_effects) frame: LocalFrame,
    judgment: AllocationJudgment,
    steps: Vec<Step>,
    value: Option<SelectedValue>,
    refused: bool,
    branch_budget: usize,
    aggregates: BTreeMap<u64, AggregateTransfer>,
    next_aggregate: u64,
}

impl LocalContinuation {
    pub(in crate::facts::sync_effects) fn new(frame: LocalFrame) -> Self {
        Self {
            frame,
            judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
            steps: vec![Step::Prepare],
            value: None,
            refused: false,
            branch_budget: 32,
            aggregates: BTreeMap::new(),
            next_aggregate: 0,
        }
    }

    #[cfg(test)]
    pub(in crate::facts::sync_effects) fn pending_argument_context(&self) -> Option<String> {
        self.steps.iter().rev().find_map(|step| match step {
            Step::Arguments {
                call, arguments, ..
            } => {
                let optional_receiver = matches!(
                    &call.callee,
                    LocalCallee::Member {
                        optional_receiver: true,
                        ..
                    }
                );
                let optional_callee = matches!(call.invocation, LocalInvocation::OptionalCall);
                Some(format!(
                    "call={:?} argument_index={} optional_receiver={optional_receiver} optional_callee={optional_callee}",
                    call.span,
                    arguments.len(),
                ))
            }
            _ => None,
        })
    }

    pub(in crate::facts::sync_effects) fn new_with_judgment(
        frame: LocalFrame,
        judgment: AllocationJudgment,
    ) -> Self {
        let mut continuation = Self::new(frame);
        continuation.judgment = judgment;
        continuation
    }

    fn begin_aggregate(&mut self, aggregate: AggregateTransfer) -> Result<u64, ()> {
        let id = self.next_aggregate;
        self.next_aggregate = id.checked_add(1).ok_or(())?;
        if self.aggregates.insert(id, aggregate).is_some() {
            return Err(());
        }
        Ok(id)
    }

    /// Roots in evaluated operands are live even when no lexical binding names
    /// them. In particular the selected callee precedes argument-side stores.
    pub(in crate::facts::sync_effects) fn retained_values(&self) -> Values {
        let mut roots = self.frame.inputs.clone();
        roots.extend(self.frame.activation.retained_values());
        roots.extend(self.frame.environment.values().flatten().cloned());
        roots.extend(self.frame.reached_values.iter().cloned());
        for postcondition in self.frame.binding_postconditions.values() {
            roots.extend(postcondition.retained_values());
        }
        roots.extend(self.frame.this_values.iter().flatten().cloned());
        roots.extend(
            self.frame
                .parameter_inputs
                .iter()
                .flatten()
                .flat_map(SelectedValue::retained_values),
        );
        let (normal, abrupt, non_normal) = self.frame.ledger.completion.clone().into_alternatives();
        roots.extend(local_abrupt_values(&abrupt, non_normal));
        for path in &abrupt {
            if let AbruptAlternative::Exact(thrown) = path {
                roots.extend(thrown.value.retained_values());
            }
        }
        match normal {
            Some(LocalNormal::Return { selected, .. }) => roots.extend(selected.retained_values()),
            Some(LocalNormal::Unknown(selected)) => roots.extend(selected.retained_values()),
            Some(LocalNormal::Next) | None => {}
        }
        if let Some(value) = &self.value {
            roots.extend(value.retained_values());
        }
        for aggregate in self.aggregates.values() {
            roots.extend(aggregate.retained_values());
        }
        for step in &self.steps {
            match step {
                Step::BinaryRight { left, .. } | Step::Selected(left) => {
                    roots.extend(left.retained_values())
                }
                Step::StoreField { receivers, .. }
                | Step::MemberGet { receivers, .. }
                | Step::ReadMember { receivers, .. } => roots.extend(receivers.iter().cloned()),
                Step::CallCallee { callee, .. } | Step::InvokeArguments { callee, .. } => {
                    roots.extend(callee.targets().iter().cloned());
                    roots.extend(callee.receiver().into_iter().flatten().cloned());
                }
                Step::Arguments {
                    callee, arguments, ..
                } => {
                    roots.extend(callee.targets().iter().cloned());
                    roots.extend(callee.receiver().into_iter().flatten().cloned());
                    roots.extend(arguments.iter().flat_map(SelectedValue::retained_values));
                }
                Step::LeaveClass {
                    selected,
                    previous_this,
                } => {
                    roots.extend(selected.retained_values());
                    roots.extend(previous_this.iter().flatten().cloned());
                }
                Step::LeaveRegion(snapshot) => {
                    for entry in &snapshot.0 {
                        if let Some(postcondition) = &entry.postcondition {
                            roots.extend(postcondition.retained_values());
                        }
                    }
                    roots.extend(
                        snapshot
                            .0
                            .iter()
                            .flat_map(|entry| entry.environment.iter().flatten())
                            .cloned(),
                    );
                }
                Step::SetConstruction { .. } => {}
                _ => {}
            }
        }
        roots
    }

    pub(in crate::facts::sync_effects) fn resume_call(
        &mut self,
        context: &mut impl LocalContext,
        result: Option<LocalValueResult>,
    ) {
        self.value = result.and_then(|result| {
            result.absorb(&mut self.frame, &mut self.judgment, context.heap_resolver())
        });
        if self.value.is_none() {
            self.refused = true;
            self.frame.mark_opaque();
        }
    }

    pub(in crate::facts::sync_effects) fn refuse_branching(&mut self) {
        self.refused = true;
        self.frame.mark_opaque();
    }

    fn call_request(
        &mut self,
        context: &mut impl LocalContext,
        call: LocalCall,
        mut callee: EvaluatedCallee,
        mut arguments: Vec<SelectedValue>,
        retained_arguments: Values,
        sites: CallSites,
    ) -> Result<LocalCallRequest, ()> {
        let mut retained_operands = callee.targets().clone();
        retained_operands.extend(callee.receiver().into_iter().flatten().cloned());
        retained_operands.extend(arguments.iter().flat_map(SelectedValue::retained_values));
        retained_operands.extend(retained_arguments);
        // A failed method-argument normalization is a refusal after the
        // source operands have run. Keep those roots before asking the adapter
        // whether the evaluated apply-list can be expanded.
        self.frame.inputs.extend(retained_operands.iter().cloned());
        let spread_expanded = !call.spread_arguments.is_empty();
        let spread_sites = call
            .spread_arguments
            .keys()
            .map(|index| {
                sites
                    .spread_sites
                    .get(index)
                    .cloned()
                    .map(|site| (*index, site))
                    .ok_or(())
            })
            .collect::<Result<BTreeMap<_, _>, _>>()?;

        if let EvaluatedCallee::FunctionCallMethod {
            method,
            target,
            receiver,
            dependencies,
        } = &mut callee
        {
            self.frame.ledger.dependencies.extend(dependencies.as_ref());
            self.frame.ledger.effects.depend_on(dependencies);
            let undefined = || SelectedValue::raw(Values::from([AbstractValue::UNDEFINED]));
            let (this_value, forwarded) = match method {
                FunctionCallMethod::Call => {
                    let this_value = if arguments.is_empty() {
                        undefined()
                    } else {
                        arguments.remove(0)
                    };
                    (this_value.values, arguments)
                }
                FunctionCallMethod::Apply => {
                    let this_value = arguments.first().cloned().unwrap_or_else(undefined);
                    let argument_list = arguments
                        .get(1)
                        .map(|value| value.values.clone())
                        .unwrap_or_else(|| Values::from([AbstractValue::UNDEFINED]));
                    retained_operands.extend(argument_list.iter().cloned());
                    let apply = context
                        .function_apply_arguments(&mut self.frame, &argument_list, call.span)
                        .ok_or(())?;
                    self.frame.ledger.dependencies.extend(&apply.dependencies);
                    self.frame.ledger.effects.depend_on(&apply.dependencies);
                    retained_operands.extend(
                        apply
                            .arguments
                            .iter()
                            .flat_map(SelectedValue::retained_values),
                    );
                    (this_value.values, apply.arguments)
                }
            };
            retained_operands.extend(target.values.iter().cloned());
            *receiver = Some(this_value);
            arguments = forwarded;
        }

        self.frame.inputs.extend(retained_operands.iter().cloned());
        Ok(LocalCallRequest {
            invocation: call.invocation.foreign_invocation(),
            callee,
            arguments,
            spread_sites,
            spread_expanded,
            retained_operands,
            source_site: sites.invocation,
            span: call.span,
        })
    }

    fn enter_region(
        &mut self,
        context: &mut impl LocalContext,
        region: &LocalRegion,
    ) -> Option<()> {
        let snapshot = LocalRegionSnapshot::enter(context, &region.bindings, &mut self.frame)?;
        self.steps.push(Step::LeaveRegion(snapshot));
        self.steps
            .push(Step::Operations(region.operations.clone(), 0));
        Some(())
    }

    /// Scalar expression adapters run the same stack without entering or
    /// finishing the surrounding body. Cleanup steps still run on refusal.
    pub(super) fn evaluate_expression(
        context: &mut impl LocalContext,
        body: &LocalBody,
        frame: &mut LocalFrame,
        judgment: &mut AllocationJudgment,
        expression: LocalExpression,
    ) -> Option<SelectedValue> {
        let owned = std::mem::replace(frame, LocalFrame::new(Context::new()));
        let mut continuation = Self::new(owned);
        continuation.judgment = judgment.clone();
        continuation.steps = vec![Step::Expression(expression)];
        while let Some(suspension) = continuation.poll_steps(context, body) {
            match suspension {
                LocalSuspension::Call(call) => {
                    let result = context.call(
                        &mut continuation.frame,
                        call.invocation,
                        &call.callee,
                        &call.arguments,
                        &call.source_site,
                        call.span,
                    );
                    continuation.resume_call(context, result);
                }
                LocalSuspension::Fork(_) => continuation.refuse_branching(),
                LocalSuspension::Complete(_) => unreachable!("step polling does not finish a body"),
            }
        }
        let selected = if continuation.refused {
            None
        } else if continuation.frame.ledger.completion.has_no_normal_path() {
            // The scalar expression API carries abrupt control in the frame.
            // An empty payload permits unwinding without fabricating a value.
            Some(SelectedValue::raw(Values::new()))
        } else {
            continuation.value
        };
        *frame = continuation.frame;
        *judgment = continuation.judgment;
        selected
    }

    fn poll_steps(
        &mut self,
        context: &mut impl LocalContext,
        body: &LocalBody,
    ) -> Option<LocalSuspension> {
        while let Some(step) = self.steps.pop() {
            // Unwind lexical scopes on every terminal completion and on refusal.
            // Only a handler may consume Throw; a return never becomes Next here.
            let control = completion_region_action(&self.frame);
            if !matches!(
                step,
                Step::Prepare
                    | Step::LeaveRegion(_)
                    | Step::Catch { .. }
                    | Step::LeaveCatch(..)
                    | Step::LeaveClass { .. }
                    | Step::FinishAggregate(_)
            ) && (self.refused
                || !matches!(
                    control,
                    RegionCompletionAction::Continue | RegionCompletionAction::ContinueMayNormal
                ))
            {
                if control == RegionCompletionAction::Refuse {
                    self.refused = true;
                }
                self.value = None;
                continue;
            }
            let result = self.step(context, body, step);
            match result {
                Ok(Some(suspension)) => return Some(suspension),
                Ok(None) => {}
                Err(()) => {
                    self.refused = true;
                    self.frame.mark_opaque();
                }
            }
        }
        None
    }

    pub(in crate::facts::sync_effects) fn poll(
        &mut self,
        context: &mut impl LocalContext,
        body: &LocalBody,
    ) -> LocalSuspension {
        if let Some(suspension) = self.poll_steps(context, body) {
            return suspension;
        }
        if matches!(self.frame.activation, LocalActivation::Evaluating) {
            self.frame.activation = if !self.refused
                && matches!(self.frame.ledger.completion, Completion::Abrupt(_))
            {
                LocalActivation::Transferred(Box::new(self.frame.ledger.clone().map_normal(|_| ())))
            } else {
                LocalActivation::Refused
            };
        }
        if !self.refused && finish_body_control(context, body, &mut self.frame).is_none() {
            self.refused = true;
        }
        let attempt = if self.refused {
            LocalExecutionAttempt::Refused {
                result: finish_refused(&mut self.frame),
                remainder: if self.frame.remainder_summarized.get() {
                    RemainderDisposition::AlreadySummarized
                } else {
                    RemainderDisposition::NeedsSummary
                },
            }
        } else {
            LocalExecutionAttempt::Completed(finish(&self.frame, self.judgment.clone()))
        };
        LocalSuspension::Complete(attempt)
    }

    fn step(
        &mut self,
        context: &mut impl LocalContext,
        body: &LocalBody,
        step: Step,
    ) -> Result<Option<LocalSuspension>, ()> {
        match step {
            Step::Prepare => {
                if self.frame.parameter_inputs.is_some() {
                    self.frame.activation = LocalActivation::Evaluating;
                }
                prepare_body_prefix(context, body, &mut self.frame).ok_or(())?;
                self.steps
                    .push(Step::Operations(body.operations.clone(), 0));
                self.steps.push(Step::PrepareBody);
                if self.frame.parameter_inputs.is_some() {
                    instantiate_parameters(context, body, &mut self.frame);
                    self.steps.push(Step::Parameters(0));
                }
            }
            Step::Parameters(index) => {
                let Some(parameter) = body.parameters.get(index) else {
                    return Ok(None);
                };
                self.steps.push(Step::Parameters(index + 1));
                let mut selected = self
                    .frame
                    .parameter_inputs
                    .as_ref()
                    .expect("selected invocation parameters")
                    .get(index)
                    .cloned()
                    .unwrap_or_else(|| {
                        SelectedValue::raw(Values::from([AbstractValue::UNDEFINED]))
                    });
                if let Some(default) = &parameter.default {
                    let (may_default, supplied) =
                        split_primitive_test(&selected.values, PrimKinds::UNDEFINED)?;
                    if may_default {
                        selected.values = supplied;
                        if !selected.values.is_empty() {
                            if self.branch_budget < 2 {
                                return Err(());
                            }
                            self.frame
                                .ledger
                                .effects
                                .compose(&EffectCoverageFragment::default());
                            let mut default_path = self.clone();
                            let mut supplied_path = self.clone();
                            default_path.branch_budget = self.branch_budget / 2;
                            supplied_path.branch_budget =
                                self.branch_budget - default_path.branch_budget;
                            default_path
                                .steps
                                .push(Step::BindParameter(parameter.binding.clone()));
                            default_path.steps.push(Step::Expression(default.clone()));
                            // Selection remains valid only if the retained operand still matches.
                            selected.selection = selected
                                .selection
                                .filter(|selection| selection.matches(&selected.values));
                            supplied_path.value = Some(selected);
                            supplied_path
                                .steps
                                .push(Step::BindParameter(parameter.binding.clone()));
                            return Ok(Some(LocalSuspension::Fork(vec![
                                default_path,
                                supplied_path,
                            ])));
                        }
                        self.steps
                            .push(Step::BindParameter(parameter.binding.clone()));
                        self.steps.push(Step::Expression(default.clone()));
                        return Ok(None);
                    }
                }
                if selected.values.is_empty() {
                    return Err(());
                }
                if let Some(allocation) = parameter.rest_allocation {
                    let site = parameter.rest_site.as_ref().ok_or(())?;
                    let arguments = self
                        .frame
                        .parameter_inputs
                        .as_ref()
                        .expect("selected invocation parameters")
                        .iter()
                        .skip(index)
                        .map(|argument| argument.values.clone())
                        .collect::<Vec<_>>();
                    let selected = rest_parameter_array(
                        context,
                        &mut self.frame,
                        &mut self.judgment,
                        site,
                        allocation,
                        &arguments,
                    )
                    .ok_or(())?;
                    bind_parameter(context, &mut self.frame, &parameter.binding, selected);
                } else {
                    bind_parameter(context, &mut self.frame, &parameter.binding, selected);
                }
            }
            Step::BindParameter(binding) => {
                let selected = self.take_value()?;
                bind_parameter(context, &mut self.frame, &binding, selected);
            }
            Step::PrepareBody => {
                prepare_body_bindings(context, body, &mut self.frame).ok_or(())?;
                if matches!(self.frame.activation, LocalActivation::Evaluating) {
                    self.frame.activation = LocalActivation::Transferred(Box::new(
                        self.frame.ledger.clone().map_normal(|_| ()),
                    ));
                }
            }
            Step::Operations(operations, index) => {
                let Some(operation) = operations.get(index) else {
                    return Ok(None);
                };
                self.steps
                    .push(Step::Operations(operations.clone(), index + 1));
                match operation.without_source() {
                    LocalOperation::Initialize(binding, Some(value)) => {
                        self.steps.push(Step::Initialize(binding.clone()));
                        self.steps.push(Step::Expression(value.clone()));
                    }
                    LocalOperation::Initialize(binding, None)
                        if !matches!(
                            body.bindings[binding],
                            LocalBinding::Var
                                | LocalBinding::Parameter
                                | LocalBinding::HoistedFunction(_)
                        ) =>
                    {
                        self.value =
                            Some(SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])));
                        self.steps.push(Step::Initialize(binding.clone()));
                    }
                    LocalOperation::Initialize(_, None) => {}
                    LocalOperation::InitializeObject { pattern, value } => {
                        self.steps.push(Step::InitializeObject(pattern.clone()));
                        self.steps.push(Step::Expression(value.clone()));
                    }
                    LocalOperation::Evaluate(value) => {
                        self.steps.push(Step::Discard);
                        self.steps.push(Step::Expression(value.clone()));
                    }
                    LocalOperation::Return(value) => {
                        let site = operation
                            .source_site()
                            .filter(|site| site.step == OperationStep::Return)
                            .cloned()
                            .ok_or(())?;
                        self.steps.push(Step::Return(site));
                        if let Some(value) = value {
                            self.steps.push(Step::Expression(value.clone()));
                        } else {
                            self.value =
                                Some(SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])));
                        }
                    }
                    LocalOperation::Throw(value) => {
                        let site = operation
                            .source_site()
                            .filter(|site| site.step == OperationStep::Throw)
                            .cloned()
                            .ok_or(())?;
                        self.steps.push(Step::Throw(site));
                        self.steps.push(Step::Expression(value.clone()));
                    }
                    LocalOperation::If {
                        test,
                        consequent,
                        alternate,
                    } => {
                        let frontier = test
                            .source_site_for_step(OperationStep::IfTest)
                            .cloned()
                            .ok_or(())?;
                        self.steps.push(Step::IfSelect {
                            consequent: consequent.clone(),
                            alternate: alternate.clone(),
                            frontier,
                        });
                        self.steps.push(Step::Expression(test.clone()));
                    }
                    LocalOperation::TryCatch {
                        body: region,
                        binding,
                        handler,
                    } => {
                        self.steps.push(Step::Catch {
                            region: region.clone(),
                            binding: binding.clone(),
                            handler: handler.clone(),
                        });
                        self.enter_region(context, region).ok_or(())?;
                    }
                    LocalOperation::Sourced { .. } => {
                        unreachable!("source wrapper is removed before operation transfer")
                    }
                }
            }
            Step::Expression(node) => match node.without_source() {
                LocalExpression::ClassDefinition(definition) => {
                    let (selected, previous_this) = begin_class_definition(
                        definition,
                        context,
                        &mut self.frame,
                        &mut self.judgment,
                    )
                    .ok_or(())?;
                    self.steps.push(Step::LeaveClass {
                        selected,
                        previous_this,
                    });
                    self.steps
                        .push(Step::ClassSteps(definition.static_steps.clone(), 0));
                }
                LocalExpression::Record {
                    allocation,
                    properties,
                } => {
                    if !matches!(context.literal_layout(*allocation), LiteralLayout::Record) {
                        return Err(());
                    }
                    let address = context
                        .heap_resolver()
                        .allocation(Kind::Object, *allocation);
                    let was_new = !context.heap().surface.has_surface_state(address);
                    let transfer = allocation_transfer::RecordTransfer::begin(
                        &mut LocalValueContext {
                            context,
                            frame: &mut self.frame,
                            source_site: None,
                        },
                        *allocation,
                        properties,
                    )
                    .ok_or(())?;
                    let aggregate = self.begin_aggregate(AggregateTransfer::Record {
                        allocation: *allocation,
                        address,
                        transfer: Box::new(transfer),
                        was_new,
                    })?;
                    self.steps.push(Step::FinishAggregate(aggregate));
                    self.steps.push(Step::RecordNext {
                        aggregate,
                        properties: properties.clone(),
                        index: 0,
                    });
                }
                LocalExpression::Array {
                    allocation,
                    elements,
                } => {
                    let transfer = allocation_transfer::ArrayTransfer::begin(
                        &mut LocalValueContext {
                            context,
                            frame: &mut self.frame,
                            source_site: None,
                        },
                        *allocation,
                    );
                    let aggregate =
                        self.begin_aggregate(AggregateTransfer::Array(Box::new(transfer)))?;
                    self.steps.push(Step::FinishAggregate(aggregate));
                    self.steps.push(Step::ArrayNext {
                        aggregate,
                        elements: elements.clone(),
                        index: 0,
                    });
                }
                LocalExpression::SetConstruction {
                    array,
                    source,
                    site,
                } => {
                    let source_site = node
                        .source_site_for_step(OperationStep::CallInvocation)
                        .cloned()
                        .ok_or(())?;
                    self.steps.push(Step::SetConstruction {
                        source: source.clone(),
                        site: *site,
                        source_site,
                    });
                    self.steps.push(Step::Expression(array.as_ref().clone()));
                }
                LocalExpression::Call(call) => {
                    let sites = Box::new(CallSites::from_expression(&node, call)?);
                    if let LocalCallee::Member {
                        receiver,
                        optional_receiver,
                        ..
                    } = &call.callee
                        && (*optional_receiver || expression_binding(receiver).is_none())
                    {
                        self.steps.push(Step::MemberReceiver {
                            call: call.clone(),
                            sites,
                        });
                        self.steps.push(Step::Expression(receiver.as_ref().clone()));
                    } else {
                        let Some(callee) = select_call_callee(
                            context,
                            call,
                            Some(&sites.getvalue),
                            &mut self.frame,
                            &mut self.judgment,
                        ) else {
                            return if self.frame.ledger.completion.has_no_normal_path() {
                                Ok(None)
                            } else {
                                Err(())
                            };
                        };
                        self.steps.push(Step::CallCallee {
                            call: call.clone(),
                            callee,
                            sites,
                        });
                    }
                }
                LocalExpression::ReadField {
                    receiver,
                    property,
                    span,
                    optional_receiver,
                } => {
                    if !*optional_receiver && let Some(binding) = expression_binding(receiver) {
                        let getvalue_site = node
                            .source_site_for_step(OperationStep::FieldGetValue)
                            .ok_or(())?;
                        self.value = read_field_expression(
                            context,
                            binding,
                            property,
                            span,
                            Some(getvalue_site),
                            &mut self.frame,
                            &mut self.judgment,
                        );
                        if self.value.is_none()
                            && !self.frame.ledger.completion.has_no_normal_path()
                        {
                            return Err(());
                        }
                    } else {
                        self.steps.push(Step::ReadReceiver {
                            property: property.clone(),
                            optional_receiver: *optional_receiver,
                            receiver_check: optional_receiver
                                .then(|| {
                                    node.source_site_for_step(OperationStep::OptionalReceiverCheck)
                                        .cloned()
                                        .ok_or(())
                                })
                                .transpose()?,
                            getvalue_site: node
                                .source_site_for_step(OperationStep::FieldGetValue)
                                .cloned()
                                .ok_or(())?,
                            span: *span,
                        });
                        self.steps.push(Step::Expression(receiver.as_ref().clone()));
                    }
                }
                LocalExpression::Conditional {
                    test,
                    consequent,
                    alternate,
                    ..
                } => {
                    self.steps.push(Step::ConditionalSelect {
                        consequent: consequent.as_ref().clone(),
                        alternate: alternate.as_ref().clone(),
                    });
                    self.steps.push(Step::Expression(test.as_ref().clone()));
                }
                LocalExpression::Logical {
                    op, left, right, ..
                } => {
                    self.steps.push(Step::LogicalSelect {
                        operator: *op,
                        right: right.as_ref().clone(),
                    });
                    self.steps.push(Step::Expression(left.as_ref().clone()));
                }
                LocalExpression::Template {
                    substitutions,
                    span,
                } => {
                    self.steps.push(Step::Template {
                        substitutions: substitutions.clone(),
                        index: 0,
                        span: *span,
                        source_site: node
                            .source_site_for_step(OperationStep::TemplateCoercion)
                            .cloned(),
                    });
                }
                LocalExpression::Sequence(expressions) => {
                    self.steps.push(Step::Sequence(expressions.clone(), 0));
                }
                LocalExpression::AssignField {
                    receiver,
                    property,
                    value,
                    span,
                } => {
                    let receiver_source_site = node
                        .source_site_for_step(OperationStep::FieldGetValue)
                        .ok_or(())?;
                    let reference = select_field_reference(
                        &mut LocalValueContext {
                            context,
                            frame: &mut self.frame,
                            source_site: None,
                        },
                        receiver,
                        Some(receiver_source_site),
                        &mut self.judgment,
                    );
                    let Some((receivers, module_prefix)) = reference else {
                        return if self.frame.ledger.completion.has_no_normal_path() {
                            Ok(None)
                        } else {
                            Err(())
                        };
                    };
                    self.steps.push(Step::StoreField {
                        receivers,
                        module_prefix,
                        property: property.clone(),
                        span: *span,
                        source_site: node
                            .source_site_for_step(OperationStep::FieldPutValue)
                            .cloned(),
                    });
                    self.steps.push(Step::Expression(value.as_ref().clone()));
                }
                LocalExpression::Assign {
                    binding,
                    value,
                    span,
                } => {
                    self.steps.push(Step::Assign(binding.clone(), *span));
                    self.steps.push(Step::Expression(value.as_ref().clone()));
                }
                LocalExpression::DefineThisField { property, value } => {
                    self.steps.push(Step::DefineThisField(property.clone()));
                    self.steps.push(Step::Expression(value.as_ref().clone()));
                }
                LocalExpression::AssignPublicThisField { property, value } => {
                    let class = constructor_receiver(context, &mut self.frame, &self.judgment)
                        .ok_or(())?
                        .class;
                    self.steps.push(Step::AssignPublicThisField {
                        property: property.clone(),
                        class,
                    });
                    self.steps.push(Step::Expression(value.as_ref().clone()));
                }
                LocalExpression::AssignPrivateThisField { property, value } => {
                    self.steps
                        .push(Step::AssignPrivateThisField(property.clone()));
                    self.steps.push(Step::Expression(value.as_ref().clone()));
                }
                LocalExpression::Binary {
                    operator,
                    left,
                    right,
                    span,
                } => {
                    self.steps.push(Step::BinaryLeft {
                        operator: *operator,
                        right: right.as_ref().clone(),
                        span: *span,
                        frontier: node
                            .source_site_for_step(OperationStep::BinaryOperator)
                            .cloned(),
                    });
                    self.steps.push(Step::Expression(left.as_ref().clone()));
                }
                _ => {
                    self.value =
                        expression(context, &node, body, &mut self.frame, &mut self.judgment);
                    if self.value.is_none() {
                        return Err(());
                    }
                }
            },
            Step::RecordNext {
                aggregate,
                properties,
                index,
            } => {
                if let Some(property) = properties.get(index) {
                    let value = property.value().clone();
                    self.steps.push(Step::RecordNext {
                        aggregate,
                        properties,
                        index: index + 1,
                    });
                    self.steps.push(Step::RecordValue { aggregate, index });
                    self.steps.push(Step::Expression(value));
                }
            }
            Step::RecordValue { aggregate, index } => {
                let selected = self.take_value()?;
                self.frame.inputs.extend(selected.values.iter().cloned());
                let Some(AggregateTransfer::Record {
                    address,
                    transfer,
                    was_new,
                    ..
                }) = self.aggregates.get_mut(&aggregate)
                else {
                    return Err(());
                };
                transfer.record_child(index, &selected.values).ok_or(())?;
                if context.heap().surface.has_surface_state(*address) {
                    *was_new = false;
                }
            }
            Step::ArrayNext {
                aggregate,
                elements,
                index,
            } => match elements.get(index) {
                None => {}
                Some(LocalArrayElement::Hole) => {
                    let Some(AggregateTransfer::Array(transfer)) =
                        self.aggregates.get_mut(&aggregate)
                    else {
                        return Err(());
                    };
                    transfer.hole();
                    self.steps.push(Step::ArrayNext {
                        aggregate,
                        elements,
                        index: index + 1,
                    });
                }
                Some(LocalArrayElement::Value(value)) => {
                    let value = value.clone();
                    self.steps.push(Step::ArrayNext {
                        aggregate,
                        elements,
                        index: index + 1,
                    });
                    self.steps.push(Step::ArrayValue(aggregate));
                    self.steps.push(Step::Expression(value));
                }
                Some(LocalArrayElement::Spread { value, span }) => {
                    let value = value.clone();
                    let span = *span;
                    let Some(AggregateTransfer::Array(transfer)) =
                        self.aggregates.get_mut(&aggregate)
                    else {
                        return Err(());
                    };
                    transfer.start_spread();
                    self.steps.push(Step::ArrayNext {
                        aggregate,
                        elements,
                        index: index + 1,
                    });
                    self.steps.push(Step::ArraySpread { aggregate, span });
                    self.steps.push(Step::Expression(value));
                }
            },
            Step::ArrayValue(aggregate) => {
                let selected = self.take_value()?;
                self.frame.inputs.extend(selected.values.iter().cloned());
                let Some(AggregateTransfer::Array(transfer)) = self.aggregates.get_mut(&aggregate)
                else {
                    return Err(());
                };
                transfer.record_child(&selected.values);
            }
            Step::ArraySpread { aggregate, span } => {
                let operand = self.take_value()?;
                self.frame.inputs.extend(operand.values.iter().cloned());
                let Some(AggregateTransfer::Array(transfer)) = self.aggregates.get_mut(&aggregate)
                else {
                    return Err(());
                };
                match transfer.record_spread(
                    &mut LocalValueContext {
                        context,
                        frame: &mut self.frame,
                        source_site: None,
                    },
                    span,
                    &operand.values,
                ) {
                    ChildTransfer::Value(()) => {}
                    ChildTransfer::BeforeExecution => return Err(()),
                    ChildTransfer::RemainderAfterPrefix { summarized } => {
                        if summarized {
                            self.frame.remainder_summarized.set(true);
                        }
                        return Err(());
                    }
                }
            }
            Step::FinishAggregate(aggregate) => {
                let transfer = self.aggregates.remove(&aggregate).ok_or(())?;
                let completion = completion_region_action(&self.frame);
                let can_complete_normally = !self.refused
                    && matches!(
                        completion,
                        RegionCompletionAction::Continue
                            | RegionCompletionAction::ContinueMayNormal
                    );
                let exact_normal = can_complete_normally && self.frame.ledger.is_exact_normal();
                if !can_complete_normally {
                    transfer.abort(&mut LocalValueContext {
                        context,
                        frame: &mut self.frame,
                        source_site: None,
                    });
                    self.value = None;
                } else {
                    if !exact_normal {
                        self.judgment = AllocationJudgment::Opaque;
                    }
                    let mut layout_dependencies = self.frame.ledger.dependencies.as_ref().clone();
                    if let Some(effects) = self.frame.ledger.closed_effect_dependencies() {
                        layout_dependencies.extend(&effects);
                    }
                    if let AllocationJudgment::CoveredAllocationOnly(dependencies) = &self.judgment
                    {
                        layout_dependencies.extend(dependencies);
                    }
                    match transfer {
                        AggregateTransfer::Record {
                            allocation,
                            transfer,
                            was_new,
                            ..
                        } => {
                            let values = transfer
                                .finish(
                                    &mut LocalValueContext {
                                        context,
                                        frame: &mut self.frame,
                                        source_site: None,
                                    },
                                    true,
                                    exact_normal,
                                    layout_dependencies,
                                )
                                .ok_or(())?;
                            mark_fresh_record(
                                context,
                                &mut self.frame,
                                allocation,
                                was_new,
                                &values,
                            );
                            self.value = Some(completing_selected(
                                SelectedValue::raw(values),
                                &mut self.frame,
                                context.heap_resolver(),
                            ));
                        }
                        AggregateTransfer::Array(transfer) => {
                            let (values, judgment, dependencies) = transfer.finish(
                                &mut LocalValueContext {
                                    context,
                                    frame: &mut self.frame,
                                    source_site: None,
                                },
                                exact_normal,
                                layout_dependencies,
                            );
                            self.judgment.compose(&judgment);
                            self.frame.ledger.dependencies.extend(&dependencies);
                            self.value = Some(completing_selected(
                                SelectedValue::raw(values),
                                &mut self.frame,
                                context.heap_resolver(),
                            ));
                        }
                    }
                }
            }
            Step::SetConstruction {
                source,
                site,
                source_site,
            } => {
                if source_site.step != OperationStep::CallInvocation {
                    return Err(());
                }
                let argument = self.take_value()?;
                self.frame.inputs.extend(argument.values.iter().cloned());
                let result = context
                    .set_construction(&mut self.frame, &source, &argument, &source_site, site)
                    .ok_or(())?;
                let selected = result
                    .absorb(&mut self.frame, &mut self.judgment, context.heap_resolver())
                    .ok_or(())?;
                self.frame.inputs.extend(selected.values.iter().cloned());
                self.value = Some(selected);
            }
            Step::DefineThisField(property) => {
                let selected = self.take_value()?;
                self.value = define_this_field_selected(
                    context,
                    &property,
                    selected,
                    &mut self.frame,
                    &mut self.judgment,
                );
                if self.value.is_none() {
                    return Err(());
                }
            }
            Step::AssignPublicThisField { property, class } => {
                let selected = self.take_value()?;
                self.value = assign_public_this_field_selected(
                    context,
                    &property,
                    class,
                    selected,
                    &mut self.frame,
                    &mut self.judgment,
                );
                if self.value.is_none() {
                    return Err(());
                }
            }
            Step::AssignPrivateThisField(property) => {
                let selected = self.take_value()?;
                self.value = assign_private_this_field_selected(
                    context,
                    &property,
                    selected,
                    &mut self.frame,
                    &mut self.judgment,
                );
                if self.value.is_none() {
                    return Err(());
                }
            }
            Step::Selected(selected) => self.value = Some(selected),
            Step::ConditionalSelect {
                consequent,
                alternate,
            } => {
                let selected = self.take_value()?;
                self.frame.inputs.extend(selected.values.iter().cloned());
                self.frame
                    .ledger
                    .effects
                    .compose(&EffectCoverageFragment::default());
                match allocation_transfer::truthiness(&selected.values).ok_or(())? {
                    allocation_transfer::Truthiness::AlwaysTruthy => {
                        self.steps.push(Step::Expression(consequent))
                    }
                    allocation_transfer::Truthiness::AlwaysFalsy => {
                        self.steps.push(Step::Expression(alternate))
                    }
                    allocation_transfer::Truthiness::Maybe => {
                        return Ok(Some(self.fork_steps(
                            Step::Expression(consequent),
                            Step::Expression(alternate),
                        )?));
                    }
                }
            }
            Step::LogicalSelect { operator, right } => {
                let mut selected = self.take_value()?;
                self.frame.inputs.extend(selected.values.iter().cloned());
                let (evaluate_right, retained) = match operator {
                    LocalLogicalOperator::NullishCoalescing => split_nullish(&selected.values)?,
                    LocalLogicalOperator::And | LocalLogicalOperator::Or => {
                        let (truthy, falsy) = split_truthiness(&selected.values)?;
                        match operator {
                            LocalLogicalOperator::And => (!truthy.is_empty(), falsy),
                            LocalLogicalOperator::Or => (!falsy.is_empty(), truthy),
                            LocalLogicalOperator::NullishCoalescing => unreachable!(),
                        }
                    }
                };
                self.frame
                    .ledger
                    .effects
                    .compose(&EffectCoverageFragment::default());
                if retained.is_empty() {
                    if !evaluate_right {
                        return Err(());
                    }
                    self.steps.push(Step::Expression(right));
                } else {
                    selected.values = retained;
                    selected.selection = selected
                        .selection
                        .take()
                        .filter(|selection| selection.matches(&selected.values));
                    selected.postcondition = selected
                        .postcondition
                        .take()
                        .filter(|proof| proof.matches(context.heap_resolver(), &selected.values));
                    if evaluate_right {
                        return Ok(Some(
                            self.fork_steps(Step::Selected(selected), Step::Expression(right))?,
                        ));
                    }
                    self.value = Some(selected);
                }
            }
            Step::MemberReceiver { call, sites } => {
                #[cfg(test)]
                let call_span = call.span;
                let selected = self.take_value()?;
                self.frame.inputs.extend(selected.values.iter().cloned());
                let LocalCallee::Member {
                    optional_receiver, ..
                } = &call.callee
                else {
                    unreachable!("member continuation owns a member reference")
                };
                let optional_receiver = *optional_receiver;
                if sites.receiver.as_ref().map(|site| site.step)
                    != Some(OperationStep::CalleeReceiver)
                    || (optional_receiver && sites.receiver_check.is_none())
                {
                    return Err(());
                }
                #[cfg(test)]
                let selected_values_debug = format!("{:?}", selected.values);
                let (may_skip, receivers) = if optional_receiver {
                    split_nullish(&selected.values)?
                } else {
                    (false, selected.values)
                };
                let receiver_selection = selected
                    .selection
                    .filter(|selection| selection.matches(&receivers));
                let next = (!receivers.is_empty()).then(|| Step::MemberGet {
                    call,
                    receivers,
                    receiver_selection,
                    sites,
                });
                #[cfg(test)]
                context.diagnostic(format!(
                    "optional-check kind=receiver call={:?} input={selected_values_debug} may_skip={may_skip} retained_receivers={:?} schedules_get={} branch_budget={}",
                    call_span,
                    next.as_ref().and_then(|step| match step {
                        Step::MemberGet { receivers, .. } => Some(receivers),
                        _ => None,
                    }),
                    next.is_some(),
                    self.branch_budget,
                ));
                let paths = self.optional_paths(may_skip, next);
                #[cfg(test)]
                if optional_receiver {
                    context.diagnostic(format!(
                        "optional-result kind=receiver call={call_span:?} disposition={} branch_budget={}",
                        match &paths {
                            Ok(Some(_)) => "forked-skip-and-receiver",
                            Ok(None) => "continued-single-path",
                            Err(()) => "refused-to-fork",
                        },
                        self.branch_budget,
                    ));
                }
                if let Some(fork) = paths? {
                    return Ok(Some(fork));
                }
            }
            Step::MemberGet {
                call,
                receivers,
                receiver_selection,
                sites,
            } => {
                let LocalCallee::Member {
                    receiver,
                    property,
                    span,
                    ..
                } = &call.callee
                else {
                    unreachable!("member continuation owns a member reference")
                };
                if sites.getvalue.step != OperationStep::CallCalleeGetValue {
                    return Err(());
                }
                let callee = select_member_callee(
                    context,
                    &mut self.frame,
                    &mut self.judgment,
                    MemberReference {
                        receiver_binding: expression_binding(receiver),
                        receiver_selection,
                        receivers,
                        property,
                        member_span: *span,
                        call_span: call.span,
                        callee_getvalue_site: Some(&sites.getvalue),
                        invocation: call.invocation.foreign_invocation(),
                    },
                );
                let Some(callee) = callee else {
                    return if self.frame.ledger.completion.has_no_normal_path() {
                        Ok(None)
                    } else {
                        Err(())
                    };
                };
                self.steps.push(Step::CallCallee {
                    call,
                    callee,
                    sites,
                });
            }
            Step::CallCallee {
                call,
                mut callee,
                sites,
            } => {
                #[cfg(test)]
                let call_span = call.span;
                self.frame.inputs.extend(callee.targets().iter().cloned());
                let native = matches!(
                    &callee,
                    EvaluatedCallee::Native { .. }
                        | EvaluatedCallee::FunctionCallMethod { .. }
                        | EvaluatedCallee::Member {
                            array_method: Some(_),
                            ..
                        }
                );
                let optional = matches!(call.invocation, LocalInvocation::OptionalCall);
                #[cfg(test)]
                let optional_receiver = matches!(
                    &call.callee,
                    LocalCallee::Member {
                        optional_receiver: true,
                        ..
                    }
                );
                if optional && sites.callee_check.is_none() {
                    return Err(());
                }
                let (may_skip, targets) = if optional && !native {
                    split_nullish(callee.targets())?
                } else {
                    (false, callee.targets().clone())
                };
                #[cfg(test)]
                let input_targets_debug = format!("{:?}", callee.targets());
                #[cfg(test)]
                let retained_targets_debug = format!("{targets:?}");
                let next = if native || !targets.is_empty() {
                    match &mut callee {
                        EvaluatedCallee::Direct(selected) => {
                            selected.values = targets;
                            selected.selection = selected
                                .selection
                                .take()
                                .filter(|selection| selection.matches(&selected.values));
                        }
                        EvaluatedCallee::Member {
                            targets: selected, ..
                        } => *selected = targets,
                        EvaluatedCallee::Native { .. } => {}
                        EvaluatedCallee::FunctionCallMethod { target, .. } => {
                            target.values = targets;
                            target.selection = target
                                .selection
                                .take()
                                .filter(|selection| selection.matches(&target.values));
                        }
                    }
                    Some(Step::InvokeArguments {
                        call,
                        callee,
                        sites,
                    })
                } else {
                    None
                };
                #[cfg(test)]
                if optional || optional_receiver {
                    context.diagnostic(format!(
                        "optional-check kind=callee call={call_span:?} optional={optional} native={native} input_targets={input_targets_debug} may_skip={may_skip} retained_targets={retained_targets_debug} schedules_invoke={} branch_budget={}",
                        next.is_some(),
                        self.branch_budget,
                    ));
                }
                let paths = self.optional_paths(may_skip, next);
                #[cfg(test)]
                if optional || optional_receiver {
                    context.diagnostic(format!(
                        "optional-result kind=callee call={call_span:?} optional={optional} optional_receiver={optional_receiver} disposition={} branch_budget={}",
                        match &paths {
                            Ok(Some(_)) => "forked-skip-and-invoke",
                            Ok(None) => "continued-single-path",
                            Err(()) => "refused-to-fork",
                        },
                        self.branch_budget,
                    ));
                }
                if let Some(fork) = paths? {
                    return Ok(Some(fork));
                }
            }
            Step::InvokeArguments {
                call,
                callee,
                sites,
            } => {
                #[cfg(test)]
                context.diagnostic(format!(
                    "arguments-start call={:?} argument_count={} callee_targets={:?} optional_receiver={} optional_callee={} branch_budget={}",
                    call.span,
                    call.arguments.len(),
                    callee.targets(),
                    matches!(
                        &call.callee,
                        LocalCallee::Member {
                            optional_receiver: true,
                            ..
                        }
                    ),
                    matches!(call.invocation, LocalInvocation::OptionalCall),
                    self.branch_budget,
                ));
                if let Some(first) = call.arguments.first() {
                    let first = first.clone();
                    self.steps.push(Step::Arguments {
                        call,
                        callee,
                        arguments: Vec::new(),
                        retained_arguments: Values::new(),
                        sites,
                        source_index: 0,
                    });
                    self.steps.push(Step::Expression(first));
                } else {
                    return Ok(Some(LocalSuspension::Call(self.call_request(
                        context,
                        call,
                        callee,
                        Vec::new(),
                        Values::new(),
                        *sites,
                    )?)));
                }
            }
            Step::ReadReceiver {
                property,
                optional_receiver,
                receiver_check,
                getvalue_site,
                span,
            } => {
                if optional_receiver && receiver_check.is_none() {
                    return Err(());
                }
                let selected = self.take_value()?;
                self.frame.inputs.extend(selected.values.iter().cloned());
                let (may_skip, receivers) = if optional_receiver {
                    split_nullish(&selected.values)?
                } else {
                    (false, selected.values)
                };
                let next = (!receivers.is_empty()).then(|| Step::ReadMember {
                    getvalue_site,
                    receivers,
                    property,
                    span,
                });
                if let Some(fork) = self.optional_paths(may_skip, next)? {
                    return Ok(Some(fork));
                }
            }
            Step::ReadMember {
                getvalue_site,
                receivers,
                property,
                span,
            } => {
                if getvalue_site.step != OperationStep::FieldGetValue {
                    return Err(());
                }
                let result =
                    read_field(context, &mut self.frame, None, &receivers, &property, span);
                self.value = result.and_then(|result| {
                    result.absorb(&mut self.frame, &mut self.judgment, context.heap_resolver())
                });
                if self.value.is_none() && !self.frame.ledger.completion.has_no_normal_path() {
                    return Err(());
                }
            }
            Step::Arguments {
                call,
                callee,
                mut arguments,
                mut retained_arguments,
                sites,
                source_index,
            } => {
                let value = self.take_value()?;
                self.frame.inputs.extend(value.retained_values());
                #[cfg(test)]
                context.diagnostic(format!(
                    "argument-value call={:?} argument_index={} value={:?} selected_callee_targets={:?} branch_budget={}",
                    call.span,
                    source_index,
                    value.values,
                    callee.targets(),
                    self.branch_budget,
                ));
                retained_arguments.extend(value.retained_values());
                if let Some(span) = call.spread_arguments.get(&source_index) {
                    let site = sites.spread_sites.get(&source_index).ok_or(())?;
                    let transfer = allocation_transfer::AllocationContext::array_spread(
                        &mut LocalValueContext {
                            context,
                            frame: &mut self.frame,
                            source_site: Some(site),
                        },
                        *span,
                        &value.values,
                    );
                    match transfer {
                        ChildTransfer::Value(spread) => {
                            self.frame.ledger.dependencies.extend(&spread.dependencies);
                            match spread.yields {
                                ArraySpreadYields::ExactSlots(slots) => {
                                    for values in slots {
                                        let selection = CallableSelection::observed(
                                            &values,
                                            Box::new(spread.dependencies.clone()),
                                        );
                                        arguments.push(SelectedValue {
                                            values,
                                            selection,
                                            postcondition: None,
                                        });
                                    }
                                }
                                ArraySpreadYields::PrimitiveOnly => return Err(()),
                            }
                        }
                        ChildTransfer::BeforeExecution
                        | ChildTransfer::RemainderAfterPrefix { .. } => return Err(()),
                    }
                } else {
                    arguments.push(value);
                }
                let next_index = source_index + 1;
                if let Some(next) = call.arguments.get(next_index) {
                    let next = next.clone();
                    self.steps.push(Step::Arguments {
                        call,
                        callee,
                        arguments,
                        retained_arguments,
                        sites,
                        source_index: next_index,
                    });
                    self.steps.push(Step::Expression(next));
                } else {
                    return Ok(Some(LocalSuspension::Call(self.call_request(
                        context,
                        call,
                        callee,
                        arguments,
                        retained_arguments,
                        *sites,
                    )?)));
                }
            }
            Step::Template {
                substitutions,
                index,
                span,
                source_site,
            } => {
                if let Some(next) = substitutions.get(index) {
                    let next = next.clone();
                    self.steps.push(Step::Template {
                        substitutions,
                        index: index + 1,
                        span,
                        source_site: source_site.clone(),
                    });
                    self.steps.push(Step::TemplateConvert { span, source_site });
                    self.steps.push(Step::Expression(next));
                } else {
                    self.value = Some(SelectedValue::raw(Values::from([AbstractValue::Value(
                        Value::Prim(PrimKinds::STRING),
                    )])));
                }
            }
            Step::TemplateConvert { span, source_site } => {
                let operand = self.take_value()?;
                self.value = convert_template_operand(
                    context,
                    operand,
                    &span,
                    source_site,
                    &mut self.frame,
                    &mut self.judgment,
                );
                if self.value.is_none() {
                    return Err(());
                }
            }
            Step::Sequence(expressions, index) => {
                if let Some(next) = expressions.get(index) {
                    self.value = None;
                    self.steps
                        .push(Step::Sequence(expressions.clone(), index + 1));
                    self.steps.push(Step::Expression(next.clone()));
                }
            }
            Step::BinaryLeft {
                operator,
                right,
                span,
                frontier,
            } => {
                let left = self.take_value()?;
                self.steps.push(Step::BinaryRight {
                    operator,
                    left,
                    span,
                    frontier,
                });
                self.steps.push(Step::Expression(right));
            }
            Step::BinaryRight {
                operator,
                left,
                span,
                frontier,
            } => {
                let right = self.take_value()?;
                self.value = apply_binary(
                    &mut LocalValueContext {
                        context,
                        frame: &mut self.frame,
                        source_site: None,
                    },
                    &operator,
                    left,
                    right,
                    &span,
                    frontier,
                    &mut self.judgment,
                );
                if self.value.is_none() {
                    return Err(());
                }
            }
            Step::Assign(binding, span) => {
                let value = self.take_value()?;
                self.value = store_binding(
                    context,
                    body,
                    &mut self.frame,
                    &mut self.judgment,
                    &binding,
                    value,
                    span,
                );
                if self.value.is_none() {
                    return Err(());
                }
            }
            Step::StoreField {
                receivers,
                module_prefix,
                property,
                span,
                source_site,
            } => {
                let selected = self.take_value()?;
                self.value = store_field_selected(
                    &mut LocalValueContext {
                        context,
                        frame: &mut self.frame,
                        source_site: source_site.as_ref(),
                    },
                    receivers,
                    module_prefix,
                    &property,
                    selected,
                    &span,
                );
                if self.value.is_none() {
                    return Err(());
                }
            }
            Step::InitializeObject(pattern) => {
                let selected = self.take_value()?;
                initialize_object_selected(
                    context,
                    body,
                    &pattern,
                    selected,
                    &mut self.frame,
                    &mut self.judgment,
                )
                .ok_or(())?;
            }
            Step::Initialize(binding) => {
                let value = self.take_value()?;
                initialize_binding(context, body, &mut self.frame, &binding, value);
            }
            Step::Discard => {
                self.take_value()?;
            }
            Step::Return(site) => {
                let value = self.take_value()?;
                self.frame.complete_return(value, site);
            }
            Step::Throw(site) => {
                let value = self.take_value()?;
                context.publish_throw(&value.values);
                self.frame.complete_throw(KnownThrow {
                    value,
                    origin: QualifiedThrowSite {
                        scope: context.heap_resolver().scope(),
                        site: site.clone(),
                    },
                    frontier: site,
                });
            }
            Step::IfSelect {
                consequent,
                alternate,
                frontier,
            } => {
                let value = self.take_value()?;
                self.frame.inputs.extend(value.values.iter().cloned());
                match allocation_transfer::truthiness(&value.values).ok_or(())? {
                    allocation_transfer::Truthiness::AlwaysTruthy => {
                        if !self.account_skipped_if_region(
                            context,
                            alternate.as_ref(),
                            &frontier,
                            SkippedCallReason::IfAlternateNotSelected,
                        ) {
                            self.frame
                                .ledger
                                .effects
                                .compose(&EffectCoverageFragment::default());
                        }
                        self.steps.push(Step::EnterBranch(Some(consequent)))
                    }
                    allocation_transfer::Truthiness::AlwaysFalsy => {
                        if !self.account_skipped_if_region(
                            context,
                            Some(&consequent),
                            &frontier,
                            SkippedCallReason::IfConsequentNotSelected,
                        ) {
                            self.frame
                                .ledger
                                .effects
                                .compose(&EffectCoverageFragment::default());
                        }
                        self.steps.push(Step::EnterBranch(alternate))
                    }
                    allocation_transfer::Truthiness::Maybe => {
                        if self.branch_budget < 2 {
                            return Err(());
                        }
                        let mut yes = self.clone();
                        let mut no = self.clone();
                        yes.branch_budget = self.branch_budget / 2;
                        no.branch_budget = self.branch_budget - yes.branch_budget;
                        if !yes.account_skipped_if_region(
                            context,
                            alternate.as_ref(),
                            &frontier,
                            SkippedCallReason::IfAlternateNotSelected,
                        ) {
                            yes.frame
                                .ledger
                                .effects
                                .compose(&EffectCoverageFragment::default());
                        }
                        if !no.account_skipped_if_region(
                            context,
                            Some(&consequent),
                            &frontier,
                            SkippedCallReason::IfConsequentNotSelected,
                        ) {
                            no.frame
                                .ledger
                                .effects
                                .compose(&EffectCoverageFragment::default());
                        }
                        yes.steps.push(Step::EnterBranch(Some(consequent)));
                        no.steps.push(Step::EnterBranch(alternate));
                        return Ok(Some(LocalSuspension::Fork(vec![yes, no])));
                    }
                }
            }
            Step::EnterBranch(region) => {
                if let Some(region) = region {
                    self.enter_region(context, &region).ok_or(())?;
                }
            }
            Step::ClassSteps(steps, index) => {
                if let Some(step) = steps.get(index) {
                    self.steps.push(Step::ClassSteps(steps.clone(), index + 1));
                    match step {
                        ClassStaticStep::Field {
                            property, value, ..
                        } => {
                            self.steps.push(Step::ClassValue {
                                property: Some(property.clone()),
                                may_escape: value.class_step_may_escape_receiver(),
                            });
                            self.steps.push(Step::Expression(value.clone()));
                        }
                        ClassStaticStep::Block { expressions, .. } => {
                            self.steps.push(Step::ClassBlock(expressions.clone(), 0));
                        }
                    }
                }
            }
            Step::ClassBlock(expressions, index) => {
                if let Some(expression) = expressions.get(index) {
                    self.steps
                        .push(Step::ClassBlock(expressions.clone(), index + 1));
                    self.steps.push(Step::ClassValue {
                        property: None,
                        may_escape: expression.class_step_may_escape_receiver(),
                    });
                    self.steps.push(Step::Expression(expression.clone()));
                }
            }
            Step::ClassValue {
                property,
                may_escape,
            } => {
                let value = self.take_value()?;
                self.frame
                    .reached_values
                    .extend(value.values.iter().cloned());
                validate_class_static_value(&mut self.frame, may_escape, &value).ok_or(())?;
                if let Some(property) = property {
                    define_class_static_field(context, &mut self.frame, &property, &value)
                        .ok_or(())?;
                }
            }
            Step::LeaveClass {
                selected,
                previous_this,
            } => {
                self.frame
                    .class_definition_witnesses
                    .pop()
                    .expect("class entry owns its witness");
                self.frame.this_values = previous_this;
                if !self.refused
                    && completion_region_action(&self.frame) == RegionCompletionAction::Continue
                {
                    self.value = Some(completing_selected(
                        selected,
                        &mut self.frame,
                        context.heap_resolver(),
                    ));
                } else {
                    // Keep the unfinished class rooted without publishing the
                    // declaration or replacing its abrupt completion.
                    self.frame.reached_values.extend(selected.values);
                    self.value = None;
                }
            }
            Step::LeaveRegion(snapshot) => {
                context.leave_region_bindings(&mut self.frame, self.refused);
                snapshot.restore(&mut self.frame);
            }
            Step::Catch {
                region,
                binding,
                handler,
            } => {
                if !self.refused {
                    self.route_catch(context, region, binding, handler)?;
                }
            }
            Step::LeaveCatch(binding, handler) => {
                if let Some(binding) = binding {
                    context.leave_catch_binding(&binding);
                    self.frame.environment.remove(&binding);
                    self.frame.binding_selections.remove(&binding);
                    self.frame.binding_postconditions.remove(&binding);
                    self.frame.initialized_bindings.remove(&binding);
                }
                if !self.refused {
                    self.record_terminal_region(context, &handler)?;
                }
            }
        }
        Ok(None)
    }

    /// Fork only after the receiver/callee has been evaluated. A skipped leaf
    /// carries undefined directly and never schedules the Get or argument work.
    fn optional_paths(
        &mut self,
        may_skip: bool,
        next: Option<Step>,
    ) -> Result<Option<LocalSuspension>, ()> {
        if !may_skip {
            self.steps.push(next.ok_or(())?);
            return Ok(None);
        }
        // Skipping is semantically known for this leaf, but this kernel does
        // not yet mint path-relative skipped-call coverage receipts.
        self.frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
        let undefined = SelectedValue::raw(Values::from([AbstractValue::UNDEFINED]));
        let Some(next) = next else {
            self.value = Some(undefined);
            return Ok(None);
        };
        Ok(Some(self.fork_steps(Step::Selected(undefined), next)?))
    }

    fn fork_steps(&self, left: Step, right: Step) -> Result<LocalSuspension, ()> {
        if self.branch_budget < 2 {
            return Err(());
        }
        let mut left_path = self.clone();
        let mut right_path = self.clone();
        left_path.branch_budget = self.branch_budget / 2;
        right_path.branch_budget = self.branch_budget - left_path.branch_budget;
        left_path.value = None;
        right_path.value = None;
        left_path.steps.push(left);
        right_path.steps.push(right);
        Ok(LocalSuspension::Fork(vec![left_path, right_path]))
    }

    fn take_value(&mut self) -> Result<SelectedValue, ()> {
        self.value
            .take()
            .filter(|value| !value.values.is_empty())
            .ok_or(())
    }

    fn record_terminal_region(
        &mut self,
        context: &mut impl LocalContext,
        region: &LocalRegion,
    ) -> Result<(), ()> {
        let frontier = match &self.frame.ledger.completion {
            Completion::Normal(LocalNormal::Return { frontier, .. }) => {
                Some((frontier.clone(), SkippedCallReason::KnownReturn))
            }
            Completion::Abrupt(thrown) => {
                Some((thrown.frontier.clone(), SkippedCallReason::KnownThrow))
            }
            _ => None,
        };
        if let Some((frontier, reason)) = frontier {
            let site = region.source_site.as_ref().ok_or(())?;
            if !context.skipped_region(&mut self.frame, site, &frontier, reason) {
                self.frame
                    .ledger
                    .effects
                    .compose(&EffectCoverageFragment::default());
            }
        }
        Ok(())
    }

    /// Account for calls in one If arm on this exact feasible execution leaf.
    /// A missing arm contains no source calls; a present arm needs its
    /// canonical region site and a closed predicate prefix before it can
    /// contribute skipped-call receipts.
    fn account_skipped_if_region(
        &mut self,
        context: &mut impl LocalContext,
        region: Option<&LocalRegion>,
        frontier: &SourceOperationSite,
        reason: SkippedCallReason,
    ) -> bool {
        let Some(region) = region else {
            return true;
        };
        let Some(region_site) = region.source_site.as_ref() else {
            return false;
        };
        context.skipped_region(&mut self.frame, region_site, frontier, reason)
    }

    fn route_catch(
        &mut self,
        context: &mut impl LocalContext,
        region: LocalRegion,
        binding: Option<Id>,
        handler: LocalRegion,
    ) -> Result<(), ()> {
        let region_site = region
            .source_site
            .as_ref()
            .filter(|site| site.step == OperationStep::TryBody)
            .ok_or(())?;
        let handler_site = handler
            .source_site
            .as_ref()
            .filter(|site| site.step == OperationStep::CatchHandler)
            .ok_or(())?;
        let selected = match self.frame.ledger.completion.clone() {
            Completion::Normal(LocalNormal::Next) => {
                if !context.skipped_region(
                    &mut self.frame,
                    handler_site,
                    region_site,
                    SkippedCallReason::HandlerNotEntered,
                ) {
                    self.frame
                        .ledger
                        .effects
                        .compose(&EffectCoverageFragment::default());
                }
                return Ok(());
            }
            Completion::Normal(LocalNormal::Return { frontier, .. }) => {
                if !context.skipped_region(
                    &mut self.frame,
                    handler_site,
                    &frontier,
                    SkippedCallReason::HandlerNotEntered,
                ) {
                    self.frame
                        .ledger
                        .effects
                        .compose(&EffectCoverageFragment::default());
                }
                self.record_terminal_region(context, &region)?;
                return Ok(());
            }
            Completion::Abrupt(thrown) => {
                self.record_terminal_region(context, &region)?;
                self.frame.ledger.completion = Completion::Normal(LocalNormal::Next);
                thrown.value
            }
            Completion::UnknownThrow(mut values) => {
                if values.0.is_empty() {
                    values.0.insert(AbstractValue::Value(Value::Top));
                }
                self.frame.ledger.completion = Completion::Normal(LocalNormal::Next);
                // Unknown throw routing grants no exact skip/coverage receipt.
                self.frame
                    .ledger
                    .effects
                    .compose(&EffectCoverageFragment::default());
                SelectedValue::raw(values.0)
            }
            // Divergence and unresolved completion are not a caught exception.
            _ => return Err(()),
        };
        if let Some(binding) = &binding {
            self.frame.environment.insert(
                binding.clone(),
                Values::from([AbstractValue::Value(Value::Unavailable)]),
            );
            context.instantiate_catch_binding(binding, &self.frame);
            self.frame
                .ledger
                .effects
                .compose(&context.declarative_binding_operation(
                    binding,
                    &self.frame,
                    BindingOperation::Create,
                ));
            self.frame.inputs.extend(selected.values.iter().cloned());
            self.frame
                .environment
                .insert(binding.clone(), selected.values.clone());
            if let Some(selection) = selected
                .selection
                .filter(|selection| selection.matches(&selected.values))
            {
                self.frame
                    .binding_selections
                    .insert(binding.clone(), selection);
            }
            context.local_binding_stored(binding, &self.frame);
            self.frame.initialized_bindings.insert(binding.clone());
            self.frame
                .ledger
                .effects
                .compose(&context.declarative_binding_operation(
                    binding,
                    &self.frame,
                    BindingOperation::Initialize,
                ));
        }
        self.steps.push(Step::LeaveCatch(binding, handler.clone()));
        self.enter_region(context, &handler).ok_or(())
    }
}

/// Partition the already normal operand; Unavailable is not a normal unknown
/// and must never become a fabricated optional success path.
pub(super) fn split_nullish(values: &Values) -> Result<(bool, Values), ()> {
    split_primitive_test(values, PrimKinds::NULL.union(PrimKinds::UNDEFINED))
}

fn split_primitive_test(values: &Values, tested: PrimKinds) -> Result<(bool, Values), ()> {
    let mut may_skip = false;
    let mut retained = Values::new();
    for value in values {
        match value {
            AbstractValue::Value(Value::Prim(kinds)) => {
                may_skip |= kinds.intersects(tested);
                let kinds = kinds.without(tested);
                if !kinds.is_empty() {
                    retained.insert(AbstractValue::Value(Value::Prim(kinds)));
                }
            }
            AbstractValue::Value(Value::Unavailable) => return Err(()),
            AbstractValue::Value(Value::Alloc(..))
            | AbstractValue::Closure(..)
            | AbstractValue::ClassMembers(_)
            | AbstractValue::Instance(..)
            | AbstractValue::ArrayIntrinsic
            | AbstractValue::GlobalObject
            | AbstractValue::ObjectAssign => {
                retained.insert(value.clone());
            }
            _ => {
                may_skip = true;
                retained.insert(value.clone());
            }
        }
    }
    Ok((may_skip, retained))
}

/// Refine primitive kind unions on short-circuit paths without claiming literal
/// booleans/numbers are known. Unknown normal values remain on both paths.
fn split_truthiness(values: &Values) -> Result<(Values, Values), ()> {
    let mut truthy = Values::new();
    let mut falsy = Values::new();
    for value in values {
        if let AbstractValue::Value(Value::Prim(kinds)) = value {
            let yes = kinds.without(PrimKinds::NULL.union(PrimKinds::UNDEFINED));
            let no = kinds.without(PrimKinds::SYMBOL);
            if !yes.is_empty() {
                truthy.insert(AbstractValue::Value(Value::Prim(yes)));
            }
            if !no.is_empty() {
                falsy.insert(AbstractValue::Value(Value::Prim(no)));
            }
        } else {
            match allocation_transfer::truthiness(&Values::from([value.clone()])).ok_or(())? {
                allocation_transfer::Truthiness::AlwaysTruthy => {
                    truthy.insert(value.clone());
                }
                allocation_transfer::Truthiness::AlwaysFalsy => {
                    falsy.insert(value.clone());
                }
                allocation_transfer::Truthiness::Maybe => {
                    truthy.insert(value.clone());
                    falsy.insert(value.clone());
                }
            }
        }
    }
    Ok((truthy, falsy))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parameter_default_partition_retains_null_and_other_supplied_kinds() {
        let kinds = PrimKinds::UNDEFINED
            .union(PrimKinds::NULL)
            .union(PrimKinds::NUMBER);
        let (may_default, supplied) = split_primitive_test(
            &Values::from([AbstractValue::Value(Value::Prim(kinds))]),
            PrimKinds::UNDEFINED,
        )
        .unwrap();
        assert!(may_default);
        assert_eq!(
            supplied,
            Values::from([AbstractValue::Value(Value::Prim(
                PrimKinds::NULL.union(PrimKinds::NUMBER)
            ))])
        );
    }

    #[test]
    fn optional_partition_refines_primitive_union_and_refuses_unavailable() {
        let kinds = PrimKinds::NULL
            .union(PrimKinds::UNDEFINED)
            .union(PrimKinds::STRING);
        let (may_skip, retained) =
            split_nullish(&Values::from([AbstractValue::Value(Value::Prim(kinds))])).unwrap();
        assert!(may_skip);
        assert_eq!(
            retained,
            Values::from([AbstractValue::Value(Value::Prim(PrimKinds::STRING))])
        );
        assert!(split_nullish(&Values::from([AbstractValue::Value(Value::Unavailable)])).is_err());
        let (may_skip, retained) =
            split_nullish(&Values::from([AbstractValue::Value(Value::Top)])).unwrap();
        assert!(may_skip);
        assert_eq!(retained, Values::from([AbstractValue::Value(Value::Top)]));
    }
}
