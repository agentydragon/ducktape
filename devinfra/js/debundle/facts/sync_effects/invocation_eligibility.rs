//! Scratch experiment: exact guards and would-hit counts. Every transfer still
//! executes normally. No result is read back into analysis or replayed.
use super::super::allocation_transfer::AllocationJudgment;
use super::*;

#[derive(Clone, Default, PartialEq)]
struct StableContext {
    active_cells: OrdMap<Id, LexicalCellId>,
    native_point_effects: NativePointEffects,
    activated_native_effects: BTreeSet<(SourceUnitOrdinal, Option<u32>)>,
    author_host_contract_uses: Vec<crate::reports::schema::ReentryFreeOperationUse>,
    class_member_effects: BTreeMap<SourceUnitOrdinal, (BTreeSet<Id>, BTreeSet<Id>)>,
    active_class_member_promotions: BTreeSet<u32>,
    prior_reachability_roots: Option<BTreeSet<SourceUnitOrdinal>>,
    current_top_owner: Option<SourceUnitOrdinal>,
    proof_uses: Vec<CallbackFactoryUseFact>,
    returned_record_proof_uses: Vec<ReturnedRecordUseFact>,
    checking_record_factory_contract: bool,
    nested_record_factory_premise: Option<(u32, u32)>,
    prior_effect_owners: BTreeSet<SourceUnitOrdinal>,
    statement_executed_owners: BTreeSet<SourceUnitOrdinal>,
    statement_invoked_callables: BTreeSet<u32>,
    array_iteration_depth: usize,
    iteration_element_records: BTreeSet<u32>,
    array_iteration_protected_objects: BTreeSet<u32>,
    array_iteration_unsupported: bool,
    invocation_depth: usize,
    callback_fallback: CallbackFallbackSummary,
    fallback_outcome: Outcome,
    assumption: Option<AssumedEffectReason>,
    fallback_callback_sizes: Option<(usize, usize)>,
    fallback_local_owners: BTreeSet<SourceUnitOrdinal>,
    fallback_local_deferred: BTreeMap<Id, SourceUnitOrdinal>,
    active: BTreeSet<ActiveInvocation>,
    invocation_memos: OrdMap<InvocationSignature, ProgramPoint>,
}
impl StableContext {
    fn new(state: &ExecutionState) -> Self {
        let ExecutionState {
            world: _,
            active_cells,
            native_point_effects,
            activated_native_effects,
            author_host_contract_uses,
            class_member_effects,
            active_class_member_promotions,
            prior_reachability_roots,
            current_top_owner,
            proof_uses,
            returned_record_proof_uses,
            checking_record_factory_contract,
            nested_record_factory_premise,
            prior_effect_owners,
            statement_executed_owners,
            statement_invoked_callables,
            array_iteration_depth,
            iteration_element_records,
            array_iteration_protected_objects,
            array_iteration_unsupported,
            invocation_depth,
            invocation_count: _,
            callback_fallback,
            fallback_outcome,
            assumption,
            fallback_callback_sizes,
            fallback_local_owners,
            fallback_local_deferred,
            capture_contexts: _,
            callback_sets: _,
            active,
            invocation_memos,
            export_callback_memos: _,
            eligibility: _,
        } = state;
        Self {
            active_cells: active_cells.clone(),
            native_point_effects: native_point_effects.clone(),
            activated_native_effects: activated_native_effects.clone(),
            author_host_contract_uses: author_host_contract_uses.clone(),
            class_member_effects: class_member_effects.clone(),
            active_class_member_promotions: active_class_member_promotions.clone(),
            prior_reachability_roots: prior_reachability_roots.clone(),
            current_top_owner: *current_top_owner,
            proof_uses: proof_uses.clone(),
            returned_record_proof_uses: returned_record_proof_uses.clone(),
            checking_record_factory_contract: *checking_record_factory_contract,
            nested_record_factory_premise: *nested_record_factory_premise,
            prior_effect_owners: prior_effect_owners.clone(),
            statement_executed_owners: statement_executed_owners.clone(),
            statement_invoked_callables: statement_invoked_callables.clone(),
            array_iteration_depth: *array_iteration_depth,
            iteration_element_records: iteration_element_records.clone(),
            array_iteration_protected_objects: array_iteration_protected_objects.clone(),
            array_iteration_unsupported: *array_iteration_unsupported,
            invocation_depth: *invocation_depth,
            callback_fallback: callback_fallback.clone(),
            fallback_outcome: fallback_outcome.clone(),
            assumption: assumption.clone(),
            fallback_callback_sizes: *fallback_callback_sizes,
            fallback_local_owners: fallback_local_owners.clone(),
            fallback_local_deferred: fallback_local_deferred.clone(),
            active: active.clone(),
            invocation_memos: invocation_memos.clone(),
        }
    }
    fn mismatch(&self, other: &Self) -> Option<&'static str> {
        if self.active_cells != other.active_cells {
            return Some("active_cells");
        }
        if self.native_point_effects != other.native_point_effects {
            return Some("native_point_effects");
        }
        if self.activated_native_effects != other.activated_native_effects {
            return Some("activated_native_effects");
        }
        if self.author_host_contract_uses != other.author_host_contract_uses {
            return Some("author_host_contract_uses");
        }
        if self.class_member_effects != other.class_member_effects {
            return Some("class_member_effects");
        }
        if self.active_class_member_promotions != other.active_class_member_promotions {
            return Some("active_class_member_promotions");
        }
        if self.prior_reachability_roots != other.prior_reachability_roots {
            return Some("prior_reachability_roots");
        }
        if self.current_top_owner != other.current_top_owner {
            return Some("current_top_owner");
        }
        if self.proof_uses != other.proof_uses {
            return Some("proof_uses");
        }
        if self.returned_record_proof_uses != other.returned_record_proof_uses {
            return Some("returned_record_proof_uses");
        }
        if self.checking_record_factory_contract != other.checking_record_factory_contract {
            return Some("checking_record_factory_contract");
        }
        if self.nested_record_factory_premise != other.nested_record_factory_premise {
            return Some("nested_record_factory_premise");
        }
        if self.prior_effect_owners != other.prior_effect_owners {
            return Some("prior_effect_owners");
        }
        if self.statement_executed_owners != other.statement_executed_owners {
            return Some("statement_executed_owners");
        }
        if self.statement_invoked_callables != other.statement_invoked_callables {
            return Some("statement_invoked_callables");
        }
        if self.array_iteration_depth != other.array_iteration_depth {
            return Some("array_iteration_depth");
        }
        if self.iteration_element_records != other.iteration_element_records {
            return Some("iteration_element_records");
        }
        if self.array_iteration_protected_objects != other.array_iteration_protected_objects {
            return Some("array_iteration_protected_objects");
        }
        if self.array_iteration_unsupported != other.array_iteration_unsupported {
            return Some("array_iteration_unsupported");
        }
        if self.invocation_depth != other.invocation_depth {
            return Some("invocation_depth");
        }
        if self.callback_fallback != other.callback_fallback {
            return Some("callback_fallback");
        }
        if self.fallback_outcome != other.fallback_outcome {
            return Some("fallback_outcome");
        }
        if self.assumption != other.assumption {
            return Some("assumption");
        }
        if self.fallback_callback_sizes != other.fallback_callback_sizes {
            return Some("fallback_callback_sizes");
        }
        if self.fallback_local_owners != other.fallback_local_owners {
            return Some("fallback_local_owners");
        }
        if self.fallback_local_deferred != other.fallback_local_deferred {
            return Some("fallback_local_deferred");
        }
        if self.active != other.active {
            return Some("active");
        }
        if self.invocation_memos != other.invocation_memos {
            return Some("invocation_memos");
        }
        None
    }
}

#[derive(Clone, PartialEq)]
struct Entry {
    signature: InvocationSignature,
    version: u64,
    stable: StableContext,
    observations: Observations,
}
struct Pending {
    callable: u32,
    owner: Option<SourceUnitOrdinal>,
    count: usize,
    entered: usize,
    entry: Option<Entry>,
    invalidated: Option<&'static str>,
}
struct Receipt {
    entry: Entry,
    outcome: Outcome,
    effects: EvaluationEffects,
    dependencies: ProofDependencies,
    cost: usize,
    nested: usize,
}
#[derive(Default)]
struct Counts {
    attempted: usize,
    eligible: usize,
    would_hit: usize,
    nested_would_hit: usize,
    nested_invocations: usize,
    refused: BTreeMap<&'static str, usize>,
    source_end: Option<u32>,
}

pub(super) struct EligibilityProbe {
    enabled: bool,
    entered: usize,
    pending: Vec<Pending>,
    receipts: Vec<Receipt>,
    counts: BTreeMap<(Option<SourceUnitOrdinal>, u32), Counts>,
}
impl EligibilityProbe {
    pub(super) fn new() -> Self {
        Self {
            enabled: std::env::var_os("DEBUNDLE_NESTED_MEMO_ELIGIBILITY").is_some(),
            entered: 0,
            pending: Vec::new(),
            receipts: Vec::new(),
            counts: BTreeMap::new(),
        }
    }
    // This method drops all guard/receipt payloads before either weak interner
    // observes liveness. Pending ancestors keep only scalar accounting metadata.
    pub(super) fn clear_retained(&mut self, reason: &'static str) {
        self.receipts.clear();
        for pending in &mut self.pending {
            pending.entry = None;
            pending.invalidated.get_or_insert(reason);
        }
    }
    pub(super) fn world_access(&mut self) {
        self.receipts.clear();
    }
    pub(super) fn clear_owner(&mut self) {
        self.clear_retained("owner_end");
        for ((owner, callable), c) in std::mem::take(&mut self.counts) {
            eprintln!(
                "nested-memo-eligibility owner={owner:?} callable={callable} source_end={:?} attempted={} eligible={} would_hit={} nested_would_hit={} nested_invocations={} refused={:?}",
                c.source_end,
                c.attempted,
                c.eligible,
                c.would_hit,
                c.nested_would_hit,
                c.nested_invocations,
                c.refused
            );
        }
    }
}

impl Executor<'_, '_> {
    pub(in crate::facts::sync_effects) fn begin_invocation_eligibility(
        &mut self,
        operation: &RuntimeOperation,
    ) -> Option<usize> {
        if !self.state.eligibility.enabled {
            return None;
        }
        let RuntimeOperation::Invoke {
            callable,
            arguments,
            argument_selections,
            captured,
            fresh_checked_constructor,
        } = operation
        else {
            return None;
        };
        self.state.eligibility.entered = self
            .state
            .eligibility
            .entered
            .checked_add(1)
            .expect("probe invocation counter exhausted");
        if !self.state.world.native.tainted_functions.contains(callable) {
            return None;
        }
        let owner = self.state.current_top_owner;
        self.state
            .eligibility
            .counts
            .entry((owner, *callable))
            .or_default()
            .attempted += 1;
        let source_end = self
            .inputs
            .index
            .callable_spans
            .get(callable)
            .map(|span| span.hi.0);
        self.state
            .eligibility
            .counts
            .entry((owner, *callable))
            .or_default()
            .source_end = source_end;
        let mode = *fresh_checked_constructor || self.state.array_iteration_depth != 0;
        let entry = (!mode).then(|| Entry {
            signature: InvocationSignature {
                callable: *callable,
                arguments: arguments.clone(),
                argument_selections: argument_selections.clone(),
                captured_context: captured.clone(),
                fresh_checked_constructor: *fresh_checked_constructor,
                reachability_revision: self.state.world.revision.reachability,
                prefix_revision: self.state.world.revision.prefix,
                top_owner: owner,
            },
            version: self.state.world.version(),
            stable: StableContext::new(&self.state),
            observations: self.observations.clone(),
        });
        let ticket = self.state.eligibility.pending.len();
        let count = self.state.invocation_count;
        let entered = self.state.eligibility.entered;
        self.state.eligibility.pending.push(Pending {
            callable: *callable,
            owner,
            count,
            entered,
            entry,
            invalidated: mode.then_some("mode"),
        });
        Some(ticket)
    }
}

impl ExecutionState {
    // Called after into_transfer, including prerequisite settlement and ledger
    // dependency adoption. The ticket is scalar; no snapshot lives on the Rust
    // stack while a child may call an interner.
    pub(in crate::facts::sync_effects) fn finish_invocation_eligibility(
        &mut self,
        ticket: usize,
        observations: &Observations,
        value: &RuntimeValue,
        judgment: &AllocationJudgment,
        effects: &EvaluationEffects,
        dependencies: &ProofDependencies,
    ) {
        assert_eq!(ticket + 1, self.eligibility.pending.len());
        let pending = self.eligibility.pending.pop().unwrap();
        let key = (pending.owner, pending.callable);
        let refuse = |probe: &mut EligibilityProbe, reason| {
            *probe
                .counts
                .entry(key)
                .or_default()
                .refused
                .entry(reason)
                .or_default() += 1;
        };
        let Some(entry) = pending.entry else {
            refuse(&mut self.eligibility, pending.invalidated.unwrap());
            return;
        };
        if entry.version != self.world.version() {
            refuse(&mut self.eligibility, "world_access");
            return;
        }
        let stable = StableContext::new(self);
        if let Some(field) = entry.stable.mismatch(&stable) {
            refuse(&mut self.eligibility, field);
            return;
        }
        if entry.observations != *observations {
            refuse(&mut self.eligibility, "observations");
            return;
        }
        let Some(cost) = self.invocation_count.checked_sub(pending.count) else {
            refuse(&mut self.eligibility, "budget_reset");
            return;
        };
        if self.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS || cost == 0 {
            refuse(&mut self.eligibility, "budget");
            return;
        }
        let RuntimeValue::Outcome(outcome) = value else {
            unreachable!()
        };
        let refusal = if !matches!(judgment, AllocationJudgment::Opaque) {
            Some("allocation")
        } else if !outcome.complete {
            Some("incomplete")
        } else if !outcome.effect_coverage.is_exact_normal() {
            Some("completion")
        } else if !outcome.body_invoked {
            Some("body_not_invoked")
        } else if outcome.unknown_call {
            Some("unknown_call")
        } else if !outcome.effects.precision_losses.is_empty() {
            Some("precision_loss")
        } else if outcome.return_selection.is_some() {
            Some("return_selection")
        } else if outcome.returns.is_empty()
            || !outcome.returns.iter().all(AbstractValue::is_primitive)
        {
            Some("return_values")
        } else {
            None
        };
        if let Some(reason) = refusal {
            refuse(&mut self.eligibility, reason);
            return;
        }
        let nested = self.eligibility.entered - pending.entered;
        let matching = self.eligibility.receipts.iter().find(|r| r.entry == entry);
        if let Some(previous) = matching {
            // A would-hit is reported only after the full repeated transfer
            // confirms the complete receipt and exact budget cost again.
            if previous.nested != nested
                || previous.cost != cost
                || previous.outcome != **outcome
                || previous.effects != *effects
                || previous.dependencies != *dependencies
            {
                refuse(&mut self.eligibility, "receipt_mismatch");
                return;
            }
            if pending
                .count
                .checked_add(previous.cost)
                .is_none_or(|n| n >= MAX_SYNCHRONOUS_INVOCATIONS)
            {
                refuse(&mut self.eligibility, "budget");
                return;
            }
            let counts = self.eligibility.counts.entry(key).or_default();
            counts.would_hit += 1;
            if nested > 0 {
                counts.nested_would_hit += 1;
                counts.nested_invocations += nested;
            }
        } else {
            // Count valid transfers separately from repeated eligible entries.
            let miss_reason = self
                .eligibility
                .receipts
                .iter()
                .find(|r| r.entry.signature == entry.signature)
                .map(|r| {
                    r.entry
                        .stable
                        .mismatch(&entry.stable)
                        .unwrap_or("entry_observations")
                })
                .unwrap_or("entry_miss");
            refuse(&mut self.eligibility, miss_reason);
        }
        self.eligibility.counts.entry(key).or_default().eligible += 1;
        if self.eligibility.receipts.len() == 256 {
            self.eligibility.receipts.remove(0);
        }
        self.eligibility.receipts.push(Receipt {
            entry,
            outcome: (**outcome).clone(),
            effects: effects.clone(),
            dependencies: dependencies.clone(),
            cost,
            nested,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(captured: CapturedContext, callbacks: Captured<Values>) -> Entry {
        let mut stable = StableContext::default();
        stable
            .fallback_outcome
            .returns
            .insert(AbstractValue::DeferredCallbacks(callbacks));
        Entry {
            signature: InvocationSignature {
                callable: 1,
                arguments: Vec::new(),
                argument_selections: Vec::new(),
                captured_context: Some(captured),
                fresh_checked_constructor: false,
                reachability_revision: 0,
                prefix_revision: 0,
                top_owner: None,
            },
            version: 0,
            stable,
            observations: Observations::default(),
        }
    }

    #[test]
    fn active_ancestor_and_depth_are_exact_inputs() {
        let base = StableContext::default();
        let mut other = base.clone();
        other.active.insert(ActiveInvocation {
            callable: 1,
            top_owner: None,
        });
        assert_eq!(base.mismatch(&other), Some("active"));
        other = base.clone();
        other.invocation_depth = 1;
        assert_eq!(base.mismatch(&other), Some("invocation_depth"));
    }

    #[test]
    fn interning_invalidation_drops_receipts_and_all_ancestor_payloads() {
        let mut captures = CaptureInterner::<LexicalCaptures>::default();
        let mut callbacks = CaptureInterner::<Values>::default();
        let captured = captures.intern(LexicalCaptures::default());
        let callback = callbacks.intern(Values::new());
        let capture_id = captured.cache_identity();
        let callback_id = callback.cache_identity();
        let weak_capture = Arc::downgrade(&captured.values);
        let weak_callback = Arc::downgrade(&callback.values);
        let guard = entry(captured, callback);
        let mut probe = EligibilityProbe::new();
        for callable in [1, 2] {
            probe.pending.push(Pending {
                callable,
                owner: None,
                count: 0,
                entered: 0,
                entry: Some(guard.clone()),
                invalidated: None,
            });
        }
        probe.receipts.push(Receipt {
            entry: guard,
            outcome: Outcome::default(),
            effects: EvaluationEffects::default(),
            dependencies: ProofDependencies::default(),
            cost: 1,
            nested: 0,
        });
        assert!(weak_capture.upgrade().is_some());
        assert!(weak_callback.upgrade().is_some());
        probe.clear_retained("interner");
        assert!(probe.receipts.is_empty());
        assert!(
            probe
                .pending
                .iter()
                .all(|p| p.entry.is_none() && p.invalidated == Some("interner"))
        );
        assert!(weak_capture.upgrade().is_none());
        assert!(weak_callback.upgrade().is_none());
        assert_ne!(
            captures.intern(LexicalCaptures::default()).cache_identity(),
            capture_id
        );
        assert_ne!(
            callbacks.intern(Values::new()).cache_identity(),
            callback_id
        );
    }
}
