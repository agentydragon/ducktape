//! Opt-in, bounded selected-origin producer journal for source-flow diagnosis.
//!
//! This is diagnostic-only. It records connected value-source witnesses at
//! existing transfer points, then checks selected targets against the
//! completed fact map without registering subscriptions. Witnesses are
//! first-recorded may-sources, not execution causality.

use super::{FlowEvent, FlowFact, FlowKey, Kind, OriginId, OriginTable, Value, Values};
use roaring::RoaringBitmap;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc;

const MAX_JOURNAL_OUTPUT_BYTES: usize = 16 * 1024 * 1024;
const MAX_JOURNAL_ESTIMATED_BYTES: usize = 64 * 1024 * 1024;
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

#[derive(Default)]
struct JournalState {
    connected: ConnectedState,
}

const MAX_CONNECTED_GROUPS: usize = 250_000;
const ESTIMATED_WITNESS_BYTES: usize = 1_024;
const ESTIMATED_BITMAP_CONTAINER_BYTES: usize = 8_256;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
enum InputWitness {
    Seed(Value),
    Parent { witness: usize, key: FlowKey },
}

#[derive(Clone, Default)]
pub(super) struct InputSnapshot {
    sources: Vec<Option<InputWitness>>,
}

pub(super) struct SourceCapture<'a> {
    trace: &'a OriginTrace,
    sources: Vec<Option<InputWitness>>,
}

impl SourceCapture<'_> {
    pub(super) fn fact(&mut self, key: FlowKey, values: &Values) {
        for (index, origin) in self.trace.origins.iter().enumerate() {
            if self.sources[index].is_some() || !self.trace.contains(values, *origin) {
                continue;
            }
            if let Some(output) = self.trace.connected_output(key, *origin) {
                self.sources[index] = Some(InputWitness::Parent {
                    witness: output.witness,
                    key: output.key,
                });
            }
        }
    }

    pub(super) fn seed(&mut self, value: Value) {
        if let Some(origin) = self.trace.origin_table.ids.get(&value)
            && let Some(index) = self.trace.origin_indices.get(origin)
            && self.sources[*index].is_none()
        {
            self.sources[*index] = Some(InputWitness::Seed(value));
        }
    }

    pub(super) fn merge(&mut self, other: &InputSnapshot) {
        for (source, incoming) in self.sources.iter_mut().zip(&other.sources) {
            if source.is_none() {
                *source = *incoming;
            }
        }
    }

    pub(super) fn child(&self) -> Self {
        Self {
            trace: self.trace,
            sources: vec![None; self.sources.len()],
        }
    }

    pub(super) fn finish(self, values: &Values) -> InputSnapshot {
        InputSnapshot {
            sources: self
                .sources
                .into_iter()
                .zip(&self.trace.origins)
                .map(|(source, origin)| {
                    self.trace
                        .contains(values, *origin)
                        .then_some(source)
                        .flatten()
                })
                .collect(),
        }
    }
}

#[derive(Default)]
struct ConnectedState {
    witnesses: Vec<ConnectedWitness>,
    direct: HashMap<SliceNode, usize>,
    property_groups: Vec<PropertyGroup>,
    property_index: HashMap<(OriginId, super::PropertySlot), Vec<usize>>,
    property_groups_by_origin: HashMap<OriginId, Vec<usize>>,
    property_group_ids: HashMap<PropertyGroupKey, usize>,
    estimated_bytes: usize,
    saturated_at: Option<&'static str>,
    unresolved_inputs: usize,
    omitted_duplicate_outputs: usize,
}

struct ConnectedWitness {
    origin: OriginId,
    event: usize,
    kind: &'static str,
    target: Option<u32>,
    source: InputWitness,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
struct PropertyGroupKey {
    origin: OriginId,
    event: usize,
    property: super::PropertySlot,
    source: InputWitness,
}

struct PropertyGroup {
    witness: usize,
    property: super::PropertySlot,
    numeric: bool,
    receivers: RoaringBitmap,
    high_containers: HashSet<u16>,
}

#[derive(Clone, Copy)]
struct ConnectedOutput {
    witness: usize,
    key: FlowKey,
}

fn lookup_connected(
    connected: &ConnectedState,
    key: FlowKey,
    origin: OriginId,
) -> Option<ConnectedOutput> {
    if let Some(witness) = connected.direct.get(&SliceNode { origin, key }) {
        return Some(ConnectedOutput {
            witness: *witness,
            key,
        });
    }
    let (receiver, property, numeric_only) = match key {
        FlowKey::Property(receiver, property) => (receiver, Some(property), false),
        FlowKey::Properties(receiver) => (receiver, None, false),
        FlowKey::NumericProperties(receiver) => (receiver, None, true),
        _ => return None,
    };
    let mut find = |candidates: &[usize]| {
        candidates.iter().find_map(|group_index| {
            let group = &connected.property_groups[*group_index];
            (group.receivers.contains(receiver) && (!numeric_only || group.numeric)).then_some(
                ConnectedOutput {
                    witness: group.witness,
                    key: FlowKey::Property(receiver, group.property),
                },
            )
        })
    };
    if let Some(property) = property {
        find(connected.property_index.get(&(origin, property))?)
    } else {
        find(connected.property_groups_by_origin.get(&origin)?)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
struct SliceNode {
    origin: OriginId,
    key: FlowKey,
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
    origin_indices: HashMap<OriginId, usize>,
    origin_sources: BTreeMap<OriginId, (String, u32)>,
    origin_table: Arc<OriginTable>,
    targets: Vec<Target>,
    key_labels: KeyLabels,
    /// Captures connected sources at the exact value-contribution sites.
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
            origin_indices: origins
                .keys()
                .copied()
                .enumerate()
                .map(|(index, origin)| (origin, index))
                .collect(),
            origin_sources: origins,
            origin_table,
            targets,
            key_labels,
            state: RefCell::default(),
        }
    }

    pub(super) fn begin_capture(&self) -> SourceCapture<'_> {
        SourceCapture {
            trace: self,
            sources: vec![None; self.origins.len()],
        }
    }

    fn connected_output(&self, key: FlowKey, origin: OriginId) -> Option<ConnectedOutput> {
        let state = self.state.borrow();
        lookup_connected(&state.connected, key, origin)
    }

    pub(super) fn record_value_output(
        &self,
        event: usize,
        kind: &'static str,
        target: Option<u32>,
        key: FlowKey,
        values: &Values,
        sources: &InputSnapshot,
    ) {
        let mut state = self.state.borrow_mut();
        let connected = &mut state.connected;
        if connected.saturated_at.is_some() {
            return;
        }
        for (index, origin) in self.origins.iter().enumerate() {
            if !self.contains(values, *origin) {
                continue;
            }
            let Some(source) = sources.sources.get(index).copied().flatten() else {
                connected.unresolved_inputs = connected.unresolved_inputs.saturating_add(1);
                continue;
            };
            let node = SliceNode {
                origin: *origin,
                key,
            };
            if connected.direct.contains_key(&node) {
                connected.omitted_duplicate_outputs =
                    connected.omitted_duplicate_outputs.saturating_add(1);
                continue;
            }
            if connected.witnesses.len() >= MAX_CONNECTED_GROUPS
                || connected.estimated_bytes + ESTIMATED_WITNESS_BYTES > MAX_JOURNAL_ESTIMATED_BYTES
            {
                connected.saturated_at = Some("witness_budget");
                eprintln!(
                    "origin-journal connected_capture_incomplete cap=witness_budget witnesses={} estimated_bytes={}",
                    connected.witnesses.len(),
                    connected.estimated_bytes
                );
                return;
            }
            let witness = connected.witnesses.len();
            if let InputWitness::Parent {
                witness: parent, ..
            } = source
                && parent >= witness
            {
                connected.unresolved_inputs = connected.unresolved_inputs.saturating_add(1);
                continue;
            }
            connected.witnesses.push(ConnectedWitness {
                origin: *origin,
                event,
                kind,
                target,
                source,
            });
            connected.direct.insert(node, witness);
            connected.estimated_bytes += ESTIMATED_WITNESS_BYTES;
        }
    }

    pub(super) fn record_property_output(
        &self,
        event: usize,
        receiver: u32,
        property: super::PropertySlot,
        numeric: bool,
        values: &Values,
        sources: &InputSnapshot,
    ) {
        let mut state = self.state.borrow_mut();
        let connected = &mut state.connected;
        if connected.saturated_at.is_some() {
            return;
        }
        for (index, origin) in self.origins.iter().enumerate() {
            if !self.contains(values, *origin) {
                continue;
            }
            let Some(source) = sources.sources.get(index).copied().flatten() else {
                connected.unresolved_inputs = connected.unresolved_inputs.saturating_add(1);
                continue;
            };
            let group_key = PropertyGroupKey {
                origin: *origin,
                event,
                property,
                source,
            };
            let group_index = if let Some(group) = connected.property_group_ids.get(&group_key) {
                *group
            } else {
                if connected.witnesses.len() >= MAX_CONNECTED_GROUPS
                    || connected.estimated_bytes + ESTIMATED_WITNESS_BYTES
                        > MAX_JOURNAL_ESTIMATED_BYTES
                {
                    connected.saturated_at = Some("group_budget");
                    eprintln!(
                        "origin-journal connected_capture_incomplete cap=group_budget groups={} estimated_bytes={}",
                        connected.property_groups.len(),
                        connected.estimated_bytes
                    );
                    return;
                }
                let witness = connected.witnesses.len();
                if let InputWitness::Parent {
                    witness: parent, ..
                } = source
                    && parent >= witness
                {
                    connected.unresolved_inputs = connected.unresolved_inputs.saturating_add(1);
                    continue;
                }
                connected.witnesses.push(ConnectedWitness {
                    origin: *origin,
                    event,
                    kind: "property_write",
                    target: None,
                    source,
                });
                let group = connected.property_groups.len();
                connected.property_groups.push(PropertyGroup {
                    witness,
                    property,
                    numeric,
                    receivers: RoaringBitmap::new(),
                    high_containers: HashSet::new(),
                });
                connected.property_group_ids.insert(group_key, group);
                connected
                    .property_index
                    .entry((*origin, property))
                    .or_default()
                    .push(group);
                connected
                    .property_groups_by_origin
                    .entry(*origin)
                    .or_default()
                    .push(group);
                connected.estimated_bytes += ESTIMATED_WITNESS_BYTES;
                group
            };
            let high = (receiver >> 16) as u16;
            let needs_container = {
                let group = &connected.property_groups[group_index];
                !group.receivers.contains(receiver) && !group.high_containers.contains(&high)
            };
            if needs_container {
                if connected.estimated_bytes + ESTIMATED_BITMAP_CONTAINER_BYTES
                    > MAX_JOURNAL_ESTIMATED_BYTES
                {
                    connected.saturated_at = Some("bitmap_budget");
                    eprintln!(
                        "origin-journal connected_capture_incomplete cap=bitmap_budget groups={} estimated_bytes={}",
                        connected.property_groups.len(),
                        connected.estimated_bytes
                    );
                    return;
                }
                connected.property_groups[group_index]
                    .high_containers
                    .insert(high);
                connected.estimated_bytes += ESTIMATED_BITMAP_CONTAINER_BYTES;
            }
            connected.property_groups[group_index]
                .receivers
                .insert(receiver);
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

    pub(super) fn report_connected(
        &self,
        facts: &BTreeMap<FlowKey, FlowFact>,
        events: &[FlowEvent],
    ) {
        let state = self.state.borrow();
        let connected = &state.connected;
        let mut output_bytes = 0usize;
        let mut emitted_steps = 0usize;
        let mut output_truncated = false;
        'targets: for target in &self.targets {
            let Some(FlowFact::Values(values)) = facts.get(&target.key) else {
                continue;
            };
            for origin in &self.origins {
                if !self.contains(values, *origin) {
                    continue;
                }
                let Some(mut cursor) = lookup_connected(connected, target.key, *origin) else {
                    let line = format!(
                        "origin-journal connected_target module={} binding={:?} key={:?} key_label={:?} origin={:?} witness=false cause=not_recorded_or_capture_saturated\n",
                        target.module,
                        target.name,
                        target.key,
                        self.key_label(target.key),
                        self.origin_sources.get(origin),
                    );
                    if !emit_limited_line(&line, &mut output_bytes) {
                        output_truncated = true;
                        break 'targets;
                    }
                    continue;
                };
                let root = cursor;
                let mut path = Vec::new();
                let mut seen = HashSet::new();
                let mut seed = None;
                let mut invalid_parent = false;
                loop {
                    if !seen.insert(cursor.witness) {
                        invalid_parent = true;
                        break;
                    }
                    let Some(witness) = connected.witnesses.get(cursor.witness) else {
                        invalid_parent = true;
                        break;
                    };
                    if witness.origin != *origin {
                        invalid_parent = true;
                        break;
                    }
                    path.push(cursor);
                    match witness.source {
                        InputWitness::Seed(value) => {
                            seed = Some(value);
                            break;
                        }
                        InputWitness::Parent { witness, key } => {
                            if witness >= cursor.witness {
                                invalid_parent = true;
                                break;
                            }
                            cursor = ConnectedOutput { witness, key };
                        }
                    }
                }
                let Some(seed) = seed else {
                    let line = format!(
                        "origin-journal connected_witness_incomplete module={} binding={:?} origin={:?} cause={}\n",
                        target.module,
                        target.name,
                        self.origin_sources.get(origin),
                        if invalid_parent {
                            "invalid_or_cyclic_parent"
                        } else {
                            "missing_seed"
                        },
                    );
                    if !emit_limited_line(&line, &mut output_bytes) {
                        output_truncated = true;
                        break 'targets;
                    }
                    continue;
                };
                path.reverse();
                let mut text = String::new();
                let header = format!(
                    "origin-journal connected_witness module={} binding={:?} context={} root={:?} root_label={:?} source_origin={:?} steps={} semantics=connected_recorded_may_source_not_execution_causality;omitted_alternatives_and_unrecorded_producers_remain_possible\n",
                    target.module,
                    target.name,
                    target.context,
                    root.key,
                    self.key_label(root.key),
                    self.origin_sources.get(origin),
                    path.len(),
                );
                let mut complete = append_bounded_path_line(&mut text, &header);
                for link in &path {
                    if !complete {
                        break;
                    }
                    let Some(witness) = connected.witnesses.get(link.witness) else {
                        complete = false;
                        break;
                    };
                    let Some(event) = events.get(witness.event) else {
                        complete = false;
                        break;
                    };
                    let (receiver, property, reported_target, target_site) = match link.key {
                        FlowKey::Property(receiver, property) => {
                            (Some(receiver), Some(property), None, None)
                        }
                        _ => (None, None, witness.target, witness.target),
                    };
                    let line = format!(
                        "origin-journal source event={} event_kind={} kind={} output={:?} output_label={:?} receiver={:?} property={:?} property_label={:?} target={:?} target_site={:?} origin={:?}\n",
                        witness.event,
                        event_kind(event),
                        witness.kind,
                        link.key,
                        self.key_label(link.key),
                        receiver,
                        property,
                        property.map(|property| self
                            .key_labels
                            .properties
                            .get(property.0 as usize)
                            .map(String::as_str)
                            .unwrap_or("<property>")),
                        reported_target,
                        target_site.map(|site| self.site_label(site)),
                        self.origin_sources.get(origin),
                    );
                    complete = append_bounded_path_line(&mut text, &line);
                    if !complete {
                        break;
                    }
                    if let InputWitness::Parent { key, .. } = witness.source {
                        let edge = format!(
                            "origin-journal witness_input from={:?} from_label={:?} to={:?} to_label={:?} event={} origin={:?}\n",
                            link.key,
                            self.key_label(link.key),
                            key,
                            self.key_label(key),
                            witness.event,
                            self.origin_sources.get(origin),
                        );
                        complete = append_bounded_path_line(&mut text, &edge);
                    }
                }
                if complete {
                    let line = format!(
                        "origin-journal witness_seed value={:?} origin={:?}\n",
                        seed,
                        self.origin_sources.get(origin),
                    );
                    complete = append_bounded_path_line(&mut text, &line);
                }
                if !complete || output_bytes.saturating_add(text.len()) > MAX_JOURNAL_OUTPUT_BYTES {
                    output_truncated = true;
                    break 'targets;
                }
                output_bytes += text.len();
                eprint!("{text}");
                emitted_steps += path.len();
            }
        }
        eprintln!(
            "origin-journal connected_summary targets={} selected_origins={} witnesses={} property_groups={} estimated_bytes={} estimated_bytes_scope=journal_witness_indexes_and_receiver_containers_only group_cap={} byte_cap={} saturated_at={:?} unresolved_input_observations={} duplicate_outputs={} emitted_steps={} output_bytes={} output_truncated={} hard_output_bytes={} semantics=first_connected_recorded_may_source_witness;not_an_execution_trace;absence_of_witness_is_not_absence_of_flow",
            self.targets.len(),
            self.origins.len(),
            connected.witnesses.len(),
            connected.property_groups.len(),
            connected.estimated_bytes,
            MAX_CONNECTED_GROUPS,
            MAX_JOURNAL_ESTIMATED_BYTES,
            connected.saturated_at,
            connected.unresolved_inputs,
            connected.omitted_duplicate_outputs,
            emitted_steps,
            output_bytes,
            output_truncated,
            MAX_JOURNAL_OUTPUT_BYTES,
        );
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
