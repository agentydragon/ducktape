//! Path-preserving synchronous transfer alternatives.
//!
//! Each feasible path keeps its own state and typed completion until a checked
//! resume point. Joined outcomes union may-effects and intersect definite
//! operation receipts; a state join is stricter and refuses semantic-world
//! differences it cannot represent.

use super::super::runtime_inference::{
    AlternativeBranchId, AlternativeFamilyId, ExecutionStateSnapshot, ProgramPoint,
};
use super::conservative_world_join::{
    ConservativeWorldJoinDisposition, conservative_join_execution_states,
};
use super::{ExecutionState, Executor, Outcome, unknown};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::abstract_domain::value::{HeapAddress, Value};
use crate::facts::evaluation_effects::EvaluationEffects;
use crate::facts::statement_facts::SynchronousCalls;
use crate::facts::sync_effects::allocation_transfer::AllocationJudgment;
use crate::facts::sync_effects::effect_coverage::{BindingEffectCoverage, EffectCoverageFragment};
use crate::facts::sync_effects::local_transfer::{
    self, CallableSelection, LocalContinuation, SelectedValue,
};
use crate::facts::sync_effects::native_operation::NativeRetainedReferences;
use crate::facts::sync_effects::value_model::AbstractValue;
use crate::facts::sync_effects::value_model::{CallSite, Values};
use crate::facts::sync_effects::{
    AbruptAlternative, Completion, NonNormalPossibilities, TransferLedger,
};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use std::rc::Rc;
use std::sync::Arc;

/// A branch-local execution checkpoint. `Outcome` owns completion and exact
/// effects; executor-side allocation and evaluation ledgers travel alongside it.
#[derive(Clone)]
pub(super) struct TransferAlternative {
    family: AlternativeFamilyId,
    parent_lineage: Vec<AlternativeBranchId>,
    program_point: ProgramPoint,
    branch: AlternativeBranchId,
    state: ExecutionStateSnapshot,
    outcome: Outcome,
    continuation: Option<LocalContinuation>,
    allocation_judgment: AllocationJudgment,
    source_covered: bool,
    evaluation_effects: EvaluationEffects,
}
impl TransferAlternative {
    pub(super) fn branch_id(&self) -> AlternativeBranchId {
        self.branch
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum TransferJoinRefusal {
    DifferentFamily,
    DifferentProgramPoint,
    DifferentParentLineage,
    NoActiveExecutionState,
    EmptyAlternativeSet,
    IncompatibleExecutionState,
    UnknownParentBranch,
    WrongChildLineage,
    DuplicateBranchIdentity,
}

/// A joined callback result and the state disposition the consumer must honor.
/// `RequiresRemainderFallback` describes state/control resumability, not
/// completion: the consumer must preserve terminal abrupt outcomes and may
/// widen/run one remainder only for a real unfinished normal continuation.
pub(super) struct ConservativeTransferResume {
    pub(super) outcome: Outcome,
    pub(super) disposition: ConservativeWorldJoinDisposition,
}

#[derive(Clone)]
pub(super) struct TransferAlternatives {
    family: AlternativeFamilyId,
    program_point: ProgramPoint,
    parent_lineage: Vec<AlternativeBranchId>,
    alternatives: Vec<TransferAlternative>,
}

impl TransferAlternatives {
    pub(super) fn start(
        executor: &Executor<'_, '_>,
        outcome: Outcome,
    ) -> Result<(Self, AlternativeBranchId), TransferJoinRefusal> {
        let family = executor.queries.allocate_alternative_family();
        let program_point = executor.queries.program_point().clone();
        let parent_lineage = executor.queries.branch_lineage();
        let first = executor.fork_transfer_alternative(family.clone(), outcome)?;
        let branch = first.branch;
        Ok((
            Self {
                family,
                program_point,
                parent_lineage,
                alternatives: vec![first],
            },
            branch,
        ))
    }

    pub(super) fn family(&self) -> AlternativeFamilyId {
        self.family.clone()
    }

    /// Visit branches sequentially in the one live executor. Branch state and
    /// lineage are installed for the callback, then moved back into its path.
    pub(super) fn for_each_mut<T>(
        &mut self,
        executor: &mut Executor<'_, '_>,
        mut transfer: impl FnMut(AlternativeBranchId, &mut Executor<'_, '_>, &mut Outcome) -> T,
    ) -> Result<Vec<T>, TransferJoinRefusal> {
        self.check_executor(executor)?;
        let mut results = Vec::with_capacity(self.alternatives.len());
        for alternative in &mut self.alternatives {
            assert_eq!(alternative.family, self.family);
            let branch = alternative.branch;
            results.push(executor.run_in_transfer_alternative(alternative, branch, &mut transfer));
        }
        Ok(results)
    }

    /// Replace a visited branch by children forked while that branch was
    /// active. Keeping both the parent and children would double-count a path.
    pub(super) fn replace_branch(
        &mut self,
        parent_branch: AlternativeBranchId,
        children: Vec<TransferAlternative>,
    ) -> Result<(), TransferJoinRefusal> {
        if children.is_empty() {
            return Err(TransferJoinRefusal::EmptyAlternativeSet);
        }
        let Some(parent_index) = self
            .alternatives
            .iter()
            .position(|alternative| alternative.branch == parent_branch)
        else {
            return Err(TransferJoinRefusal::UnknownParentBranch);
        };
        let parent_lineage = self.alternatives[parent_index].state.branch_lineage.clone();
        let mut branch_ids = self
            .alternatives
            .iter()
            .filter(|alternative| alternative.branch != parent_branch)
            .map(|alternative| alternative.branch)
            .collect::<BTreeSet<_>>();
        for child in &children {
            if child.family != self.family
                || child.program_point != self.program_point
                || child.parent_lineage != parent_lineage
                || child.state.branch_lineage.len() != parent_lineage.len() + 1
                || !child.state.branch_lineage.starts_with(&parent_lineage)
                || child.state.branch_lineage.last() != Some(&child.branch)
            {
                return Err(TransferJoinRefusal::WrongChildLineage);
            }
            if !branch_ids.insert(child.branch) {
                return Err(TransferJoinRefusal::DuplicateBranchIdentity);
            }
        }
        let mut children = Some(children);
        let mut replacement =
            Vec::with_capacity(self.alternatives.len() - 1 + children.as_ref().map_or(0, Vec::len));
        for (index, alternative) in self.alternatives.drain(..).enumerate() {
            if index == parent_index {
                replacement.extend(
                    children
                        .take()
                        .expect("the replaced family leaf is visited once"),
                );
            } else {
                replacement.push(alternative);
            }
        }
        self.alternatives = replacement;
        Ok(())
    }

    /// Union the real typed ledgers. This operation is total for every valid,
    /// nonempty alternative set; inability to install a common state is
    /// reported separately and carries this outcome.
    pub(super) fn joined_outcome(&self) -> Outcome {
        assert!(!self.alternatives.is_empty());
        let outcomes = self
            .alternatives
            .iter()
            .map(|alternative| &alternative.outcome)
            .collect::<Vec<_>>();
        let evaluations = self
            .alternatives
            .iter()
            .map(|alternative| &alternative.evaluation_effects)
            .collect::<Vec<_>>();
        join_outcomes(&outcomes, &evaluations)
    }

    /// Consume this alternative family only after installing an owned state
    /// join. The exact-world collapse is the cheap strict path; if it refuses,
    /// use the fieldwise conservative join. If both refuse, return the entire
    /// carrier so callers cannot fall back on the executor's pre-branch state.
    pub(super) fn try_resume_conservative_state(
        self,
        executor: &mut Executor<'_, '_>,
    ) -> Result<ConservativeTransferResume, Box<(Self, TransferJoinRefusal)>> {
        let outcome = self.joined_outcome();
        if let Err(reason) = self.check_executor(executor) {
            return Err(Box::new((self, reason)));
        }

        let strict_state = join_execution_states(&self.alternatives, &self.parent_lineage);
        let joined_state = match strict_state {
            Ok(state) => Ok((state, ConservativeWorldJoinDisposition::Resume)),
            Err(_) => {
                let state_refs = self
                    .alternatives
                    .iter()
                    .map(|alternative| alternative.state.as_ref())
                    .collect::<Vec<_>>();
                conservative_join_execution_states(&state_refs, &self.parent_lineage)
                    .map(|joined| (joined.state, joined.disposition))
            }
        };
        let (state, disposition) = match joined_state {
            Ok(joined) => joined,
            Err(reason) => return Err(Box::new((self, reason))),
        };

        let allocation_judgment = join_allocation_judgments(
            &self
                .alternatives
                .iter()
                .map(|alternative| &alternative.allocation_judgment)
                .collect::<Vec<_>>(),
        );
        let source_covered = self
            .alternatives
            .iter()
            .all(|alternative| alternative.source_covered);
        let evaluation_effects = join_evaluation_effects(&self.alternatives);

        executor.state.0 = Some(Rc::new(state));
        executor.allocation_judgment = if disposition == ConservativeWorldJoinDisposition::Resume {
            allocation_judgment
        } else {
            AllocationJudgment::Opaque
        };
        executor.source_covered = source_covered;
        executor.evaluation_effects = evaluation_effects;

        Ok(ConservativeTransferResume {
            outcome,
            disposition,
        })
    }

    fn check_executor(&self, executor: &Executor<'_, '_>) -> Result<(), TransferJoinRefusal> {
        if !executor.queries.owns_alternative_family(&self.family) {
            return Err(TransferJoinRefusal::DifferentFamily);
        }
        if *executor.queries.program_point() != self.program_point {
            return Err(TransferJoinRefusal::DifferentProgramPoint);
        }
        if executor.queries.branch_lineage() != self.parent_lineage {
            return Err(TransferJoinRefusal::DifferentParentLineage);
        }
        Ok(())
    }

    fn replace_branch_with_descendants(
        &mut self,
        parent_branch: AlternativeBranchId,
        parent_lineage: &[AlternativeBranchId],
        children: Vec<TransferAlternative>,
    ) -> Result<(), TransferJoinRefusal> {
        if children.is_empty() {
            return Err(TransferJoinRefusal::EmptyAlternativeSet);
        }
        let Some(parent_index) = self
            .alternatives
            .iter()
            .position(|alternative| alternative.branch == parent_branch)
        else {
            return Err(TransferJoinRefusal::UnknownParentBranch);
        };
        let mut branch_ids = self
            .alternatives
            .iter()
            .filter(|alternative| alternative.branch != parent_branch)
            .map(|alternative| alternative.branch)
            .collect::<BTreeSet<_>>();
        for child in &children {
            if child.family != self.family
                || child.program_point != self.program_point
                || child.parent_lineage.len() < parent_lineage.len()
                || !child.parent_lineage.starts_with(parent_lineage)
                || child.state.branch_lineage.len() != child.parent_lineage.len() + 1
                || !child.state.branch_lineage.starts_with(parent_lineage)
                || !child
                    .state
                    .branch_lineage
                    .starts_with(&child.parent_lineage)
                || child.state.branch_lineage.last() != Some(&child.branch)
            {
                return Err(TransferJoinRefusal::WrongChildLineage);
            }
            if !branch_ids.insert(child.branch) {
                return Err(TransferJoinRefusal::DuplicateBranchIdentity);
            }
        }
        let mut children = Some(children);
        let mut replacement =
            Vec::with_capacity(self.alternatives.len() - 1 + children.as_ref().map_or(0, Vec::len));
        for (index, alternative) in self.alternatives.drain(..).enumerate() {
            if index == parent_index {
                replacement.extend(
                    children
                        .take()
                        .expect("the replaced family leaf is visited once"),
                );
            } else {
                replacement.push(alternative);
            }
        }
        self.alternatives = replacement;
        Ok(())
    }
}

/// Runtime transport for a set of executable path alternatives. Unlike a
/// joined Outcome, this retains each path's state and typed completion until
/// a caller explicitly resumes or refuses the family.
#[derive(Clone)]
pub(in crate::facts::sync_effects::execution_index) struct RuntimeTransferFamily {
    alternatives: TransferAlternatives,
}

impl RuntimeTransferFamily {
    pub(super) fn from_alternatives(alternatives: TransferAlternatives) -> Self {
        Self { alternatives }
    }

    pub(super) fn capture(executor: &Executor<'_, '_>, outcome: Outcome) -> Self {
        let (alternatives, _) = TransferAlternatives::start(executor, outcome)
            .expect("runtime family capture requires an active execution state");
        Self { alternatives }
    }

    /// Fork two sibling paths from the same current state. The caller must
    /// supply already-decided branch outcomes; opaque completions are never
    /// split here.
    pub(super) fn fork(executor: &Executor<'_, '_>, outcome: Outcome) -> Self {
        Self::fork_count(executor, outcome, std::num::NonZeroUsize::new(2).unwrap())
    }

    /// Split one post-prefix world into a checked nonempty completion family.
    /// Each leaf starts from the same snapshot; values and stores are applied
    /// only after that leaf has become the active world.
    pub(super) fn fork_count(
        executor: &Executor<'_, '_>,
        outcome: Outcome,
        count: std::num::NonZeroUsize,
    ) -> Self {
        let family = executor.queries.allocate_alternative_family();
        let alternatives = (0..count.get())
            .map(|_| {
                executor
                    .fork_transfer_alternative(family.clone(), outcome.clone())
                    .expect("runtime family fork requires an active execution state")
            })
            .collect();
        Self {
            alternatives: TransferAlternatives {
                family,
                program_point: executor.queries.program_point().clone(),
                parent_lineage: executor.queries.branch_lineage(),
                alternatives,
            },
        }
    }

    pub(super) fn len(&self) -> usize {
        self.alternatives.alternatives.len()
    }

    pub(super) fn branch_ids(&self) -> Vec<AlternativeBranchId> {
        self.alternatives
            .alternatives
            .iter()
            .map(|leaf| leaf.branch)
            .collect()
    }

    pub(super) fn family_id(&self) -> AlternativeFamilyId {
        self.alternatives.family()
    }

    pub(super) fn for_each_mut<T>(
        &mut self,
        executor: &mut Executor<'_, '_>,
        transfer: impl FnMut(AlternativeBranchId, &mut Executor<'_, '_>, &mut Outcome) -> T,
    ) -> Result<Vec<T>, TransferJoinRefusal> {
        self.alternatives.for_each_mut(executor, transfer)
    }

    pub(super) fn replace_branch(
        &mut self,
        parent: AlternativeBranchId,
        children: Vec<TransferAlternative>,
    ) -> Result<(), TransferJoinRefusal> {
        self.alternatives.replace_branch(parent, children)
    }

    pub(super) fn try_resume_conservative_state(
        self,
        executor: &mut Executor<'_, '_>,
    ) -> Result<ConservativeTransferResume, Box<(Self, TransferJoinRefusal)>> {
        let Self { alternatives } = self;
        alternatives
            .try_resume_conservative_state(executor)
            .map_err(|refusal| {
                let (alternatives, reason) = *refusal;
                Box::new((Self { alternatives }, reason))
            })
    }

    pub(super) fn is_singleton(&self) -> bool {
        self.len() == 1
    }

    pub(super) fn has_may_normal_result(&self) -> bool {
        self.alternatives
            .alternatives
            .iter()
            .any(|leaf| leaf.outcome.ledger.may_normal().is_some())
    }

    pub(super) fn retained_values(&self) -> Values {
        let mut retained = Values::new();
        for alternative in &self.alternatives.alternatives {
            retained.extend(alternative.outcome.reached.iter().cloned());
            if let Some(continuation) = &alternative.continuation {
                retained.extend(continuation.retained_values());
            }
            let (normal, abrupt, non_normal) = alternative
                .outcome
                .ledger
                .completion
                .clone()
                .into_alternatives();
            if let Some(normal) = normal {
                retained.extend(normal.retained_values());
            }
            for path in abrupt {
                match path {
                    AbruptAlternative::Exact(thrown) => {
                        retained.extend(thrown.value.retained_values());
                    }
                    AbruptAlternative::Unknown(Some(values)) if !values.0.is_empty() => {
                        retained.extend(values.0)
                    }
                    AbruptAlternative::Unknown(_) => {
                        retained.insert(AbstractValue::Value(Value::Top));
                    }
                }
            }
            if non_normal.may_throw_without_payload || non_normal.unresolved {
                retained.insert(AbstractValue::Value(Value::Top));
            }
        }
        retained
    }

    /// Move a family result from a child request to its caller's point. The
    /// run allocator and parent lineage stay fixed; only the request cursor
    /// changes.
    pub(super) fn reanchor(&mut self, executor: &Executor<'_, '_>) {
        assert!(
            executor
                .queries
                .owns_alternative_family(&self.alternatives.family)
        );
        assert_eq!(
            executor.queries.branch_lineage(),
            self.alternatives.parent_lineage,
            "a runtime family may only be reanchored at its original parent lineage"
        );
        let program_point = executor.queries.program_point().clone();
        self.alternatives.program_point = program_point.clone();
        for alternative in &mut self.alternatives.alternatives {
            alternative.program_point = program_point.clone();
        }
    }

    /// Apply a nested family-producing operation once per current leaf, then
    /// splice its descendant leaves into this family without flattening their
    /// state or changing their branch ancestry.
    pub(in crate::facts::sync_effects::execution_index) fn with_continuation(
        mut self,
        continuation: LocalContinuation,
    ) -> Self {
        for leaf in &mut self.alternatives.alternatives {
            assert!(
                leaf.continuation.is_none(),
                "a returned callee has consumed its own caller stack"
            );
            leaf.continuation = Some(continuation.clone());
        }
        self
    }

    pub(super) fn bind(
        self,
        executor: &mut Executor<'_, '_>,
        mut transfer: impl FnMut(&mut Executor<'_, '_>, Outcome) -> Self,
    ) -> Self {
        self.bind_owned(executor, |_, active, outcome, continuation| {
            assert!(
                continuation.is_none(),
                "a suspended caller must be consumed by bind_continuations"
            );
            transfer(active, outcome)
        })
    }

    pub(super) fn bind_with_branch(
        self,
        executor: &mut Executor<'_, '_>,
        mut transfer: impl FnMut(AlternativeBranchId, &mut Executor<'_, '_>, Outcome) -> Self,
    ) -> Self {
        self.bind_owned(executor, |branch, active, outcome, continuation| {
            assert!(
                continuation.is_none(),
                "a suspended caller must be consumed by bind_continuations"
            );
            transfer(branch, active, outcome)
        })
    }

    pub(super) fn bind_continuations(
        self,
        executor: &mut Executor<'_, '_>,
        mut transfer: impl FnMut(&mut Executor<'_, '_>, Outcome, LocalContinuation) -> Self,
    ) -> Self {
        self.bind_owned(executor, |_, active, outcome, continuation| {
            transfer(
                active,
                outcome,
                continuation.expect("each callee leaf owns its caller continuation"),
            )
        })
    }

    fn bind_owned(
        mut self,
        executor: &mut Executor<'_, '_>,
        mut transfer: impl FnMut(
            AlternativeBranchId,
            &mut Executor<'_, '_>,
            Outcome,
            Option<LocalContinuation>,
        ) -> RuntimeTransferFamily,
    ) -> RuntimeTransferFamily {
        let mut continuations = self
            .alternatives
            .alternatives
            .iter_mut()
            .map(|leaf| (leaf.branch, leaf.continuation.take()))
            .collect::<BTreeMap<_, _>>();
        let family = self.alternatives.family();
        let parent_program_point = self.alternatives.program_point.clone();
        let results = self
            .alternatives
            .for_each_mut(executor, |parent_branch, active, outcome| {
                let input_lineage = active.queries.branch_lineage();
                let input = std::mem::take(outcome);
                let continuation = continuations
                    .remove(&parent_branch)
                    .expect("each leaf has one continuation slot");
                let mut child = transfer(parent_branch, active, input, continuation);
                child.reanchor(active);
                assert!(active.queries.owns_alternative_family(&family));
                assert_eq!(child.alternatives.parent_lineage, input_lineage);
                let mut leaves = child.alternatives.alternatives;
                for leaf in &mut leaves {
                    assert_eq!(leaf.family, child.alternatives.family);
                    assert_eq!(leaf.program_point, parent_program_point);
                    leaf.family = family.clone();
                    leaf.program_point = parent_program_point.clone();
                }
                (parent_branch, input_lineage, leaves)
            })
            .expect("runtime family binding preserves its active parent");
        for (parent_branch, input_lineage, children) in results {
            self.alternatives
                .replace_branch_with_descendants(parent_branch, &input_lineage, children)
                .expect("runtime family binding returns descendant leaves");
        }
        RuntimeTransferFamily {
            alternatives: self.alternatives,
        }
    }

    pub(super) fn map(
        mut self,
        executor: &mut Executor<'_, '_>,
        transfer: impl FnMut(&mut Executor<'_, '_>, Outcome) -> Outcome,
    ) -> Self {
        self.map_in_place(executor, transfer);
        self
    }

    pub(super) fn map_in_place(
        &mut self,
        executor: &mut Executor<'_, '_>,
        mut transfer: impl FnMut(&mut Executor<'_, '_>, Outcome) -> Outcome,
    ) {
        self.alternatives
            .for_each_mut(executor, |_, active, outcome| {
                let input = std::mem::take(outcome);
                *outcome = transfer(active, input);
            })
            .expect("runtime family mapping preserves its active branch set");
    }

    pub(super) fn into_scalar_refusal(mut self, executor: &mut Executor<'_, '_>) -> Outcome {
        let retained = self.retained_values();
        let suspended = self
            .alternatives
            .alternatives
            .iter()
            .any(|leaf| leaf.continuation.is_some());
        for leaf in &mut self.alternatives.alternatives {
            if let Some(continuation) = leaf.continuation.take() {
                leaf.outcome.reached.extend(continuation.retained_values());
            }
        }
        if self.is_singleton() {
            let mut outcome = self.install_singleton(executor);
            outcome.reached.extend(retained);
            if suspended {
                outcome.complete = false;
                outcome.mark_uncertain_completion();
            }
            return outcome;
        }
        self.reanchor(executor);
        let mut resumed = self
            .alternatives
            .try_resume_conservative_state(executor)
            .unwrap_or_else(|refusal| {
                let (_, reason) = *refusal;
                panic!("runtime family cannot be scalarized safely: {reason:?}")
            });
        resumed.outcome.reached.extend(retained);
        resumed.outcome.complete = false;
        resumed.outcome.mark_uncertain_completion();
        resumed.outcome
    }

    /// Project a family only after its source owner has consumed all
    /// continuation work. A state-control refusal cannot replay that work, so
    /// it returns retained values with incomplete completion.
    pub(super) fn into_completed_outcome(mut self, executor: &mut Executor<'_, '_>) -> Outcome {
        let retained = self.retained_values();
        assert!(
            self.alternatives
                .alternatives
                .iter()
                .all(|leaf| leaf.continuation.is_none()),
            "completed projection requires consumed continuations"
        );
        if self.is_singleton() {
            let mut outcome = self.install_singleton(executor);
            outcome.reached.extend(retained);
            return outcome;
        }
        self.reanchor(executor);
        let mut resumed = self
            .alternatives
            .try_resume_conservative_state(executor)
            .unwrap_or_else(|refusal| {
                let (_, reason) = *refusal;
                panic!("completed runtime family has incompatible state: {reason:?}")
            });
        resumed.outcome.reached.extend(retained);
        if resumed.disposition == ConservativeWorldJoinDisposition::RequiresRemainderFallback {
            resumed.outcome.complete = false;
            resumed.outcome.mark_uncertain_completion();
        }
        resumed.outcome
    }

    fn install_singleton(mut self, executor: &mut Executor<'_, '_>) -> Outcome {
        self.reanchor(executor);
        self.alternatives
            .check_executor(executor)
            .expect("singleton runtime family is reanchored to its caller");
        let parent_lineage = self.alternatives.parent_lineage;
        let alternative = self
            .alternatives
            .alternatives
            .pop()
            .expect("singleton runtime family contains one leaf");
        let TransferAlternative {
            mut state,
            outcome,
            allocation_judgment,
            source_covered,
            evaluation_effects,
            ..
        } = alternative;
        Rc::make_mut(&mut state).branch_lineage = parent_lineage;
        executor.state.0 = Some(state);
        executor.allocation_judgment = allocation_judgment;
        executor.source_covered = source_covered;
        executor.evaluation_effects = evaluation_effects;
        outcome
    }
}

impl Executor<'_, '_> {
    pub(super) fn fork_transfer_alternative(
        &self,
        family: AlternativeFamilyId,
        outcome: Outcome,
    ) -> Result<TransferAlternative, TransferJoinRefusal> {
        if !self.queries.owns_alternative_family(&family) {
            return Err(TransferJoinRefusal::DifferentFamily);
        }
        let program_point = self.queries.program_point().clone();
        let parent_lineage = self.queries.branch_lineage();
        let Some(state) = self.state.0.as_ref() else {
            return Err(TransferJoinRefusal::NoActiveExecutionState);
        };
        if state.branch_lineage != parent_lineage {
            return Err(TransferJoinRefusal::DifferentParentLineage);
        }
        let (branch, state) = self.queries.fork_alternative(state);
        Ok(TransferAlternative {
            family,
            parent_lineage,
            program_point,
            branch,
            state,
            outcome,
            continuation: None,
            allocation_judgment: self.allocation_judgment.clone(),
            source_covered: self.source_covered,
            evaluation_effects: self.evaluation_effects.clone(),
        })
    }

    fn run_in_transfer_alternative<T>(
        &mut self,
        alternative: &mut TransferAlternative,
        branch: AlternativeBranchId,
        transfer: &mut impl FnMut(AlternativeBranchId, &mut Self, &mut Outcome) -> T,
    ) -> T {
        assert_eq!(branch, alternative.branch);
        assert!(alternative.program_point == *self.queries.program_point());
        assert!(
            alternative
                .state
                .branch_lineage
                .starts_with(&alternative.parent_lineage)
        );
        assert_eq!(
            alternative.state.branch_lineage.last(),
            Some(&alternative.branch)
        );
        let prior_state = self.state.0.replace(alternative.state.clone());
        let prior_allocation = std::mem::replace(
            &mut self.allocation_judgment,
            alternative.allocation_judgment.clone(),
        );
        let prior_source_covered =
            std::mem::replace(&mut self.source_covered, alternative.source_covered);
        let prior_evaluation_effects = std::mem::replace(
            &mut self.evaluation_effects,
            alternative.evaluation_effects.clone(),
        );
        let lineage = alternative.state.branch_lineage.clone();
        let lineage_scope = self.queries.enter_branch_lineage(&lineage);
        let result = transfer(branch, self, &mut alternative.outcome);
        alternative.state = self
            .state
            .0
            .take()
            .expect("an alternative transfer returns ownership of its state");
        alternative.allocation_judgment = self.allocation_judgment.clone();
        alternative.source_covered = self.source_covered;
        alternative.evaluation_effects = self.evaluation_effects.clone();
        drop(lineage_scope);
        self.state.0 = prior_state;
        self.allocation_judgment = prior_allocation;
        self.source_covered = prior_source_covered;
        self.evaluation_effects = prior_evaluation_effects;
        result
    }
}

pub(super) fn join_outcomes(outcomes: &[&Outcome], evaluations: &[&EvaluationEffects]) -> Outcome {
    assert!(!outcomes.is_empty());
    let effects = join_synchronous_calls(outcomes);
    let own_property_store_receipts = local_transfer::join_own_property_store_receipts(
        &outcomes
            .iter()
            .map(|outcome| outcome.own_property_store_receipts.as_slice())
            .collect::<Vec<_>>(),
    );
    let effect_fragments = outcomes
        .iter()
        .map(|outcome| &outcome.ledger.effects)
        .collect::<Vec<_>>();
    let joined_effects = EffectCoverageFragment::join_alternatives(&effect_fragments)
        .expect("a nonempty outcome set has an effect-fragment join");

    let mut normal_values = Values::new();
    let mut normal_outcomes = Vec::new();
    let mut abrupt_alternatives = Vec::new();
    let mut non_normal = NonNormalPossibilities::default();
    let mut reached = Values::new();
    let mut dependencies = ProofDependencies::default();
    let mut executed_owners = BTreeSet::new();
    // `body_invoked` and `complete` are definite evidence. A branch-only
    // invocation is represented by the call-observation may obligation, not
    // by upgrading this bit at the join.
    let mut body_invoked = true;
    let mut complete = true;
    let mut unknown_call = false;
    let mut root_effect_identity = outcomes[0].root_effect_identity.clone();
    let mut allocations = Vec::with_capacity(outcomes.len());
    for outcome in outcomes {
        let (normal, abrupt, branch_non_normal) =
            outcome.ledger.completion.clone().into_alternatives();
        if let Some(normal) = normal {
            normal_values.extend(normal.values.iter().cloned());
            normal_outcomes.push(*outcome);
        }
        for alternative in abrupt {
            match &alternative {
                AbruptAlternative::Exact(thrown) => {
                    reached.extend(thrown.value.values.iter().cloned());
                }
                AbruptAlternative::Unknown(Some(values)) => {
                    if values.0.is_empty() {
                        reached.insert(AbstractValue::Value(Value::Top));
                    } else {
                        reached.extend(values.0.iter().cloned());
                    }
                }
                AbruptAlternative::Unknown(None) => {
                    reached.insert(AbstractValue::Value(Value::Top));
                }
            }
            abrupt_alternatives.push(alternative);
        }
        non_normal.may_throw_without_payload |= branch_non_normal.may_throw_without_payload;
        non_normal.may_diverge |= branch_non_normal.may_diverge;
        non_normal.unresolved |= branch_non_normal.unresolved;
        reached.extend(outcome.reached.iter().cloned());
        dependencies.extend(outcome.ledger.dependencies.as_ref());
        if let Some(closed) = outcome.ledger.effects.closed_domain_dependencies() {
            dependencies.extend(&closed);
        }
        executed_owners.extend(outcome.executed_owners.iter().copied());
        body_invoked &= outcome.body_invoked;
        complete &= outcome.complete;
        unknown_call |= outcome.unknown_call;
        if root_effect_identity != outcome.root_effect_identity {
            root_effect_identity = None;
        }
        allocations.push(&outcome.ledger.allocation);
    }
    for evaluation in evaluations {
        for evidence in evaluation.global_accesses.values() {
            dependencies.extend(&evidence.dependencies);
        }
    }

    let normal = if normal_outcomes.is_empty() {
        None
    } else {
        if normal_values.is_empty() {
            normal_values = unknown();
        }
        Some(join_selected_values(&normal_outcomes, normal_values))
    };
    if non_normal.may_throw_without_payload {
        reached.insert(AbstractValue::Value(Value::Top));
    }
    let completion = Completion::from_alternatives(normal, abrupt_alternatives, non_normal);
    let allocation = join_allocation_judgments(&allocations);
    let ledger = TransferLedger::new(
        completion,
        joined_effects,
        Box::new(dependencies),
        allocation,
    );
    Outcome {
        effects: Box::new(effects),
        body_invoked,
        ledger,
        root_effect_identity,
        reached,
        own_property_store_receipts,
        complete,
        executed_owners,
        unknown_call,
    }
}

fn join_selected_values(outcomes: &[&Outcome], values: Values) -> SelectedValue {
    let mut selection = None;
    let selections = outcomes
        .iter()
        .map(|outcome| {
            outcome
                .ledger
                .may_normal()
                .and_then(|value| value.selection.as_ref())
        })
        .collect::<Vec<_>>();
    if selections.iter().all(Option::is_some) {
        let first = selections[0].unwrap();
        if selections
            .iter()
            .all(|candidate| candidate.unwrap().target == first.target)
        {
            let mut dependencies = first.dependencies.as_ref().clone();
            for candidate in selections.iter().skip(1).flatten() {
                dependencies.extend(candidate.dependencies.as_ref());
            }
            selection = Some(CallableSelection {
                target: first.target.clone(),
                dependencies: Box::new(dependencies),
            });
        }
    }
    SelectedValue {
        values,
        selection,
        // Native postconditions are exact to one transfer path. Until they
        // have an explicit join, dropping them is conservative.
        postcondition: None,
    }
}

fn join_allocation_judgments(judgments: &[&AllocationJudgment]) -> AllocationJudgment {
    let mut dependencies = ProofDependencies::default();
    for judgment in judgments {
        let AllocationJudgment::CoveredAllocationOnly(covered) = judgment else {
            return AllocationJudgment::Opaque;
        };
        dependencies.extend(covered.as_ref());
    }
    AllocationJudgment::CoveredAllocationOnly(Box::new(dependencies))
}

fn join_synchronous_calls(outcomes: &[&Outcome]) -> SynchronousCalls {
    let calls = outcomes
        .iter()
        .map(|outcome| outcome.effects.as_ref())
        .collect::<Vec<_>>();
    let first = calls[0];
    // Author/whole-owner authority is not itself joinable. The per-domain
    // effect fragment above retains only domains covered on every path.
    let mut joined = SynchronousCalls {
        complete: calls.iter().all(|calls| calls.complete),
        completed_initialization: common_option(&calls, |calls| &calls.completed_initialization),
        root_normal_completion: common_option(&calls, |calls| &calls.root_normal_completion),
        root_outward_throw: common_option(&calls, |calls| &calls.root_outward_throw),
        initialization_coverage_refusal: calls
            .iter()
            .all(|calls| {
                calls.initialization_coverage_refusal == first.initialization_coverage_refusal
            })
            .then_some(first.initialization_coverage_refusal)
            .flatten(),
        initializer_binding_effects: calls
            .iter()
            .all(|calls| calls.initializer_binding_effects == first.initializer_binding_effects)
            .then(|| first.initializer_binding_effects.clone())
            .flatten(),
        purity_disagreement: calls
            .iter()
            .all(|calls| calls.purity_disagreement == first.purity_disagreement)
            .then(|| first.purity_disagreement.clone())
            .flatten(),
        local_record_factory_purity: common_option(&calls, |calls| {
            &calls.local_record_factory_purity
        }),
        binding_effect_coverage: if calls
            .iter()
            .all(|calls| calls.binding_effect_coverage == first.binding_effect_coverage)
        {
            first.binding_effect_coverage.clone()
        } else {
            BindingEffectCoverage::Unclassified
        },
        ..SynchronousCalls::default()
    };

    let mut aliases_known = true;
    let mut aliases = BTreeSet::new();
    let mut settled = first.settled_array_elements.clone();
    for calls in &calls {
        joined.evaluation_effects.extend(&calls.evaluation_effects);
        joined.reads.extend(calls.reads.iter().cloned());
        joined.rebinds.extend(calls.rebinds.iter().cloned());
        joined
            .uninitialized_reads
            .extend(calls.uninitialized_reads.iter().cloned());
        joined.fallback_only.reads.extend(
            calls
                .fallback_only
                .reads
                .iter()
                .map(|(binding, reason)| (binding.clone(), reason.clone())),
        );
        joined.fallback_only.rebinds.extend(
            calls
                .fallback_only
                .rebinds
                .iter()
                .map(|(binding, reason)| (binding.clone(), reason.clone())),
        );
        joined.fallback_only.uninitialized_reads.extend(
            calls
                .fallback_only
                .uninitialized_reads
                .iter()
                .map(|(binding, reason)| (binding.clone(), reason.clone())),
        );
        joined.heap_contents.extend(&calls.heap_contents);
        joined.heap_access_provenance.absorb(
            &calls.heap_access_provenance,
            crate::facts::HeapAccessOrigin::ExecutedOperation,
        );
        joined
            .required_initialized_module_cells
            .extend(calls.required_initialized_module_cells.iter().copied());
        for requirement in &calls.conditional_provider_targets {
            if !joined.conditional_provider_targets.contains(requirement) {
                joined
                    .conditional_provider_targets
                    .push(requirement.clone());
            }
        }
        for requirement in &calls.required_imported_primitive_reads {
            joined.require_imported_primitive_read(requirement.clone());
        }
        for receipt in calls.operation_support_receipts() {
            joined.record_operation_support(receipt.clone());
        }
        for loss in &calls.precision_losses {
            if !joined.precision_losses.contains(loss) {
                joined.precision_losses.push(loss.clone());
            }
        }
        union_vec(
            &mut joined.local_record_factory_uses,
            &calls.local_record_factory_uses,
        );
        union_vec(
            &mut joined.local_record_factory_rejections,
            &calls.local_record_factory_rejections,
        );
        union_vec(
            &mut joined.native_integrity_checks,
            &calls.native_integrity_checks,
        );
        union_vec(
            &mut joined.native_prototype_property_checks,
            &calls.native_prototype_property_checks,
        );
        union_vec(
            &mut joined.native_prefix_observations,
            &calls.native_prefix_observations,
        );
        union_vec(
            &mut joined.native_integrity_uses,
            &calls.native_integrity_uses,
        );
        union_vec(
            &mut joined.native_operation_input_uses,
            &calls.native_operation_input_uses,
        );
        union_vec(
            &mut joined.imported_primitive_array_spread_input_uses,
            &calls.imported_primitive_array_spread_input_uses,
        );
        union_vec(&mut joined.inference_uses, &calls.inference_uses);
        union_vec(
            &mut joined.callback_factory_uses,
            &calls.callback_factory_uses,
        );
        union_vec(
            &mut joined.returned_record_uses,
            &calls.returned_record_uses,
        );
        union_vec(
            &mut joined.author_host_contract_uses,
            &calls.author_host_contract_uses,
        );
        match &calls.global_object_aliases {
            Some(current) => aliases.extend(current.iter().cloned()),
            None => aliases_known = false,
        }
        settled.retain(|binding, elements| {
            calls.settled_array_elements.get(binding) == Some(elements)
        });
    }
    if aliases_known {
        joined.global_object_aliases = Some(aliases);
    }
    joined.settled_array_elements = settled;
    // If a binding was read/rebound on any concrete path, it is observed even
    // if a sibling also contributed it through a conservative path.
    joined
        .fallback_only
        .reads
        .retain(|binding, _| !joined.reads.iter().any(|(read, _)| read == binding));
    joined
        .fallback_only
        .rebinds
        .retain(|binding, _| !joined.rebinds.iter().any(|(write, _)| write == binding));
    joined
        .fallback_only
        .uninitialized_reads
        .retain(|binding, _| !joined.uninitialized_reads.contains(binding));
    joined
}

fn union_vec<T: Clone + PartialEq>(target: &mut Vec<T>, source: &[T]) {
    for item in source {
        if !target.contains(item) {
            target.push(item.clone());
        }
    }
}

fn common_option<T: Clone + PartialEq>(
    calls: &[&SynchronousCalls],
    field: impl for<'a> Fn(&'a SynchronousCalls) -> &'a Option<T>,
) -> Option<T> {
    let first = calls.first().map(|calls| field(calls))?;
    calls
        .iter()
        .all(|calls| field(calls) == first)
        .then(|| first.clone())
        .flatten()
}

fn join_evaluation_effects(alternatives: &[TransferAlternative]) -> EvaluationEffects {
    let mut joined = EvaluationEffects::default();
    for alternative in alternatives {
        joined.extend(&alternative.evaluation_effects);
    }
    joined
}

fn join_execution_states(
    alternatives: &[TransferAlternative],
    parent_lineage: &[AlternativeBranchId],
) -> Result<ExecutionState, TransferJoinRefusal> {
    let Some(first) = alternatives.first() else {
        return Err(TransferJoinRefusal::EmptyAlternativeSet);
    };
    let baseline = first.state.as_ref();
    let mut leaf_lineages = Vec::with_capacity(alternatives.len());
    let mut leaf_ids = BTreeSet::new();
    for alternative in alternatives {
        let state = alternative.state.as_ref();
        if alternative.family != first.family
            || alternative.program_point != first.program_point
            || alternative.parent_lineage.len() < parent_lineage.len()
            || !alternative.parent_lineage.starts_with(parent_lineage)
            || state.branch_lineage.len() <= parent_lineage.len()
            || !state.branch_lineage.starts_with(parent_lineage)
            || state.branch_lineage.len() != alternative.parent_lineage.len() + 1
            || !state
                .branch_lineage
                .starts_with(&alternative.parent_lineage)
            || state.branch_lineage.last() != Some(&alternative.branch)
            || !leaf_ids.insert(alternative.branch)
            || !baseline.world.same_contents(&state.world)
            || !Arc::ptr_eq(&baseline.invocation_budget, &state.invocation_budget)
            || !baseline
                .capture_identities
                .shares_arena(&state.capture_identities)
            || !baseline
                .capture_contexts
                .shares_arena(&state.capture_contexts)
            || !baseline.callback_sets.shares_arena(&state.callback_sets)
            || !baseline
                .provider_private_graphs
                .shares_arena(&state.provider_private_graphs)
            || !baseline
                .provider_closures
                .shares_arena(&state.provider_closures)
            || baseline.active_cell_scopes != state.active_cell_scopes
            || baseline.invocation_depth != state.invocation_depth
            || baseline.array_iteration_depth != state.array_iteration_depth
            || baseline.current_top_owner != state.current_top_owner
            || baseline.prior_reachability_roots != state.prior_reachability_roots
            || baseline.active_class_member_promotions != state.active_class_member_promotions
            || baseline.checking_record_factory_contract != state.checking_record_factory_contract
            || baseline.fallback_callback_sizes != state.fallback_callback_sizes
            || baseline.native_initializer_candidate != state.native_initializer_candidate
            || baseline.native_initializer_transfer != state.native_initializer_transfer
            || *baseline.record_factory_prototype_premises.borrow()
                != *state.record_factory_prototype_premises.borrow()
            || baseline.active != state.active
            || baseline.assumption.is_some()
            || state.assumption.is_some()
        {
            return Err(TransferJoinRefusal::IncompatibleExecutionState);
        }
        leaf_lineages.push(state.branch_lineage.as_slice());
    }
    for (index, lineage) in leaf_lineages.iter().enumerate() {
        if leaf_lineages
            .iter()
            .enumerate()
            .any(|(other_index, other)| {
                index != other_index && (lineage.starts_with(other) || other.starts_with(lineage))
            })
        {
            // A replaced parent and one of its descendants are not two
            // feasible leaves; accepting both would double-count that path.
            return Err(TransferJoinRefusal::DuplicateBranchIdentity);
        }
    }

    let mut joined = baseline.clone();
    let retained_references = join_native_retained_reference_maps(alternatives);
    let retained_references_may_be_unknown = alternatives.iter().any(|alternative| {
        alternative
            .state
            .world
            .get()
            .native_retained_references_may_be_unknown
    });
    if retained_references != baseline.world.get().prefix_native_retained_references
        || retained_references_may_be_unknown
            != baseline
                .world
                .get()
                .native_retained_references_may_be_unknown
    {
        let world = joined.world.make_mut();
        world.native_retained_references_may_be_unknown = retained_references_may_be_unknown;
        world.prefix_native_retained_references = retained_references;
        if retained_references_may_be_unknown {
            let allocations = world
                .prefix_native_retained_references
                .keys()
                .copied()
                .collect::<Vec<_>>();
            for allocation in allocations {
                if let Some(references) =
                    world.prefix_native_retained_references.get_mut(&allocation)
                {
                    references.may_contain_unknown = true;
                }
            }
        }
    }
    joined.branch_lineage = parent_lineage.to_vec();
    joined.invocation_memos = Default::default();
    joined.ledger_dependencies = ProofDependencies::default();
    joined.native_integrity_checks = Default::default();
    joined.native_prototype_property_checks = Default::default();
    joined.native_prefix_observations = Default::default();
    joined.native_point_effects = super::NativePointEffects::join_alternatives(
        &alternatives
            .iter()
            .map(|alternative| &alternative.state.native_point_effects)
            .collect::<Vec<_>>(),
    );
    joined.activated_native_effects.clear();
    joined.author_host_contract_uses.clear();
    joined.native_integrity_uses = Default::default();
    joined.native_operation_input_uses = Default::default();
    joined.imported_primitive_array_spread_input_uses = Default::default();
    joined.proof_dependencies = Default::default();
    joined.inference_uses = Default::default();
    joined.class_member_effects.clear();
    joined.proof_uses.clear();
    joined.returned_record_proof_uses.clear();
    joined.prior_effect_owners.clear();
    joined.statement_executed_owners.clear();
    joined.statement_invoked_callables.clear();
    joined.array_iteration_protected_objects.clear();
    joined.fallback_local_owners.clear();
    joined.fallback_local_deferred.clear();
    joined.callback_fallback = Default::default();
    joined.fallback_outcome = join_outcomes(
        &alternatives
            .iter()
            .map(|alternative| &alternative.state.fallback_outcome)
            .collect::<Vec<_>>(),
        &[],
    );
    joined.selected_array_call_results = common_selected_array_results(alternatives);
    joined.array_iteration_unsupported = alternatives
        .iter()
        .any(|alternative| alternative.state.array_iteration_unsupported);

    let mut prototype_checks =
        BTreeMap::<String, crate::reports::schema::NativePrototypePropertyCheck>::new();
    let mut operation_inputs =
        BTreeMap::<(u32, u32), crate::reports::schema::NativeOperationInputUse>::new();
    for alternative in alternatives {
        let state = alternative.state.as_ref();
        joined
            .ledger_dependencies
            .extend(&state.ledger_dependencies);
        joined
            .native_integrity_checks
            .borrow_mut()
            .extend(state.native_integrity_checks.borrow().iter().cloned());
        for (property, check) in state.native_prototype_property_checks.borrow().iter() {
            prototype_checks
                .entry(property.clone())
                .and_modify(|current| {
                    current.intact &= check.intact;
                    current.blocking_mutation_count = current
                        .blocking_mutation_count
                        .max(check.blocking_mutation_count);
                })
                .or_insert_with(|| check.clone());
        }
        joined
            .native_prefix_observations
            .borrow_mut()
            .extend(state.native_prefix_observations.borrow().iter().cloned());
        joined
            .activated_native_effects
            .extend(state.activated_native_effects.iter().copied());
        union_vec(
            &mut joined.author_host_contract_uses,
            &state.author_host_contract_uses,
        );
        joined
            .native_integrity_uses
            .borrow_mut()
            .extend(state.native_integrity_uses.borrow().iter().cloned());
        for (key, use_fact) in state.native_operation_input_uses.borrow().iter() {
            if operation_inputs
                .get(key)
                .is_some_and(|existing| existing != use_fact)
            {
                return Err(TransferJoinRefusal::IncompatibleExecutionState);
            }
            operation_inputs
                .entry(*key)
                .or_insert_with(|| use_fact.clone());
        }
        joined
            .imported_primitive_array_spread_input_uses
            .borrow_mut()
            .extend(
                state
                    .imported_primitive_array_spread_input_uses
                    .borrow()
                    .iter()
                    .cloned(),
            );
        joined
            .proof_dependencies
            .borrow_mut()
            .extend(&state.proof_dependencies.borrow());
        joined
            .inference_uses
            .borrow_mut()
            .extend(state.inference_uses.borrow().iter().cloned());
        for (owner, (reads, writes)) in &state.class_member_effects {
            let current = joined.class_member_effects.entry(*owner).or_default();
            current.0.extend(reads.iter().cloned());
            current.1.extend(writes.iter().cloned());
        }
        union_vec(&mut joined.proof_uses, &state.proof_uses);
        union_vec(
            &mut joined.returned_record_proof_uses,
            &state.returned_record_proof_uses,
        );
        joined
            .prior_effect_owners
            .extend(state.prior_effect_owners.iter().copied());
        joined
            .statement_executed_owners
            .extend(state.statement_executed_owners.iter().copied());
        joined
            .statement_invoked_callables
            .extend(state.statement_invoked_callables.iter().copied());
        joined
            .array_iteration_protected_objects
            .extend(state.array_iteration_protected_objects.iter().copied());
        joined
            .fallback_local_deferred
            .extend(state.fallback_local_deferred.clone());
        join_callback_fallback(&mut joined.callback_fallback, &state.callback_fallback);
    }
    if let Some(first) = alternatives.first() {
        joined.fallback_local_owners = first
            .state
            .fallback_local_owners
            .iter()
            .filter(|owner| {
                alternatives
                    .iter()
                    .all(|alternative| alternative.state.fallback_local_owners.contains(owner))
            })
            .copied()
            .collect();
        joined.callback_fallback.expanded = first
            .state
            .callback_fallback
            .expanded
            .iter()
            .filter(|owner| {
                alternatives
                    .iter()
                    .all(|alternative| alternative.state.callback_fallback.expanded.contains(owner))
            })
            .copied()
            .collect();
        joined.callback_fallback.incomplete_expanded = alternatives
            .iter()
            .all(|alternative| alternative.state.callback_fallback.incomplete_expanded);
    }
    *joined.native_prototype_property_checks.borrow_mut() = prototype_checks;
    *joined.native_operation_input_uses.borrow_mut() = operation_inputs;
    Ok(joined)
}

fn common_selected_array_results(
    alternatives: &[TransferAlternative],
) -> BTreeMap<CallSite, (SourceUnitOrdinal, Values)> {
    let Some(first) = alternatives.first() else {
        return BTreeMap::new();
    };
    let mut common = first.state.selected_array_call_results.clone();
    for alternative in &alternatives[1..] {
        common.retain(|site, result| {
            alternative.state.selected_array_call_results.get(site) == Some(result)
        });
    }
    common
}

fn join_native_retained_reference_maps(
    alternatives: &[TransferAlternative],
) -> imbl::OrdMap<HeapAddress, NativeRetainedReferences> {
    let mut joined = alternatives[0]
        .state
        .world
        .get()
        .prefix_native_retained_references
        .clone();
    for alternative in &alternatives[1..] {
        for (allocation, references) in &alternative
            .state
            .world
            .get()
            .prefix_native_retained_references
        {
            if let Some(existing) = joined.get_mut(allocation) {
                existing.weak_union(references);
            } else {
                joined.insert(*allocation, references.clone());
            }
        }
    }
    joined
}

fn join_callback_fallback(
    joined: &mut super::CallbackFallbackSummary,
    branch: &super::CallbackFallbackSummary,
) {
    joined.deferred.extend(branch.deferred.clone());
    joined.reads.extend(branch.reads.iter().cloned());
    joined.rebinds.extend(branch.rebinds.iter().cloned());
    union_vec(&mut joined.read_additions, &branch.read_additions);
    union_vec(&mut joined.rebind_additions, &branch.rebind_additions);
}

#[cfg(test)]
mod imported_read_requirement_tests {
    use super::super::Outcome;
    use super::join_synchronous_calls;
    use crate::facts::statement_facts::{RequiredImportedPrimitiveRead, SynchronousCalls};
    use crate::facts::sync_effects::effect_coverage::{
        LocalBodyIdentity, OperationStep, SourceOperationSite,
    };
    use source_identity::SourceUnitOrdinal;
    use std::collections::BTreeSet;
    use swc_common::{BytePos, Span, SyntaxContext};

    fn requirement(owner: u32, root: u32, ordinal: u32) -> RequiredImportedPrimitiveRead {
        let owner = SourceUnitOrdinal(owner as usize);
        RequiredImportedPrimitiveRead {
            source_owner: owner,
            source_site: Some(SourceOperationSite {
                owner,
                body: LocalBodyIdentity::Callable {
                    key: ordinal,
                    owner,
                },
                span: Span::new(BytePos(ordinal), BytePos(ordinal + 1)),
                ordinal,
                step: OperationStep::Expression,
                control_test_ordinal: None,
            }),
            consumer_root: Some(SourceUnitOrdinal(root as usize)),
            source_binding: ("answer".into(), SyntaxContext::empty()),
            source_specifier: "./peer.js".to_owned(),
            imported_name: "answer".to_owned(),
            provider_module: "static/peer".to_owned(),
            provider_export: "answer".to_owned(),
            dependency_closure: BTreeSet::from(["static/peer".to_owned()]),
            provider_closure: Some(crate::analysis_inputs::ResolvedProviderDependencyClosure {
                modules: BTreeSet::from(["static/peer".to_owned()]),
                admitted_host_modules: BTreeSet::new(),
            }),
            source_cells: BTreeSet::from([crate::analysis_inputs::PrimitiveExportCell {
                module: "static/peer".to_owned(),
                export: "answer".to_owned(),
            }]),
        }
    }

    #[test]
    fn imported_read_obligations_union_across_nested_calls_and_feasible_alternatives() {
        let first = requirement(4, 1, 10);
        let second = requirement(5, 1, 20);

        let mut nested = SynchronousCalls::default();
        nested.require_imported_primitive_read(first.clone());
        let mut outer = Outcome::default();
        outer.absorb_call_effects(
            &mut nested,
            None,
            crate::facts::HeapAccessOrigin::ExecutedOperation,
        );
        assert_eq!(
            outer.effects.required_imported_primitive_reads,
            vec![first.clone()]
        );

        let mut first_path = Outcome::default();
        first_path
            .effects
            .require_imported_primitive_read(first.clone());
        let mut second_path = Outcome::default();
        second_path
            .effects
            .require_imported_primitive_read(second.clone());
        let joined = join_synchronous_calls(&[&first_path, &second_path]);
        assert_eq!(
            joined.required_imported_primitive_reads,
            vec![first, second],
            "a read on either feasible path remains a may-execution obligation"
        );
    }
}
