//! Fixed-point rules that evaluate a `FlowProgram`, plus the test-only
//! reference pass.

use super::*;

#[derive(Clone)]
pub(super) struct CachedExpression {
    pub(super) values: Values,
    pub(super) dependencies: BTreeSet<FlowKey>,
}

/// Event-local target/argument coordinates preserve the old transfer order.
/// These are bounded source identities, never execution or call-path identities.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub(super) struct ArgumentEdge {
    pub(super) function: u32,
    pub(super) argument: usize,
}

pub(super) struct FormalEdge {
    pub(super) parameter: BindingSlot,
    pub(super) delivered: Values,
}

pub(super) struct Invocation {
    pub(super) callee: Option<CachedExpression>,
    pub(super) callee_dirty: bool,
    pub(super) arguments: Vec<Option<CachedExpression>>,
    pub(super) dirty: BTreeSet<usize>,
    /// Exact currently consumed reads, including absence. Solver watchers also
    /// retain historical reads, which may wake the event without dirtying an arg.
    pub(super) readers: BTreeMap<FlowKey, BTreeSet<usize>>,
    pub(super) targets: BTreeSet<u32>,
    pub(super) waiting: BTreeSet<u32>,
    pub(super) ready: BTreeSet<u32>,
    pub(super) edges: BTreeMap<ArgumentEdge, FormalEdge>,
    pub(super) consumers: Vec<BTreeSet<ArgumentEdge>>,
}

impl Invocation {
    pub(super) fn new(arguments: usize) -> Self {
        Self {
            callee: None,
            callee_dirty: true,
            arguments: vec![None; arguments],
            dirty: BTreeSet::new(),
            readers: BTreeMap::new(),
            targets: BTreeSet::new(),
            waiting: BTreeSet::new(),
            ready: BTreeSet::new(),
            edges: BTreeMap::new(),
            consumers: vec![BTreeSet::new(); arguments],
        }
    }

    pub(super) fn changed(&mut self, changed: &BTreeSet<FlowKey>) {
        if self
            .callee
            .as_ref()
            .is_some_and(|callee| !callee.dependencies.is_disjoint(changed))
        {
            self.callee_dirty = true;
        }
        for key in changed {
            if let Some(readers) = self.readers.get(key) {
                self.dirty.extend(readers);
            }
            if let FlowKey::ReadyParameters(function) = key
                && self.waiting.remove(function)
            {
                self.ready.insert(*function);
            }
        }
    }
}

/// One source write activates new receivers without replaying old receivers.
/// RHS growth fans out the shared full result to preserve bitmap Arc adoption.
/// Fixed receivers need only an RHS slot; immutable fields need no state.
#[derive(Default)]
pub(super) struct PropertyWrite {
    pub(super) receiver: Option<Box<PropertyReceivers>>,
    pub(super) right: Option<CachedExpression>,
    pub(super) right_dirty: bool,
}

#[derive(Default)]
pub(super) struct PropertyReceivers {
    pub(super) expression: Option<CachedExpression>,
    pub(super) dirty: bool,
    pub(super) receivers: BTreeSet<u32>,
}

pub(super) enum PropertyTarget<'a> {
    Fixed(u32),
    Expression(&'a FlowValueExpr),
}

impl PropertyWrite {
    pub(super) fn changed(&mut self, changed: &BTreeSet<FlowKey>) {
        if let Some(receiver) = self.receiver.as_mut()
            && receiver
                .expression
                .as_ref()
                .is_some_and(|expr| !expr.dependencies.is_disjoint(changed))
        {
            receiver.dirty = true;
        }
        if self
            .right
            .as_ref()
            .is_some_and(|expr| !expr.dependencies.is_disjoint(changed))
        {
            self.right_dirty = true;
        }
    }
}

pub(super) struct FlowRules {
    pub(super) metrics: work_metrics::WorkMetrics,
    /// Exact expression results and activated source edges, retired together at
    /// completion. No result is published before its first ordered use.
    pub(super) invocations: RefCell<Vec<Option<Box<Invocation>>>>,
    pub(super) property_writes: RefCell<Vec<Option<Box<PropertyWrite>>>>,
    pub(super) events: Arc<Vec<FlowEvent>>,
    pub(super) returns: Arc<ReturnInputs>,
    pub(super) property_producers: PropertyProducerCensus,
    pub(super) numeric_properties: Vec<bool>,
    pub(super) declared: Vec<bool>,
    pub(super) origin_table: Arc<OriginTable>,
    #[cfg(test)]
    pub(super) work: Rc<RefCell<FlowWorkStats>>,
}

#[cfg(test)]
impl Drop for FlowRules {
    fn drop(&mut self) {
        let mut work = self.work.borrow_mut();
        work.property_state_capacity = self.property_writes.get_mut().capacity();
        for write in self.property_writes.get_mut().iter().flatten() {
            if let Some(receiver) = &write.receiver {
                work.property_dynamic_records += 1;
                work.property_retained_receivers += receiver.receivers.len();
                if let Some(expr) = &receiver.expression {
                    work.property_retained_origins += expr.values.len();
                    work.property_retained_reads += expr.dependencies.len();
                }
            } else {
                work.property_fixed_records += 1;
            }
            if let Some(expr) = &write.right {
                work.property_retained_origins += expr.values.len();
                work.property_retained_reads += expr.dependencies.len();
            }
        }
    }
}

impl FlowRules {
    pub(super) fn invoke(
        &self,
        index: usize,
        callee: &FlowValueExpr,
        args: &[FlowValueExpr],
        context: &mut impl FactContext<FlowKey, FlowFact>,
    ) {
        let mut invocations = self.invocations.borrow_mut();
        let invocation = invocations[index].as_mut().expect("invocation event state");
        // Snapshot all new targets before any formal write. A write can dirty
        // the callee, but must not activate a target until the next event run.
        if invocation.callee_dirty {
            let cached = self.cached_expression(callee, context);
            #[cfg(test)]
            {
                self.work.borrow_mut().callee_evaluations += 1;
            }
            let added = invocation.callee.as_ref().map_or_else(
                || cached.values.clone(),
                |previous| cached.values.difference(&previous.values),
            );
            for value in &added {
                if let Value::Alloc(Kind::Function, function) = value
                    && invocation.targets.insert(function)
                {
                    invocation.ready.insert(function);
                    #[cfg(test)]
                    {
                        self.work.borrow_mut().target_discoveries += 1;
                    }
                }
            }
            invocation.callee = Some(cached);
            invocation.callee_dirty = false;
        }
        let mut pending = BTreeSet::new();
        for argument in &invocation.dirty {
            pending.extend(&invocation.consumers[*argument]);
        }
        for function in std::mem::take(&mut invocation.ready) {
            let params = match context.read(FlowKey::ReadyParameters(function)).cloned() {
                Some(FlowFact::ReadyParameters(params)) => params,
                Some(_) => unreachable!("ready parameter key contains metadata"),
                None => {
                    invocation.waiting.insert(function);
                    continue;
                }
            };
            for (argument, param) in params.into_iter().take(args.len()).enumerate() {
                let Some(parameter) = param else {
                    continue;
                };
                let edge = ArgumentEdge { function, argument };
                invocation.edges.insert(
                    edge,
                    FormalEdge {
                        parameter,
                        delivered: Values::empty(self.origin_table.clone()),
                    },
                );
                invocation.consumers[argument].insert(edge);
                pending.insert(edge);
                #[cfg(test)]
                {
                    self.work.borrow_mut().activated_edges += 1;
                }
            }
        }
        while let Some(edge) = pending.pop_first() {
            let argument = edge.argument;
            if invocation.arguments[argument].is_none() || invocation.dirty.remove(&argument) {
                let cached = self.cached_expression(&args[argument], context);
                #[cfg(test)]
                {
                    self.work.borrow_mut().argument_evaluations += 1;
                }
                if let Some(previous) = &invocation.arguments[argument] {
                    for dependency in previous.dependencies.difference(&cached.dependencies) {
                        let readers = invocation
                            .readers
                            .get_mut(dependency)
                            .expect("indexed argument read");
                        readers.remove(&argument);
                        if readers.is_empty() {
                            invocation.readers.remove(dependency);
                        }
                    }
                }
                for dependency in &cached.dependencies {
                    invocation
                        .readers
                        .entry(*dependency)
                        .or_default()
                        .insert(argument);
                }
                invocation.arguments[argument] = Some(cached);
            }
            let values = &invocation.arguments[argument]
                .as_ref()
                .expect("ordered argument evaluated")
                .values;
            let formal = invocation.edges.get_mut(&edge).expect("activated edge");
            #[cfg(test)]
            {
                let mut work = self.work.borrow_mut();
                work.edge_visits += 1;
                work.difference_inputs += values.len();
            }
            let parameter = formal.parameter;
            // Keep only the compact Values frontier in the edge index; the
            // generic family fact wrapper is needed just during delivery.
            let mut delivered = FlowFact::Values(formal.delivered.clone());
            let change = context.propose_delta(
                FlowKey::Binding(parameter),
                FlowFact::Values(values.clone()),
                &mut delivered,
            );
            let FlowFact::Values(delivered) = delivered else {
                unreachable!("value delta frontier")
            };
            formal.delivered = delivered;
            if !values.is_empty() {
                context.propose(FlowKey::ValueFlowActive, FlowFact::ValueFlowActive);
            }
            if change.value {
                let key = FlowKey::Binding(parameter);
                if invocation
                    .callee
                    .as_ref()
                    .is_some_and(|callee| callee.dependencies.contains(&key))
                {
                    invocation.callee_dirty = true;
                }
                if let Some(readers) = invocation.readers.get(&key) {
                    for argument in readers {
                        invocation.dirty.insert(*argument);
                        // Already-passed edges retain the next-round wakeup;
                        // only later targets/arguments observe this write now.
                        pending.extend(
                            invocation.consumers[*argument].range((
                                std::ops::Bound::Excluded(edge),
                                std::ops::Bound::Unbounded,
                            )),
                        );
                    }
                }
            }
        }
    }

    pub(super) fn values_of_expr_tracking(
        &self,
        expr: &FlowValueExpr,
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
        dependencies: Option<&mut FxHashSet<FlowKey>>,
    ) -> Values {
        let mut unknowns = self
            .metrics
            .enabled()
            .then(work_metrics::SourceUnknownCounts::default);
        let mut dependencies = dependencies;
        let mut values = ValuesBuilder::new(self.origin_table.clone());
        append_value_expr(
            expr,
            &mut values,
            &self.declared,
            &self.returns,
            &mut |key| {
                if let FlowKey::Property(object, property) = key {
                    if !self.property_producers.may_produce(object, property) {
                        #[cfg(test)]
                        {
                            self.work.borrow_mut().property_impossible_reads_skipped += 1;
                        }
                        // Keep the existing Top result for missing properties,
                        // but avoid a watcher and cached dependency edge that no
                        // event in this completed source census can ever change.
                        return None;
                    }
                    #[cfg(test)]
                    {
                        self.work.borrow_mut().property_possible_reads_tracked += 1;
                    }
                }
                #[cfg(test)]
                if matches!(key, FlowKey::NumericProperties(_)) {
                    self.work.borrow_mut().numeric_property_reads += 1;
                }
                if let Some(dependencies) = dependencies.as_deref_mut() {
                    let inserted = dependencies.insert(key);
                    #[cfg(test)]
                    {
                        let mut work = self.work.borrow_mut();
                        work.expression_dependency_read_occurrences += 1;
                        if inserted {
                            work.expression_dependency_unique_keys += 1;
                        }
                    }
                    #[cfg(not(test))]
                    let _ = inserted;
                }
                match context.read(key) {
                    Some(FlowFact::Values(values)) => Some(values.clone()),
                    Some(_) => {
                        unreachable!("value expressions cannot read parameter metadata")
                    }
                    None => None,
                }
            },
            &mut |cause| {
                if let Some(unknowns) = &mut unknowns {
                    unknowns.record(cause);
                }
            },
        );
        if let Some(unknowns) = &unknowns {
            self.metrics.source_unknowns(unknowns);
        }
        values.finish()
    }

    pub(super) fn cached_expression(
        &self,
        expr: &FlowValueExpr,
        context: &mut impl FactContext<FlowKey, FlowFact>,
    ) -> CachedExpression {
        let mut dependencies = FxHashSet::default();
        let values = self.values_of_expr_tracking(expr, context, Some(&mut dependencies));
        CachedExpression {
            values,
            dependencies: dependencies.into_iter().collect(),
        }
    }

    pub(super) fn propose_values(
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
        key: FlowKey,
        values: Values,
    ) -> bool {
        if values.is_empty() {
            return false;
        }
        let changed = context.propose(key, FlowFact::Values(values));
        context.propose(FlowKey::ValueFlowActive, FlowFact::ValueFlowActive);
        changed.value
    }

    pub(super) fn write_property(
        &self,
        index: usize,
        object: PropertyTarget<'_>,
        property: PropertySlot,
        right: &FlowValueExpr,
        context: &mut impl FactContext<FlowKey, FlowFact>,
    ) {
        // Primitive-only RHS values carry no may-origin and can never grow.
        if matches!(right, FlowValueExpr::Empty) {
            return;
        }
        let mut writes = self.property_writes.borrow_mut();
        let Some(write) = writes[index].as_mut() else {
            let PropertyTarget::Fixed(object) = object else {
                unreachable!("dynamic write has state")
            };
            let values = self.values_of_expr_tracking(right, context, None);
            #[cfg(test)]
            {
                self.work.borrow_mut().property_direct_evaluations += 1;
            }
            if !values.is_empty() {
                self.propose_property(context, object, property, &values);
                context.propose(FlowKey::ValueFlowActive, FlowFact::ValueFlowActive);
                #[cfg(test)]
                {
                    self.work.borrow_mut().property_join_inputs +=
                        self.property_fact_count(property) * values.len();
                }
            }
            return;
        };
        // Preserve RHS-before-receiver evaluation and evaluate both before any
        // write. Self-dependent expressions therefore refresh on the next run.
        let right_grew = if write.right.is_none() || std::mem::take(&mut write.right_dirty) {
            let cached = self.cached_expression(right, context);
            #[cfg(test)]
            {
                self.work.borrow_mut().property_rhs_evaluations += 1;
            }
            let grew = write.right.as_ref().map_or_else(
                || !cached.values.is_empty(),
                |previous| {
                    let unknown_added = cached.values.unknown && !previous.values.unknown;
                    if unknown_added || Arc::ptr_eq(&cached.values.bitmap, &previous.values.bitmap)
                    {
                        return unknown_added;
                    }
                    #[cfg(test)]
                    {
                        let mut work = self.work.borrow_mut();
                        work.property_subset_checks += 1;
                        work.property_comparison_inputs +=
                            cached.values.len() + previous.values.len();
                    }
                    !cached.values.bitmap.is_subset(&previous.values.bitmap)
                },
            );
            write.right = Some(cached);
            grew
        } else {
            false
        };
        let current = &write.right.as_ref().expect("ordered RHS evaluated").values;
        #[cfg(test)]
        let current_count = current.len();
        let PropertyTarget::Expression(object) = object else {
            let PropertyTarget::Fixed(object) = object else {
                unreachable!()
            };
            if right_grew {
                self.propose_property(context, object, property, current);
                context.propose(FlowKey::ValueFlowActive, FlowFact::ValueFlowActive);
                #[cfg(test)]
                {
                    self.work.borrow_mut().property_join_inputs +=
                        self.property_fact_count(property) * current_count;
                }
            }
            return;
        };
        let receivers = write.receiver.as_mut().expect("dynamic receiver state");
        let mut new_receivers = Vec::new();
        if receivers.expression.is_none() || std::mem::take(&mut receivers.dirty) {
            let cached = self.cached_expression(object, context);
            #[cfg(test)]
            {
                let mut work = self.work.borrow_mut();
                work.property_receiver_evaluations += 1;
                if let Some(previous) = &receivers.expression {
                    work.property_subtractions += 1;
                    work.property_comparison_inputs += cached.values.len() + previous.values.len();
                }
            }
            let added = receivers.expression.as_ref().map_or_else(
                || cached.values.clone(),
                |previous| cached.values.difference(&previous.values),
            );
            for origin in &added {
                #[cfg(test)]
                {
                    self.work.borrow_mut().property_receiver_scans += 1;
                }
                if let Value::Alloc(Kind::Object, receiver) | Value::Alloc(Kind::Set, receiver) =
                    origin
                    && !receivers.receivers.contains(&receiver)
                {
                    new_receivers.push(receiver);
                }
            }
            receivers.expression = Some(cached);
        }
        // Full-current proposals let every subset destination adopt the same
        // immutable bitmap. Delta joins would COW-clone the old shared bitmap
        // separately for every receiver, retaining receiver-by-origin payloads.
        if right_grew {
            for receiver in &receivers.receivers {
                self.propose_property(context, *receiver, property, current);
                #[cfg(test)]
                {
                    self.work.borrow_mut().property_join_inputs +=
                        self.property_fact_count(property) * current_count;
                }
            }
        }
        for receiver in new_receivers {
            receivers.receivers.insert(receiver);
            #[cfg(test)]
            {
                self.work.borrow_mut().property_receiver_activations += 1;
            }
            if !current.is_empty() {
                self.propose_property(context, receiver, property, current);
                #[cfg(test)]
                {
                    self.work.borrow_mut().property_join_inputs +=
                        self.property_fact_count(property) * current_count;
                }
            }
        }
        // Watchers are cumulative and SOURCE_ONLY carries no proof premises:
        // cached reads need neither replay nor per-receiver proof frontiers.
        if !receivers.receivers.is_empty() && !current.is_empty() {
            context.propose(FlowKey::ValueFlowActive, FlowFact::ValueFlowActive);
        }
    }

    pub(super) fn propose_property(
        &self,
        context: &mut impl FactContext<FlowKey, FlowFact>,
        object: u32,
        property: PropertySlot,
        values: &Values,
    ) {
        #[cfg(test)]
        {
            let mut work = self.work.borrow_mut();
            work.property_receiver_visits += 1;
            work.property_proposals += self.property_fact_count(property);
            work.numeric_property_proposals +=
                usize::from(self.numeric_properties[property.0 as usize]);
        }
        context.propose(
            FlowKey::Property(object, property),
            FlowFact::Values(values.clone()),
        );
        context.propose(
            FlowKey::Properties(object),
            FlowFact::Values(values.clone()),
        );
        if self.numeric_properties[property.0 as usize] {
            context.propose(
                FlowKey::NumericProperties(object),
                FlowFact::Values(values.clone()),
            );
        }
    }

    #[cfg(test)]
    pub(super) fn property_fact_count(&self, property: PropertySlot) -> usize {
        2 + usize::from(self.numeric_properties[property.0 as usize])
    }

    pub(super) fn singleton_value(&self, value: Value) -> Values {
        Values::singleton(self.origin_table.clone(), value)
    }
}

impl IterativeRules for FlowRules {
    type Key = FlowKey;
    type Fact = FlowFact;
    type Rule = usize;
    type MergeScratch = FlowMergeScratch;
    const SOURCE_ONLY: bool = true;
    const EVALUATES_CHANGES: bool = true;

    fn new_merge_scratch(&self) -> Self::MergeScratch {
        #[cfg(test)]
        {
            FlowMergeScratch::with_work(self.work.clone())
        }
        #[cfg(not(test))]
        {
            FlowMergeScratch::default()
        }
    }

    fn metric_key_family(key: &FlowKey) -> work_metrics::KeyFamily {
        use work_metrics::KeyFamily;
        match key {
            FlowKey::ValueFlowActive => KeyFamily::Active,
            FlowKey::Binding(_) => KeyFamily::Binding,
            FlowKey::CandidateBinding(_) => KeyFamily::CandidateBinding,
            FlowKey::CandidateReachable(_) => KeyFamily::CandidateReachable,
            FlowKey::Property(_, _) => KeyFamily::Property,
            FlowKey::Properties(_) | FlowKey::NumericProperties(_) => KeyFamily::Properties,
            FlowKey::Return(_) => KeyFamily::Return,
            FlowKey::Parameters(_) => KeyFamily::Parameters,
            FlowKey::ReadyParameters(_) => KeyFamily::ReadyParameters,
            FlowKey::InvokedCells(_) => KeyFamily::InvokedCells,
            FlowKey::RetainedCells(_) => KeyFamily::RetainedCells,
            FlowKey::SourceCells(_) => KeyFamily::SourceCells,
            FlowKey::Initialization(_) => KeyFamily::Initialization,
            FlowKey::ReadOnlyCallback(_) => KeyFamily::ReadOnlyCallback,
        }
    }

    fn metric_event_kind(&self, rule: &usize) -> work_metrics::EventKind {
        use work_metrics::EventKind;
        match &self.events[*rule] {
            FlowEvent::Initialization { .. } => EventKind::Initialization,
            FlowEvent::FunctionDeclaration { .. } => EventKind::FunctionDeclaration,
            FlowEvent::ClassDeclaration { .. } => EventKind::ClassDeclaration,
            FlowEvent::Variable { .. } => EventKind::Variable,
            FlowEvent::AssignBinding { .. } => EventKind::AssignBinding,
            FlowEvent::AssignProperty { .. } => EventKind::AssignProperty,
            FlowEvent::WritePropertyExpr { .. } => EventKind::WritePropertyExpr,
            FlowEvent::WritePropertyBinding { .. } => EventKind::WritePropertyBinding,
            FlowEvent::WritePropertyValue { .. } => EventKind::WritePropertyValue,
            FlowEvent::FunctionMetadata { .. } => EventKind::FunctionMetadata,
            FlowEvent::Return { .. } => EventKind::Return,
            FlowEvent::Invoke { .. } => EventKind::Invoke,
        }
    }

    fn merge_observed(
        key: &FlowKey,
        current: &mut FlowFact,
        contribution: FlowFact,
        metrics: &mut work_metrics::Batch,
    ) -> bool {
        match (current, contribution) {
            (FlowFact::Values(current), FlowFact::Values(contribution))
                if matches!(
                    key,
                    FlowKey::Binding(_)
                        | FlowKey::Property(_, _)
                        | FlowKey::Properties(_)
                        | FlowKey::NumericProperties(_)
                        | FlowKey::Return(_)
                ) =>
            {
                assert!(Arc::ptr_eq(&current.origins, &contribution.origins));
                let bitmap_changed =
                    union_bitmap_observed(&mut current.bitmap, &contribution.bitmap, |operation| {
                        metrics.add(operation, 1)
                    });
                let unknown_changed = contribution.unknown && !current.unknown;
                current.unknown |= contribution.unknown;
                bitmap_changed || unknown_changed
            }
            (current, contribution) => Self::merge(key, current, contribution),
        }
    }

    fn merge_with_scratch(
        key: &FlowKey,
        current: &mut FlowFact,
        contribution: FlowFact,
        scratch: &mut Self::MergeScratch,
    ) -> bool {
        match (current, contribution) {
            (FlowFact::Values(current), FlowFact::Values(contribution))
                if matches!(key, FlowKey::Property(_, _) | FlowKey::Properties(_)) =>
            {
                merge_values_with_scratch(current, contribution, scratch, |_| {})
            }
            (current, contribution) => merge_flow_fact(key, current, contribution),
        }
    }

    fn merge_observed_with_scratch(
        key: &FlowKey,
        current: &mut FlowFact,
        contribution: FlowFact,
        metrics: &mut work_metrics::Batch,
        scratch: &mut Self::MergeScratch,
    ) -> bool {
        match (current, contribution) {
            (FlowFact::Values(current), FlowFact::Values(contribution))
                if matches!(key, FlowKey::Property(_, _) | FlowKey::Properties(_)) =>
            {
                merge_values_with_scratch(current, contribution, scratch, |operation| {
                    metrics.add(operation, 1)
                })
            }
            (current, contribution) => Self::merge_observed(key, current, contribution, metrics),
        }
    }

    fn evaluate_changed(
        &self,
        index: &usize,
        changed: &BTreeSet<FlowKey>,
        context: &mut impl FactContext<FlowKey, FlowFact>,
    ) {
        if matches!(self.events[*index], FlowEvent::Invoke { .. }) {
            self.invocations.borrow_mut()[*index]
                .as_mut()
                .expect("invocation event state")
                .changed(changed);
        }
        if let Some(write) = self.property_writes.borrow_mut()[*index].as_mut() {
            write.changed(changed);
        }
        self.evaluate(index, context);
    }

    fn evaluate(
        &self,
        index: &usize,
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
    ) {
        #[cfg(test)]
        {
            self.work.borrow_mut().event_executions += 1;
        }
        let event = &self.events[*index];
        match event {
            FlowEvent::Initialization { id, sites } => {
                context.propose(
                    FlowKey::Initialization(*id),
                    FlowFact::Initialization(sites.clone()),
                );
            }
            FlowEvent::FunctionDeclaration { id, origin }
            | FlowEvent::ClassDeclaration { id, origin } => {
                let values = self.singleton_value(*origin);
                Self::propose_values(context, FlowKey::Binding(*id), values);
            }
            FlowEvent::Variable { id, init } => {
                let values = self.values_of_expr_tracking(init, context, None);
                Self::propose_values(context, FlowKey::Binding(*id), values);
            }
            FlowEvent::AssignBinding { id, right } => {
                let values = self.values_of_expr_tracking(right, context, None);
                Self::propose_values(context, FlowKey::Binding(*id), values);
            }
            FlowEvent::AssignProperty {
                object,
                property,
                right,
            } => {
                self.write_property(
                    *index,
                    PropertyTarget::Expression(object),
                    *property,
                    right,
                    context,
                );
            }
            FlowEvent::WritePropertyExpr {
                object,
                property,
                value,
            } => {
                self.write_property(
                    *index,
                    PropertyTarget::Fixed(*object),
                    *property,
                    value,
                    context,
                );
            }
            FlowEvent::WritePropertyBinding {
                object,
                property,
                binding,
            } => {
                self.write_property(
                    *index,
                    PropertyTarget::Fixed(*object),
                    *property,
                    &FlowValueExpr::Binding(*binding),
                    context,
                );
            }
            FlowEvent::WritePropertyValue {
                object,
                property,
                value,
            } => {
                self.write_property(
                    *index,
                    PropertyTarget::Fixed(*object),
                    *property,
                    &FlowValueExpr::Value(*value),
                    context,
                );
            }
            FlowEvent::FunctionMetadata { function, params } => {
                context.propose(
                    FlowKey::Parameters(*function),
                    FlowFact::Parameters(params.clone()),
                );
                // The legacy solver only woke calls waiting for metadata if
                // some non-empty value fact grew in its initial source sweep.
                if context.read(FlowKey::ValueFlowActive).is_some() {
                    context.propose(
                        FlowKey::ReadyParameters(*function),
                        FlowFact::ReadyParameters(params.clone()),
                    );
                }
            }
            FlowEvent::Return { function, value } => {
                let values = self.values_of_expr_tracking(value, context, None);
                Self::propose_values(context, FlowKey::Return(*function), values);
            }
            FlowEvent::Invoke { callee, args } => {
                self.invoke(*index, callee, args, context);
            }
        }
    }

    fn difference(
        key: &Self::Key,
        contribution: &Self::Fact,
        delivered: &Self::Fact,
    ) -> Option<Self::Fact> {
        let (FlowKey::Binding(_), FlowFact::Values(values), FlowFact::Values(delivered)) =
            (key, contribution, delivered)
        else {
            unreachable!("invocation deltas deliver values to formals");
        };
        let delta = values.difference(delivered);
        (!delta.is_empty()).then_some(FlowFact::Values(delta))
    }

    #[cfg(test)]
    fn delta_atoms(delta: &Self::Fact) -> usize {
        let FlowFact::Values(values) = delta else {
            unreachable!("origin delta")
        };
        values.len()
    }

    fn merge(key: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        merge_flow_fact(key, current, contribution)
    }
}

pub(super) fn merge_flow_fact(
    key: &FlowKey,
    current: &mut FlowFact,
    contribution: FlowFact,
) -> bool {
    match (key, current, contribution) {
        (
            FlowKey::ReadOnlyCallback(_),
            FlowFact::ReadOnlyCallback(current),
            FlowFact::ReadOnlyCallback(contribution),
        ) => {
            assert_eq!(
                *current, contribution,
                "immutable callback source certificate"
            );
            false
        }
        (
            FlowKey::Initialization(_),
            FlowFact::Initialization(current),
            FlowFact::Initialization(contribution),
        ) => current.union_with(contribution),
        (
            FlowKey::InvokedCells(_) | FlowKey::RetainedCells(_) | FlowKey::SourceCells(_),
            FlowFact::Cells(current),
            FlowFact::Cells(contribution),
        ) => current.union_with(&contribution),
        (
            FlowKey::Parameters(_),
            FlowFact::Parameters(current),
            FlowFact::Parameters(contribution),
        ) => {
            assert_eq!(current, &contribution, "parameter metadata is immutable");
            false
        }
        (
            FlowKey::ReadyParameters(_),
            FlowFact::ReadyParameters(current),
            FlowFact::ReadyParameters(contribution),
        ) => {
            assert_eq!(
                current, &contribution,
                "ready parameter metadata is immutable"
            );
            false
        }
        (FlowKey::ValueFlowActive, FlowFact::ValueFlowActive, FlowFact::ValueFlowActive) => false,
        (
            FlowKey::CandidateReachable(_) | FlowKey::CandidateBinding(_),
            FlowFact::CandidateReachability(current),
            FlowFact::CandidateReachability(contribution),
        ) => union_bitmap(current, &contribution),
        (
            FlowKey::Binding(_)
            | FlowKey::Property(_, _)
            | FlowKey::Properties(_)
            | FlowKey::NumericProperties(_)
            | FlowKey::Return(_),
            FlowFact::Values(current),
            FlowFact::Values(contribution),
        ) => current.union_with(&contribution),
        _ => unreachable!("flow key and fact variants must agree"),
    }
}

#[cfg(test)]
pub(super) struct FlowPass<'a> {
    pub(super) flow: &'a mut ChunkValueFlow,
    pub(super) changed: &'a mut bool,
    pub(super) source_unit: SourceUnitOrdinal,
    pub(super) current_function: Option<u32>,
}

#[cfg(test)]
impl FlowPass<'_> {
    pub(super) fn merge_binding(&mut self, id: Id, values: Values) {
        *self.changed |= self.flow.merge_binding(id, values);
    }

    pub(super) fn invoke(&mut self, callee: &Expr, args: &[ExprOrSpread]) {
        let targets = self.flow.values_of_expr(callee);
        for target in targets {
            let Value::Alloc(Kind::Function, function) = target else {
                continue;
            };
            let Some(params) = self.flow.params.get(&function).cloned() else {
                continue;
            };
            for (param, arg) in params.into_iter().zip(args) {
                if let Some(param) = param {
                    let values = self.flow.values_of_expr(&arg.expr);
                    self.merge_binding(param, values);
                }
            }
        }
    }

    pub(super) fn visit_function_body(&mut self, function: u32, visit: impl FnOnce(&mut Self)) {
        self.flow
            .callable_source_unit
            .entry(Value::Alloc(Kind::Function, function))
            .or_insert(self.source_unit);
        let previous = self.current_function.replace(function);
        visit(self);
        self.current_function = previous;
    }
}

#[cfg(test)]
impl Visit for FlowPass<'_> {
    fn visit_fn_decl(&mut self, node: &FnDecl) {
        let key = node.function.span.lo.0;
        let value = Value::Alloc(Kind::Function, key);
        self.flow
            .canonical
            .entry(value)
            .or_insert_with(|| node.ident.to_id());
        let values = self.flow.singleton_value(value);
        self.merge_binding(node.ident.to_id(), values);
        node.function.visit_with(self);
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        let value = Value::Alloc(Kind::Class, node.class.span.lo.0);
        self.flow
            .canonical
            .entry(value)
            .or_insert_with(|| node.ident.to_id());
        self.flow
            .callable_source_unit
            .entry(value)
            .or_insert(self.source_unit);
        let values = self.flow.singleton_value(value);
        self.merge_binding(node.ident.to_id(), values);
        node.class.visit_with(self);
    }

    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let Pat::Ident(binding) = &node.name
            && let Some(init) = node.init.as_deref()
        {
            let values = self.flow.values_of_expr(init);
            if self.current_function.is_none() {
                for value in &values {
                    if matches!(
                        value,
                        Value::Alloc(Kind::Function, _) | Value::Alloc(Kind::Class, _)
                    ) && matches!(
                        init.unwrap_parens(),
                        Expr::Fn(_) | Expr::Arrow(_) | Expr::Class(_)
                    ) {
                        self.flow
                            .canonical
                            .entry(value)
                            .or_insert_with(|| binding.id.to_id());
                    }
                }
            }
            self.merge_binding(binding.id.to_id(), values);
        }
        node.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        let values = self.flow.values_of_expr(&node.right);
        match binding_targets::simple_write_target(&node.left) {
            Some(binding_targets::SimpleWriteTarget::Binding(binding)) => {
                self.merge_binding(binding, values);
            }
            Some(binding_targets::SimpleWriteTarget::Member(member)) => {
                if let Some(prop) = static_prop(&member.prop) {
                    for origin in self.flow.values_of_expr(&member.obj) {
                        if let Value::Alloc(Kind::Object, object) = origin {
                            *self.changed |=
                                self.flow
                                    .merge_property(object, prop.clone(), values.clone());
                        }
                    }
                }
            }
            None => {}
        }
        node.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, node: &ObjectLit) {
        let object = node.span.lo.0;
        for prop in &node.props {
            if let PropOrSpread::Prop(prop) = prop {
                match prop.as_ref() {
                    Prop::KeyValue(kv) => {
                        if let Some(name) = object_prop(&kv.key) {
                            let values = self.flow.values_of_expr(&kv.value);
                            *self.changed |= self.flow.merge_property(object, name, values);
                        }
                    }
                    Prop::Shorthand(ident) => {
                        let values = self.flow.values_of_binding(&ident.to_id());
                        *self.changed |=
                            self.flow
                                .merge_property(object, ident.sym.to_string(), values);
                    }
                    Prop::Method(method) => {
                        if let Some(name) = object_prop(&method.key) {
                            let values = self.flow.singleton_value(Value::Alloc(
                                Kind::Function,
                                method.function.span.lo.0,
                            ));
                            *self.changed |= self.flow.merge_property(object, name, values);
                        }
                    }
                    _ => {}
                }
            }
        }
        node.visit_children_with(self);
    }

    fn visit_array_lit(&mut self, node: &ArrayLit) {
        let object = node.span.lo.0;
        for (index, element) in node.elems.iter().enumerate() {
            if let Some(element) = element {
                let values = self.flow.values_of_expr(&element.expr);
                *self.changed |= self.flow.merge_property(object, index.to_string(), values);
            }
        }
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        let key = node.span.lo.0;
        self.flow.params.entry(key).or_insert_with(|| {
            node.params
                .iter()
                .map(|param| match &param.pat {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                })
                .collect()
        });
        self.visit_function_body(key, |s| node.visit_children_with(s));
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let key = node.span.lo.0;
        self.flow.params.entry(key).or_insert_with(|| {
            node.params
                .iter()
                .map(|param| match param {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                })
                .collect()
        });
        self.visit_function_body(key, |s| {
            if let ArrowFunctionBody::Expr(expr) = node.body.as_ref() {
                let values = s.flow.values_of_expr(expr);
                *s.changed |= s.flow.merge_return(key, values);
            }
            node.visit_children_with(s);
        });
    }

    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        if let (Some(function), Some(arg)) = (self.current_function, node.arg.as_deref()) {
            let values = self.flow.values_of_expr(arg);
            *self.changed |= self.flow.merge_return(function, values);
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee {
            self.invoke(callee, &node.args);
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        if let Some(args) = &node.args {
            self.invoke(&node.callee, args);
        }
        node.visit_children_with(self);
    }
}
