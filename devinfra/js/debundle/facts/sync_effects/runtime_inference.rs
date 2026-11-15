//! Position-specific execution facts. A later source prefix consumes the
//! preceding prefix's settled state and never changes that earlier state.

use super::allocation_transfer::AllocationJudgment;
use super::effect_coverage::EffectCoverageFragment;
use super::execution_index::Call;
use super::interpreter::{
    ExecutionProgramInputs, ExecutionState, Executor, Observations, Outcome, OwnPropertyQuery,
};
use super::kernel::frame::{CallableSelection, EvaluatedCallee};
use super::noninvoking_initialization::{EvaluatedInitializer, OriginalInitializer};
use super::property_inference::OwnPropertyProof;
use super::runtime_callbacks::{self, CallbackOperation, CallbackResult};
use super::runtime_heap::{HeapOperation, HeapResult};
use super::runtime_models::{self, ModelOperation, ModelResult};
use super::value_model::{CapturedContext, Context, ValueExpr, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::OrderedCallAdmissionRefusal;
use crate::facts::evaluation_effects::EvaluationEffects;
use crate::facts::statement_facts::SynchronousCalls;
use crate::facts::{
    CompletedPurity, LocalRecordFactoryPurityCertificate, OwnerInitializationCoverageRefusalReason,
    PreparedPurity,
};
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use crate::program_execution::{RuntimeSourceScope, RuntimeSourceUnitId};
use crate::reports::schema::{NativeProgramPointKind, NativeProgramPointStep};
use source_identity::SourceUnitOrdinal;
use std::cell::{Cell, RefCell};
use swc_common::Span;

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum RuntimeKey {
    Prefix(usize),
    /// Current-owner preparatory classifier obligations at this prefix.
    PreparedPurity(SourceOwner),
    /// Final verdict published only after every owner finalization step.
    CompletedPurity(SourceOwner),
    Effects(SourceOwner),
    Request(ProgramPoint),
    Result(ProgramPoint),
    OwnPropertyRequest(ProgramPoint),
    OwnPropertyResult(ProgramPoint),
    InvocationMemo(ProgramPoint),
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum RuntimeRule {
    ExecuteOwner(usize),
    Transfer(ProgramPoint),
    OwnProperty(ProgramPoint),
}

enum RuntimeFact {
    Prefix(Box<ExecutionState>),
    PreparedPurity(Box<PreparedPurity>),
    CompletedPurity(Box<CompletedPurity>),
    Effects(Box<SynchronousCalls>),
    Request(Box<RuntimeRequest>),
    Result(Box<RuntimeResult>),
    OwnPropertyRequest(Box<OwnPropertyQuery>),
    OwnPropertyResult(Option<Box<OwnPropertyProof>>),
    InvocationMemo(Box<super::interpreter::invocation_replay::InvocationMemo>),
}

/// The path identifies an occurrence within its parent's transfer, rather than
/// conflating repeated evaluation of the same expression in different contexts.
type SourceOwner = RuntimeSourceUnitId;

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) struct ProgramPoint {
    owner: SourceOwner,
    path: Vec<(NativeProgramPointKind, usize)>,
}

/// The ordered caller has already read its callee and arguments. The call
/// transfer must use these snapshots rather than evaluate their source again.
pub(super) struct CallOperands {
    pub(super) callee: EvaluatedCallee,
    pub(super) arguments: Vec<Values>,
    pub(super) argument_selections: Vec<Option<CallableSelection>>,
    /// The exact root has independent pure-initializer and operation inputs.
    /// This only admits an existing callback-factory result transfer; the
    /// ordered caller still checks the operation domains.
    pub(super) selected_pure_initializer_call: bool,
    /// Exact author-pinned provider result for one initialized literal-const
    /// string argument; carries no later-call or object-shape inference.
    pub(super) selected_const_factory_result: bool,
}

pub(super) enum CallTransfer {
    /// Consult the accompanying outcome for effect completeness. No return
    /// value was requested, or the selected proof supplies only effects.
    EffectsOnly,
    Values(Values),
    /// The ordered operand path declined this target. Its source-based
    /// helpers cannot safely replay already evaluated operands.
    UnsupportedOperands(OrderedCallAdmissionRefusal),
}

impl CallTransfer {
    pub(super) fn requested(needs_value: bool, values: impl FnOnce() -> Values) -> Self {
        if needs_value {
            Self::Values(values())
        } else {
            Self::EffectsOnly
        }
    }
}

pub(super) enum RuntimeOperation {
    Heap(HeapOperation),
    Evaluate {
        expression: ValueExpr,
        environment: Context,
        captures: Context,
        source_covered: bool,
    },
    EvaluateInitializer {
        source: OriginalInitializer,
        expression: ValueExpr,
        environment: Context,
        captures: Context,
    },
    Invoke {
        callable: crate::program_execution::RuntimeAllocationId,
        arguments: Vec<Values>,
        argument_selections: Vec<Option<CallableSelection>>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
        callsite: Option<runtime_callbacks::InvocationSite>,
    },
    Call {
        call: Call,
        environment: Context,
        captures: Context,
        outcome: Box<Outcome>,
        operands: Option<Box<CallOperands>>,
    },
    Callbacks(CallbackOperation),
    Model(ModelOperation),
}

pub(super) enum RuntimeValue {
    Heap(HeapResult),
    Values(Values),
    Initializer {
        values: Values,
        evaluated: Option<Box<EvaluatedInitializer>>,
        operation_coverage: Option<Box<EffectCoverageFragment>>,
        refusal: Option<OwnerInitializationCoverageRefusalReason>,
    },
    Outcome(Box<Outcome>),
    Call {
        outcome: Box<Outcome>,
        transfer: CallTransfer,
    },
    Callbacks(CallbackResult),
    Model(ModelResult),
}

struct RuntimeRequest {
    state: Box<ExecutionState>,
    /// The parent's run-level admission facts so far; see [`Observations`].
    observations: Observations,
    operation: RuntimeOperation,
}

#[must_use]
pub(super) struct RuntimeResult {
    pub(super) state: Box<ExecutionState>,
    pub(super) observations: Observations,
    pub(super) value: RuntimeValue,
    pub(super) allocation_judgment: AllocationJudgment,
    pub(super) evaluation_effects: EvaluationEffects,
}

/// Transfers consume settled inputs and cannot send information back to their
/// predecessors. Evaluate these acyclic children immediately on the same fact
/// context, then retire their single-consumer payloads. Growing fact families
/// continue to use the dependency worklist. This avoids replaying a parent or
/// retaining every heap snapshot for a long, flat sequence of source calls.
#[derive(Clone, Copy, Default, PartialEq, Eq)]
pub(super) struct RuntimeQueryCursor {
    next_child: usize,
    next_allocation: usize,
}

pub(super) struct RuntimeQueries<'source, 'query> {
    inputs: &'query ExecutionProgramInputs<'source>,
    point: ProgramPoint,
    active_source: Cell<RuntimeSourceScope>,
    next_child: Cell<usize>,
    next_allocation: Cell<usize>,
    // CLEANUP(added 2026-11-09): the last interior-mutable observation ledger.
    //   Remove once query results return their dependencies and the adopter
    //   composes them; until then `HeapContext::current_prerequisites` reads
    //   this beside `World.prerequisites` at the moment of each kernel read.
    dependencies: RefCell<ProofDependencies>,
    context: RefCell<&'query mut dyn FactContext<RuntimeKey, RuntimeFact>>,
}

impl RuntimeQueries<'_, '_> {
    fn prepare_owner(&self, owner: SourceUnitOrdinal, effects: crate::purity::Effects) {
        self.context.borrow_mut().propose(
            RuntimeKey::PreparedPurity(self.source_owner(owner)),
            RuntimeFact::PreparedPurity(Box::new(PreparedPurity::new(effects))),
        );
    }

    pub(super) fn exchange_source(&self, source: RuntimeSourceScope) -> RuntimeSourceScope {
        self.active_source.replace(source)
    }

    pub(super) fn cursor(&self) -> RuntimeQueryCursor {
        RuntimeQueryCursor {
            next_child: self.next_child.get(),
            next_allocation: self.next_allocation.get(),
        }
    }

    pub(super) fn restore_replayed_cursor(&self, cursor: RuntimeQueryCursor) {
        self.next_child.set(cursor.next_child);
        self.next_allocation.set(cursor.next_allocation);
    }

    fn source_owner(&self, ordinal: SourceUnitOrdinal) -> SourceOwner {
        SourceOwner {
            source: self.active_source.get(),
            ordinal,
        }
    }

    pub(super) fn allocation_prefix(
        &self,
    ) -> Option<crate::program_execution::effects::ProgramAllocationEvent> {
        let source = self.inputs.source_for_scope(self.point.owner.source)?;
        Some(crate::program_execution::effects::ProgramAllocationEvent {
            owner: crate::program_execution::effects::ProgramSourceUnitId {
                module: source.module_id?.to_owned(),
                ordinal: self.point.owner.ordinal,
            },
            path: self.point.path.clone(),
            occurrence: 0,
        })
    }

    /// One actual kernel allocation, separate from metadata/source projections.
    pub(super) fn allocation_event(
        &self,
    ) -> Option<crate::program_execution::effects::ProgramAllocationEvent> {
        let occurrence = self.next_allocation.get();
        self.next_allocation.set(
            occurrence
                .checked_add(1)
                .expect("allocation occurrence exhausted"),
        );
        let source = self.inputs.source_for_scope(self.point.owner.source)?;
        let module = source.module_id?;
        Some(crate::program_execution::effects::ProgramAllocationEvent {
            owner: crate::program_execution::effects::ProgramSourceUnitId {
                module: module.to_owned(),
                ordinal: self.point.owner.ordinal,
            },
            path: self.point.path.clone(),
            occurrence,
        })
    }

    pub(super) fn point_path(&self) -> Vec<NativeProgramPointStep> {
        self.point
            .path
            .iter()
            .map(|(kind, ordinal)| NativeProgramPointStep {
                kind: *kind,
                ordinal: *ordinal,
            })
            .collect()
    }
    pub(super) fn invocation_memo(
        &self,
        point: &ProgramPoint,
    ) -> super::interpreter::invocation_replay::InvocationMemo {
        let mut context = self.context.borrow_mut();
        let Some(RuntimeFact::InvocationMemo(outcome)) =
            context.read_final(RuntimeKey::InvocationMemo(point.clone()))
        else {
            unreachable!("a live invocation memo points to its completed outcome");
        };
        outcome.as_ref().clone()
    }

    pub(super) fn publish_invocation_memo(
        &self,
        outcome: &super::interpreter::invocation_replay::InvocationMemo,
    ) -> ProgramPoint {
        self.context.borrow_mut().propose(
            RuntimeKey::InvocationMemo(self.point.clone()),
            RuntimeFact::InvocationMemo(Box::new(outcome.clone())),
        );
        self.point.clone()
    }

    pub(super) fn retire_invocation_memo(&self, point: ProgramPoint) {
        assert!(matches!(
            self.context
                .borrow_mut()
                .take_final(RuntimeKey::InvocationMemo(point)),
            Some(RuntimeFact::InvocationMemo(_))
        ));
    }

    /// Current-owner proof preparation always consumes the unchanged classifier.
    pub(super) fn prepared_statement_is_pure(&self, owner: SourceUnitOrdinal) -> bool {
        assert_eq!(
            self.source_owner(owner),
            self.point.owner,
            "prepared purity is current-owner only"
        );
        match self
            .context
            .borrow_mut()
            .read_final(RuntimeKey::PreparedPurity(self.source_owner(owner)))
        {
            Some(RuntimeFact::PreparedPurity(purity)) => {
                self.dependencies
                    .borrow_mut()
                    .extend(&purity.proof_dependencies());
                purity.is_pure()
            }
            _ => unreachable!("prepared purity precedes current-owner execution"),
        }
    }

    /// Earlier owners, and the finalized current owner's frontier, consume
    /// only the verdict published after all evaluation/certificate work.
    pub(super) fn completed_statement_is_pure(&self, owner: SourceUnitOrdinal) -> bool {
        // A provider may have completed before the caller, or be suspended in
        // a cycle. Only a completed fact grants a purity capability.
        match self
            .context
            .borrow_mut()
            .read_final(RuntimeKey::CompletedPurity(self.source_owner(owner)))
        {
            Some(RuntimeFact::CompletedPurity(purity)) => {
                self.dependencies
                    .borrow_mut()
                    .extend(purity.proof_dependencies());
                purity.is_pure()
            }
            _ => false,
        }
    }

    pub(super) fn complete_owner_purity(
        &self,
        owner: SourceUnitOrdinal,
        certificate: Option<&LocalRecordFactoryPurityCertificate>,
        private_own_field_reads: &[Span],
        read_dependencies: &ProofDependencies,
    ) -> Option<crate::reports::schema::PurityCompletionDisagreement> {
        assert!(self.source_owner(owner) == self.point.owner);
        let mut context = self.context.borrow_mut();
        let Some(RuntimeFact::PreparedPurity(prepared)) =
            context.take_final(RuntimeKey::PreparedPurity(self.source_owner(owner)))
        else {
            unreachable!("owner finalization consumes its prepared obligations once");
        };
        let (completed, disagreement) =
            prepared.complete(certificate, private_own_field_reads, read_dependencies);
        context.assume(completed.proof_dependencies());
        context.propose(
            RuntimeKey::CompletedPurity(self.source_owner(owner)),
            RuntimeFact::CompletedPurity(Box::new(completed)),
        );
        disagreement
    }

    pub(super) fn assume(&self, dependencies: &ProofDependencies) {
        self.dependencies.borrow_mut().extend(dependencies);
        self.context.borrow_mut().assume(dependencies);
    }

    pub(super) fn dependencies(&self) -> ProofDependencies {
        self.dependencies.borrow().clone()
    }

    pub(super) fn extend_dependencies(&self, dependencies: &mut ProofDependencies) {
        dependencies.extend(&self.dependencies.borrow());
    }

    fn child_point(&self, kind: NativeProgramPointKind) -> ProgramPoint {
        let ordinal = self.next_child.get();
        self.next_child.set(ordinal + 1);
        let mut point = self.point.clone();
        point.path.push((kind, ordinal));
        point
    }

    pub(super) fn project_own_property(&self, query: OwnPropertyQuery) -> Option<OwnPropertyProof> {
        let point = self.child_point(NativeProgramPointKind::OwnProperty);
        let mut context = self.context.borrow_mut();
        context.propose(
            RuntimeKey::OwnPropertyRequest(point.clone()),
            RuntimeFact::OwnPropertyRequest(Box::new(query)),
        );
        RuntimeRules::project_own_property(self.inputs, &point, &mut **context);
        let Some(RuntimeFact::OwnPropertyResult(proof)) =
            context.take_final(RuntimeKey::OwnPropertyResult(point))
        else {
            unreachable!("a settled property query produces a terminal optional proof");
        };
        if let Some(proof) = &proof {
            self.dependencies.borrow_mut().extend(&proof.dependencies);
        }
        proof.map(|proof| *proof)
    }

    pub(super) fn request(
        &self,
        state: Box<ExecutionState>,
        observations: Observations,
        operation: RuntimeOperation,
    ) -> RuntimeResult {
        let kind = match &operation {
            RuntimeOperation::Heap(_) => NativeProgramPointKind::Heap,
            RuntimeOperation::Evaluate { .. } | RuntimeOperation::EvaluateInitializer { .. } => {
                NativeProgramPointKind::Evaluate
            }
            RuntimeOperation::Invoke { .. } => NativeProgramPointKind::Invoke,
            RuntimeOperation::Call { .. } => NativeProgramPointKind::Call,
            RuntimeOperation::Callbacks(_) => NativeProgramPointKind::Callbacks,
            RuntimeOperation::Model(_) => NativeProgramPointKind::Model,
        };
        let point = self.child_point(kind);
        let mut context = self.context.borrow_mut();
        context.propose(
            RuntimeKey::Request(point.clone()),
            RuntimeFact::Request(Box::new(RuntimeRequest {
                state,
                observations,
                operation,
            })),
        );
        RuntimeRules::transfer(self.inputs, &point, &mut **context);
        let Some(RuntimeFact::Result(result)) = context.take_final(RuntimeKey::Result(point))
        else {
            unreachable!("a transfer with settled inputs produces a terminal result");
        };
        *result
    }
}

struct RuntimeRules<'source, 'inputs> {
    inputs: &'inputs ExecutionProgramInputs<'source>,
    schedule: Vec<SourceOwner>,
    stop_at_abrupt: bool,
}

impl IterativeRules for RuntimeRules<'_, '_> {
    type Key = RuntimeKey;
    type Fact = RuntimeFact;
    type Rule = RuntimeRule;

    fn producers_for(&self, key: &RuntimeKey, _: &RuntimeFact) -> Vec<RuntimeRule> {
        match key {
            RuntimeKey::Prefix(position) if *position < self.schedule.len() => {
                vec![RuntimeRule::ExecuteOwner(*position)]
            }
            RuntimeKey::Request(point) => vec![RuntimeRule::Transfer(point.clone())],
            RuntimeKey::OwnPropertyRequest(point) => vec![RuntimeRule::OwnProperty(point.clone())],
            RuntimeKey::Prefix(_)
            | RuntimeKey::PreparedPurity(_)
            | RuntimeKey::CompletedPurity(_)
            | RuntimeKey::Effects(_)
            | RuntimeKey::Result(_)
            | RuntimeKey::OwnPropertyResult(_)
            | RuntimeKey::InvocationMemo(_) => Vec::new(),
        }
    }

    fn evaluate(
        &self,
        rule: &RuntimeRule,
        context: &mut impl FactContext<RuntimeKey, RuntimeFact>,
    ) {
        match rule {
            RuntimeRule::ExecuteOwner(position) => self.execute_owner(*position, context),
            RuntimeRule::Transfer(point) => Self::transfer(self.inputs, point, context),
            RuntimeRule::OwnProperty(point) => {
                Self::project_own_property(self.inputs, point, context)
            }
        }
    }

    fn merge(key: &RuntimeKey, current: &mut RuntimeFact, contribution: RuntimeFact) -> bool {
        // Each key has one producer operating on an immutable predecessor.
        // Re-evaluation can add proof dependencies, never a different state.
        assert!(matches!(
            (key, current, contribution),
            (
                RuntimeKey::Prefix(_),
                RuntimeFact::Prefix(_),
                RuntimeFact::Prefix(_)
            ) | (
                RuntimeKey::PreparedPurity(_),
                RuntimeFact::PreparedPurity(_),
                RuntimeFact::PreparedPurity(_)
            ) | (
                RuntimeKey::CompletedPurity(_),
                RuntimeFact::CompletedPurity(_),
                RuntimeFact::CompletedPurity(_)
            ) | (
                RuntimeKey::Effects(_),
                RuntimeFact::Effects(_),
                RuntimeFact::Effects(_)
            ) | (
                RuntimeKey::Request(_),
                RuntimeFact::Request(_),
                RuntimeFact::Request(_)
            ) | (
                RuntimeKey::Result(_),
                RuntimeFact::Result(_),
                RuntimeFact::Result(_)
            ) | (
                RuntimeKey::OwnPropertyRequest(_),
                RuntimeFact::OwnPropertyRequest(_),
                RuntimeFact::OwnPropertyRequest(_)
            ) | (
                RuntimeKey::OwnPropertyResult(_),
                RuntimeFact::OwnPropertyResult(_),
                RuntimeFact::OwnPropertyResult(_)
            ) | (
                RuntimeKey::InvocationMemo(_),
                RuntimeFact::InvocationMemo(_),
                RuntimeFact::InvocationMemo(_)
            )
        ));
        false
    }
}

impl RuntimeRules<'_, '_> {
    fn project_own_property(
        inputs: &ExecutionProgramInputs<'_>,
        point: &ProgramPoint,
        context: &mut dyn FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let Some(RuntimeFact::OwnPropertyRequest(query)) =
            context.take_final(RuntimeKey::OwnPropertyRequest(point.clone()))
        else {
            return;
        };
        let proof = inputs
            .source_for_scope(query.source_object.source)
            .and_then(|source| source.project_own_property(*query));
        if let Some(proof) = &proof {
            context.assume(&proof.dependencies);
        }
        context.propose(
            RuntimeKey::OwnPropertyResult(point.clone()),
            RuntimeFact::OwnPropertyResult(proof.map(Box::new)),
        );
    }

    fn execute_owner(
        &self,
        position: usize,
        context: &mut impl FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let Some(RuntimeFact::Prefix(state)) = context.take_final(RuntimeKey::Prefix(position))
        else {
            return;
        };
        let selected = self.schedule[position];
        let owner = selected.ordinal;
        let queries = RuntimeQueries {
            inputs: self.inputs,
            point: ProgramPoint {
                owner: selected,
                path: Vec::new(),
            },
            active_source: Cell::new(self.inputs.root.index.source_scope()),
            next_child: Cell::new(0),
            next_allocation: Cell::new(0),
            dependencies: RefCell::default(),
            context: RefCell::new(context),
        };
        let mut executor = Executor::new(self.inputs, state, Observations::default(), queries);
        executor.validate_evaluation = self.stop_at_abrupt;
        executor.state.effect_owner = Some(selected);
        let effects = executor
            .in_source(selected.source, |executor| {
                use super::world::program_modules::ModuleEvaluationPhase;
                executor.state.world_mut().module_phase =
                    ModuleEvaluationPhase::Evaluating { next_owner: owner };
                let prepared = executor
                    .source_inputs()
                    .statement_effects(owner, &executor.state);
                executor.queries.prepare_owner(owner, prepared);
                let effects = executor.execute_owner(owner);
                executor.state.world_mut().module_phase =
                    if owner.0 + 1 == executor.source_inputs().facts.len() {
                        ModuleEvaluationPhase::Evaluated
                    } else {
                        ModuleEvaluationPhase::Evaluating {
                            next_owner: SourceUnitOrdinal(owner.0 + 1),
                        }
                    };
                effects
            })
            .expect("every scheduled source has instantiated module cells");
        executor.mark_allocation_opaque();
        let (mut state, _, _, _) = executor.into_transfer();
        // Requests/results are taken at their single-consumer sites, and
        // owner finalization retires invocation memos. No active transfer
        // frame can hold a cell reference outside this settled prefix now.
        state.reclaim_lexical_cells(position + 1 == self.schedule.len());
        let effects_completion = effects.completion;
        context.propose(
            RuntimeKey::Effects(selected),
            RuntimeFact::Effects(Box::new(effects)),
        );
        if !self.stop_at_abrupt
            || effects_completion != crate::program_execution::schedule::OwnerCompletion::Abrupt
        {
            context.propose(RuntimeKey::Prefix(position + 1), RuntimeFact::Prefix(state));
        }
    }

    fn transfer(
        inputs: &ExecutionProgramInputs<'_>,
        point: &ProgramPoint,
        context: &mut dyn FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let Some(RuntimeFact::Request(request)) =
            context.take_final(RuntimeKey::Request(point.clone()))
        else {
            return;
        };
        let RuntimeRequest {
            state,
            observations,
            operation,
        } = *request;
        let queries = RuntimeQueries {
            inputs,
            point: point.clone(),
            active_source: Cell::new(match &state.current_program {
                Some(module) => inputs
                    .program_sources
                    .and_then(|sources| sources.get(module))
                    .expect("an active source has a prepared view")
                    .index
                    .source_scope(),
                None => inputs.root.index.source_scope(),
            }),
            next_child: Cell::new(0),
            next_allocation: Cell::new(0),
            dependencies: RefCell::default(),
            context: RefCell::new(context),
        };
        let mut executor = Executor::new(inputs, state, observations, queries);
        if !matches!(
            &operation,
            RuntimeOperation::Evaluate { .. } | RuntimeOperation::EvaluateInitializer { .. }
        ) {
            executor.mark_allocation_opaque();
        }
        let value = match operation {
            RuntimeOperation::Heap(operation) => {
                RuntimeValue::Heap(operation.execute(&mut executor))
            }
            RuntimeOperation::Evaluate {
                expression,
                environment,
                captures,
                source_covered,
            } => {
                if source_covered {
                    executor.cover_source_expression();
                }
                RuntimeValue::Values(executor.evaluate_transfer(
                    &expression,
                    &environment,
                    &captures,
                ))
            }
            RuntimeOperation::EvaluateInitializer {
                source,
                expression,
                environment,
                captures,
            } => {
                let (values, judgment, native_constructor_transfer, operation_coverage) = executor
                    .evaluate_initializer_transfer(&source, &expression, &environment, &captures);
                let operation_source = source.operation_source(&expression).is_some();
                let evaluated = if operation_source {
                    None
                } else {
                    source.evaluated(
                        executor.source_index().source_scope(),
                        &expression,
                        &values,
                        &judgment,
                        native_constructor_transfer,
                    )
                };
                let covered_operation = operation_source
                    && !values.is_empty()
                    && !values.contains(&super::value_model::AbstractValue::Value(
                        crate::facts::abstract_domain::value::Value::Unavailable,
                    ))
                    && operation_coverage
                        .as_ref()
                        .and_then(EffectCoverageFragment::normal_dependencies)
                        .is_some();
                let refusal = (evaluated.is_none() && !covered_operation)
                    .then_some(OwnerInitializationCoverageRefusalReason::InitializerResultUnproved);
                RuntimeValue::Initializer {
                    values,
                    evaluated: evaluated.map(Box::new),
                    operation_coverage: operation_coverage
                        .filter(|_| covered_operation)
                        .map(Box::new),
                    refusal,
                }
            }
            RuntimeOperation::Invoke {
                callable,
                arguments,
                argument_selections,
                captured,
                fresh_checked_constructor,
                callsite,
            } => RuntimeValue::Outcome(Box::new(executor.invoke_transfer(
                callable,
                arguments,
                argument_selections,
                captured,
                fresh_checked_constructor,
                callsite,
            ))),
            RuntimeOperation::Call {
                call,
                environment,
                captures,
                mut outcome,
                operands,
            } => {
                executor.promote_importer_cell_reads(&call, &mut outcome);
                let transfer = match operands {
                    Some(operands) => executor.execute_supplied_call(
                        &call,
                        &environment,
                        &captures,
                        &mut outcome,
                        *operands,
                    ),
                    None => {
                        executor.execute_call_transfer(&call, &environment, &captures, &mut outcome)
                    }
                };
                RuntimeValue::Call { outcome, transfer }
            }
            RuntimeOperation::Model(operation) => {
                RuntimeValue::Model(runtime_models::dispatch(&mut executor, operation))
            }
            RuntimeOperation::Callbacks(operation) => {
                RuntimeValue::Callbacks(runtime_callbacks::dispatch(&mut executor, operation))
            }
        };
        let (state, observations, allocation_judgment, evaluation_effects) =
            executor.into_transfer();
        context.propose(
            RuntimeKey::Result(point.clone()),
            RuntimeFact::Result(Box::new(RuntimeResult {
                state,
                observations,
                value,
                allocation_judgment,
                evaluation_effects,
            })),
        );
    }
}

pub(super) fn infer_synchronous_execution(
    inputs: &ExecutionProgramInputs<'_>,
    initial: ExecutionState,
    owner_count: usize,
) -> Vec<(SynchronousCalls, CompletedPurity)> {
    let root_scope = inputs.root.index.source_scope();
    let schedule = match (
        inputs.root.executables,
        inputs.root.module_id,
        inputs.program_sources,
    ) {
        (Some(program), Some(entry), Some(sources)) => {
            match program.source_evaluation_order(entry) {
                source_identity::ProgramLinkResolution::Resolved(order) => order
                    .into_iter()
                    .flat_map(|module| {
                        let source = if module == entry {
                            inputs.root
                        } else {
                            sources
                                .get(&module)
                                .expect("linked modules have prepared source views")
                        };
                        (0..source.facts.len()).map(move |ordinal| SourceOwner {
                            source: source.index.source_scope(),
                            ordinal: SourceUnitOrdinal(ordinal),
                        })
                    })
                    .collect(),
                _ => (0..owner_count)
                    .map(|ordinal| SourceOwner {
                        source: root_scope,
                        ordinal: SourceUnitOrdinal(ordinal),
                    })
                    .collect(),
            }
        }
        _ => (0..owner_count)
            .map(|ordinal| SourceOwner {
                source: root_scope,
                ordinal: SourceUnitOrdinal(ordinal),
            })
            .collect(),
    };
    let mut facts = infer_scheduled_execution(inputs, initial, schedule, false);
    (0..owner_count)
        .map(|ordinal| {
            let owner = SourceOwner {
                source: root_scope,
                ordinal: SourceUnitOrdinal(ordinal),
            };
            facts
                .remove(&owner)
                .expect("every source owner has a completed execution and purity result")
        })
        .collect()
}

/// Execute exactly the requested owner rows in one instantiated program world.
/// No later owner is submitted after a definite uncaught abrupt completion.
pub(crate) fn infer_scheduled_execution(
    inputs: &ExecutionProgramInputs<'_>,
    initial: ExecutionState,
    schedule: Vec<SourceOwner>,
    stop_at_abrupt: bool,
) -> std::collections::BTreeMap<SourceOwner, (SynchronousCalls, CompletedPurity)> {
    let owners = schedule.clone();
    let mut inference = IterativeInference::new(RuntimeRules {
        inputs,
        schedule,
        stop_at_abrupt,
    });
    inference.seed(
        RuntimeKey::Prefix(0),
        RuntimeFact::Prefix(Box::new(initial)),
    );
    let mut facts = inference.solve([]).into_values();
    owners
        .into_iter()
        .filter_map(|owner| {
            match (
                facts.remove(&RuntimeKey::Effects(owner)),
                facts.remove(&RuntimeKey::CompletedPurity(owner)),
            ) {
                (Some(RuntimeFact::Effects(calls)), Some(RuntimeFact::CompletedPurity(purity))) => {
                    Some((owner, (*calls, *purity)))
                }
                (None, None) if stop_at_abrupt => None,
                _ => unreachable!("an executed owner has both effects and finalized purity"),
            }
        })
        .collect()
}
