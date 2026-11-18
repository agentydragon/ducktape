//! Opt-in, bounded selected-origin producer journal for source-flow diagnosis.
//!
//! This is diagnostic-only. It records values already observed by the solver,
//! never performs additional fact reads, and reports the reverse slice of the
//! selected output bindings rather than formatting the whole fact graph.

use super::{FlowEvent, FlowFact, FlowKey, Kind, OriginId, OriginTable, Value, Values};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, VecDeque};

const MAX_JOURNAL_EDGES: usize = 300_000;
const MAX_JOURNAL_NODES: usize = 100_000;
const MAX_JOURNAL_READS: usize = 300_000;
const MAX_JOURNAL_OUTPUT_BYTES: usize = 16 * 1024 * 1024;
const MAX_SELECTOR_BYTES: usize = 4 * 1024;
const MAX_SELECTORS: usize = 32;

#[derive(Clone, Debug)]
pub(super) struct Selection {
    pub(super) bindings: Vec<BindingSelection>,
    pub(super) functions: Vec<FunctionSelection>,
}

#[derive(Clone, Debug)]
pub(super) struct BindingSelection {
    pub(super) module: String,
    pub(super) name: String,
    pub(super) span: [u32; 2],
}

#[derive(Clone, Debug)]
pub(super) struct FunctionSelection {
    pub(super) module: String,
    pub(super) offset: u32,
}

impl Selection {
    /// Bindings use `module|name|span-lo|span-hi`; functions use
    /// `module|function-span-lo`. Semicolon separates selectors in each env.
    pub(super) fn from_env() -> Result<Option<Self>, String> {
        let bindings = std::env::var("DEBUNDLE_DEBUG_ORIGIN_JOURNAL_BINDINGS").unwrap_or_default();
        let functions =
            std::env::var("DEBUNDLE_DEBUG_ORIGIN_JOURNAL_FUNCTIONS").unwrap_or_default();
        if bindings.len() + functions.len() > MAX_SELECTOR_BYTES {
            return Err(format!(
                "origin journal selectors exceed {MAX_SELECTOR_BYTES} bytes"
            ));
        }
        if bindings.is_empty() && functions.is_empty() {
            return Ok(None);
        }
        let mut parsed_bindings = Vec::new();
        for selector in bindings.split(';').filter(|part| !part.is_empty()) {
            let fields: Vec<_> = selector.split('|').collect();
            if fields.len() != 4 {
                return Err(format!(
                    "invalid binding selector field count: {selector:?}"
                ));
            }
            parsed_bindings.push(BindingSelection {
                module: fields[0].to_owned(),
                name: fields[1].to_owned(),
                span: [
                    fields[2]
                        .parse()
                        .map_err(|_| format!("invalid span start in {selector:?}"))?,
                    fields[3]
                        .parse()
                        .map_err(|_| format!("invalid span end in {selector:?}"))?,
                ],
            });
        }
        let mut parsed_functions = Vec::new();
        for selector in functions.split(';').filter(|part| !part.is_empty()) {
            let (module, offset) = selector
                .rsplit_once('|')
                .ok_or_else(|| format!("invalid function selector: {selector:?}"))?;
            parsed_functions.push(FunctionSelection {
                module: module.to_owned(),
                offset: offset
                    .parse()
                    .map_err(|_| format!("invalid function offset in {selector:?}"))?,
            });
        }
        if parsed_bindings.is_empty() || parsed_functions.is_empty() {
            return Err("both binding and function selectors are required".to_owned());
        }
        if parsed_bindings.len() + parsed_functions.len() > MAX_SELECTORS {
            return Err(format!(
                "origin journal accepts at most {MAX_SELECTORS} selectors"
            ));
        }
        Ok(Some(Self {
            bindings: parsed_bindings,
            functions: parsed_functions,
        }))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
enum InputKind {
    ObservedRead,
    CachedDependency,
    SourceLiteral,
    Unattributed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
struct Producer {
    event: usize,
    kind: &'static str,
    target: Option<u32>,
    argument: Option<usize>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
struct Edge {
    origin: OriginId,
    output: FlowKey,
    producer: Producer,
    input: Option<FlowKey>,
    input_kind: InputKind,
}

#[derive(Clone, Debug)]
pub(super) struct Target {
    pub(super) module: String,
    pub(super) name: String,
    pub(super) context: u32,
    pub(super) key: FlowKey,
}

pub(super) struct KeyLabels {
    pub(super) bindings: Vec<String>,
    pub(super) sites: Vec<(String, u32)>,
    pub(super) properties: Vec<String>,
}

pub(super) struct CachedTransfer<'a> {
    pub(super) event: usize,
    pub(super) kind: &'static str,
    pub(super) target: Option<u32>,
    pub(super) argument: Option<usize>,
    pub(super) output: FlowKey,
    pub(super) values: &'a Values,
    pub(super) dependencies: &'a BTreeSet<FlowKey>,
    pub(super) source: Option<&'a super::FlowValueExpr>,
    pub(super) table: &'a OriginTable,
}

pub(super) struct ProjectionReport<'a> {
    pub(super) target: &'a Target,
    pub(super) global: &'a Values,
    pub(super) projected: &'a Values,
    pub(super) local_origins: &'a OriginTable,
    pub(super) local_candidate_offsets: &'a [u32],
}

#[derive(Default)]
struct JournalCounts {
    omitted_edges: usize,
    omitted_nodes: usize,
    omitted_event_reads: usize,
}

pub(super) struct OriginTrace {
    origins: BTreeSet<OriginId>,
    origin_sources: BTreeMap<OriginId, (String, u32)>,
    targets: Vec<Target>,
    key_labels: KeyLabels,
    edges: RefCell<BTreeSet<Edge>>,
    nodes: RefCell<BTreeSet<FlowKey>>,
    /// Read witnesses are keyed by event so cached expressions can be traced
    /// at later uses without another FactContext read.
    event_reads: RefCell<BTreeSet<(usize, OriginId, FlowKey)>>,
    counts: RefCell<JournalCounts>,
}

impl OriginTrace {
    pub(super) fn new(
        origins: BTreeMap<OriginId, (String, u32)>,
        targets: Vec<Target>,
        key_labels: KeyLabels,
    ) -> Self {
        Self {
            origins: origins.keys().copied().collect(),
            origin_sources: origins,
            targets,
            key_labels,
            edges: RefCell::default(),
            nodes: RefCell::default(),
            event_reads: RefCell::default(),
            counts: RefCell::default(),
        }
    }

    pub(super) fn selected_origins(&self) -> &BTreeSet<OriginId> {
        &self.origins
    }

    pub(super) fn target_for_key(&self, key: FlowKey) -> Option<&Target> {
        self.targets.iter().find(|target| target.key == key)
    }

    pub(super) fn selected_sources_for_module(&self, module: &str) -> Vec<(OriginId, u32)> {
        self.origins
            .iter()
            .filter(|origin| {
                self.origin_sources
                    .get(origin)
                    .is_some_and(|(selected_module, _)| selected_module == module)
            })
            .map(|origin| (*origin, self.origin_sources[origin].1))
            .collect()
    }

    fn contains(&self, values: &Values, origin: OriginId) -> bool {
        values.bitmap.contains(origin.0)
    }

    fn key_label(&self, key: FlowKey) -> String {
        let site = |site: u32| {
            self.key_labels
                .sites
                .get(site as usize)
                .map(|(module, offset)| format!("{module}@{offset}"))
                .unwrap_or_else(|| format!("site#{site}"))
        };
        match key {
            FlowKey::Binding(slot) => self
                .key_labels
                .bindings
                .get(slot.0 as usize)
                .cloned()
                .unwrap_or_else(|| format!("binding#{}", slot.0)),
            FlowKey::Property(object, property) => format!(
                "{}.{}",
                site(object),
                self.key_labels
                    .properties
                    .get(property.0 as usize)
                    .map(String::as_str)
                    .unwrap_or("<property>")
            ),
            FlowKey::Properties(object) => format!("{}.*", site(object)),
            FlowKey::NumericProperties(object) => format!("{}.[numeric]", site(object)),
            FlowKey::Return(function) => format!("{}.[[return]]", site(function)),
            FlowKey::CandidateBinding(slot) => format!(
                "candidate:{}",
                self.key_labels
                    .bindings
                    .get(slot.0 as usize)
                    .map(String::as_str)
                    .unwrap_or("<binding>")
            ),
            FlowKey::CandidateReachable(origin) => format!("summary-origin#{}", origin.0),
            FlowKey::ReadOnlyCallback(function) => {
                format!("{}.[[read-only-callback]]", site(function))
            }
            FlowKey::Parameters(function) | FlowKey::ReadyParameters(function) => {
                format!("{}.[[parameters]]", site(function))
            }
            FlowKey::Initialization(binding) => format!(
                "{}.[[initialization]]",
                self.key_labels
                    .bindings
                    .get(binding.0 as usize)
                    .map(String::as_str)
                    .unwrap_or("<binding>")
            ),
            FlowKey::ValueFlowActive => "value-flow-active".to_owned(),
            FlowKey::InvokedCells(function) => format!("{}.[[invoked-cells]]", site(function)),
            FlowKey::RetainedCells(function) => format!("{}.[[retained-cells]]", site(function)),
            FlowKey::SourceCells(function) => format!("{}.[[source-cells]]", site(function)),
        }
    }

    fn note_read(&self, event: usize, key: FlowKey, values: &Values) {
        for origin in &self.origins {
            if !self.contains(values, *origin) {
                continue;
            }
            let mut reads = self.event_reads.borrow_mut();
            let item = (event, *origin, key);
            if reads.contains(&item) {
                continue;
            }
            if reads.len() >= MAX_JOURNAL_READS {
                self.counts.borrow_mut().omitted_event_reads += 1;
            } else {
                reads.insert(item);
            }
        }
    }

    fn observed_for(&self, event: usize, origin: OriginId, key: FlowKey) -> bool {
        self.event_reads.borrow().contains(&(event, origin, key))
    }

    fn insert_edge(&self, edge: Edge) {
        let mut edges = self.edges.borrow_mut();
        if edges.contains(&edge) {
            return;
        }
        if edges.len() >= MAX_JOURNAL_EDGES {
            self.counts.borrow_mut().omitted_edges += 1;
            return;
        }
        let mut nodes = self.nodes.borrow_mut();
        for key in [Some(edge.output), edge.input].into_iter().flatten() {
            if nodes.contains(&key) {
                continue;
            }
            if nodes.len() >= MAX_JOURNAL_NODES {
                self.counts.borrow_mut().omitted_nodes += 1;
                return;
            }
            nodes.insert(key);
        }
        edges.insert(edge);
    }

    pub(super) fn record_event_contribution(
        &self,
        event: usize,
        source: &FlowEvent,
        output: FlowKey,
        contribution: &Values,
        reads: &BTreeSet<(OriginId, FlowKey)>,
        table: &OriginTable,
    ) {
        if matches!(
            source,
            FlowEvent::Invoke { .. }
                | FlowEvent::AssignProperty { .. }
                | FlowEvent::WritePropertyExpr { .. }
                | FlowEvent::WritePropertyBinding { .. }
                | FlowEvent::WritePropertyValue { .. }
        ) {
            // Invocation and property-write values may be reused from cached
            // expressions; their exact dependencies are recorded at transfer.
            return;
        }
        let producer = Producer {
            event,
            kind: event_kind(source),
            target: None,
            argument: None,
        };
        for origin in &self.origins {
            if !self.contains(contribution, *origin) {
                continue;
            }
            let mut matched = false;
            for (read_origin, input) in reads {
                if read_origin == origin {
                    matched = true;
                    self.insert_edge(Edge {
                        origin: *origin,
                        output,
                        producer,
                        input: Some(*input),
                        input_kind: InputKind::ObservedRead,
                    });
                }
            }
            if event_contains_origin(source, *origin, table) {
                self.insert_edge(Edge {
                    origin: *origin,
                    output,
                    producer,
                    input: None,
                    input_kind: InputKind::SourceLiteral,
                });
            } else if !matched {
                self.insert_edge(Edge {
                    origin: *origin,
                    output,
                    producer,
                    input: None,
                    input_kind: InputKind::Unattributed,
                });
            }
        }
    }

    pub(super) fn record_cached_transfer(&self, transfer: CachedTransfer<'_>) {
        let CachedTransfer {
            event,
            kind,
            target,
            argument,
            output,
            values,
            dependencies,
            source,
            table,
        } = transfer;
        let producer = Producer {
            event,
            kind,
            target,
            argument,
        };
        for origin in &self.origins {
            if !self.contains(values, *origin) {
                continue;
            }
            let matching: Vec<_> = dependencies
                .iter()
                .copied()
                .filter(|key| self.observed_for(event, *origin, *key))
                .collect();
            if !matching.is_empty() {
                for input in matching {
                    self.insert_edge(Edge {
                        origin: *origin,
                        output,
                        producer,
                        input: Some(input),
                        input_kind: InputKind::ObservedRead,
                    });
                }
            } else if !dependencies.is_empty() {
                // Cache state can outlive the evaluation that filled it. Keep
                // the declared dependency path, but label it as unconfirmed.
                for input in dependencies {
                    self.insert_edge(Edge {
                        origin: *origin,
                        output,
                        producer,
                        input: Some(*input),
                        input_kind: InputKind::CachedDependency,
                    });
                }
            } else {
                self.insert_edge(Edge {
                    origin: *origin,
                    output,
                    producer,
                    input: None,
                    input_kind: if source
                        .is_some_and(|expr| expression_contains_origin(expr, *origin, table))
                    {
                        InputKind::SourceLiteral
                    } else {
                        InputKind::Unattributed
                    },
                });
            }
        }
    }

    pub(super) fn report_global(&self, facts: &BTreeMap<FlowKey, FlowFact>) {
        for target in &self.targets {
            let fact = facts.get(&target.key);
            let values = fact.and_then(|fact| match fact {
                FlowFact::Values(values) => Some(values),
                _ => None,
            });
            let known = values.map_or(0, |values| values.bitmap.len());
            let unknown = values.is_some_and(|values| values.unknown);
            let origins: Vec<_> = self
                .origins
                .iter()
                .filter(|origin| values.is_some_and(|values| self.contains(values, **origin)))
                .copied()
                .collect();
            eprintln!(
                "origin-journal global module={} binding={:?} context={} key={:?} key_label={:?} present={} known={} unknown={} selected_origins={:?}",
                target.module,
                target.name,
                target.context,
                target.key,
                self.key_label(target.key),
                values.is_some(),
                known,
                unknown,
                origins
                    .iter()
                    .map(|origin| (origin.0, &self.origin_sources[origin]))
                    .collect::<Vec<_>>(),
            );
        }
        self.report_slice(facts);
    }

    pub(super) fn report_projection(&self, report: ProjectionReport<'_>) {
        let ProjectionReport {
            target,
            global,
            projected,
            local_origins,
            local_candidate_offsets,
        } = report;
        let global_selected: Vec<_> = self
            .origins
            .iter()
            .filter(|origin| self.contains(global, **origin))
            .map(|origin| self.origin_sources[origin].clone())
            .collect();
        let projected_selected: Vec<_> = self
            .origins
            .iter()
            .map(|origin| {
                let (module, offset) = &self.origin_sources[origin];
                let local_membership = (module == &target.module).then(|| {
                    local_origins
                        .ids
                        .get(&Value::Alloc(Kind::Function, *offset))
                        .is_some_and(|local| projected.bitmap.contains(local.0))
                });
                ((module, offset), local_membership)
            })
            .collect();
        eprintln!(
            "origin-journal projection module={} binding={:?} context={} global_selected={:?} projected_same_module_selected={:?} global_unknown={} projected_unknown={} global_known={} projected_known={} selected_function_candidate_slots={} selected_function_candidate_offsets={:?} candidate_closure_provenance=not_traced",
            target.module,
            target.name,
            target.context,
            global_selected,
            projected_selected,
            global.unknown,
            projected.unknown,
            global.bitmap.len(),
            projected.bitmap.len(),
            local_candidate_offsets.len(),
            local_candidate_offsets,
        );
    }

    fn report_slice(&self, facts: &BTreeMap<FlowKey, FlowFact>) {
        let edges = self.edges.borrow();
        let mut by_output: BTreeMap<(OriginId, FlowKey), Vec<Edge>> = BTreeMap::new();
        for edge in edges.iter().copied() {
            by_output
                .entry((edge.origin, edge.output))
                .or_default()
                .push(edge);
        }
        let mut pending = VecDeque::new();
        for target in &self.targets {
            let Some(FlowFact::Values(values)) = facts.get(&target.key) else {
                continue;
            };
            for origin in &self.origins {
                if self.contains(values, *origin) {
                    pending.push_back((*origin, target.key));
                }
            }
        }
        let mut seen = BTreeSet::new();
        let mut selected = BTreeSet::new();
        while let Some((origin, output)) = pending.pop_front() {
            if !seen.insert((origin, output)) {
                continue;
            }
            if let Some(producers) = by_output.get(&(origin, output)) {
                for edge in producers {
                    selected.insert(*edge);
                    if let Some(input) = edge.input {
                        pending.push_back((origin, input));
                    }
                }
            }
        }
        let counts = self.counts.borrow();
        eprintln!(
            "origin-journal totals targets={} origins={} recorded_edges={} reverse_slice_edges={} reverse_slice_nodes={} omitted_edges={} omitted_nodes={} omitted_event_reads={} edge_semantics=may_dependency_not_causation hard_caps=edges:{MAX_JOURNAL_EDGES},nodes:{MAX_JOURNAL_NODES},reads:{MAX_JOURNAL_READS},output_bytes:{MAX_JOURNAL_OUTPUT_BYTES} candidate_closure_provenance=omitted",
            self.targets.len(),
            self.origins.len(),
            edges.len(),
            selected.len(),
            seen.len(),
            counts.omitted_edges,
            counts.omitted_nodes,
            counts.omitted_event_reads,
        );
        let mut output_bytes = 0usize;
        let mut omitted_output = 0usize;
        for edge in selected {
            let line = format!(
                "origin-journal edge origin={:?} source={:?} output={:?} output_label={:?} event={} producer={} target={:?} argument={:?} input={:?} input_label={:?} input_kind={:?}\n",
                edge.origin,
                self.origin_sources.get(&edge.origin),
                edge.output,
                self.key_label(edge.output),
                edge.producer.event,
                edge.producer.kind,
                edge.producer.target,
                edge.producer.argument,
                edge.input,
                edge.input.map(|key| self.key_label(key)),
                edge.input_kind,
            );
            if output_bytes + line.len() > MAX_JOURNAL_OUTPUT_BYTES {
                omitted_output += 1;
                continue;
            }
            output_bytes += line.len();
            eprint!("{line}");
        }
        if omitted_output > 0 {
            eprintln!(
                "origin-journal output-truncated omitted_lines={omitted_output} byte_cap={MAX_JOURNAL_OUTPUT_BYTES}"
            );
        }
    }
}

pub(super) struct TracedContext<'a, C> {
    inner: &'a mut C,
    trace: Option<&'a OriginTrace>,
    event: usize,
    source: &'a FlowEvent,
    reads: BTreeSet<(OriginId, FlowKey)>,
    table: &'a OriginTable,
}

impl<'a, C> TracedContext<'a, C> {
    pub(super) fn new(
        inner: &'a mut C,
        trace: Option<&'a OriginTrace>,
        event: usize,
        source: &'a FlowEvent,
        table: &'a OriginTable,
    ) -> Self {
        Self {
            inner,
            trace,
            event,
            source,
            reads: BTreeSet::new(),
            table,
        }
    }

    fn record(&self, key: FlowKey, contribution: &FlowFact) {
        let Some(trace) = self.trace else {
            return;
        };
        let FlowFact::Values(values) = contribution else {
            return;
        };
        trace.record_event_contribution(
            self.event,
            self.source,
            key,
            values,
            &self.reads,
            self.table,
        );
    }
}

impl<C: crate::inference::FactContext<FlowKey, FlowFact>>
    crate::inference::FactContext<FlowKey, FlowFact> for TracedContext<'_, C>
{
    fn read(&mut self, key: FlowKey) -> Option<&FlowFact> {
        let fact = self.inner.read(key);
        if let (Some(trace), Some(FlowFact::Values(values))) = (self.trace, fact) {
            for origin in trace.selected_origins() {
                if trace.contains(values, *origin) {
                    self.reads.insert((*origin, key));
                    trace.note_read(self.event, key, values);
                }
            }
        }
        fact
    }

    fn propose(&mut self, key: FlowKey, contribution: FlowFact) -> crate::inference::FactChange {
        self.record(key, &contribution);
        self.inner.propose(key, contribution)
    }

    fn propose_delta(
        &mut self,
        key: FlowKey,
        contribution: FlowFact,
        delivered: &mut FlowFact,
    ) -> crate::inference::FactChange {
        self.record(key, &contribution);
        self.inner.propose_delta(key, contribution, delivered)
    }

    fn assume(&mut self, dependencies: &crate::analysis_inputs::ProofDependencies) {
        self.inner.assume(dependencies);
    }

    fn read_final(&mut self, key: FlowKey) -> Option<&FlowFact> {
        self.inner.read_final(key)
    }

    fn take_final(&mut self, key: FlowKey) -> Option<FlowFact> {
        self.inner.take_final(key)
    }
}

pub(super) fn event_kind(event: &FlowEvent) -> &'static str {
    match event {
        FlowEvent::Initialization { .. } => "initialization",
        FlowEvent::FunctionDeclaration { .. } => "function_declaration",
        FlowEvent::ClassDeclaration { .. } => "class_declaration",
        FlowEvent::Variable { .. } => "variable",
        FlowEvent::AssignBinding { .. } => "assign_binding",
        FlowEvent::AssignProperty { .. } => "assign_property",
        FlowEvent::WritePropertyExpr { .. } => "write_property_expr",
        FlowEvent::WritePropertyBinding { .. } => "write_property_binding",
        FlowEvent::WritePropertyValue { .. } => "write_property_value",
        FlowEvent::FunctionMetadata { .. } => "function_metadata",
        FlowEvent::Return { .. } => "return",
        FlowEvent::Invoke { .. } => "invoke",
    }
}

pub(super) fn expression_contains_origin(
    expr: &super::FlowValueExpr,
    selected: OriginId,
    table: &OriginTable,
) -> bool {
    let mut pending = vec![expr];
    while let Some(expr) = pending.pop() {
        match expr {
            super::FlowValueExpr::Value(value) => {
                if table.id(*value) == selected {
                    return true;
                }
            }
            super::FlowValueExpr::Union(left, right) => {
                pending.push(left);
                pending.push(right);
            }
            super::FlowValueExpr::Member { .. }
            | super::FlowValueExpr::NumericMember { .. }
            | super::FlowValueExpr::Call { .. } => {}
            super::FlowValueExpr::Binding(_)
            | super::FlowValueExpr::Empty
            | super::FlowValueExpr::Unknown(_) => {}
        }
    }
    false
}

fn event_contains_origin(event: &FlowEvent, selected: OriginId, table: &OriginTable) -> bool {
    match event {
        FlowEvent::FunctionDeclaration { origin, .. }
        | FlowEvent::ClassDeclaration { origin, .. } => table.id(*origin) == selected,
        FlowEvent::Variable { init, .. }
        | FlowEvent::AssignBinding { right: init, .. }
        | FlowEvent::Return { value: init, .. } => {
            expression_contains_origin(init, selected, table)
        }
        FlowEvent::AssignProperty { right, .. } => {
            expression_contains_origin(right, selected, table)
        }
        FlowEvent::WritePropertyExpr { value, .. } => {
            expression_contains_origin(value, selected, table)
        }
        FlowEvent::WritePropertyBinding { .. } => false,
        FlowEvent::WritePropertyValue { value, .. } => table.id(*value) == selected,
        FlowEvent::Invoke { callee, args } => {
            expression_contains_origin(callee, selected, table)
                || args
                    .iter()
                    .any(|arg| expression_contains_origin(arg, selected, table))
        }
        FlowEvent::Initialization { .. } | FlowEvent::FunctionMetadata { .. } => false,
    }
}
