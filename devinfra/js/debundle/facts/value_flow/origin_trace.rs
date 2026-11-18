//! Opt-in, bounded selected-origin producer journal for source-flow diagnosis.
//!
//! This is diagnostic-only. It records values already observed by the solver
//! and reads the completed fact map without registering subscriptions. It
//! reports a reverse slice of selected outputs rather than formatting the
//! whole fact graph.

use super::{
    FlowEvent, FlowFact, FlowKey, FlowValueExpr, Kind, OriginId, OriginTable, ReturnInputs,
    ReturnSummary, Value, Values, ValuesBuilder,
};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::sync::Arc;

const MAX_JOURNAL_GROUPS: usize = 250_000;
const MAX_JOURNAL_OUTPUTS: usize = 400_000;
const MAX_JOURNAL_OUTPUT_BYTES: usize = 16 * 1024 * 1024;
const MAX_SLICE_NODES: usize = 100_000;
const MAX_EXPRESSION_EVALUATIONS: usize = 25_000;
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
struct TransferKey {
    origin: OriginId,
    event: usize,
    kind: &'static str,
    argument: Option<usize>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Ord, PartialOrd)]
struct OutputWitness {
    key: FlowKey,
    target: Option<u32>,
}

#[derive(Default)]
struct TransferGroup {
    outputs: HashSet<OutputWitness>,
}

#[derive(Default)]
struct JournalCounts {
    groups: usize,
    outputs: usize,
    truncated_at: Option<&'static str>,
}

#[derive(Default)]
struct JournalState {
    groups: HashMap<TransferKey, TransferGroup>,
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
    origin_table: Arc<OriginTable>,
    targets: Vec<Target>,
    key_labels: KeyLabels,
    /// Dynamic output targets are captured while transfer caches are live;
    /// source expressions are evaluated later from completed facts on demand.
    state: RefCell<JournalState>,
}

impl OriginTrace {
    pub(super) fn new(
        origins: BTreeMap<OriginId, (String, u32)>,
        origin_table: Arc<OriginTable>,
        targets: Vec<Target>,
        key_labels: KeyLabels,
    ) -> Self {
        Self {
            origins: origins.keys().copied().collect(),
            origin_sources: origins,
            origin_table,
            targets,
            key_labels,
            state: RefCell::default(),
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
                eprintln!(
                    "origin-journal capture_incomplete cap=groups groups={} outputs={} limit={MAX_JOURNAL_GROUPS}",
                    state.counts.groups, state.counts.outputs
                );
                return false;
            }
            state.groups.insert(key, TransferGroup::default());
            state.counts.groups += 1;
        }
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
            eprintln!(
                "origin-journal capture_incomplete cap=outputs groups={} outputs={} limit={MAX_JOURNAL_OUTPUTS}",
                state.counts.groups, state.counts.outputs
            );
            return false;
        }
        if !Self::ensure_group(state, key) {
            return false;
        }
        state.groups.get_mut(&key).unwrap().outputs.insert(output);
        state.counts.outputs += 1;
        true
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
    }

    pub(super) fn report_slice(
        &self,
        facts: &BTreeMap<FlowKey, FlowFact>,
        events: &[FlowEvent],
        returns: &ReturnInputs,
        declared: &[bool],
        numeric_properties: &[bool],
    ) {
        let state = self.state.borrow();
        let mut dynamic_by_output: HashMap<(OriginId, FlowKey), Vec<(TransferKey, OutputWitness)>> =
            HashMap::new();
        for (transfer, group) in &state.groups {
            for output in &group.outputs {
                dynamic_by_output
                    .entry((transfer.origin, output.key))
                    .or_default()
                    .push((*transfer, *output));
            }
        }
        for producers in dynamic_by_output.values_mut() {
            producers.sort_unstable();
        }
        let mut static_by_output: HashMap<FlowKey, Vec<usize>> = HashMap::new();
        for (index, event) in events.iter().enumerate() {
            let output = match event {
                FlowEvent::FunctionDeclaration { id, .. }
                | FlowEvent::ClassDeclaration { id, .. }
                | FlowEvent::Variable { id, .. }
                | FlowEvent::AssignBinding { id, .. } => Some(FlowKey::Binding(*id)),
                FlowEvent::Return { function, .. } => Some(FlowKey::Return(*function)),
                FlowEvent::WritePropertyExpr {
                    object, property, ..
                }
                | FlowEvent::WritePropertyBinding {
                    object, property, ..
                }
                | FlowEvent::WritePropertyValue {
                    object, property, ..
                } => {
                    static_by_output
                        .entry(FlowKey::Properties(*object))
                        .or_default()
                        .push(index);
                    if numeric_properties.get(property.0 as usize) == Some(&true) {
                        static_by_output
                            .entry(FlowKey::NumericProperties(*object))
                            .or_default()
                            .push(index);
                    }
                    Some(FlowKey::Property(*object, *property))
                }
                _ => None,
            };
            if let Some(output) = output {
                static_by_output.entry(output).or_default().push(index);
            }
        }

        let mut pending = VecDeque::new();
        for target in &self.targets {
            if let Some(FlowFact::Values(values)) = facts.get(&target.key) {
                for origin in &self.origins {
                    if self.contains(values, *origin) {
                        pending.push_back((*origin, target.key));
                    }
                }
            }
        }
        let mut seen = HashSet::new();
        let mut visited_events = BTreeSet::new();
        let mut dynamic_sources: HashMap<TransferKey, (Vec<FlowKey>, Vec<Value>, bool)> =
            HashMap::new();
        let mut evaluated = 0usize;
        let mut unresolved = 0usize;
        let mut output_bytes = 0usize;
        let mut truncated = None;
        let mut seed_found = false;
        let mut unexplored_producers = 0usize;
        while let Some((origin, output)) = pending.pop_front() {
            if !seen.insert((origin, output)) {
                continue;
            }
            if !fact_has_origin(facts, output, origin, self) {
                continue;
            }
            if seen.len() > MAX_SLICE_NODES {
                truncated = Some("slice_nodes");
                break;
            }
            let mut traced_output = false;
            if let Some(indexes) = static_by_output.get(&output) {
                for (position, index) in indexes.iter().enumerate() {
                    if visited_events.insert((origin, *index, output, None, "static")) {
                        let event = &events[*index];
                        let source = static_event_source(event);
                        match source {
                            StaticSource::Expression(expr) => {
                                if !self.expression_has_origin(
                                    expr,
                                    origin,
                                    facts,
                                    returns,
                                    declared,
                                    &mut evaluated,
                                ) {
                                    continue;
                                }
                                traced_output = true;
                                let mut keys = Vec::new();
                                let mut seeds = Vec::new();
                                self.source_keys_for_expr(
                                    expr,
                                    origin,
                                    facts,
                                    returns,
                                    declared,
                                    &mut evaluated,
                                    &mut keys,
                                    &mut seeds,
                                );
                                if keys.is_empty() && seeds.is_empty() {
                                    unresolved += 1;
                                    if !self.emit_event(
                                        *index,
                                        event,
                                        output,
                                        origin,
                                        None,
                                        "unattributed",
                                        &mut output_bytes,
                                    ) {
                                        truncated = Some("output_bytes");
                                        break;
                                    }
                                } else {
                                    if !self.emit_event(
                                        *index,
                                        event,
                                        output,
                                        origin,
                                        None,
                                        "value_source",
                                        &mut output_bytes,
                                    ) {
                                        truncated = Some("output_bytes");
                                        break;
                                    }
                                    self.emit_sources(
                                        *index,
                                        event,
                                        output,
                                        origin,
                                        None,
                                        &keys,
                                        &seeds,
                                        &mut output_bytes,
                                        &mut truncated,
                                    );
                                    pending.extend(keys.into_iter().map(|key| (origin, key)));
                                    seed_found |= !seeds.is_empty();
                                }
                            }
                            StaticSource::Value(value) => {
                                if *value == self.origin_table.origin(origin) {
                                    traced_output = true;
                                    if !self.emit_event(
                                        *index,
                                        event,
                                        output,
                                        origin,
                                        None,
                                        "literal_seed",
                                        &mut output_bytes,
                                    ) {
                                        truncated = Some("output_bytes");
                                        break;
                                    }
                                    seed_found = true;
                                }
                            }
                            StaticSource::Binding(slot) => {
                                let key = FlowKey::Binding(slot);
                                if fact_has_origin(facts, key, origin, self) {
                                    traced_output = true;
                                    if !self.emit_edge(
                                        *index,
                                        event,
                                        output,
                                        key,
                                        origin,
                                        None,
                                        "binding_source",
                                        &mut output_bytes,
                                    ) {
                                        truncated = Some("output_bytes");
                                        break;
                                    }
                                    pending.push_back((origin, key));
                                }
                            }
                            StaticSource::None => {}
                        }
                        if seed_found || truncated.is_some() {
                            unexplored_producers += indexes.len() - position - 1;
                            break;
                        }
                    }
                }
            }
            if seed_found {
                unexplored_producers +=
                    dynamic_by_output.get(&(origin, output)).map_or(0, Vec::len);
                truncated = Some("first_seed_witness_only");
                break;
            }
            if truncated.is_some() {
                break;
            }
            if let Some(producers) = dynamic_by_output.get(&(origin, output)) {
                for (position, (transfer, witness)) in producers.iter().enumerate() {
                    let Some(event) = events.get(transfer.event) else {
                        unresolved += 1;
                        continue;
                    };
                    let source = dynamic_sources.get(transfer).cloned().or_else(|| {
                        let Some(expr) = dynamic_event_source(event, *transfer) else {
                            let result = (Vec::new(), Vec::new(), false);
                            dynamic_sources.insert(*transfer, result.clone());
                            return Some(result);
                        };
                        if !self.expression_has_origin(
                            expr,
                            origin,
                            facts,
                            returns,
                            declared,
                            &mut evaluated,
                        ) {
                            let result = (Vec::new(), Vec::new(), false);
                            dynamic_sources.insert(*transfer, result.clone());
                            return Some(result);
                        }
                        let mut keys = Vec::new();
                        let mut seeds = Vec::new();
                        self.source_keys_for_expr(
                            expr,
                            origin,
                            facts,
                            returns,
                            declared,
                            &mut evaluated,
                            &mut keys,
                            &mut seeds,
                        );
                        let result = (keys, seeds, true);
                        dynamic_sources.insert(*transfer, result.clone());
                        Some(result)
                    });
                    let Some((keys, seeds, expression_matched)) = source else {
                        continue;
                    };
                    if !expression_matched {
                        unresolved += 1;
                        traced_output = true;
                        if !self.emit_event(
                            transfer.event,
                            event,
                            witness.key,
                            origin,
                            witness.target,
                            "cached_output_not_reproduced",
                            &mut output_bytes,
                        ) {
                            truncated = Some("output_bytes");
                            break;
                        }
                        continue;
                    }
                    traced_output = true;
                    visited_events.insert((
                        origin,
                        transfer.event,
                        witness.key,
                        witness.target,
                        transfer.kind,
                    ));
                    let kind = if keys.is_empty() && seeds.is_empty() {
                        "unattributed_dynamic_source"
                    } else {
                        "value_source"
                    };
                    if keys.is_empty() && seeds.is_empty() {
                        unresolved += 1;
                    }
                    if !self.emit_event(
                        transfer.event,
                        event,
                        witness.key,
                        origin,
                        witness.target,
                        kind,
                        &mut output_bytes,
                    ) {
                        truncated = Some("output_bytes");
                        break;
                    }
                    self.emit_sources(
                        transfer.event,
                        event,
                        witness.key,
                        origin,
                        witness.target,
                        &keys,
                        &seeds,
                        &mut output_bytes,
                        &mut truncated,
                    );
                    pending.extend(keys.into_iter().map(|key| (origin, key)));
                    seed_found |= !seeds.is_empty();
                    if truncated.is_some() {
                        unexplored_producers += producers.len() - position - 1;
                        break;
                    }
                    if seed_found {
                        unexplored_producers += producers.len() - position - 1;
                        break;
                    }
                }
            }
            if !traced_output {
                unresolved += 1;
                let line = format!(
                    "origin-journal unattributed_key key={:?} label={:?} source_origin={:?} reason=no_selected_value_producer_indexed\n",
                    output,
                    self.key_label(output),
                    self.origin_sources.get(&origin),
                );
                if !emit_limited_line(&line, &mut output_bytes) {
                    truncated = Some("output_bytes");
                }
            }
            if seed_found && truncated.is_none() {
                truncated = Some("first_seed_witness_only");
            }
            if truncated.is_some() {
                break;
            }
            if evaluated >= MAX_EXPRESSION_EVALUATIONS {
                truncated = Some("expression_evaluations");
                break;
            }
        }
        eprintln!(
            "origin-journal demand_slice targets={} origins={} dynamic_groups={} dynamic_outputs={} reached_keys={} source_events={} expression_evaluations={} unattributed={} capture_truncated_at={:?} stop_reason={:?} unexplored_frontier_keys={} unexplored_producers={} hard_caps=groups:{MAX_JOURNAL_GROUPS},outputs:{MAX_JOURNAL_OUTPUTS},slice_nodes:{MAX_SLICE_NODES},expression_evaluations:{MAX_EXPRESSION_EVALUATIONS},output_bytes:{MAX_JOURNAL_OUTPUT_BYTES} semantics=exact_final_fact_value_may_sources_not_execution_causality summary_sources=argument,argument_property,argument_or_default,one_forwarder_else_return_fact unsupported_or_unattributed_are_explicit=true",
            self.targets.len(),
            self.origins.len(),
            state.counts.groups,
            state.counts.outputs,
            seen.len(),
            visited_events.len(),
            evaluated,
            unresolved,
            state.counts.truncated_at,
            truncated,
            pending.len(),
            unexplored_producers,
        );
    }

    fn emit_event(
        &self,
        index: usize,
        event: &FlowEvent,
        output: FlowKey,
        origin: OriginId,
        target: Option<u32>,
        kind: &str,
        output_bytes: &mut usize,
    ) -> bool {
        let line = format!(
            "origin-journal source event={} kind={} output={:?} output_label={:?} target={:?} target_site={:?} source_origin={:?} event_kind={:?}\n",
            index,
            kind,
            output,
            self.key_label(output),
            target,
            target.map(|site| self.site_label(site)),
            self.origin_sources.get(&origin),
            event_kind(event),
        );
        emit_limited_line(&line, output_bytes)
    }

    #[allow(clippy::too_many_arguments)]
    fn emit_edge(
        &self,
        event_index: usize,
        event: &FlowEvent,
        from: FlowKey,
        to: FlowKey,
        origin: OriginId,
        target: Option<u32>,
        kind: &str,
        output_bytes: &mut usize,
    ) -> bool {
        let line = format!(
            "origin-journal edge event={} kind={} from={:?} from_label={:?} to={:?} to_label={:?} target={:?} target_site={:?} source_origin={:?} event_kind={:?}\n",
            event_index,
            kind,
            from,
            self.key_label(from),
            to,
            self.key_label(to),
            target,
            target.map(|site| self.site_label(site)),
            self.origin_sources.get(&origin),
            event_kind(event),
        );
        emit_limited_line(&line, output_bytes)
    }

    #[allow(clippy::too_many_arguments)]
    fn emit_sources(
        &self,
        event_index: usize,
        event: &FlowEvent,
        from: FlowKey,
        origin: OriginId,
        target: Option<u32>,
        keys: &[FlowKey],
        seeds: &[Value],
        output_bytes: &mut usize,
        truncated: &mut Option<&'static str>,
    ) {
        for key in keys {
            if !self.emit_edge(
                event_index,
                event,
                from,
                *key,
                origin,
                target,
                "value_source",
                output_bytes,
            ) {
                *truncated = Some("output_bytes");
                return;
            }
        }
        for seed in seeds {
            let line = format!(
                "origin-journal seed event={} output={:?} output_label={:?} target={:?} value={:?} source_origin={:?}\n",
                event_index,
                from,
                self.key_label(from),
                target,
                seed,
                self.origin_sources.get(&origin),
            );
            if !emit_limited_line(&line, output_bytes) {
                *truncated = Some("output_bytes");
                return;
            }
        }
    }

    fn expression_has_origin(
        &self,
        expr: &FlowValueExpr,
        origin: OriginId,
        facts: &BTreeMap<FlowKey, FlowFact>,
        returns: &ReturnInputs,
        declared: &[bool],
        evaluations: &mut usize,
    ) -> bool {
        let Some(values) = self.evaluate_expr(expr, facts, returns, declared, evaluations) else {
            return false;
        };
        self.contains(&values, origin)
    }

    fn evaluate_expr(
        &self,
        expr: &FlowValueExpr,
        facts: &BTreeMap<FlowKey, FlowFact>,
        returns: &ReturnInputs,
        declared: &[bool],
        evaluations: &mut usize,
    ) -> Option<Values> {
        if *evaluations >= MAX_EXPRESSION_EVALUATIONS {
            return None;
        }
        *evaluations += 1;
        let mut values = ValuesBuilder::new(self.origin_table.clone());
        super::append_value_expr(
            expr,
            &mut values,
            declared,
            returns,
            &mut |key| match facts.get(&key) {
                Some(FlowFact::Values(values)) => Some(values.clone()),
                _ => None,
            },
            &mut |_| {},
        );
        Some(values.finish())
    }

    #[allow(clippy::too_many_arguments)]
    fn source_keys_for_expr(
        &self,
        expr: &FlowValueExpr,
        origin: OriginId,
        facts: &BTreeMap<FlowKey, FlowFact>,
        returns: &ReturnInputs,
        declared: &[bool],
        evaluations: &mut usize,
        keys: &mut Vec<FlowKey>,
        seeds: &mut Vec<Value>,
    ) {
        if !self.expression_has_origin(expr, origin, facts, returns, declared, evaluations) {
            return;
        }
        match expr {
            FlowValueExpr::Binding(slot) => keys.push(FlowKey::Binding(*slot)),
            FlowValueExpr::Value(value) if *value == self.origin_table.origin(origin) => {
                seeds.push(*value)
            }
            FlowValueExpr::Value(_) | FlowValueExpr::Empty | FlowValueExpr::Unknown(_) => {}
            FlowValueExpr::Union(left, right) => {
                self.source_keys_for_expr(
                    left,
                    origin,
                    facts,
                    returns,
                    declared,
                    evaluations,
                    keys,
                    seeds,
                );
                self.source_keys_for_expr(
                    right,
                    origin,
                    facts,
                    returns,
                    declared,
                    evaluations,
                    keys,
                    seeds,
                );
            }
            FlowValueExpr::Member { object, property } => {
                let Some(objects) =
                    self.evaluate_expr(object, facts, returns, declared, evaluations)
                else {
                    return;
                };
                for value in &objects {
                    if let Value::Alloc(Kind::Object | Kind::Set, site) = value {
                        let key = match property {
                            Some(property) => FlowKey::Property(site, *property),
                            None => FlowKey::Properties(site),
                        };
                        if fact_has_origin(facts, key, origin, self) {
                            keys.push(key);
                        }
                    }
                }
            }
            FlowValueExpr::NumericMember { object } => {
                let Some(objects) =
                    self.evaluate_expr(object, facts, returns, declared, evaluations)
                else {
                    return;
                };
                for value in &objects {
                    if let Value::Alloc(Kind::Object | Kind::Set, site) = value {
                        let key = FlowKey::NumericProperties(site);
                        if fact_has_origin(facts, key, origin, self) {
                            keys.push(key);
                        }
                    }
                }
            }
            FlowValueExpr::Call {
                callee,
                argument: Some(argument),
                factory_importer,
            } => {
                let Some(targets) =
                    self.evaluate_expr(callee, facts, returns, declared, evaluations)
                else {
                    return;
                };
                let actual = &returns.arguments[*argument];
                for target in &targets {
                    let Value::Alloc(Kind::Function, function) = target else {
                        continue;
                    };
                    let target_call = FlowValueExpr::Call {
                        callee: Box::new(FlowValueExpr::Value(Value::Alloc(
                            Kind::Function,
                            function,
                        ))),
                        argument: Some(*argument),
                        factory_importer: *factory_importer,
                    };
                    if !self.expression_has_origin(
                        &target_call,
                        origin,
                        facts,
                        returns,
                        declared,
                        evaluations,
                    ) {
                        continue;
                    }
                    match returns.summaries.get(&function) {
                        Some(ReturnSummary::Argument) => self.source_keys_for_expr(
                            actual,
                            origin,
                            facts,
                            returns,
                            declared,
                            evaluations,
                            keys,
                            seeds,
                        ),
                        Some(ReturnSummary::ArgumentProperty(property)) => self.property_sources(
                            actual,
                            Some(*property),
                            origin,
                            facts,
                            returns,
                            declared,
                            evaluations,
                            keys,
                        ),
                        Some(ReturnSummary::ArgumentOrDefault) => {
                            self.source_keys_for_expr(
                                actual,
                                origin,
                                facts,
                                returns,
                                declared,
                                evaluations,
                                keys,
                                seeds,
                            );
                            self.property_sources(
                                actual,
                                returns.default_property,
                                origin,
                                facts,
                                returns,
                                declared,
                                evaluations,
                                keys,
                            );
                        }
                        Some(ReturnSummary::ForwardChoices(helpers)) => {
                            for helper in helpers {
                                if let Some(FlowFact::Values(helper_targets)) =
                                    facts.get(&FlowKey::Binding(*helper))
                                {
                                    for helper_target in helper_targets {
                                        if let Value::Alloc(Kind::Function, helper_function) =
                                            helper_target
                                        {
                                            match returns.summaries.get(&helper_function) {
                                                Some(ReturnSummary::Argument) => self
                                                    .source_keys_for_expr(
                                                        actual,
                                                        origin,
                                                        facts,
                                                        returns,
                                                        declared,
                                                        evaluations,
                                                        keys,
                                                        seeds,
                                                    ),
                                                Some(ReturnSummary::ArgumentProperty(property)) => {
                                                    self.property_sources(
                                                        actual,
                                                        Some(*property),
                                                        origin,
                                                        facts,
                                                        returns,
                                                        declared,
                                                        evaluations,
                                                        keys,
                                                    )
                                                }
                                                _ => {
                                                    let key = FlowKey::Return(helper_function);
                                                    if fact_has_origin(facts, key, origin, self) {
                                                        keys.push(key);
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        None => {
                            let key = FlowKey::Return(function);
                            if fact_has_origin(facts, key, origin, self) {
                                keys.push(key);
                            }
                        }
                    }
                }
            }
            FlowValueExpr::Call {
                callee,
                argument: None,
                factory_importer,
            } => {
                let Some(targets) =
                    self.evaluate_expr(callee, facts, returns, declared, evaluations)
                else {
                    return;
                };
                for target in &targets {
                    if let Value::Alloc(Kind::Function, function) = target {
                        let target_call = FlowValueExpr::Call {
                            callee: Box::new(FlowValueExpr::Value(Value::Alloc(
                                Kind::Function,
                                function,
                            ))),
                            argument: None,
                            factory_importer: *factory_importer,
                        };
                        if self.expression_has_origin(
                            &target_call,
                            origin,
                            facts,
                            returns,
                            declared,
                            evaluations,
                        ) {
                            let key = FlowKey::Return(function);
                            if fact_has_origin(facts, key, origin, self) {
                                keys.push(key);
                            }
                        }
                    }
                }
            }
        }
        keys.sort_unstable();
        keys.dedup();
    }

    #[allow(clippy::too_many_arguments)]
    fn property_sources(
        &self,
        actual: &FlowValueExpr,
        property: Option<super::PropertySlot>,
        origin: OriginId,
        facts: &BTreeMap<FlowKey, FlowFact>,
        returns: &ReturnInputs,
        declared: &[bool],
        evaluations: &mut usize,
        keys: &mut Vec<FlowKey>,
    ) {
        let Some(objects) = self.evaluate_expr(actual, facts, returns, declared, evaluations)
        else {
            return;
        };
        for value in &objects {
            if let Value::Alloc(Kind::Object | Kind::Set, site) = value {
                let key =
                    property.map_or(FlowKey::Properties(site), |p| FlowKey::Property(site, p));
                if fact_has_origin(facts, key, origin, self) {
                    keys.push(key);
                }
            }
        }
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
}

fn emit_limited_line(line: &str, output_bytes: &mut usize) -> bool {
    if output_bytes.saturating_add(line.len()) > MAX_JOURNAL_OUTPUT_BYTES {
        return false;
    }
    *output_bytes += line.len();
    eprint!("{line}");
    true
}

enum StaticSource<'a> {
    Expression(&'a FlowValueExpr),
    Value(&'a Value),
    Binding(super::BindingSlot),
    None,
}

fn static_event_source(event: &FlowEvent) -> StaticSource<'_> {
    match event {
        FlowEvent::FunctionDeclaration { origin, .. }
        | FlowEvent::ClassDeclaration { origin, .. }
        | FlowEvent::WritePropertyValue { value: origin, .. } => StaticSource::Value(origin),
        FlowEvent::Variable { init, .. }
        | FlowEvent::AssignBinding { right: init, .. }
        | FlowEvent::AssignProperty { right: init, .. }
        | FlowEvent::WritePropertyExpr { value: init, .. }
        | FlowEvent::Return { value: init, .. } => StaticSource::Expression(init),
        FlowEvent::WritePropertyBinding { binding, .. } => StaticSource::Binding(*binding),
        _ => StaticSource::None,
    }
}

fn dynamic_event_source(event: &FlowEvent, transfer: TransferKey) -> Option<&FlowValueExpr> {
    match (event, transfer.kind) {
        (FlowEvent::Invoke { args, .. }, "invoke_argument") => {
            args.get(transfer.argument?).map(|argument| argument)
        }
        (FlowEvent::AssignProperty { right, .. }, "property_rhs")
        | (FlowEvent::WritePropertyExpr { value: right, .. }, "property_rhs") => Some(right),
        _ => None,
    }
}

fn fact_has_origin(
    facts: &BTreeMap<FlowKey, FlowFact>,
    key: FlowKey,
    origin: OriginId,
    trace: &OriginTrace,
) -> bool {
    matches!(facts.get(&key), Some(FlowFact::Values(values)) if trace.contains(values, origin))
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
