//! Opt-in, bounded selected-origin producer journal for source-flow diagnosis.
//!
//! This is diagnostic-only. It records values already observed by the solver,
//! never performs additional fact reads, and reports the reverse slice of the
//! selected output bindings rather than formatting the whole fact graph.

use super::{FlowEvent, FlowFact, FlowKey, Kind, OriginId, OriginTable, Value, Values};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};

const MAX_JOURNAL_GROUPS: usize = 250_000;
const MAX_JOURNAL_INPUTS: usize = 400_000;
const MAX_JOURNAL_OUTPUTS: usize = 400_000;
const MAX_JOURNAL_READS: usize = 300_000;
const MAX_JOURNAL_OUTPUT_BYTES: usize = 16 * 1024 * 1024;
const MAX_SLICE_NODES: usize = 100_000;
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

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Ord, PartialOrd)]
enum InputKind {
    ObservedRead,
    CachedDependency,
    SourceLiteral,
    Unattributed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Ord, PartialOrd)]
struct TransferKey {
    origin: OriginId,
    event: usize,
    kind: &'static str,
    argument: Option<usize>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Ord, PartialOrd)]
struct InputWitness {
    key: Option<FlowKey>,
    input_kind: InputKind,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Ord, PartialOrd)]
struct OutputWitness {
    key: FlowKey,
    target: Option<u32>,
}

#[derive(Default)]
struct TransferGroup {
    inputs: HashSet<InputWitness>,
    outputs: HashSet<OutputWitness>,
}

#[derive(Default)]
struct JournalCounts {
    groups: usize,
    inputs: usize,
    outputs: usize,
    event_reads: usize,
    truncated_at: Option<&'static str>,
}

#[derive(Default)]
struct JournalState {
    groups: HashMap<TransferKey, TransferGroup>,
    event_reads: HashSet<(usize, OriginId, FlowKey)>,
    counts: JournalCounts,
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

pub(super) struct CachedInputs<'a> {
    pub(super) event: usize,
    pub(super) kind: &'static str,
    pub(super) argument: Option<usize>,
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

pub(super) struct OriginTrace {
    origins: BTreeSet<OriginId>,
    origin_sources: BTreeMap<OriginId, (String, u32)>,
    targets: Vec<Target>,
    key_labels: KeyLabels,
    /// Inputs and outputs are separate sets: a group's union is a may-dependency
    /// relation, not a claim that every input carried every output.
    state: RefCell<JournalState>,
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
            state: RefCell::default(),
        }
    }

    pub(super) fn selected_origins(&self) -> &BTreeSet<OriginId> {
        &self.origins
    }

    pub(super) fn is_recording(&self) -> bool {
        self.state.borrow().counts.truncated_at.is_none()
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

    fn site_label(&self, site: u32) -> String {
        self.key_labels
            .sites
            .get(site as usize)
            .map(|(module, offset)| format!("{module}@{offset}"))
            .unwrap_or_else(|| format!("site#{site}"))
    }

    fn key_label(&self, key: FlowKey) -> String {
        let site = |site| self.site_label(site);
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

    fn ensure_group(state: &mut JournalState, key: TransferKey) -> bool {
        if state.counts.truncated_at.is_some() {
            return false;
        }
        if !state.groups.contains_key(&key) {
            if state.counts.groups >= MAX_JOURNAL_GROUPS {
                state.counts.truncated_at = Some("groups");
                return false;
            }
            state.groups.insert(key, TransferGroup::default());
            state.counts.groups += 1;
        }
        true
    }

    fn add_input(state: &mut JournalState, key: TransferKey, input: InputWitness) -> bool {
        if state.counts.truncated_at.is_some() {
            return false;
        }
        if state
            .groups
            .get(&key)
            .is_some_and(|group| group.inputs.contains(&input))
        {
            return true;
        }
        if state.counts.inputs >= MAX_JOURNAL_INPUTS {
            state.counts.truncated_at = Some("inputs");
            return false;
        }
        if !Self::ensure_group(state, key) {
            return false;
        }
        state.groups.get_mut(&key).unwrap().inputs.insert(input);
        state.counts.inputs += 1;
        true
    }

    fn add_output(state: &mut JournalState, key: TransferKey, output: OutputWitness) -> bool {
        if state.counts.truncated_at.is_some() {
            return false;
        }
        if state
            .groups
            .get(&key)
            .is_some_and(|group| group.outputs.contains(&output))
        {
            return true;
        }
        if state.counts.outputs >= MAX_JOURNAL_OUTPUTS {
            state.counts.truncated_at = Some("outputs");
            return false;
        }
        if !Self::ensure_group(state, key) {
            return false;
        }
        state.groups.get_mut(&key).unwrap().outputs.insert(output);
        state.counts.outputs += 1;
        true
    }

    fn note_read(&self, event: usize, key: FlowKey, values: &Values) {
        let mut state = self.state.borrow_mut();
        if state.counts.truncated_at.is_some() {
            return;
        }
        for origin in &self.origins {
            if !self.contains(values, *origin) {
                continue;
            }
            let read = (event, *origin, key);
            if state.event_reads.contains(&read) {
                continue;
            }
            if state.counts.event_reads >= MAX_JOURNAL_READS {
                state.counts.truncated_at = Some("event_reads");
                return;
            }
            state.event_reads.insert(read);
            state.counts.event_reads += 1;
        }
    }

    pub(super) fn record_event_contribution(
        &self,
        event: usize,
        source: &FlowEvent,
        output: FlowKey,
        contribution: &Values,
        reads: &HashSet<(OriginId, FlowKey)>,
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
        let mut state = self.state.borrow_mut();
        if state.counts.truncated_at.is_some() {
            return;
        }
        for origin in &self.origins {
            if !self.contains(contribution, *origin) {
                continue;
            }
            let transfer = TransferKey {
                origin: *origin,
                event,
                kind: event_kind(source),
                argument: None,
            };
            if !Self::ensure_group(&mut state, transfer)
                || !Self::add_output(
                    &mut state,
                    transfer,
                    OutputWitness {
                        key: output,
                        target: None,
                    },
                )
            {
                return;
            }
            let mut matched = false;
            for (read_origin, input) in reads {
                if read_origin == origin {
                    matched = true;
                    if !Self::add_input(
                        &mut state,
                        transfer,
                        InputWitness {
                            key: Some(*input),
                            input_kind: InputKind::ObservedRead,
                        },
                    ) {
                        return;
                    }
                }
            }
            if event_contains_origin(source, *origin, table) {
                if !Self::add_input(
                    &mut state,
                    transfer,
                    InputWitness {
                        key: None,
                        input_kind: InputKind::SourceLiteral,
                    },
                ) {
                    return;
                }
            } else if !matched {
                if !Self::add_input(
                    &mut state,
                    transfer,
                    InputWitness {
                        key: None,
                        input_kind: InputKind::Unattributed,
                    },
                ) {
                    return;
                }
            }
        }
    }

    /// Collect an expression's may-inputs once per cached evaluation, before
    /// its value is fanned out to formal bindings or receiver properties.
    pub(super) fn record_cached_inputs(&self, transfer: CachedInputs<'_>) {
        let CachedInputs {
            event,
            kind,
            argument,
            values,
            dependencies,
            source,
            table,
        } = transfer;
        let mut state = self.state.borrow_mut();
        if state.counts.truncated_at.is_some() {
            return;
        }
        for origin in &self.origins {
            if !self.contains(values, *origin) {
                continue;
            }
            let transfer = TransferKey {
                origin: *origin,
                event,
                kind,
                argument,
            };
            if !Self::ensure_group(&mut state, transfer) {
                return;
            }
            let mut has_input = false;
            for input in dependencies {
                let input_kind = if state.event_reads.contains(&(event, *origin, *input)) {
                    InputKind::ObservedRead
                } else {
                    InputKind::CachedDependency
                };
                if !Self::add_input(
                    &mut state,
                    transfer,
                    InputWitness {
                        key: Some(*input),
                        input_kind,
                    },
                ) {
                    return;
                }
                has_input = true;
            }
            if source.is_some_and(|expr| expression_contains_origin(expr, *origin, table)) {
                if !Self::add_input(
                    &mut state,
                    transfer,
                    InputWitness {
                        key: None,
                        input_kind: InputKind::SourceLiteral,
                    },
                ) {
                    return;
                }
                has_input = true;
            }
            if !has_input
                && !Self::add_input(
                    &mut state,
                    transfer,
                    InputWitness {
                        key: None,
                        input_kind: InputKind::Unattributed,
                    },
                )
            {
                return;
            }
        }
    }

    /// Register one output target without rescanning the expression inputs.
    pub(super) fn record_cached_output(
        &self,
        event: usize,
        kind: &'static str,
        argument: Option<usize>,
        target: Option<u32>,
        output: FlowKey,
        values: &Values,
    ) {
        let mut state = self.state.borrow_mut();
        if state.counts.truncated_at.is_some() {
            return;
        }
        for origin in &self.origins {
            if !self.contains(values, *origin) {
                continue;
            }
            let transfer = TransferKey {
                origin: *origin,
                event,
                kind,
                argument,
            };
            if !Self::ensure_group(&mut state, transfer)
                || !Self::add_output(
                    &mut state,
                    transfer,
                    OutputWitness {
                        key: output,
                        target,
                    },
                )
            {
                return;
            }
            if state.groups[&transfer].inputs.is_empty()
                && !Self::add_input(
                    &mut state,
                    transfer,
                    InputWitness {
                        key: None,
                        input_kind: InputKind::Unattributed,
                    },
                )
            {
                return;
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
        let state = self.state.borrow();
        let mut groups: Vec<_> = state.groups.iter().collect();
        groups.sort_unstable_by_key(|(key, _)| **key);
        let mut by_output: HashMap<(OriginId, FlowKey), Vec<usize>> = HashMap::new();
        for (index, (key, group)) in groups.iter().enumerate() {
            for output in &group.outputs {
                by_output
                    .entry((key.origin, output.key))
                    .or_default()
                    .push(index);
            }
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
        let mut seen = HashSet::new();
        let mut selected = HashSet::new();
        let mut slice_truncated = false;
        while let Some((origin, output)) = pending.pop_front() {
            if seen.contains(&(origin, output)) {
                continue;
            }
            if seen.len() >= MAX_SLICE_NODES {
                slice_truncated = true;
                break;
            }
            seen.insert((origin, output));
            if let Some(producers) = by_output.get(&(origin, output)) {
                for index in producers {
                    if !selected.insert(*index) {
                        continue;
                    }
                    let (_, group) = groups[*index];
                    for input in &group.inputs {
                        let Some(input_key) = input.key else {
                            continue;
                        };
                        // Cached dependencies are only may-edges. Continue the
                        // reverse walk only when the final fact still contains
                        // this selected origin at the candidate input key.
                        let contains_origin = matches!(facts.get(&input_key), Some(FlowFact::Values(values)) if self.contains(values, origin));
                        if contains_origin {
                            pending.push_back((origin, input_key));
                        }
                    }
                }
            }
        }
        eprintln!(
            "origin-journal totals targets={} origins={} recorded_groups={} recorded_inputs={} recorded_outputs={} recorded_event_reads={} reverse_slice_groups={} reverse_slice_nodes={} truncated_at={:?} slice_truncated={} hard_caps=groups:{MAX_JOURNAL_GROUPS},inputs:{MAX_JOURNAL_INPUTS},outputs:{MAX_JOURNAL_OUTPUTS},event_reads:{MAX_JOURNAL_READS},slice_nodes:{MAX_SLICE_NODES},output_bytes:{MAX_JOURNAL_OUTPUT_BYTES} aggregation=union_over_evaluations_inputs_not_paired_to_outputs=true edge_semantics=may_dependency_not_causation candidate_closure_provenance=omitted",
            self.targets.len(),
            self.origins.len(),
            state.counts.groups,
            state.counts.inputs,
            state.counts.outputs,
            state.counts.event_reads,
            selected.len(),
            seen.len(),
            state.counts.truncated_at,
            slice_truncated,
        );
        let mut output_bytes = 0usize;
        let mut output_truncated = false;
        'groups: for (group_id, (key, group)) in groups.iter().enumerate() {
            if !selected.contains(&group_id) {
                continue;
            }
            let line = format!(
                "origin-journal group id={group_id} origin={:?} source={:?} event={} kind={} argument={:?} input_count={} output_count={} aggregation=may_inputs_unpaired_to_outputs\n",
                key.origin,
                self.origin_sources.get(&key.origin),
                key.event,
                key.kind,
                key.argument,
                group.inputs.len(),
                group.outputs.len(),
            );
            if !emit_limited_line(&line, &mut output_bytes) {
                output_truncated = true;
                break;
            }
            let mut inputs: Vec<_> = group.inputs.iter().copied().collect();
            inputs.sort_unstable();
            for input in inputs {
                let input_has_origin = input.key.is_some_and(|input_key| {
                    matches!(facts.get(&input_key), Some(FlowFact::Values(values)) if self.contains(values, key.origin))
                });
                let line = format!(
                    "origin-journal input group={group_id} kind={:?} key={:?} label={:?} final_contains_origin={}\n",
                    input.input_kind,
                    input.key,
                    input.key.map(|input_key| self.key_label(input_key)),
                    input_has_origin,
                );
                if !emit_limited_line(&line, &mut output_bytes) {
                    output_truncated = true;
                    break 'groups;
                }
            }
            let mut outputs: Vec<_> = group.outputs.iter().copied().collect();
            outputs.sort_unstable();
            for output in outputs {
                let line = format!(
                    "origin-journal output group={group_id} target={:?} target_site={:?} key={:?} label={:?}\n",
                    output.target,
                    output.target.map(|site| self.site_label(site)),
                    output.key,
                    self.key_label(output.key),
                );
                if !emit_limited_line(&line, &mut output_bytes) {
                    output_truncated = true;
                    break 'groups;
                }
            }
        }
        eprintln!(
            "origin-journal output status=printed journal_truncated_at={:?} output_truncated={} bytes_written={} byte_cap={MAX_JOURNAL_OUTPUT_BYTES}",
            state.counts.truncated_at, output_truncated, output_bytes,
        );
    }
}

fn emit_limited_line(line: &str, output_bytes: &mut usize) -> bool {
    if output_bytes.saturating_add(line.len()) > MAX_JOURNAL_OUTPUT_BYTES {
        return false;
    }
    *output_bytes += line.len();
    eprint!("{line}");
    true
}

pub(super) struct TracedContext<'a, C> {
    inner: &'a mut C,
    trace: Option<&'a OriginTrace>,
    event: usize,
    source: &'a FlowEvent,
    reads: HashSet<(OriginId, FlowKey)>,
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
            reads: HashSet::new(),
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
        if let (Some(trace), Some(FlowFact::Values(values))) = (self.trace, fact)
            && trace.is_recording()
        {
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
