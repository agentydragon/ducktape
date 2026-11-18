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
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
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
struct JournalCounts {
    groups: usize,
    outputs: usize,
    duplicate_observations: usize,
    omitted_alternatives: usize,
    truncated_at: Option<&'static str>,
}

#[derive(Default)]
struct JournalState {
    // Keep only the first dynamic producer per selected-origin/output pair.
    // This is a best-effort witness candidate, not an inventory of all
    // contributing producers; final-fact reevaluation can still fail or cycle.
    transfers: HashSet<TransferKey>,
    outputs: HashMap<SliceNode, (TransferKey, OutputWitness)>,
    counts: JournalCounts,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
struct SliceNode {
    origin: OriginId,
    key: FlowKey,
}

#[derive(Clone, Copy)]
struct ProducerWitness {
    event: usize,
    target: Option<u32>,
    kind: &'static str,
}

#[derive(Clone, Copy)]
struct SliceParent {
    parent: SliceNode,
    producer: ProducerWitness,
}

#[derive(Clone, Copy)]
enum TraceDestination {
    Key(FlowKey),
    Seed(Value),
}

#[derive(Clone, Copy)]
struct TraceStep {
    output: SliceNode,
    producer: ProducerWitness,
    destination: TraceDestination,
}

#[derive(Clone)]
struct SourceEvaluation {
    keys: Vec<FlowKey>,
    seeds: Vec<Value>,
    matched: bool,
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

    fn add_output(
        state: &mut JournalState,
        key: TransferKey,
        output: OutputWitness,
        origin: OriginId,
    ) -> bool {
        if state.counts.truncated_at.is_some() {
            return false;
        }
        let node = SliceNode {
            origin,
            key: output.key,
        };
        if let Some((existing_transfer, existing_output)) = state.outputs.get(&node) {
            if *existing_transfer == key && *existing_output == output {
                state.counts.duplicate_observations =
                    state.counts.duplicate_observations.saturating_add(1);
            } else {
                state.counts.omitted_alternatives =
                    state.counts.omitted_alternatives.saturating_add(1);
            }
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
        if !state.transfers.contains(&key) && state.counts.groups >= MAX_JOURNAL_GROUPS {
            state.counts.truncated_at = Some("groups");
            eprintln!(
                "origin-journal capture_incomplete cap=groups groups={} outputs={} limit={MAX_JOURNAL_GROUPS}",
                state.counts.groups, state.counts.outputs
            );
            return false;
        }
        state.transfers.insert(key);
        state.outputs.insert(node, (key, output));
        state.counts.groups = state.transfers.len();
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
            if !Self::add_output(
                &mut state,
                transfer,
                OutputWitness {
                    key: output,
                    target,
                },
                *origin,
            ) {
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

        let mut pending = Vec::new();
        let mut root_targets = HashMap::new();
        let mut discovery_limit_hit = false;
        for (target_index, target) in self.targets.iter().enumerate() {
            if let Some(FlowFact::Values(values)) = facts.get(&target.key) {
                for origin in &self.origins {
                    if self.contains(values, *origin) {
                        let node = SliceNode {
                            origin: *origin,
                            key: target.key,
                        };
                        if root_targets.contains_key(&node) {
                            continue;
                        }
                        if root_targets.len() >= MAX_SLICE_NODES {
                            discovery_limit_hit = true;
                            break;
                        }
                        root_targets.insert(node, target_index);
                        pending.push(node);
                    }
                }
            }
        }
        pending.reverse();

        let mut seen = HashSet::new();
        let mut predecessors: HashMap<SliceNode, SliceParent> = HashMap::new();
        let mut dynamic_sources: HashMap<TransferKey, SourceEvaluation> = HashMap::new();
        let mut evaluated = 0usize;
        let mut unresolved = 0usize;
        let mut missing_samples = Vec::new();
        let mut stop_reason = discovery_limit_hit.then_some("slice_nodes");
        let mut found_seed = None;
        while let Some(node) = pending.pop() {
            if discovery_limit_hit {
                break;
            }
            if seen.contains(&node) {
                continue;
            }
            seen.insert(node);
            if !fact_has_origin(facts, node.key, node.origin, self) {
                continue;
            }

            let mut sources = Vec::new();
            let mut matched_producer = false;
            if let Some(indexes) = static_by_output.get(&node.key) {
                for index in indexes {
                    let event = &events[*index];
                    let producer = ProducerWitness {
                        event: *index,
                        target: None,
                        kind: "static_source",
                    };
                    match static_event_source(event) {
                        StaticSource::Expression(expr) => {
                            if !self.expression_has_origin(
                                expr,
                                node.origin,
                                facts,
                                returns,
                                declared,
                                &mut evaluated,
                            ) {
                                continue;
                            }
                            matched_producer = true;
                            let mut keys = Vec::new();
                            let mut seeds = Vec::new();
                            self.source_keys_for_expr(
                                expr,
                                node.origin,
                                facts,
                                returns,
                                declared,
                                &mut evaluated,
                                &mut keys,
                                &mut seeds,
                            );
                            if let Some(seed) = seeds.first() {
                                found_seed = Some((node, producer, *seed));
                                break;
                            }
                            if keys.is_empty() {
                                unresolved += 1;
                                if missing_samples.len() < 16 {
                                    missing_samples.push((
                                        node,
                                        "unattributed_static_source",
                                        Some(*index),
                                    ));
                                }
                            } else {
                                sources.extend(keys.into_iter().map(|key| (key, producer)));
                            }
                        }
                        StaticSource::Value(value)
                            if *value == self.origin_table.origin(node.origin) =>
                        {
                            matched_producer = true;
                            found_seed = Some((node, producer, *value));
                            break;
                        }
                        StaticSource::Binding(slot) => {
                            let key = FlowKey::Binding(slot);
                            if fact_has_origin(facts, key, node.origin, self) {
                                matched_producer = true;
                                sources.push((key, producer));
                            }
                        }
                        StaticSource::Value(_) | StaticSource::None => {}
                    }
                    if found_seed.is_some() {
                        break;
                    }
                }
            }
            if found_seed.is_some() {
                break;
            }

            if let Some((transfer, witness)) = state.outputs.get(&node) {
                let event = events.get(transfer.event);
                if let Some(event) = event {
                    let source = if let Some(cached) = dynamic_sources.get(transfer) {
                        cached.clone()
                    } else {
                        let result = match dynamic_event_source(event, *transfer) {
                            Some(expr)
                                if self.expression_has_origin(
                                    expr,
                                    node.origin,
                                    facts,
                                    returns,
                                    declared,
                                    &mut evaluated,
                                ) =>
                            {
                                let mut keys = Vec::new();
                                let mut seeds = Vec::new();
                                self.source_keys_for_expr(
                                    expr,
                                    node.origin,
                                    facts,
                                    returns,
                                    declared,
                                    &mut evaluated,
                                    &mut keys,
                                    &mut seeds,
                                );
                                SourceEvaluation {
                                    keys,
                                    seeds,
                                    matched: true,
                                }
                            }
                            _ => SourceEvaluation {
                                keys: Vec::new(),
                                seeds: Vec::new(),
                                matched: false,
                            },
                        };
                        dynamic_sources.insert(*transfer, result.clone());
                        result
                    };
                    matched_producer |= source.matched;
                    if source.matched {
                        let producer = ProducerWitness {
                            event: transfer.event,
                            target: witness.target,
                            kind: transfer.kind,
                        };
                        if let Some(seed) = source.seeds.first() {
                            found_seed = Some((node, producer, *seed));
                        } else if source.keys.is_empty() {
                            unresolved += 1;
                            if missing_samples.len() < 16 {
                                missing_samples.push((
                                    node,
                                    "unattributed_dynamic_source",
                                    Some(transfer.event),
                                ));
                            }
                        } else {
                            sources.extend(source.keys.into_iter().map(|key| (key, producer)));
                        }
                    } else {
                        unresolved += 1;
                        if missing_samples.len() < 16 {
                            missing_samples.push((
                                node,
                                "cached_output_not_reproduced",
                                Some(transfer.event),
                            ));
                        }
                    }
                } else {
                    unresolved += 1;
                    if missing_samples.len() < 16 {
                        missing_samples.push((node, "missing_dynamic_event", Some(transfer.event)));
                    }
                }
            }

            if found_seed.is_some() {
                break;
            }
            if !matched_producer {
                unresolved += 1;
                if missing_samples.len() < 16 {
                    missing_samples.push((node, "no_selected_value_producer", None));
                }
            }
            sources.sort_unstable_by_key(|(key, producer)| (*key, producer.event));
            sources.dedup_by_key(|(key, _)| *key);
            for (key, producer) in sources.into_iter().rev() {
                let child = SliceNode {
                    origin: node.origin,
                    key,
                };
                if root_targets.contains_key(&child)
                    || seen.contains(&child)
                    || predecessors.contains_key(&child)
                {
                    continue;
                }
                if root_targets.len() + predecessors.len() >= MAX_SLICE_NODES {
                    discovery_limit_hit = true;
                    stop_reason = Some("slice_nodes");
                    break;
                }
                predecessors.insert(
                    child,
                    SliceParent {
                        parent: node,
                        producer,
                    },
                );
                pending.push(child);
            }
            if discovery_limit_hit {
                break;
            }
            if evaluated >= MAX_EXPRESSION_EVALUATIONS {
                stop_reason = Some("expression_evaluations");
                break;
            }
        }

        let mut steps = Vec::new();
        let mut witness_root = None;
        if let Some((seed_node, seed_producer, seed)) = found_seed {
            steps.push(TraceStep {
                output: seed_node,
                producer: seed_producer,
                destination: TraceDestination::Seed(seed),
            });
            let mut cursor = seed_node;
            while !root_targets.contains_key(&cursor) {
                let Some(parent) = predecessors.get(&cursor) else {
                    stop_reason = Some("missing_predecessor");
                    break;
                };
                steps.push(TraceStep {
                    output: parent.parent,
                    producer: parent.producer,
                    destination: TraceDestination::Key(cursor.key),
                });
                cursor = parent.parent;
                if steps.len() > MAX_SLICE_NODES {
                    stop_reason = Some("path_nodes");
                    break;
                }
            }
            if root_targets.contains_key(&cursor) {
                witness_root = Some((cursor, root_targets[&cursor]));
                steps.reverse();
            } else {
                steps.clear();
            }
        }

        if found_seed.is_some() && stop_reason.is_none() {
            stop_reason = Some("first_connected_witness_only");
        }
        let witness_found = witness_root.is_some();
        let mut output_bytes = 0usize;
        let mut witness_emitted_complete = false;
        if let Some((root, target_index)) = witness_root {
            let target = &self.targets[target_index];
            let header = format!(
                "origin-journal connected_witness module={} binding={:?} context={} root={:?} root_label={:?} source_origin={:?} steps={} alternatives_omitted={} duplicate_observations={}\n",
                target.module,
                target.name,
                target.context,
                root.key,
                self.key_label(root.key),
                self.origin_sources.get(&root.origin),
                steps.len(),
                state.counts.omitted_alternatives,
                state.counts.duplicate_observations,
            );
            let mut path_text = String::new();
            let mut path_fits = append_bounded_path_line(&mut path_text, &header);
            for step in &steps {
                if !path_fits {
                    break;
                }
                let Some(event) = events.get(step.producer.event) else {
                    stop_reason = Some("witness_event_missing");
                    path_fits = false;
                    break;
                };
                let source_line = self.event_line(
                    step.producer.event,
                    event,
                    step.output.key,
                    step.output.origin,
                    step.producer.target,
                    step.producer.kind,
                );
                if !append_bounded_path_line(&mut path_text, &source_line) {
                    path_fits = false;
                    break;
                }
                match step.destination {
                    TraceDestination::Key(key) => {
                        let edge_line = self.edge_line(
                            step.producer.event,
                            event,
                            step.output.key,
                            key,
                            step.output.origin,
                            step.producer.target,
                            "witness_input",
                        );
                        if !append_bounded_path_line(&mut path_text, &edge_line) {
                            path_fits = false;
                        }
                    }
                    TraceDestination::Seed(value) => {
                        let line = format!(
                            "origin-journal witness_seed output={:?} output_label={:?} value={:?} source_origin={:?}\n",
                            step.output.key,
                            self.key_label(step.output.key),
                            value,
                            self.origin_sources.get(&step.output.origin),
                        );
                        if !append_bounded_path_line(&mut path_text, &line) {
                            path_fits = false;
                        }
                    }
                }
            }
            if path_fits {
                output_bytes = path_text.len();
                eprint!("{path_text}");
                witness_emitted_complete = true;
            } else if stop_reason.is_none() || stop_reason == Some("first_connected_witness_only") {
                stop_reason = Some("output_bytes");
            }
        }
        for (node, reason, event) in &missing_samples {
            if stop_reason == Some("output_bytes") {
                break;
            }
            let line = format!(
                "origin-journal missing_sample key={:?} label={:?} source_origin={:?} event={:?} reason={}\n",
                node.key,
                self.key_label(node.key),
                self.origin_sources.get(&node.origin),
                event,
                reason,
            );
            if !emit_limited_line(&line, &mut output_bytes) {
                stop_reason = Some("output_bytes");
                break;
            }
        }
        let capture_complete = state.counts.truncated_at.is_none();
        eprintln!(
            "origin-journal demand_slice targets={} origins={} unique_dynamic_outputs={} duplicate_observations={} duplicate_observations_complete={} omitted_dynamic_alternatives_observed={} omitted_alternatives_complete={} discovered_keys={} reached_keys={} witness_found={} witness_emitted_complete={} witness_steps={} expression_evaluations={} unresolved={} capture_truncated_at={:?} stop_reason={:?} frontier_keys={} missing_samples={} hard_caps=groups:{MAX_JOURNAL_GROUPS},outputs:{MAX_JOURNAL_OUTPUTS},slice_nodes:{MAX_SLICE_NODES},expression_evaluations:{MAX_EXPRESSION_EVALUATIONS},output_bytes:{MAX_JOURNAL_OUTPUT_BYTES} semantics=one_connected_final_fact_may_source_witness_not_execution_causality;unseen_alternatives_are_not_ruled_out",
            self.targets.len(),
            self.origins.len(),
            state.counts.outputs,
            state.counts.duplicate_observations,
            capture_complete,
            state.counts.omitted_alternatives,
            capture_complete,
            root_targets.len() + predecessors.len(),
            seen.len(),
            witness_found,
            witness_emitted_complete,
            steps.len(),
            evaluated,
            unresolved,
            state.counts.truncated_at,
            stop_reason.unwrap_or("frontier_exhausted_no_absence_claim"),
            pending.len(),
            missing_samples.len(),
        );
    }

    #[allow(clippy::too_many_arguments)]
    fn event_line(
        &self,
        index: usize,
        event: &FlowEvent,
        output: FlowKey,
        origin: OriginId,
        target: Option<u32>,
        kind: &str,
    ) -> String {
        format!(
            "origin-journal source event={} kind={} output={:?} output_label={:?} target={:?} target_site={:?} source_origin={:?} event_kind={:?}\n",
            index,
            kind,
            output,
            self.key_label(output),
            target,
            target.map(|site| self.site_label(site)),
            self.origin_sources.get(&origin),
            event_kind(event),
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn edge_line(
        &self,
        event_index: usize,
        event: &FlowEvent,
        from: FlowKey,
        to: FlowKey,
        origin: OriginId,
        target: Option<u32>,
        kind: &str,
    ) -> String {
        format!(
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
        )
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

fn append_bounded_path_line(path: &mut String, line: &str) -> bool {
    if path.len().saturating_add(line.len()) > MAX_JOURNAL_OUTPUT_BYTES {
        return false;
    }
    path.push_str(line);
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
        (FlowEvent::Invoke { args, .. }, "invoke_argument") => args.get(transfer.argument?),
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
