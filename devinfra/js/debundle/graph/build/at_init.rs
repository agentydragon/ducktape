use super::*;

/// Binding-ordering `Sequenced` edges of one `promote_at_init_calls` pass,
/// one per `(from, to)` pair. A pair several rules order keeps the first
/// cause offered, unless a later one is derived on both sides and the kept one
/// is not: the commuting-statements gate reads only the retained cause, so a
/// derived conflict must not hide behind an assumed one.
#[derive(Default)]
pub(super) struct BindingOrderEdges(BTreeMap<(SourceUnitId, SourceUnitId), SequencedCause>);

impl BindingOrderEdges {
    fn offer(&mut self, from: SourceUnitId, to: SourceUnitId, cause: SequencedCause) {
        match self.0.entry((from, to)) {
            std::collections::btree_map::Entry::Vacant(entry) => {
                entry.insert(cause);
            }
            std::collections::btree_map::Entry::Occupied(mut entry) => {
                if binding_effects_observed(&cause) && !binding_effects_observed(entry.get()) {
                    entry.insert(cause);
                }
            }
        }
    }
}

/// Whether a binding-ordering cause was derived from observed effects on
/// both endpoints; `false` for every other rule.
pub(super) fn binding_effects_observed(cause: &SequencedCause) -> bool {
    match cause {
        SequencedCause::SharedVarAccess { source, target, .. }
        | SequencedCause::BindingTimeline { source, target, .. } => {
            *source == EffectProvenance::Observed && *target == EffectProvenance::Observed
        }
        SequencedCause::UninitializedObservation { observation, .. } => {
            *observation == EffectProvenance::Observed
        }
        SequencedCause::StrictChain
        | SequencedCause::PreservedOrder { .. }
        | SequencedCause::CellOrder { .. } => false,
    }
}

/// Source units that write or observe one binding, each with the provenance
/// of its effect (observed wins when a unit has both).
pub(super) type BindingAccesses = BTreeMap<Id, BTreeMap<SourceUnitId, EffectProvenance>>;

pub(super) fn record_access(
    accesses: &mut BindingAccesses,
    binding: &Id,
    source_unit: SourceUnitId,
    provenance: EffectProvenance,
) {
    accesses
        .entry(binding.clone())
        .or_default()
        .entry(source_unit)
        .and_modify(|existing| existing.join(&provenance))
        .or_insert(provenance);
}

/// Offer one binding's source-order timeline: consecutive writers, and each
/// reader against its nearest preceding and following writer. No
/// reader-to-reader edges: independent observations of an unchanged cell need
/// no relative order and can form false cycles.
pub(super) fn offer_binding_timeline(
    binding: &Id,
    writers: &BTreeMap<SourceUnitId, EffectProvenance>,
    readers: &BTreeMap<SourceUnitId, EffectProvenance>,
    existing_order_pairs: &BTreeSet<(SourceUnitId, SourceUnitId)>,
    edges: &mut BindingOrderEdges,
) {
    let mut offer = |from: SourceUnitId,
                     to: SourceUnitId,
                     order: BindingTimelineOrder,
                     source: &EffectProvenance,
                     target: &EffectProvenance| {
        if !existing_order_pairs.contains(&(from, to)) {
            edges.offer(
                from,
                to,
                SequencedCause::BindingTimeline {
                    binding: binding.0.clone(),
                    order,
                    source: source.clone(),
                    target: target.clone(),
                },
            );
        }
    };
    let writer_events: Vec<_> = writers.iter().collect();
    for pair in writer_events.windows(2) {
        let (&earlier, earlier_provenance) = pair[0];
        let (&later, later_provenance) = pair[1];
        offer(
            later,
            earlier,
            BindingTimelineOrder::WriterAfterWriter,
            later_provenance,
            earlier_provenance,
        );
    }
    for (&reader, read) in readers {
        if let Some((&previous_writer, write)) = writers.range(..reader).next_back() {
            offer(
                reader,
                previous_writer,
                BindingTimelineOrder::ReaderAfterWriter,
                read,
                write,
            );
        }
        if let Some((&next_writer, write)) = writers
            .range((
                std::ops::Bound::Excluded(reader),
                std::ops::Bound::Unbounded,
            ))
            .next()
        {
            offer(
                next_writer,
                reader,
                BindingTimelineOrder::WriterAfterReader,
                write,
                read,
            );
        }
    }
}

/// Promote function-body lazy reads/rebinds to eager source unit edges from
/// every statement that at-init-calls the function. Transitive over
/// the call graph among chunk-declared functions: a top-level
/// `f()` whose `f` calls `g` in its body promotes through `g`'s lazy
/// reads/rebinds too. See docs/design.md "At-init call promotion".
///
/// Per-statement dedup: at most one promoted eager edge per
/// (caller, target-source unit) pair. Rebind targets are also promoted as
/// eager order edges; the write site's direct rebind edge enforces
/// write legality. Without dedup, a single at-init call to a
/// function with N transitive lazy reads would emit N edges from the
/// caller, and multiple at-init calls in the same statement would
/// multiply that further.
///
/// Promote at-init call effects into the graph. Returns the raw-edge index
/// of every promoted `EagerUse` whose read or rebind only a conservative path
/// contributed, with that path (the shared-`var` access order reads it).
pub(super) fn promote_at_init_calls(
    facts: &[StatementFacts],
    binding_source_unit: &HashMap<Id, SourceUnitId>,
    shared_vars: &BTreeSet<Id>,
    stmt_by_source_unit: &HashMap<SourceUnitId, &StatementFacts>,
    target_is_hoisted: impl Fn(&Id) -> bool,
    raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
) -> BTreeMap<usize, AssumedEffectReason> {
    let (writers_by_binding, readers_by_binding) = binding_effect_timelines(facts);

    // A pair may be observed by several callback callsites or be both a
    // future declaration and a future writer. Emit each temporal ordering
    // constraint only once within this pass; pairs an earlier rule already
    // orders get no timeline edge.
    let mut binding_order = BindingOrderEdges::default();
    let existing_order_pairs: BTreeSet<(SourceUnitId, SourceUnitId)> = raw_edges
        .iter()
        .filter(|(_, _, reason)| reason.constrains_init_order())
        .map(|(from, to, _)| (*from, *to))
        .collect();
    let mut direct_eager_bindings: BTreeSet<(SourceUnitId, SourceUnitId, Id)> = raw_edges
        .iter()
        .filter_map(|(from, to, reason)| {
            (reason.kind == DepKind::EagerUse && reason.role == EdgeRole::Direct)
                .then(|| {
                    reason
                        .binding
                        .as_ref()
                        .map(|binding| (*from, *to, binding.clone()))
                })
                .flatten()
        })
        .collect();
    offer_binding_timelines(
        &writers_by_binding,
        &readers_by_binding,
        &existing_order_pairs,
        &mut binding_order,
    );

    // Bindings rebound anywhere in the chunk: their value at call
    // time may not be the function lexically defined at their source unit,
    // so calls to them are not precisely resolvable.
    let rebound: BTreeSet<Id> = facts
        .iter()
        .flat_map(|s| s.rebinds.eager.iter().chain(s.rebinds.lazy.iter()).cloned())
        .collect();

    // A callee binding is precisely resolvable iff (a) its declaring
    // statement binds it directly to a function value (`function f`,
    // `const f = () => ...`) and (b) it is never rebound. Everything
    // else — aliases (`const g = readB`), object-literal methods,
    // conditional initializers, rebound functions — takes the
    // conservative read-closure fallback below.
    let resolvable_callee = |id: &Id| {
        resolve_direct_function_source_unit(
            id,
            binding_source_unit,
            shared_vars,
            stmt_by_source_unit,
            &rebound,
        )
    };

    // Per-source unit "fallback roots": source units through which a function
    // value could reach a call the source unit's first-order body makes
    // but promotion can't follow — the source units of bindings the
    // unresolved call mentions, the source units of chunk-declared
    // Ident callees that aren't direct never-rebound functions,
    // and the source unit itself when the unresolved call carries an
    // inline function expression. At-init callers inherit these
    // roots transitively through the call graph and expand them
    // through the source-unit read closure.
    let fallback_roots_of = |sources: &BTreeSet<Id>,
                             inline_fn: bool,
                             self_source_unit: SourceUnitId|
     -> BTreeSet<SourceUnitId> {
        let mut roots: BTreeSet<SourceUnitId> = sources
            .iter()
            .filter_map(|id| binding_source_unit.get(id).copied())
            .collect();
        if inline_fn {
            roots.insert(self_source_unit);
        }
        roots
    };
    let CallGraphClosure {
        scc_of,
        reads: scc_reads,
        rebinds: scc_rebinds,
        fallback_roots: scc_fallback_roots,
        needs_caller_values: scc_needs_caller_values,
    } = close_over_call_graph(
        facts,
        binding_source_unit,
        stmt_by_source_unit,
        &resolvable_callee,
        &fallback_roots_of,
        &target_is_hoisted,
    );

    // 5. Emit promoted edges with per-statement, per-kind dedup.
    //    Each emitted reason carries `EdgeRole::PromotedAtInit {
    //    callee_source_unit }` so the realizability gate can drop the edge
    //    when caller and callee land in different assignment slots —
    //    the body read fires inside a call into a different module,
    //    after that callee module (and its imports) have already
    //    evaluated, so the manufactured constraint from R to the
    //    target's module is redundant with the already-recorded
    //    R -> callee-module edge. See [`EdgeRole`].
    //
    //    Statements whose at-init calls can't all be resolved also take
    //    the conservative fallback. Any function value an unresolved call
    //    invokes must have reached it through a mentioned binding (callee,
    //    arguments or computed keys) or an inline function expression. Each
    //    such root expands through the full source-unit read graph; every
    //    reachable unit contributes its lazy reads and rebinds at any depth.
    //    Values from other chunks and dynamic global/property stashes may not
    //    resolve to one callable (see docs/design.md, "At-init call promotion").
    //    Fallback edges use the caller as their callee source unit, so neither
    //    projection drops them for a precisely known callee in another module.
    let mut fallback: Option<SourceUnitReadClosure> = None;
    let mut fallback_eager_uses = BTreeMap::new();
    for stmt in facts {
        if let Some(synchronous) = &stmt.synchronous_calls {
            let caller = SourceUnitId(stmt.ordinal.0);
            let mut emitted = BTreeSet::new();

            // A read known to observe a binding before its source
            // initialization is not a safely-droppable call promotion: moving
            // the declaration ahead of this callback would erase a TDZ or
            // change the initial `undefined` value. Keep a direct dependency
            // on the declaration, and when that declaration is later in the
            // source, pair it with reverse source order. EagerUse alone would
            // merely ask the later provider to run first; the pair makes the
            // two source units atomic so the emitted module retains original order.
            for target_binding in &synchronous.uninitialized_reads {
                let Some(&target_source_unit) = binding_source_unit.get(target_binding) else {
                    continue;
                };
                if caller == target_source_unit {
                    continue;
                }
                if direct_eager_bindings.insert((
                    caller,
                    target_source_unit,
                    target_binding.clone(),
                )) {
                    if let Some(reason) = synchronous.fallback_read(target_binding) {
                        fallback_eager_uses.insert(raw_edges.len(), reason.clone());
                    }
                    raw_edges.push((
                        caller,
                        target_source_unit,
                        EdgeReason::for_binding(
                            DepKind::EagerUse,
                            stmt.ordinal,
                            target_binding.clone(),
                        ),
                    ));
                }
                if caller < target_source_unit {
                    binding_order.offer(
                        target_source_unit,
                        caller,
                        SequencedCause::UninitializedObservation {
                            binding: target_binding.0.clone(),
                            observation: EffectProvenance::from_fallback(
                                synchronous.fallback_uninitialized_read(target_binding),
                            ),
                        },
                    );
                }
            }

            let reads = synchronous
                .reads
                .iter()
                .filter(|(binding, _)| !synchronous.uninitialized_reads.contains(binding))
                .map(|(binding, callee)| (binding, callee, synchronous.fallback_read(binding)));
            let rebinds = synchronous
                .rebinds
                .iter()
                .map(|(binding, callee)| (binding, callee, synchronous.fallback_rebind(binding)));
            for (target_binding, callee_ordinal, fallback) in reads.chain(rebinds) {
                if target_is_hoisted(target_binding) {
                    continue;
                }
                let Some(&target_source_unit) = binding_source_unit.get(target_binding) else {
                    continue;
                };
                let callee_source_unit = SourceUnitId(callee_ordinal.0);
                if caller == target_source_unit
                    || !emitted.insert((target_source_unit, callee_source_unit))
                {
                    continue;
                }
                if let Some(reason) = fallback {
                    fallback_eager_uses.insert(raw_edges.len(), reason.clone());
                }
                raw_edges.push((
                    caller,
                    target_source_unit,
                    EdgeReason::for_binding(
                        DepKind::EagerUse,
                        stmt.ordinal,
                        target_binding.clone(),
                    )
                    .promoted_at_init(callee_source_unit),
                ));
            }
            match synchronous.binding_effect_authority() {
                crate::facts::sync_effects::BindingEffectAuthority::Unclassified => {}
                crate::facts::sync_effects::BindingEffectAuthority::LegacyExecution {
                    complete: true,
                }
                | crate::facts::sync_effects::BindingEffectAuthority::AuthorSourceOnly
                | crate::facts::sync_effects::BindingEffectAuthority::OperationReceipt => {
                    continue;
                }
                crate::facts::sync_effects::BindingEffectAuthority::LegacyExecution {
                    complete: false,
                }
                | crate::facts::sync_effects::BindingEffectAuthority::OrderedRootInitializerUncovered => {}
            }
        }
        if stmt.calls.eager.is_empty()
            && stmt.at_init_unresolved_sources.is_empty()
            && !stmt.at_init_unresolved_inline_fn
        {
            continue;
        }
        let caller = SourceUnitId(stmt.ordinal.0);
        let mut promoted_read_targets: BTreeSet<SourceUnitId> = BTreeSet::new();
        let mut fallback_roots = fallback_roots_of(&stmt.at_init_unresolved_sources, false, caller);
        let mut promote_scc_bindings = |scc_idx: usize, callee| {
            // Reads precede rebinds: when both identify the same target unit,
            // its first binding and callee are retained as the edge witness.
            for binding in scc_reads[scc_idx].iter().chain(&scc_rebinds[scc_idx]) {
                let target = binding_source_unit[binding];
                if target != caller && promoted_read_targets.insert(target) {
                    raw_edges.push((
                        caller,
                        target,
                        EdgeReason::for_binding(DepKind::EagerUse, stmt.ordinal, binding.clone())
                            .promoted_at_init(callee),
                    ));
                }
            }
        };
        if stmt.at_init_unresolved_inline_fn
            && let Some(&scc_idx) = scc_of.get(&caller)
        {
            fallback_roots.extend(scc_fallback_roots[scc_idx].iter().copied());
            promote_scc_bindings(scc_idx, caller);
        }
        for callee_id in &stmt.calls.eager {
            let Some(callee_source_unit) = resolvable_callee(callee_id) else {
                // A bare-Ident callee that isn't chunk-declared is a
                // global/import — out of single-chunk analysis scope
                // (documented precondition in docs/design.md). A
                // chunk-declared but unresolvable callee falls back
                // through the read closure of its own source unit.
                if let Some(&callee_source_unit) = binding_source_unit.get(callee_id) {
                    fallback_roots.insert(callee_source_unit);
                }
                continue;
            };
            let Some(&scc_idx) = scc_of.get(&callee_source_unit) else {
                continue;
            };
            fallback_roots.extend(scc_fallback_roots[scc_idx].iter().copied());
            if scc_needs_caller_values[scc_idx] {
                // Include the caller itself (inline callback bodies and
                // local aliases) and every chunk binding it reads (argument
                // values). The fallback then closes over their
                // lazy bodies. This is deliberately conservative until a
                // parameter's specific value can be proven.
                fallback_roots.insert(caller);
                fallback_roots.extend(fallback_roots_of(&stmt.reads.eager, false, caller));
            }
            promote_scc_bindings(scc_idx, callee_source_unit);
        }
        if fallback_roots.is_empty() {
            continue;
        }
        let fallback =
            fallback.get_or_insert_with(|| SourceUnitReadClosure::new_synchronous(facts));
        for root in fallback_roots {
            let (closure_reads, closure_rebinds) =
                fallback.lazy_effects_for_source_unit(SourceUnitOrdinal(root.0));
            // Rebind targets get an *order* constraint only (EagerUse,
            // not EagerRebind): assignment to a TDZ-locked binding
            // throws until its statement runs, so the target must
            // evaluate before the caller — but write LEGALITY
            // (read-only imports) is already enforced by the direct
            // `LazyRebind` / `DeferredRebind` edge from the statement
            // lexically containing the write, which forces the write
            // site to co-locate with the declarer. Emitting
            // `EagerRebind` here would force the *caller* into the
            // co-location group too, rejecting realizable shapes
            // (`c.bump(1)` calling a co-located setter).
            for target_binding in closure_reads.iter().chain(closure_rebinds.iter()) {
                if target_is_hoisted(target_binding) {
                    continue;
                }
                let Some(target_source_unit) = binding_source_unit.get(target_binding) else {
                    continue;
                };
                if caller == *target_source_unit
                    || !promoted_read_targets.insert(*target_source_unit)
                {
                    continue;
                }
                // The closure assumes the unresolved callee may run here.
                fallback_eager_uses
                    .insert(raw_edges.len(), AssumedEffectReason::UnresolvedCallClosure);
                raw_edges.push((
                    caller,
                    *target_source_unit,
                    EdgeReason::for_binding(
                        DepKind::EagerUse,
                        stmt.ordinal,
                        target_binding.clone(),
                    )
                    .promoted_at_init(caller),
                ));
            }
        }
    }
    for ((from, to), cause) in binding_order.0 {
        raw_edges.push((
            from,
            to,
            EdgeReason::sequenced(SourceUnitOrdinal(from.0), cause),
        ));
    }
    fallback_eager_uses
}

/// Index statement source units that can change a chunk binding's value or
/// observable object state when they execute, and those that read it. A
/// callback read must see the source-prefix value, so the callback-read and
/// writer source units form one source-order timeline. This also covers
/// mutations of an already initialized object: an ordinary promoted read
/// dependency on its declaration alone would not stop a later mutation from
/// moving before the callback. Unknown effects still take the existing strict
/// S-chain fallback; this index only records effects with a known binding
/// target. Each effect carries its provenance: only a synchronous summary's
/// fallback contributes an assumed one. Returns `(writers, readers)`.
fn binding_effect_timelines(facts: &[StatementFacts]) -> (BindingAccesses, BindingAccesses) {
    let mut writers_by_binding = BindingAccesses::new();
    let mut readers_by_binding = BindingAccesses::new();
    for stmt in facts {
        let source_unit = SourceUnitId(stmt.ordinal.0);
        for binding in &stmt.reads.eager {
            record_access(
                &mut readers_by_binding,
                binding,
                source_unit,
                EffectProvenance::Observed,
            );
        }
        // Lexical declarations become observable at their statement. Function
        // declarations are initialized during module instantiation and are
        // therefore not execution-time writes.
        // Imports describe module linkage; evaluating the import declaration
        // does not write the imported binding in this chunk. Keep this guard
        // explicit even though `collect_declared_names` currently gives
        // imports no local declarations.
        if stmt.initializes_bindings_at_evaluation() {
            for binding in stmt.declared.difference(&stmt.var_declared) {
                record_access(
                    &mut writers_by_binding,
                    binding,
                    source_unit,
                    EffectProvenance::Observed,
                );
            }
        }
        for binding in stmt
            .var_initializers
            .iter()
            .chain(&stmt.rebinds.eager)
            .chain(&stmt.local_effects)
        {
            record_access(
                &mut writers_by_binding,
                binding,
                source_unit,
                EffectProvenance::Observed,
            );
        }
        // A summary's rebinds execute at this statement's callsite, not at
        // the lexical source unit containing the callback body.
        if let Some(synchronous) = &stmt.synchronous_calls {
            for (binding, _) in &synchronous.reads {
                record_access(
                    &mut readers_by_binding,
                    binding,
                    source_unit,
                    EffectProvenance::from_fallback(synchronous.fallback_read(binding)),
                );
            }
            // Keep the graph sound even if an availability-specific read is
            // not duplicated in the general read set.
            for binding in &synchronous.uninitialized_reads {
                record_access(
                    &mut readers_by_binding,
                    binding,
                    source_unit,
                    EffectProvenance::from_fallback(
                        synchronous.fallback_uninitialized_read(binding),
                    ),
                );
            }
            for (binding, _) in &synchronous.rebinds {
                record_access(
                    &mut writers_by_binding,
                    binding,
                    source_unit,
                    EffectProvenance::from_fallback(synchronous.fallback_rebind(binding)),
                );
            }
        }
    }
    (writers_by_binding, readers_by_binding)
}

/// Preserve each observed binding's writer order and place each reader
/// against only its nearest preceding/following writer. Ordinary eager
/// readers need this timeline even when no callback reads the binding:
/// a pure initializer may otherwise move across a later assignment.
/// The timeline of observed effects alone is offered too: when an assumed
/// (fallback) effect sits between two observed ones, the nearest-effect
/// edges route their derived order through it, and a premise making an
/// assumed edge inert must not drop that order.
fn offer_binding_timelines(
    writers_by_binding: &BindingAccesses,
    readers_by_binding: &BindingAccesses,
    existing_order_pairs: &BTreeSet<(SourceUnitId, SourceUnitId)>,
    binding_order: &mut BindingOrderEdges,
) {
    let no_readers = BTreeMap::new();
    for (binding, writers) in writers_by_binding {
        let readers = readers_by_binding.get(binding).unwrap_or(&no_readers);
        if readers.is_empty() {
            continue;
        }
        offer_binding_timeline(
            binding,
            writers,
            readers,
            existing_order_pairs,
            binding_order,
        );
        let observed = |accesses: &BTreeMap<SourceUnitId, EffectProvenance>| {
            accesses
                .iter()
                .filter(|(_, provenance)| **provenance == EffectProvenance::Observed)
                .map(|(source_unit, provenance)| (*source_unit, provenance.clone()))
                .collect::<BTreeMap<_, _>>()
        };
        let (observed_writers, observed_readers) = (observed(writers), observed(readers));
        if observed_writers.len() != writers.len() || observed_readers.len() != readers.len() {
            offer_binding_timeline(
                binding,
                &observed_writers,
                &observed_readers,
                existing_order_pairs,
                binding_order,
            );
        }
    }
}

/// Per-SCC transitive effects of the at-init promotion call graph, indexed by
/// the SCC numbering in `scc_of`.
struct CallGraphClosure {
    scc_of: BTreeMap<SourceUnitId, usize>,
    reads: Vec<BTreeSet<Id>>,
    rebinds: Vec<BTreeSet<Id>>,
    fallback_roots: Vec<BTreeSet<SourceUnitId>>,
    needs_caller_values: Vec<bool>,
}

fn close_over_call_graph(
    facts: &[StatementFacts],
    binding_source_unit: &HashMap<Id, SourceUnitId>,
    stmt_by_source_unit: &HashMap<SourceUnitId, &StatementFacts>,
    resolvable_callee: &impl Fn(&Id) -> Option<SourceUnitId>,
    fallback_roots_of: &impl Fn(&BTreeSet<Id>, bool, SourceUnitId) -> BTreeSet<SourceUnitId>,
    target_is_hoisted: &impl Fn(&Id) -> bool,
) -> CallGraphClosure {
    // 1. Build the call graph: source unit → source unit edges for each
    //    resolvable callee reachable via *first-order* calls.lazy.
    //    Nested-closure calls (e.g. inside an arrow returned by the
    //    body) don't fire when the body is invoked synchronously, so
    //    they don't belong on the promotion call graph — see
    //    docs/design.md "At-init call promotion" and the e2e test
    //    `e2e/cases/initialization/nested_closure_{read,rebind}`.
    //
    //    Add every source unit whose body has any first-order lazy reads /
    //    rebinds / calls — or an unresolvable first-order callee — as
    //    a node, so the closure pass below covers it even if it makes
    //    no resolvable calls (e.g. `function readB() { return B; }`).
    let source_unit_first_order_roots = |stmt: &StatementFacts| -> BTreeSet<SourceUnitId> {
        let source_unit = SourceUnitId(stmt.ordinal.0);
        let mut roots = fallback_roots_of(
            stmt.first_order_unresolved_sources(),
            stmt.first_order_unresolved_inline_fn(),
            source_unit,
        );
        for callee_id in stmt.first_order_calls() {
            if resolvable_callee(callee_id).is_none()
                && let Some(&callee_source_unit) = binding_source_unit.get(callee_id)
            {
                roots.insert(callee_source_unit);
            }
        }
        roots
    };
    let mut call_graph: DiGraphMap<SourceUnitId, ()> = DiGraphMap::new();
    for stmt in facts {
        let source_unit = SourceUnitId(stmt.ordinal.0);
        if !stmt.first_order_calls().is_empty()
            || !stmt.first_order_reads().is_empty()
            || !stmt.first_order_rebinds().is_empty()
            || !stmt.first_order_unresolved_sources().is_empty()
            || stmt.first_order_unresolved_inline_fn()
        {
            call_graph.add_node(source_unit);
        }
    }
    for stmt in facts {
        if stmt.first_order_calls().is_empty() {
            continue;
        }
        let caller = SourceUnitId(stmt.ordinal.0);
        for callee_id in stmt.first_order_calls() {
            let Some(callee_source_unit) = resolvable_callee(callee_id) else {
                continue;
            };
            call_graph.add_node(callee_source_unit);
            call_graph.add_edge(caller, callee_source_unit, ());
        }
    }

    // 2. Tarjan SCC. `tarjan_scc` returns SCCs in reverse topological
    //    order: leaves (no outgoing edges to other SCCs) first.
    let sccs = tarjan_scc(&call_graph);
    let mut scc_of: BTreeMap<SourceUnitId, usize> = BTreeMap::new();
    for (idx, scc) in sccs.iter().enumerate() {
        for source_unit in scc {
            scc_of.insert(*source_unit, idx);
        }
    }

    // 3. Per-source unit seeds: own reads.lazy / rebinds.lazy resolved to
    //    SWC Ids. Filters out targets whose source unit is a function
    //    declaration — function bindings are hoisted at module
    //    instantiation (Phase 1 of ESM linking), so a cross-module
    //    read of a function never observes a TDZ. Promoting such
    //    reads would spuriously close cycles for shapes like mutual
    //    recursion across modules (`function even(){odd()}` /
    //    `function odd(){even()}`), which are actually realizable.
    //    Other declared kinds (VarDecl, ClassDecl) are kept: const /
    //    let / class are TDZ-locked until their statement runs, so a
    //    cross-module read inside an at-init-called function does
    //    fire the realizability hazard. `var` is technically hoisted
    //    too but is rare enough not to warrant a separate distinction
    //    in StatementKind.
    let mut scc_reads: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];
    let mut scc_rebinds: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];
    let mut scc_fallback_roots: Vec<BTreeSet<SourceUnitId>> = vec![BTreeSet::new(); sccs.len()];
    // A first-order call through a parameter/local cannot be mapped to a
    // chunk-top source unit here. The value may have come from an argument at the
    // eventual at-init call site, so carry that obligation through the call
    // graph instead of silently dropping the Id in fallback_roots_of.
    let mut scc_needs_caller_values = vec![false; sccs.len()];

    // 4. Closure over the call graph. Iterate SCCs in
    //    reverse-topological order (leaves first). For each SCC,
    //    union members' own seeds plus successor SCC closures, and
    //    propagate fallback roots the same way.
    for (scc_idx, scc) in sccs.iter().enumerate() {
        let mut reads: BTreeSet<Id> = BTreeSet::new();
        let mut rebinds: BTreeSet<Id> = BTreeSet::new();
        let mut roots: BTreeSet<SourceUnitId> = BTreeSet::new();
        let mut needs_caller_values = false;
        for source_unit in scc {
            let Some(stmt) = stmt_by_source_unit.get(source_unit) else {
                continue;
            };
            roots.extend(source_unit_first_order_roots(stmt));
            needs_caller_values |= stmt
                .first_order_unresolved_sources()
                .iter()
                .chain(stmt.first_order_calls().iter())
                .any(|id| {
                    stmt.local_bindings.contains(id) && !binding_source_unit.contains_key(id)
                });
            for id in stmt.first_order_reads() {
                if binding_source_unit.contains_key(id) && !target_is_hoisted(id) {
                    reads.insert(id.clone());
                }
            }
            for id in stmt.first_order_rebinds() {
                if binding_source_unit.contains_key(id) {
                    rebinds.insert(id.clone());
                }
            }
        }
        for source_unit in scc {
            for (_, target, _) in call_graph.edges(*source_unit) {
                let Some(&target_scc) = scc_of.get(&target) else {
                    continue;
                };
                if target_scc == scc_idx {
                    continue;
                }
                reads.extend(scc_reads[target_scc].iter().cloned());
                rebinds.extend(scc_rebinds[target_scc].iter().cloned());
                roots.extend(scc_fallback_roots[target_scc].iter().copied());
                needs_caller_values |= scc_needs_caller_values[target_scc];
            }
        }
        scc_reads[scc_idx] = reads;
        scc_rebinds[scc_idx] = rebinds;
        scc_fallback_roots[scc_idx] = roots;
        scc_needs_caller_values[scc_idx] = needs_caller_values;
    }
    CallGraphClosure {
        scc_of,
        reads: scc_reads,
        rebinds: scc_rebinds,
        fallback_roots: scc_fallback_roots,
        needs_caller_values: scc_needs_caller_values,
    }
}
