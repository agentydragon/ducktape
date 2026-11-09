//! Position-specific execution facts. A later source prefix consumes the
//! preceding prefix's settled state and never changes that earlier state.

use super::super::allocation_transfer::{AllocationJudgment, AllocationValue, ChildTransfer};
use super::super::local_transfer::{
    CallableSelection, EvaluatedCallee, LocalContinuation, SelectedValue,
};
use super::super::noninvoking_initialization::{EvaluatedInitializer, OriginalInitializer};
use super::super::property_inference::OwnPropertyProof;
use super::super::value_model::{CapturedContext, Context, ValueExpr, Values};
use super::Call;
use super::interpreter::{
    ExecutionInputs, ExecutionState, Executor, Outcome, OwnPropertyQuery, RuntimeTransferFamily,
};
use super::runtime_callbacks::{self, CallbackOperation, CallbackResult};
use super::runtime_heap::{HeapOperation, HeapResult};
use super::runtime_models::{self, ModelOperation, ModelResult};
use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::OrderedCallAdmissionRefusal;
use crate::facts::evaluation_effects::EvaluationEffects;
use crate::facts::statement_facts::SynchronousCalls;
use crate::facts::{
    CompletedPurity, LocalRecordFactoryPurityCertificate, OwnerInitializationCoverageRefusalReason,
    PreparedPurity,
};
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use crate::reports::schema::{NativeProgramPointKind, NativeProgramPointStep};
use source_identity::SourceUnitOrdinal;
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

/// Copy-on-write execution state owned by this run-local inference context.
pub(super) type ExecutionStateSnapshot = Rc<ExecutionState>;

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum RuntimeKey {
    Prefix(SourceUnitOrdinal),
    /// Current-owner preparatory classifier obligations at this prefix.
    PreparedPurity(SourceUnitOrdinal),
    /// Final verdict published only after every owner finalization step.
    CompletedPurity(SourceUnitOrdinal),
    Effects(SourceUnitOrdinal),
    Request(ProgramPoint),
    Result(ProgramPoint),
    OwnPropertyRequest(ProgramPoint),
    OwnPropertyResult(ProgramPoint),
    InvocationMemo(ProgramPoint),
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum RuntimeRule {
    ExecuteOwner(SourceUnitOrdinal),
    Transfer(ProgramPoint),
    OwnProperty(ProgramPoint),
}

enum RuntimeFact {
    Prefix(ExecutionStateSnapshot),
    PreparedPurity(Box<PreparedPurity>),
    CompletedPurity(Box<CompletedPurity>),
    Effects(Box<SynchronousCalls>),
    Request(Box<RuntimeRequest>),
    Result(Box<RuntimeResult>),
    OwnPropertyRequest(Box<OwnPropertyQuery>),
    OwnPropertyResult(Option<Box<OwnPropertyProof>>),
    InvocationMemo(Box<(RuntimeTransferFamily, Option<ExecutionStateSnapshot>)>),
}

/// The path identifies an occurrence within its parent's transfer, rather than
/// conflating repeated evaluation of the same expression in different contexts.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) struct ProgramPoint {
    owner: SourceUnitOrdinal,
    path: Vec<(NativeProgramPointKind, usize)>,
    branch_lineage: Vec<AlternativeBranchId>,
}

/// Unique identity for one feasible branch within a single inference run.
/// Runtime facts and invocation memos include the full lineage so sibling
/// paths cannot alias merely because they perform the same child ordinal.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) struct AlternativeBranchId(u64);

/// Identity for one explicitly joined set of path alternatives. It shares the
/// run-scoped allocator with branch identities but cannot be confused with one.
#[derive(Debug, Clone)]
pub(super) struct AlternativeFamilyId {
    allocator: BranchLineageArena,
    ordinal: u64,
}

impl PartialEq for AlternativeFamilyId {
    fn eq(&self, other: &Self) -> bool {
        self.ordinal == other.ordinal && self.allocator.shares_arena(&other.allocator)
    }
}

impl Eq for AlternativeFamilyId {}

#[derive(Debug, Clone, Default)]
struct BranchLineageArena(Arc<AtomicU64>);

impl BranchLineageArena {
    fn allocate(&self) -> AlternativeBranchId {
        AlternativeBranchId(self.allocate_id())
    }

    fn allocate_family(&self) -> AlternativeFamilyId {
        AlternativeFamilyId {
            allocator: self.clone(),
            ordinal: self.allocate_id(),
        }
    }

    fn shares_arena(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }

    fn allocate_id(&self) -> u64 {
        self.0
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |next| {
                next.checked_add(1)
            })
            .expect("alternative branch identity exhausted")
    }
}

/// The ordered caller has already read its callee and arguments. The call
/// transfer must use these snapshots rather than evaluate their source again.
pub(super) struct CallOperands {
    pub(super) callee: EvaluatedCallee,
    pub(super) arguments: Vec<SelectedValue>,
    /// The exact root has independent pure-initializer and operation inputs.
    /// This only admits an existing callback-factory result transfer; the
    /// ordered caller still checks the operation domains.
    pub(super) selected_pure_initializer_call: bool,
}

pub(super) enum CallTransfer {
    /// Consult the accompanying outcome for effect completeness. No return
    /// value was requested, or the selected proof supplies only effects.
    EffectsOnly,
    /// The accompanying outcome ledger contains the requested result
    /// projection. This enum is only a disposition, never another result
    /// authority.
    ResultAvailable,
    /// The ordered operand path declined this target. Its source-based
    /// helpers cannot safely replay already evaluated operands.
    UnsupportedOperands(OrderedCallAdmissionRefusal),
    /// The opaque invocation protocol consumed all evaluated operands. This
    /// carries no result or coverage proof; a caller remainder still needs fallback.
    SummarizedOpaque(OrderedCallAdmissionRefusal),
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
        callable: u32,
        arguments: Vec<Values>,
        argument_selections: Vec<Option<CallableSelection>>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
        receiver: Option<Values>,
        continuation: Option<Box<LocalContinuation>>,
    },
    Call {
        continuation: Option<Box<LocalContinuation>>,
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
    AllocationTransfer(Box<ChildTransfer<AllocationValue>>),
    Initializer {
        transfer: Box<ChildTransfer<AllocationValue>>,
        evaluated: Option<Box<EvaluatedInitializer>>,
        refusal: Option<OwnerInitializationCoverageRefusalReason>,
    },
    Outcome(Box<RuntimeTransferFamily>),
    Call {
        family: Box<RuntimeTransferFamily>,
        transfer: CallTransfer,
    },
    Callbacks(CallbackResult),
    Model(ModelResult),
}

struct RuntimeRequest {
    state: ExecutionStateSnapshot,
    operation: RuntimeOperation,
}

pub(super) struct RuntimeResult {
    pub(super) state: ExecutionStateSnapshot,
    pub(super) value: RuntimeValue,
    pub(super) allocation_judgment: AllocationJudgment,
    pub(super) evaluation_effects: EvaluationEffects,
}

/// Transfers consume settled inputs and cannot send information back to their
/// predecessors. Evaluate these acyclic children immediately on the same fact
/// context, then retire their single-consumer payloads. Growing fact families
/// continue to use the dependency worklist. This avoids replaying a parent or
/// retaining every heap snapshot for a long, flat sequence of source calls.
pub(super) struct RuntimeQueries<'source, 'query> {
    inputs: &'query ExecutionInputs<'source>,
    point: ProgramPoint,
    next_child: Cell<usize>,
    branch_lineage: Rc<RefCell<Vec<AlternativeBranchId>>>,
    branch_ids: BranchLineageArena,
    dependencies: RefCell<ProofDependencies>,
    context: RefCell<&'query mut dyn FactContext<RuntimeKey, RuntimeFact>>,
}

pub(super) struct BranchLineageScope {
    lineage: Rc<RefCell<Vec<AlternativeBranchId>>>,
    prior: Vec<AlternativeBranchId>,
}

impl Drop for BranchLineageScope {
    fn drop(&mut self) {
        *self.lineage.borrow_mut() = std::mem::take(&mut self.prior);
    }
}

impl RuntimeQueries<'_, '_> {
    pub(super) fn program_point(&self) -> &ProgramPoint {
        &self.point
    }
    /// Source ownership of this transfer, including read-only proof queries
    /// outside an active top-level execution frame. This grants no invocation.
    pub(super) fn source_owner(&self) -> SourceUnitOrdinal {
        self.point.owner
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
        entry: &ExecutionStateSnapshot,
    ) -> Option<RuntimeTransferFamily> {
        let mut context = self.context.borrow_mut();
        let Some(RuntimeFact::InvocationMemo(outcome)) =
            context.read_final(RuntimeKey::InvocationMemo(point.clone()))
        else {
            unreachable!("a live invocation memo points to its completed outcome");
        };
        // Whole-world replay requires correspondence with the input snapshot.
        // Structural equivalence can broaden this admission later; a summary
        // signature alone does not prove that unrelated caller state is equal.
        outcome
            .1
            .as_ref()
            .filter(|saved| Rc::ptr_eq(saved, entry))
            .map(|_| outcome.0.clone())
    }

    pub(super) fn publish_invocation_memo(
        &self,
        family: &RuntimeTransferFamily,
        entry: Option<ExecutionStateSnapshot>,
    ) -> ProgramPoint {
        let mut point = self.point.clone();
        point.branch_lineage = self.branch_lineage.borrow().clone();
        self.context.borrow_mut().propose(
            RuntimeKey::InvocationMemo(point.clone()),
            RuntimeFact::InvocationMemo(Box::new((family.clone(), entry))),
        );
        point
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
            owner, self.point.owner,
            "prepared purity is current-owner only"
        );
        match self
            .context
            .borrow_mut()
            .read_final(RuntimeKey::PreparedPurity(owner))
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
        assert!(owner <= self.point.owner);
        match self
            .context
            .borrow_mut()
            .read_final(RuntimeKey::CompletedPurity(owner))
        {
            Some(RuntimeFact::CompletedPurity(purity)) => {
                self.dependencies
                    .borrow_mut()
                    .extend(purity.proof_dependencies());
                purity.is_pure()
            }
            _ => unreachable!("completed purity requires a finalized owner"),
        }
    }

    pub(super) fn complete_owner_purity(
        &self,
        owner: SourceUnitOrdinal,
        certificate: Option<&LocalRecordFactoryPurityCertificate>,
    ) -> Option<crate::reports::schema::PurityCompletionDisagreement> {
        assert_eq!(owner, self.point.owner);
        let mut context = self.context.borrow_mut();
        let Some(RuntimeFact::PreparedPurity(prepared)) =
            context.take_final(RuntimeKey::PreparedPurity(owner))
        else {
            unreachable!("owner finalization consumes its prepared obligations once");
        };
        let (completed, disagreement) = prepared.complete(certificate);
        context.assume(completed.proof_dependencies());
        context.propose(
            RuntimeKey::CompletedPurity(owner),
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
        point.branch_lineage = self.branch_lineage.borrow().clone();
        point
    }

    /// Clone an execution snapshot for a possible path while preserving the
    /// shared semantic identity arena and run-scoped expansion budget.
    pub(super) fn fork_alternative(
        &self,
        state: &ExecutionStateSnapshot,
    ) -> (AlternativeBranchId, ExecutionStateSnapshot) {
        assert_eq!(
            state.branch_lineage.as_slice(),
            self.branch_lineage.borrow().as_slice(),
            "alternative forks must start from the active state lineage"
        );
        let branch = self.branch_ids.allocate();
        // Existing memo points belong to the parent lineage. Calls in this
        // fork use a distinct signature and must not retire/reuse those
        // shared fact entries as if this branch owned them.
        let fork = state.fork_for_alternative(branch);
        (branch, Rc::new(fork))
    }

    /// Evaluate one branch sequentially against the exclusive FactContext.
    /// The scope is restored on normal return and unwind; no context clone is
    /// made, and all child fact keys carry this branch's lineage.
    pub(super) fn enter_branch_lineage(
        &self,
        lineage: &[AlternativeBranchId],
    ) -> BranchLineageScope {
        let prior = std::mem::replace(&mut *self.branch_lineage.borrow_mut(), lineage.to_vec());
        BranchLineageScope {
            lineage: self.branch_lineage.clone(),
            prior,
        }
    }

    pub(super) fn allocate_alternative_family(&self) -> AlternativeFamilyId {
        self.branch_ids.allocate_family()
    }

    pub(super) fn owns_alternative_family(&self, family: &AlternativeFamilyId) -> bool {
        self.branch_ids.shares_arena(&family.allocator)
    }

    pub(super) fn branch_lineage(&self) -> Vec<AlternativeBranchId> {
        self.branch_lineage.borrow().clone()
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
        state: ExecutionStateSnapshot,
        operation: RuntimeOperation,
    ) -> RuntimeResult {
        assert_eq!(
            state.branch_lineage.as_slice(),
            self.branch_lineage.borrow().as_slice(),
            "execution state and runtime fact request must share branch lineage"
        );
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
            RuntimeFact::Request(Box::new(RuntimeRequest { state, operation })),
        );
        RuntimeRules::transfer(self.inputs, &point, &self.branch_ids, &mut **context);
        let Some(RuntimeFact::Result(result)) = context.take_final(RuntimeKey::Result(point))
        else {
            unreachable!("a transfer with settled inputs produces a terminal result");
        };
        *result
    }
}

struct RuntimeRules<'source, 'inputs> {
    inputs: &'inputs ExecutionInputs<'source>,
    owner_count: usize,
    branch_ids: BranchLineageArena,
}

impl IterativeRules for RuntimeRules<'_, '_> {
    type Key = RuntimeKey;
    type Fact = RuntimeFact;
    type Rule = RuntimeRule;

    fn producers_for(&self, key: &RuntimeKey, _: &RuntimeFact) -> Vec<RuntimeRule> {
        match key {
            RuntimeKey::Prefix(owner) if owner.0 < self.owner_count => {
                vec![RuntimeRule::ExecuteOwner(*owner)]
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
            RuntimeRule::ExecuteOwner(owner) => self.execute_owner(*owner, context),
            RuntimeRule::Transfer(point) => {
                Self::transfer(self.inputs, point, &self.branch_ids, context)
            }
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
        inputs: &ExecutionInputs<'_>,
        point: &ProgramPoint,
        context: &mut dyn FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let Some(RuntimeFact::OwnPropertyRequest(query)) =
            context.take_final(RuntimeKey::OwnPropertyRequest(point.clone()))
        else {
            return;
        };
        let proof = inputs.project_own_property(*query);
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
        owner: SourceUnitOrdinal,
        context: &mut impl FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let Some(RuntimeFact::Prefix(state)) = context.take_final(RuntimeKey::Prefix(owner)) else {
            return;
        };
        // Preparatory classifier obligations remain exactly the current
        // source/prefix transfer. Finalization publishes a separate verdict.
        context.propose(
            RuntimeKey::PreparedPurity(owner),
            RuntimeFact::PreparedPurity(Box::new(PreparedPurity::new(
                self.inputs.statement_effects(owner, &state),
            ))),
        );
        let branch_lineage = state.branch_lineage.clone();
        let queries = RuntimeQueries {
            inputs: self.inputs,
            point: ProgramPoint {
                owner,
                path: Vec::new(),
                branch_lineage: branch_lineage.clone(),
            },
            next_child: Cell::new(0),
            branch_lineage: Rc::new(RefCell::new(branch_lineage)),
            branch_ids: self.branch_ids.clone(),
            dependencies: RefCell::default(),
            context: RefCell::new(context),
        };
        let mut executor = Executor::new(self.inputs, state, queries);
        let effects = executor.execute_owner(owner);
        executor.mark_allocation_opaque();
        let (mut state, _, _) = executor.into_transfer();
        // Requests/results are taken at their single-consumer sites, and
        // owner finalization retires invocation memos. No active transfer
        // frame can hold a cell reference outside this settled prefix now.
        Rc::make_mut(&mut state).reclaim_lexical_cells(owner.0 + 1 == self.owner_count);
        context.propose(
            RuntimeKey::Effects(owner),
            RuntimeFact::Effects(Box::new(effects)),
        );
        context.propose(
            RuntimeKey::Prefix(SourceUnitOrdinal(owner.0 + 1)),
            RuntimeFact::Prefix(state),
        );
    }

    fn transfer(
        inputs: &ExecutionInputs<'_>,
        point: &ProgramPoint,
        branch_ids: &BranchLineageArena,
        context: &mut dyn FactContext<RuntimeKey, RuntimeFact>,
    ) {
        let Some(RuntimeFact::Request(request)) =
            context.take_final(RuntimeKey::Request(point.clone()))
        else {
            return;
        };
        let RuntimeRequest { state, operation } = *request;
        let queries = RuntimeQueries {
            inputs,
            point: point.clone(),
            next_child: Cell::new(0),
            branch_lineage: Rc::new(RefCell::new(point.branch_lineage.clone())),
            branch_ids: branch_ids.clone(),
            dependencies: RefCell::default(),
            context: RefCell::new(context),
        };
        let mut executor = Executor::new(inputs, state, queries);
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
                RuntimeValue::AllocationTransfer(Box::new(executor.evaluate_transfer(
                    &expression,
                    &environment,
                    &captures,
                )))
            }
            RuntimeOperation::EvaluateInitializer {
                source,
                expression,
                environment,
                captures,
            } => {
                let (transfer, native_constructor_transfer, operation_ledger) = executor
                    .evaluate_initializer_transfer(&source, &expression, &environment, &captures);
                let operation_source = source.operation_source(&expression).is_some();
                let evaluated = if operation_source {
                    None
                } else if let ChildTransfer::Value(value) = &transfer {
                    source.evaluated(
                        executor.heap_resolver(),
                        &expression,
                        value,
                        native_constructor_transfer,
                    )
                } else {
                    None
                };
                let covered_operation = operation_source
                    && matches!(&transfer, ChildTransfer::Value(value)
                        if value.ledger.is_exact_normal())
                    && operation_ledger
                        .as_ref()
                        .and_then(|ledger| ledger.normal_dependencies())
                        .is_some();
                let refusal = (evaluated.is_none() && !covered_operation)
                    .then_some(OwnerInitializationCoverageRefusalReason::InitializerResultUnproved);
                RuntimeValue::Initializer {
                    transfer: Box::new(transfer),
                    evaluated: evaluated.map(Box::new),
                    refusal,
                }
            }
            RuntimeOperation::Invoke {
                callable,
                arguments,
                argument_selections,
                captured,
                fresh_checked_constructor,
                receiver,
                continuation,
            } => {
                let family = executor.invoke_transfer(
                    callable,
                    arguments,
                    argument_selections,
                    captured,
                    fresh_checked_constructor,
                    receiver,
                );
                let family = match continuation {
                    Some(continuation) => family.with_continuation(*continuation),
                    None => family,
                };
                RuntimeValue::Outcome(Box::new(family))
            }
            RuntimeOperation::Call {
                continuation,
                call,
                environment,
                captures,
                outcome,
                operands,
            } => {
                let (transfer, family) = executor.execute_call_transfer_family(
                    &call,
                    &environment,
                    &captures,
                    *outcome,
                    operands.map(|operands| *operands),
                );
                let family = match continuation {
                    Some(continuation) => family.with_continuation(*continuation),
                    None => family,
                };
                RuntimeValue::Call {
                    family: Box::new(family),
                    transfer,
                }
            }
            RuntimeOperation::Model(operation) => {
                RuntimeValue::Model(runtime_models::dispatch(&mut executor, operation))
            }
            RuntimeOperation::Callbacks(operation) => {
                RuntimeValue::Callbacks(runtime_callbacks::dispatch(&mut executor, operation))
            }
        };
        let (state, allocation_judgment, evaluation_effects) = executor.into_transfer();
        context.propose(
            RuntimeKey::Result(point.clone()),
            RuntimeFact::Result(Box::new(RuntimeResult {
                state,
                value,
                allocation_judgment,
                evaluation_effects,
            })),
        );
    }
}

pub(super) fn infer_synchronous_execution(
    inputs: &ExecutionInputs<'_>,
    initial: ExecutionState,
    owner_count: usize,
) -> Vec<(SynchronousCalls, CompletedPurity)> {
    let branch_ids = BranchLineageArena::default();
    let mut inference = IterativeInference::new(RuntimeRules {
        inputs,
        owner_count,
        branch_ids,
    });
    inference.seed(
        RuntimeKey::Prefix(SourceUnitOrdinal(0)),
        RuntimeFact::Prefix(Rc::new(initial)),
    );
    let mut facts = inference.solve([]).into_values();
    (0..owner_count)
        .map(|ordinal| {
            let owner = SourceUnitOrdinal(ordinal);
            match (
                facts.remove(&RuntimeKey::Effects(owner)),
                facts.remove(&RuntimeKey::CompletedPurity(owner)),
            ) {
                (Some(RuntimeFact::Effects(calls)), Some(RuntimeFact::CompletedPurity(purity))) => {
                    (*calls, *purity)
                }
                _ => unreachable!("every source owner has a completed execution and purity result"),
            }
        })
        .collect()
}
