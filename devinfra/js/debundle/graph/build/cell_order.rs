use super::*;

/// Preserve source-order observations of mutable array contents. Binding-cell
/// ordering cannot do this: an array initializer writes the object binding,
/// while a later `push` or indexed write changes the object behind it.
///
/// A statement's cells are its own at-init cells and those of the callables
/// its synchronous execution invoked; an execution the interpreter could not
/// complete may touch any contents and is a heap-frontier barrier.
pub(super) fn emit_heap_content_order(
    facts: &[StatementFacts],
    raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
) {
    // Only one eager occurrence with no deferred occurrence proves that a
    // cell was born at this source position. A literal inside a callable may
    // have allocated before an intervening opaque call, even if invoked again
    // afterwards, so its site cannot discharge the old frontier.
    let unique_eager_allocations = OneShotEagerAllocations::from_facts(facts);

    // A hoisted or escaped callable can allocate before its lexical statement.
    // Ordinary own-property cells likewise have no unique eager birth proof.
    // Array length inherits the array's checked eager birth below.
    let mut has_live_heap_contents = facts.iter().any(|stmt| {
        // A lexical-only initializer assertion can remove the conservative
        // binding coupling without bounding provider-owned heap state. Keep
        // opaque ordering on both sides of it even with no modeled local cells.
        stmt.synchronous_calls
            .as_ref()
            .is_some_and(|calls| calls.initializer_binding_effects.is_some())
            || !stmt.deferred_heap_allocations.is_empty()
            || stmt
                .synchronous_calls
                .iter()
                .flat_map(|calls| {
                    calls
                        .heap_contents
                        .reads
                        .iter()
                        .chain(&calls.heap_contents.writes)
                })
                .any(|cell| {
                    matches!(
                        cell,
                        EffectCell::OwnProperty { .. } | EffectCell::ObjectShape(_)
                    )
                })
    });
    let mut events = Vec::new();
    for stmt in facts {
        let own = &stmt.heap_contents;
        let own_heap = stmt.own_heap_content_summary();
        let mut allocations = own.allocations.clone();
        let mut effects = heap_content_cells(&own.reads, &own.writes);
        // Every disjunct that holds is retained as a `HeapBarrierCause`
        // on the edges the barrier emits.
        let mut opaque_causes = BTreeSet::new();
        if own_heap.opaque {
            opaque_causes.insert(HeapBarrierCause::ArrayContentEffects);
        }
        if !stmt.dataflow_summarizable() && !stmt.purity.is_pure() {
            opaque_causes.insert(HeapBarrierCause::NonSummarizableImpure);
        }
        if let Some(calls) = &stmt.synchronous_calls {
            let callees = &calls.heap_contents;
            allocations.extend(callees.allocations.iter().cloned());
            union_effects(
                &mut effects,
                &heap_content_cells(&callees.reads, &callees.writes),
            );
            if callees.opaque {
                opaque_causes.insert(HeapBarrierCause::CalleeHeapEffects);
            }
        }
        // If the interpreter cannot completely summarize this source unit, an
        // otherwise-pure expression may still observe mutable array contents
        // through coercion, a getter, or a callable it did not resolve.
        if !stmt
            .synchronous_calls
            .as_ref()
            .is_some_and(|calls| calls.complete)
        {
            opaque_causes.insert(HeapBarrierCause::IncompleteSynchronousExecution);
        }
        if let Some(calls) = &stmt.synchronous_calls
            && calls
                .initializer_external_effects
                .as_ref()
                .is_some_and(|effect_use| {
                    matches!(
                        effect_use.effects,
                        spec::InitializerExternalEffects::Isolated
                            | spec::InitializerExternalEffects::PrivateMetadata
                    )
                })
        {
            // A validated author promise covers only this initializer's
            // unknown heap accesses. Keep source-observed cells, allocation
            // events and non-heap/impure ordering; never complete execution.
            effects = heap_content_cells(&own.reads, &own.writes);
            union_effects(
                &mut effects,
                &heap_content_cells(
                    &calls.non_assumed_heap_accesses.reads,
                    &calls.non_assumed_heap_accesses.writes,
                ),
            );
            opaque_causes.remove(&HeapBarrierCause::ArrayContentEffects);
            opaque_causes.remove(&HeapBarrierCause::CalleeHeapEffects);
            opaque_causes.remove(&HeapBarrierCause::IncompleteSynchronousExecution);
        }
        let opaque = !opaque_causes.is_empty();
        has_live_heap_contents |=
            !allocations.is_empty() || !effects.reads.is_empty() || !effects.writes.is_empty();
        if !allocations.is_empty() {
            events.push((
                SourceUnitId(stmt.ordinal.0),
                stmt.ordinal,
                crate::StatementEffectSummary {
                    reads: BTreeSet::new(),
                    writes: allocations,
                },
                CellOrderBarrier::HeapAllocation,
            ));
        }
        if has_live_heap_contents
            && (!effects.reads.is_empty() || !effects.writes.is_empty() || opaque)
        {
            events.push((
                SourceUnitId(stmt.ordinal.0),
                stmt.ordinal,
                effects,
                if opaque {
                    CellOrderBarrier::HeapFrontier {
                        causes: opaque_causes,
                    }
                } else {
                    CellOrderBarrier::None
                },
            ));
        }
    }
    let mut sequence_pairs: BTreeSet<_> = raw_edges
        .iter()
        .filter(|(_, _, reason)| reason.kind == DepKind::Sequenced)
        .map(|(from, to, _)| (*from, *to))
        .collect();
    emit_cell_order_events(
        events,
        raw_edges,
        &mut sequence_pairs,
        Some(&unique_eager_allocations),
    );
}

pub(super) fn heap_content_cells(
    reads: &BTreeSet<EffectCell>,
    writes: &BTreeSet<EffectCell>,
) -> crate::StatementEffectSummary {
    crate::StatementEffectSummary {
        reads: reads
            .iter()
            .filter(|cell| {
                matches!(
                    cell,
                    EffectCell::ArrayContents(_)
                        | EffectCell::SetContents(_)
                        | EffectCell::ObjectShape(_)
                        | EffectCell::OwnProperty { .. }
                )
            })
            .cloned()
            .collect(),
        writes: writes
            .iter()
            .filter(|cell| {
                matches!(
                    cell,
                    EffectCell::ArrayContents(_)
                        | EffectCell::SetContents(_)
                        | EffectCell::ObjectShape(_)
                        | EffectCell::OwnProperty { .. }
                )
            })
            .cloned()
            .collect(),
    }
}

/// A source allocation site that can occur only in one eager top-level owner.
/// The graph may use its birth after an opaque frontier as negative alias
/// evidence for that earlier frontier. A lazy or duplicate site is excluded.
pub(super) struct OneShotEagerAllocations(BTreeSet<EffectCell>);

impl OneShotEagerAllocations {
    fn from_facts(facts: &[StatementFacts]) -> Self {
        let lazy: BTreeSet<_> = facts
            .iter()
            .flat_map(|stmt| stmt.deferred_heap_allocations.iter().cloned())
            .collect();
        let mut eager_counts = BTreeMap::<EffectCell, usize>::new();
        for cell in facts
            .iter()
            .flat_map(|stmt| stmt.heap_one_shot_allocations.iter().cloned())
        {
            *eager_counts.entry(cell).or_default() += 1;
        }
        Self(
            eager_counts
                .into_iter()
                .filter_map(|(cell, count)| (count == 1 && !lazy.contains(&cell)).then_some(cell))
                .collect(),
        )
    }

    fn contains(&self, cell: &EffectCell) -> bool {
        self.0.contains(cell)
    }
}

/// Resolve only chunk bindings initialized directly to a function/class
/// summary and never rebound or split by a shared `var` declaration.
/// Both at-init promotion and heap-content propagation use this boundary so
/// their call graph assumptions stay aligned.
pub(super) fn resolve_direct_function_source_unit(
    binding: &Id,
    binding_source_unit: &HashMap<Id, SourceUnitId>,
    shared_vars: &BTreeSet<Id>,
    stmt_by_source_unit: &HashMap<SourceUnitId, &StatementFacts>,
    rebound: &BTreeSet<Id>,
) -> Option<SourceUnitId> {
    let source_unit = *binding_source_unit.get(binding)?;
    let stmt = stmt_by_source_unit.get(&source_unit)?;
    (stmt.declares_direct_function || stmt.constructor.is_some())
        .then_some(source_unit)
        .filter(|_| !rebound.contains(binding) && !shared_vars.contains(binding))
}

pub(super) fn union_effects(
    into: &mut crate::StatementEffectSummary,
    from: &crate::StatementEffectSummary,
) -> bool {
    let old_reads = into.reads.len();
    let old_writes = into.writes.len();
    into.reads.extend(from.reads.iter().cloned());
    into.writes.extend(from.writes.iter().cloned());
    old_reads != into.reads.len() || old_writes != into.writes.len()
}

#[derive(Clone, PartialEq, Eq)]
pub(super) enum CellOrderBarrier {
    None,
    /// A fresh allocation seeds later heap observations without depending on
    /// prior heap effects or resetting an existing source-origin timeline.
    HeapAllocation,
    /// Preserve the S-chain behavior: an unsummarized impure statement
    /// depends on every preceding impure source unit. `causes` is
    /// retained on every edge the fallback emits.
    AllPriorEvents {
        causes: BTreeSet<DataflowOpacityCause>,
    },
    /// Unknown heap effects depend on the current heap-cell frontier and
    /// become the barrier for later heap observations. `causes` is
    /// retained on every edge the barrier emits.
    HeapFrontier {
        causes: BTreeSet<HeapBarrierCause>,
    },
}

impl CellOrderBarrier {
    fn from_opacity(causes: &BTreeSet<DataflowOpacityCause>) -> Self {
        if causes.is_empty() {
            Self::None
        } else {
            Self::AllPriorEvents {
                causes: causes.clone(),
            }
        }
    }

    /// The endpoint classification retained on emitted edges.
    fn effect_class(&self) -> CellEffectClass {
        match self {
            Self::None => CellEffectClass::Summarized,
            Self::HeapAllocation => CellEffectClass::HeapAllocation,
            Self::AllPriorEvents { causes } => CellEffectClass::OpaqueBarrier {
                causes: causes.clone(),
            },
            Self::HeapFrontier { causes } => CellEffectClass::HeapFrontier {
                causes: causes.clone(),
            },
        }
    }
}

/// Side-effect ordering edges (`S` per docs/design.md "Module dep graphs").
/// At source unit level, links impure top-level statements so any realizable
/// schedule preserves their observable order.
///
/// Two emission modes selected by [`GraphOptions`]:
///
/// - **Strict chain** (default): every later impure statement gets one
///   incoming `Sequenced` edge from the immediately previous impure
///   statement. Transitive reduction of the total order; soundest path.
/// - **Dataflow-aware** (`dataflow_aware_s_chain = true`): emit
///   `Sequenced(curr → prev)` only when `curr` reads or writes a cell
///   `prev` wrote (last-writer-precedes-reader). Statements that fail
///   the `dataflow_summarizable` check (dynamic globalThis key, `with`,
///   direct `eval`, `Function(...)` constructor, `defineProperty` on
///   globals, `Proxy` on globals) fall back to the strict edge against
///   every prior impure source unit. See `README.md` →
///   "Conditionally-correct optimizations" for the precondition this
///   relaxation requires.
///
/// `purity` is computed upstream (`classify_expr_purity`) so pure
/// literal initializers (`const X = 42`, `const X = { a: 1 }`,
/// function/class declarations without observable static init) don't
/// contribute to S. Without that precision the cross-module S graph
/// would be dense enough to reject realistic specs for trivially pure
/// const sequences.
pub(super) fn emit_s_chain(
    facts: &[StatementFacts],
    options: GraphOptions,
    raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
) {
    if !options.dataflow_aware_s_chain {
        let mut prev: Option<SourceUnitId> = None;
        for stmt in facts.iter().filter(|s| !s.purity.is_pure()) {
            let from = SourceUnitId(stmt.ordinal.0);
            if let Some(to) = prev
                && from != to
            {
                raw_edges.push((
                    from,
                    to,
                    EdgeReason::sequenced(stmt.ordinal, SequencedCause::StrictChain),
                ));
            }
            prev = Some(from);
        }
        return;
    }

    // Dataflow-aware emission. For each impure `curr`, emit an
    // incoming Sequenced edge from:
    //
    // - the most recent prior impure source unit that wrote any cell in
    //   `curr.reads ∪ curr.writes` (read-after-write /
    //   write-after-write), and
    // - every prior impure source unit that READ a cell `curr` writes
    //   since that cell's last write (write-after-read — without
    //   this, a later writer could be scheduled before an earlier
    //   reader and the reader would observe the new value).
    //
    // Statements with `dataflow_summarizable = false` are treated as
    // touching every cell — they get edges to every prior impure
    // source unit and become a barrier for subsequent statements.
    let mut sequence_pairs: BTreeSet<(SourceUnitId, SourceUnitId)> = raw_edges
        .iter()
        .filter(|(_, _, reason)| reason.kind == DepKind::Sequenced)
        .map(|(from, to, _)| (*from, *to))
        .collect();
    emit_cell_order_events(
        facts
            .iter()
            .filter(|stmt| !stmt.purity.is_pure())
            .map(|stmt| {
                (
                    SourceUnitId(stmt.ordinal.0),
                    stmt.ordinal,
                    stmt.effects(),
                    CellOrderBarrier::from_opacity(&stmt.dataflow_opacity()),
                )
            }),
        raw_edges,
        &mut sequence_pairs,
        None,
    );
}

/// Shared last-writer / reader-since-write timeline for binding, global, and
/// heap-content cells. Write-after-read dependencies preserve the value that
/// an earlier pure observer sees before a later mutation.
pub(super) fn emit_cell_order_events(
    events: impl IntoIterator<
        Item = (
            SourceUnitId,
            SourceUnitOrdinal,
            crate::StatementEffectSummary,
            CellOrderBarrier,
        ),
    >,
    raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
    sequence_pairs: &mut BTreeSet<(SourceUnitId, SourceUnitId)>,
    unique_eager_allocations: Option<&OneShotEagerAllocations>,
) {
    let mut last_writer = CellOverlapIndex::<SourceUnitId>::default();
    let mut readers_since_last_write = CellOverlapIndex::<BTreeSet<SourceUnitId>>::default();
    let mut prior_source_units: Vec<SourceUnitId> = Vec::new();
    let mut opaque_barrier: Option<SourceUnitId> = None;
    let mut pending_allocations = CellOverlapIndex::<BTreeSet<SourceUnitId>>::default();
    let mut born_after_barrier = BTreeSet::new();
    // Classification of every source unit seen so far, for the `to` side
    // of retained causes. A source unit's content event (summarized or
    // barrier) supersedes its earlier allocation event.
    let mut class_of: BTreeMap<SourceUnitId, CellEffectClass> = BTreeMap::new();
    // Every reason `from` must follow each target, keyed by target.
    let mut targets: BTreeMap<SourceUnitId, BTreeSet<CellConflict>> = BTreeMap::new();
    fn depend(
        targets: &mut BTreeMap<SourceUnitId, BTreeSet<CellConflict>>,
        to: SourceUnitId,
        conflict: CellConflict,
    ) {
        targets.entry(to).or_default().insert(conflict);
    }
    for (from, ordinal, effects, barrier_mode) in events {
        let from_class = barrier_mode.effect_class();
        if barrier_mode == CellOrderBarrier::HeapAllocation {
            for cell in effects.writes {
                if opaque_barrier.is_some()
                    && unique_eager_allocations.is_some_and(|unique| unique.contains(&cell))
                {
                    born_after_barrier.insert(cell.clone());
                }
                pending_allocations.get_or_default(cell).insert(from);
            }
            class_of.entry(from).or_insert(from_class);
            continue;
        }
        let all_prior = matches!(barrier_mode, CellOrderBarrier::AllPriorEvents { .. });
        if !all_prior {
            for (cell, is_write) in effects
                .reads
                .iter()
                .map(|cell| (cell, false))
                .chain(effects.writes.iter().map(|cell| (cell, true)))
            {
                for (overlap, allocations) in pending_allocations.overlapping(cell) {
                    for &to in allocations {
                        depend(
                            &mut targets,
                            to,
                            CellConflict::UseAfterAllocation {
                                cell: EffectCellReport::from(cell),
                                overlap: CellOverlapReport::from(&overlap),
                            },
                        );
                    }
                }
                for (overlap, &to) in last_writer.overlapping(cell) {
                    let conflict = if is_write {
                        CellConflict::WriteAfterWrite {
                            cell: EffectCellReport::from(cell),
                            overlap: CellOverlapReport::from(&overlap),
                        }
                    } else {
                        CellConflict::ReadAfterWrite {
                            cell: EffectCellReport::from(cell),
                            overlap: CellOverlapReport::from(&overlap),
                        }
                    };
                    depend(&mut targets, to, conflict);
                }
                if is_write {
                    for (overlap, readers) in readers_since_last_write.overlapping(cell) {
                        for &to in readers {
                            depend(
                                &mut targets,
                                to,
                                CellConflict::WriteAfterRead {
                                    cell: EffectCellReport::from(cell),
                                    overlap: CellOverlapReport::from(&overlap),
                                },
                            );
                        }
                    }
                }
            }
            // Unknown effects are barriers for subsequent summarized events.
            // Checked own array slots share the uniquely eager array's birth,
            // never a repeated site's or an ordinary record property's.
            if let Some(barrier) = opaque_barrier
                && (barrier_mode != CellOrderBarrier::None
                    || effects.reads.iter().chain(&effects.writes).any(|cell| {
                        !born_after_barrier.contains(cell)
                            && !matches!(cell,
                                    EffectCell::OwnProperty { origin, .. }
                                        if born_after_barrier.contains(
                                            &EffectCell::ArrayContents(*origin))
                                            && may_overlap(
                                                cell,
                                                &EffectCell::ArrayContents(*origin),
                                            ).is_some())
                    }))
            {
                depend(&mut targets, barrier, CellConflict::AfterBarrier);
            }
        }
        if all_prior {
            // The S-chain's historical fallback: depend on every prior event
            // and become a barrier for later summarized events.
            for &to in &prior_source_units {
                depend(&mut targets, to, CellConflict::BarrierAfterPrior);
            }
            opaque_barrier = Some(from);
            born_after_barrier.clear();
        } else if matches!(barrier_mode, CellOrderBarrier::HeapFrontier { .. }) {
            // An opaque heap operation only needs the live cell frontier.
            // Every earlier read/write is either on that frontier or already
            // constrained before a frontier event. Reset the per-cell state
            // so later observations depend on the opaque operation itself.
            for (cell, allocations) in pending_allocations.iter() {
                for &to in allocations {
                    depend(
                        &mut targets,
                        to,
                        CellConflict::FrontierAfterCell { cell: cell.into() },
                    );
                }
            }
            pending_allocations.clear();
            for (cell, &to) in last_writer.iter() {
                depend(
                    &mut targets,
                    to,
                    CellConflict::FrontierAfterCell { cell: cell.into() },
                );
            }
            for (cell, readers) in readers_since_last_write.iter() {
                for &to in readers {
                    depend(
                        &mut targets,
                        to,
                        CellConflict::FrontierAfterCell { cell: cell.into() },
                    );
                }
            }
            opaque_barrier = Some(from);
            born_after_barrier.clear();
            last_writer.clear();
            readers_since_last_write.clear();
        }
        for (to, conflicts) in std::mem::take(&mut targets) {
            if from != to && sequence_pairs.insert((from, to)) {
                let to_class = class_of
                    .get(&to)
                    .cloned()
                    .expect("every cell-timeline target is an earlier event");
                raw_edges.push((
                    from,
                    to,
                    EdgeReason::sequenced(
                        ordinal,
                        SequencedCause::CellOrder {
                            from: from_class.clone(),
                            to: to_class,
                            conflicts,
                        },
                    ),
                ));
            }
        }
        if barrier_mode == CellOrderBarrier::None {
            for cell in effects.writes {
                readers_since_last_write.remove_dominated(&cell);
                last_writer.remove_dominated(&cell);
                last_writer.insert(cell, from);
            }
            for cell in effects.reads {
                readers_since_last_write.get_or_default(cell).insert(from);
            }
        }
        class_of.insert(from, from_class);
        prior_source_units.push(from);
    }
}

#[cfg(test)]
mod array_heap_frontier_tests {
    use super::*;

    fn heap_frontier() -> CellOrderBarrier {
        CellOrderBarrier::HeapFrontier {
            causes: BTreeSet::from([HeapBarrierCause::ArrayContentEffects]),
        }
    }

    fn heap_event(
        ordinal: usize,
        reads: &[u32],
        writes: &[u32],
        barrier: CellOrderBarrier,
    ) -> (
        SourceUnitId,
        SourceUnitOrdinal,
        crate::StatementEffectSummary,
        CellOrderBarrier,
    ) {
        (
            SourceUnitId(ordinal),
            SourceUnitOrdinal(ordinal),
            crate::StatementEffectSummary {
                reads: reads
                    .iter()
                    .copied()
                    .map(EffectCell::ArrayContents)
                    .collect(),
                writes: writes
                    .iter()
                    .copied()
                    .map(EffectCell::ArrayContents)
                    .collect(),
            },
            barrier,
        )
    }

    fn cell_event(
        ordinal: usize,
        reads: &[EffectCell],
        writes: &[EffectCell],
    ) -> (
        SourceUnitId,
        SourceUnitOrdinal,
        crate::StatementEffectSummary,
        CellOrderBarrier,
    ) {
        (
            SourceUnitId(ordinal),
            SourceUnitOrdinal(ordinal),
            crate::StatementEffectSummary {
                reads: reads.iter().cloned().collect(),
                writes: writes.iter().cloned().collect(),
            },
            CellOrderBarrier::None,
        )
    }

    fn own_property(origin: u32, property: &str) -> EffectCell {
        EffectCell::OwnProperty {
            origin,
            property: property.into(),
        }
    }

    #[test]
    fn narrow_array_write_keeps_broad_writer_live_for_later_index_read() {
        let events = [
            cell_event(0, &[], &[EffectCell::ArrayContents(9)]),
            cell_event(1, &[], &[own_property(9, "0")]),
            cell_event(2, &[own_property(9, "1")], &[]),
        ];
        let mut edges = Vec::new();
        let mut pairs = BTreeSet::new();
        emit_cell_order_events(events, &mut edges, &mut pairs, None);
        assert!(pairs.contains(&(SourceUnitId(1), SourceUnitId(0))));
        assert!(pairs.contains(&(SourceUnitId(2), SourceUnitId(0))));
        assert!(!pairs.contains(&(SourceUnitId(2), SourceUnitId(1))));
    }

    #[test]
    fn narrow_array_write_keeps_broad_reader_live_for_later_index_write() {
        let events = [
            cell_event(0, &[EffectCell::ArrayContents(9)], &[]),
            cell_event(1, &[], &[own_property(9, "0")]),
            cell_event(2, &[], &[own_property(9, "1")]),
        ];
        let mut edges = Vec::new();
        let mut pairs = BTreeSet::new();
        emit_cell_order_events(events, &mut edges, &mut pairs, None);
        assert!(pairs.contains(&(SourceUnitId(1), SourceUnitId(0))));
        assert!(pairs.contains(&(SourceUnitId(2), SourceUnitId(0))));
        assert!(!pairs.contains(&(SourceUnitId(2), SourceUnitId(1))));
    }

    #[test]
    fn fresh_allocation_is_pending_without_reading_prior_opaque_effects() {
        let events = [
            heap_event(0, &[], &[], heap_frontier()),
            heap_event(1, &[], &[10], CellOrderBarrier::HeapAllocation),
            heap_event(2, &[], &[], heap_frontier()),
        ];
        let mut edges = Vec::new();
        let mut pairs = BTreeSet::new();
        emit_cell_order_events(events, &mut edges, &mut pairs, None);
        assert!(!pairs.contains(&(SourceUnitId(1), SourceUnitId(0))));
        assert!(pairs.contains(&(SourceUnitId(2), SourceUnitId(1))));
        assert!(pairs.contains(&(SourceUnitId(2), SourceUnitId(0))));
    }

    #[test]
    fn repeated_source_origin_allocation_preserves_content_writer_and_readers() {
        let events = [
            heap_event(0, &[], &[10], CellOrderBarrier::HeapAllocation),
            heap_event(1, &[], &[10], CellOrderBarrier::None),
            heap_event(2, &[10], &[], CellOrderBarrier::None),
            heap_event(3, &[], &[10], CellOrderBarrier::HeapAllocation),
            heap_event(4, &[10], &[], CellOrderBarrier::None),
            heap_event(5, &[], &[10], CellOrderBarrier::None),
        ];
        let mut edges = Vec::new();
        let mut pairs = BTreeSet::new();
        emit_cell_order_events(events, &mut edges, &mut pairs, None);
        assert!(pairs.contains(&(SourceUnitId(4), SourceUnitId(1))));
        assert!(pairs.contains(&(SourceUnitId(4), SourceUnitId(3))));
        assert!(pairs.contains(&(SourceUnitId(5), SourceUnitId(2))));
        assert!(pairs.contains(&(SourceUnitId(5), SourceUnitId(4))));
    }

    #[test]
    fn allocation_does_not_exempt_same_statement_opaque_execution() {
        let events = [
            heap_event(0, &[], &[10], CellOrderBarrier::HeapAllocation),
            heap_event(1, &[], &[], heap_frontier()),
            heap_event(2, &[], &[20], CellOrderBarrier::HeapAllocation),
            heap_event(2, &[], &[], heap_frontier()),
            heap_event(3, &[10], &[], CellOrderBarrier::None),
        ];
        let mut edges = Vec::new();
        let mut pairs = BTreeSet::new();
        emit_cell_order_events(events, &mut edges, &mut pairs, None);
        assert!(pairs.contains(&(SourceUnitId(1), SourceUnitId(0))));
        assert!(pairs.contains(&(SourceUnitId(2), SourceUnitId(1))));
        assert!(pairs.contains(&(SourceUnitId(3), SourceUnitId(2))));
    }

    #[test]
    fn repeated_opaque_heap_barriers_keep_edges_linear_in_the_live_frontier() {
        const ARRAY_COUNT: usize = 1024;
        const BARRIER_COUNT: usize = 16;

        let mut events = Vec::new();
        for array in 0..ARRAY_COUNT {
            let mut effects = crate::StatementEffectSummary::default();
            effects
                .reads
                .insert(EffectCell::ArrayContents(array as u32));
            events.push((
                SourceUnitId(array),
                SourceUnitOrdinal(array),
                effects,
                CellOrderBarrier::None,
            ));
        }
        for barrier in 0..BARRIER_COUNT {
            let source_unit = ARRAY_COUNT + barrier;
            events.push((
                SourceUnitId(source_unit),
                SourceUnitOrdinal(source_unit),
                crate::StatementEffectSummary::default(),
                heap_frontier(),
            ));
        }
        let last_barrier = ARRAY_COUNT + BARRIER_COUNT - 1;
        for array in 0..ARRAY_COUNT {
            let source_unit = ARRAY_COUNT + BARRIER_COUNT + array;
            let mut effects = crate::StatementEffectSummary::default();
            effects
                .reads
                .insert(EffectCell::ArrayContents(array as u32));
            events.push((
                SourceUnitId(source_unit),
                SourceUnitOrdinal(source_unit),
                effects,
                CellOrderBarrier::None,
            ));
        }

        let mut edges = Vec::new();
        let mut sequence_pairs = BTreeSet::new();
        emit_cell_order_events(events, &mut edges, &mut sequence_pairs, None);

        // First barrier follows the 1,024 live readers. The following 15
        // barriers form a chain, and the final readers follow the last one.
        // Retaining the old per-array reader frontier would add 15 * 1,024
        // redundant edges between earlier readers and later barriers.
        assert_eq!(edges.len(), 2 * ARRAY_COUNT + BARRIER_COUNT - 1);
        for array in 0..ARRAY_COUNT {
            assert!(sequence_pairs.contains(&(SourceUnitId(ARRAY_COUNT), SourceUnitId(array),)));
            assert!(sequence_pairs.contains(&(
                SourceUnitId(ARRAY_COUNT + BARRIER_COUNT + array),
                SourceUnitId(last_barrier),
            )));
        }
        for barrier in 1..BARRIER_COUNT {
            assert!(sequence_pairs.contains(&(
                SourceUnitId(ARRAY_COUNT + barrier),
                SourceUnitId(ARRAY_COUNT + barrier - 1),
            )));
        }
    }
}
