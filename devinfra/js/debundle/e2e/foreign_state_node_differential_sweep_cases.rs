//! Node-differential sweep over cross-chunk stateful provider shapes: for
//! every generated program and split, a spec the gate accepts must emit an
//! entry that behaves like its source under Node.
//!
//! Each case is the product of a provider state shape, the path an importer
//! takes to that state's reader and writer, and the source order of the two
//! statements. The second statement always moves to a logical module, so the
//! emitted program evaluates it first. The sweep asserts no particular
//! verdict per case: a refusal is always sound, and an acceptance is checked
//! against Node.

use debundle_e2e_support::*;

/// A provider chunk `static/state` exporting a writer `next` and a reader
/// `peek` of one piece of module state.
struct StateShape {
    id: &'static str,
    provider: &'static str,
}

const STATE_SHAPES: &[StateShape] = &[
    StateShape {
        id: "let_rebound",
        provider: "let count = 0;\n\
                   export function next() { return ++count; }\n\
                   export function peek() { return count; }\n",
    },
    StateShape {
        id: "var_reassigned",
        provider: "var count = 0;\n\
                   export function next() { count = count + 1; return count; }\n\
                   export function peek() { return count; }\n",
    },
    StateShape {
        id: "object_property",
        provider: "const state = { count: 0 };\n\
                   export function next() { return ++state.count; }\n\
                   export function peek() { return state.count; }\n",
    },
    StateShape {
        id: "array_contents",
        provider: "const items = [];\n\
                   export function next() { items.push(1); return items.length; }\n\
                   export function peek() { return items.length; }\n",
    },
    StateShape {
        id: "array_element",
        provider: "const cells = [0];\n\
                   export function next() { cells[0] = cells[0] + 1; return cells[0]; }\n\
                   export function peek() { return cells[0]; }\n",
    },
    StateShape {
        id: "map_entries",
        provider: "const entries = new Map();\n\
                   export function next() { entries.set(entries.size, 1); return entries.size; }\n\
                   export function peek() { return entries.size; }\n",
    },
    StateShape {
        id: "class_static_field",
        provider: "class Counter { static count = 0; }\n\
                   export function next() { return ++Counter.count; }\n\
                   export function peek() { return Counter.count; }\n",
    },
    StateShape {
        id: "function_property",
        provider: "export function next() { next.count = (next.count || 0) + 1; return next.count; }\n\
                   export function peek() { return next.count || 0; }\n",
    },
    StateShape {
        id: "closure_cell",
        provider: "function makeCounter() {\n\
                     let count = 0;\n\
                     return { next() { return ++count; }, peek() { return count; } };\n\
                   }\n\
                   const counter = makeCounter();\n\
                   export function next() { return counter.next(); }\n\
                   export function peek() { return counter.peek(); }\n",
    },
    StateShape {
        id: "function_rebound",
        provider: "let current = () => 0;\n\
                   export function next() { const value = current() + 1; current = () => value; return value; }\n\
                   export function peek() { return current(); }\n",
    },
    StateShape {
        id: "global_property",
        provider: "globalThis.sweepCount = 0;\n\
                   export function next() { return ++globalThis.sweepCount; }\n\
                   export function peek() { return globalThis.sweepCount; }\n",
    },
    StateShape {
        id: "object_alias",
        provider: "const state = { count: 0 };\n\
                   const view = state;\n\
                   export function next() { state.count = state.count + 1; return state.count; }\n\
                   export function peek() { const { count } = view; return count; }\n",
    },
];

/// How the entry chunk reaches the provider's writer and reader: its import
/// lines, the two call expressions, any local helper declarations, and the
/// chunks between the entry and `static/state`.
struct AccessPath {
    id: &'static str,
    imports: &'static str,
    helpers: &'static str,
    write: &'static str,
    read: &'static str,
    chunks: &'static [(&'static str, &'static str)],
}

const ACCESS_PATHS: &[AccessPath] = &[
    AccessPath {
        id: "named_imports",
        imports: "import { next, peek } from './state.js';\n",
        helpers: "",
        write: "next()",
        read: "peek()",
        chunks: &[],
    },
    AccessPath {
        id: "reexport_facade",
        imports: "import { next, peek } from './facade.js';\n",
        helpers: "",
        write: "next()",
        read: "peek()",
        chunks: &[(
            "static/facade",
            "export { next, peek } from './state.js';\n",
        )],
    },
    AccessPath {
        id: "import_local_export_facade",
        imports: "import { next, peek } from './facade.js';\n",
        helpers: "",
        write: "next()",
        read: "peek()",
        chunks: &[(
            "static/facade",
            "import { next, peek } from './state.js';\nexport { next, peek };\n",
        )],
    },
    AccessPath {
        id: "namespace_import",
        imports: "import * as state from './state.js';\n",
        helpers: "",
        write: "state.next()",
        read: "state.peek()",
        chunks: &[],
    },
    AccessPath {
        id: "separate_wrapper_chunks",
        imports: "import { write } from './writer.js';\nimport { read } from './reader.js';\n",
        helpers: "",
        write: "write()",
        read: "read()",
        chunks: &[
            (
                "static/writer",
                "import { next } from './state.js';\nexport function write() { return next(); }\n",
            ),
            (
                "static/reader",
                "import { peek } from './state.js';\nexport function read() { return peek(); }\n",
            ),
        ],
    },
    AccessPath {
        id: "arrow_wrapper_chunk",
        imports: "import { read, write } from './wrappers.js';\n",
        helpers: "",
        write: "write()",
        read: "read()",
        chunks: &[(
            "static/wrappers",
            "import { next, peek } from './state.js';\n\
             export const write = () => next();\n\
             export const read = () => peek();\n",
        )],
    },
    AccessPath {
        id: "exported_object_methods",
        imports: "import { api } from './api.js';\n",
        helpers: "",
        write: "api.write()",
        read: "api.read()",
        chunks: &[(
            "static/api",
            "import { next, peek } from './state.js';\n\
             export const api = { write() { return next(); }, read() { return peek(); } };\n",
        )],
    },
    AccessPath {
        id: "local_wrapper_functions",
        imports: "import { next, peek } from './state.js';\n",
        helpers: "function write() { return next(); }\nfunction read() { return peek(); }\n",
        write: "write()",
        read: "read()",
        chunks: &[],
    },
    AccessPath {
        id: "local_arrow_wrappers",
        imports: "import { next, peek } from './state.js';\n",
        helpers: "const write = () => next();\nconst read = () => peek();\n",
        write: "write()",
        read: "read()",
        chunks: &[],
    },
    AccessPath {
        id: "provider_invoked_callbacks",
        imports: "import { next, peek } from './state.js';\nimport { apply } from './apply.js';\n",
        helpers: "",
        write: "apply(next)",
        read: "apply(peek)",
        chunks: &[(
            "static/apply",
            "export function apply(callback) { return callback(); }\n",
        )],
    },
    AccessPath {
        id: "native_invoked_callbacks",
        imports: "import { next, peek } from './state.js';\n",
        helpers: "",
        write: "[0].map(next)[0]",
        read: "[0].map(peek)[0]",
        chunks: &[],
    },
    AccessPath {
        id: "immediately_invoked_functions",
        imports: "import { next, peek } from './state.js';\n",
        helpers: "",
        write: "(() => next())()",
        read: "(() => peek())()",
        chunks: &[],
    },
    AccessPath {
        id: "reader_second_importer_chunk",
        imports: "import { next } from './state.js';\nimport { read } from './reader.js';\n",
        helpers: "",
        write: "next()",
        read: "read()",
        chunks: &[(
            "static/reader",
            "import { peek } from './state.js';\nexport function read() { return peek(); }\n",
        )],
    },
    AccessPath {
        id: "getter_exported_object",
        imports: "import { next } from './state.js';\nimport { view } from './view.js';\n",
        helpers: "",
        write: "next()",
        read: "view.value",
        chunks: &[(
            "static/view",
            "import { peek } from './state.js';\nexport const view = { get value() { return peek(); } };\n",
        )],
    },
    AccessPath {
        id: "template_reader",
        imports: "import { next, peek } from './state.js';\n",
        helpers: "",
        write: "next()",
        read: "`${peek()}`",
        chunks: &[],
    },
];

/// Programs whose reader is not a call: the entry reads an imported binding
/// or one of its properties directly.
struct DirectRead {
    id: &'static str,
    provider: &'static str,
    imports: &'static str,
    write: &'static str,
    read: &'static str,
    /// Chunks between the entry and `static/state`.
    chunks: &'static [(&'static str, &'static str)],
}

const DIRECT_READS: &[DirectRead] = &[
    DirectRead {
        id: "live_binding",
        provider: "export let count = 0;\nexport function next() { return ++count; }\n",
        imports: "import { count, next } from './state.js';\n",
        write: "next()",
        read: "count",
        chunks: &[],
    },
    DirectRead {
        id: "live_binding_reexport",
        provider: "export let count = 0;\nexport function next() { return ++count; }\n",
        imports: "import { count, next } from './facade.js';\n",
        write: "next()",
        read: "count",
        chunks: &[(
            "static/facade",
            "export { count, next } from './state.js';\n",
        )],
    },
    DirectRead {
        id: "live_binding_object_literal",
        provider: "export let count = 0;\nexport function next() { return ++count; }\n",
        imports: "import { count, next } from './state.js';\n",
        write: "next()",
        read: "({ count }).count",
        chunks: &[],
    },
    DirectRead {
        id: "live_default_export_binding",
        provider: "let count = 0;\nexport { count as default };\nexport function next() { return ++count; }\n",
        imports: "import count, { next } from './state.js';\n",
        write: "next()",
        read: "count",
        chunks: &[],
    },
    DirectRead {
        id: "imported_object_property",
        provider: "export const state = { count: 0 };\nexport function next() { return ++state.count; }\n",
        imports: "import { next, state } from './state.js';\n",
        write: "next()",
        read: "state.count",
        chunks: &[],
    },
    DirectRead {
        id: "imported_array_length",
        provider: "export const items = [];\nexport function next() { items.push(1); return items.length; }\n",
        imports: "import { items, next } from './state.js';\n",
        write: "next()",
        read: "items.length",
        chunks: &[],
    },
    DirectRead {
        id: "imported_array_element",
        provider: "export const cells = [0];\nexport function next() { cells[0] = cells[0] + 1; return cells[0]; }\n",
        imports: "import { cells, next } from './state.js';\n",
        write: "next()",
        read: "cells[0]",
        chunks: &[],
    },
    DirectRead {
        id: "namespace_member_live_binding",
        provider: "export let count = 0;\nexport function next() { return ++count; }\n",
        imports: "import * as state from './state.js';\n",
        write: "state.next()",
        read: "state.count",
        chunks: &[],
    },
    DirectRead {
        id: "class_static_field_live_binding",
        provider: "export let count = 0;\nexport function next() { return ++count; }\n",
        imports: "import { count, next } from './state.js';\n",
        write: "next()",
        read: "(class { static value = count; }).value",
        chunks: &[],
    },
    DirectRead {
        id: "importer_side_setter_live_binding",
        provider: "export let count = 0;\nexport function set(value) { count = value; return value; }\n",
        imports: "import { count, set } from './state.js';\n",
        write: "set(1)",
        read: "count",
        chunks: &[],
    },
];

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum SweepFamily {
    ProviderBindingCells,
    ProviderCollections,
    ProviderObjectState,
    ProviderIndirectState,
    DirectReads,
    StatelessProviderCalls,
}

impl SweepFamily {
    fn label(self) -> &'static str {
        match self {
            Self::ProviderBindingCells => "provider binding cells",
            Self::ProviderCollections => "provider collections",
            Self::ProviderObjectState => "provider object state",
            Self::ProviderIndirectState => "provider indirect state",
            Self::DirectReads => "direct reads",
            Self::StatelessProviderCalls => "stateless provider calls",
        }
    }
}

struct SweepCase {
    id: String,
    family: SweepFamily,
    scenario_index: usize,
    variant_index: usize,
    imports: &'static str,
    helpers: &'static str,
    first: &'static str,
    second: &'static str,
    chunks: Vec<(&'static str, &'static str)>,
    dataflow_aware: bool,
}

struct SweepScenario {
    id: String,
    imports: &'static str,
    helpers: &'static str,
    write: &'static str,
    read: &'static str,
    chunks: Vec<(&'static str, &'static str)>,
}

fn append_scenario_cases(
    cases: &mut Vec<SweepCase>,
    family: SweepFamily,
    scenario_index: usize,
    scenario: SweepScenario,
) {
    for (order_index, (order_id, first, second)) in [
        ("writer_then_reader", scenario.write, scenario.read),
        ("reader_then_writer", scenario.read, scenario.write),
    ]
    .into_iter()
    .enumerate()
    {
        for (flow_index, dataflow_aware) in [false, true].into_iter().enumerate() {
            let flow_id = if dataflow_aware {
                "dataflow_aware"
            } else {
                "dataflow_unaware"
            };
            cases.push(SweepCase {
                id: format!("{}/{order_id}/{flow_id}", scenario.id),
                family,
                scenario_index,
                variant_index: order_index * 2 + flow_index,
                imports: scenario.imports,
                helpers: scenario.helpers,
                first,
                second,
                chunks: scenario.chunks.clone(),
                dataflow_aware,
            });
        }
    }
}

fn case_registry() -> Vec<SweepCase> {
    let mut cases = Vec::with_capacity(820);
    for (family, shapes) in [
        (SweepFamily::ProviderBindingCells, &STATE_SHAPES[..3]),
        (SweepFamily::ProviderCollections, &STATE_SHAPES[3..6]),
        (SweepFamily::ProviderObjectState, &STATE_SHAPES[6..9]),
        (SweepFamily::ProviderIndirectState, &STATE_SHAPES[9..]),
    ] {
        for (shape_index, shape) in shapes.iter().enumerate() {
            for (path_index, path) in ACCESS_PATHS.iter().enumerate() {
                let mut chunks = vec![("static/state", shape.provider)];
                chunks.extend_from_slice(path.chunks);
                append_scenario_cases(
                    &mut cases,
                    family,
                    shape_index * ACCESS_PATHS.len() + path_index,
                    SweepScenario {
                        id: format!("state/{}/{}", shape.id, path.id),
                        imports: path.imports,
                        helpers: path.helpers,
                        write: path.write,
                        read: path.read,
                        chunks,
                    },
                );
            }
        }
    }
    for (scenario_index, direct) in DIRECT_READS.iter().enumerate() {
        let mut chunks = vec![("static/state", direct.provider)];
        chunks.extend_from_slice(direct.chunks);
        append_scenario_cases(
            &mut cases,
            SweepFamily::DirectReads,
            scenario_index,
            SweepScenario {
                id: format!("direct/{}", direct.id),
                imports: direct.imports,
                helpers: "",
                write: direct.write,
                read: direct.read,
                chunks,
            },
        );
    }
    let stateless_provider = "const base = 1;\n\
        export function next() { return base + 1; }\n\
        export function peek() { return base; }\n";
    for (scenario_index, path) in ACCESS_PATHS.iter().enumerate() {
        let mut chunks = vec![("static/state", stateless_provider)];
        chunks.extend_from_slice(path.chunks);
        append_scenario_cases(
            &mut cases,
            SweepFamily::StatelessProviderCalls,
            scenario_index,
            SweepScenario {
                id: format!("stateless/{}", path.id),
                imports: path.imports,
                helpers: path.helpers,
                write: path.write,
                read: path.read,
                chunks,
            },
        );
    }
    cases
}

fn partition() -> (usize, usize) {
    let count = std::env::var("DEBUNDLE_SWEEP_PARTITION_COUNT")
        .ok()
        .map(|value| {
            value
                .parse::<usize>()
                .expect("partition count must be an integer")
        })
        .unwrap_or(1);
    let index = std::env::var("DEBUNDLE_SWEEP_PARTITION_INDEX")
        .ok()
        .map(|value| {
            value
                .parse::<usize>()
                .expect("partition index must be an integer")
        })
        .unwrap_or(0);
    assert!(count > 0, "partition count must be positive");
    assert!(index < count, "partition index must be less than count");
    (index, count)
}

fn partition_owner(case: &SweepCase, count: usize) -> usize {
    (case.scenario_index + case.variant_index) % count
}

fn fnv1a(bytes: impl IntoIterator<Item = u8>) -> u64 {
    bytes.into_iter().fold(0xcbf29ce484222325, |hash, byte| {
        (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3)
    })
}

fn digest_case_ids<'a>(ids: impl IntoIterator<Item = &'a str>) -> String {
    let hash = fnv1a(ids.into_iter().flat_map(|id| id.bytes().chain(*b"\n")));
    format!("{hash:016x}")
}

fn digest_roster(cases: &[SweepCase]) -> String {
    let hash = fnv1a(cases.iter().flat_map(|case| {
        let mut bytes = Vec::new();
        for part in [
            case.id.as_str(),
            case.imports,
            case.helpers,
            case.first,
            case.second,
        ] {
            bytes.extend_from_slice(part.as_bytes());
            bytes.push(0);
        }
        bytes.push(u8::from(case.dataflow_aware));
        for (path, source) in &case.chunks {
            bytes.extend_from_slice(path.as_bytes());
            bytes.push(0);
            bytes.extend_from_slice(source.as_bytes());
            bytes.push(0);
        }
        bytes.into_iter()
    }));
    format!("{hash:016x}")
}

/// Assert that the registry has the original family sizes and that each
/// supported manual partition count covers every row exactly once.
pub fn assert_case_registry() {
    let cases = case_registry();
    assert_eq!(cases.len(), 820, "the differential sweep roster changed");
    let ids: Vec<_> = cases.iter().map(|case| case.id.as_str()).collect();
    let unique_ids: std::collections::HashSet<_> = ids.iter().copied().collect();
    assert_eq!(unique_ids.len(), 820, "case IDs must be unique");
    let id_digest = digest_case_ids(ids.iter().copied());
    let roster_digest = digest_roster(&cases);
    eprintln!("case registry: rows=820 id_fnv1a64={id_digest} roster_fnv1a64={roster_digest}");
    for (family, expected_count) in [
        (SweepFamily::ProviderBindingCells, 180),
        (SweepFamily::ProviderCollections, 180),
        (SweepFamily::ProviderObjectState, 180),
        (SweepFamily::ProviderIndirectState, 180),
        (SweepFamily::DirectReads, 40),
        (SweepFamily::StatelessProviderCalls, 60),
    ] {
        assert_eq!(
            cases.iter().filter(|case| case.family == family).count(),
            expected_count,
            "{} family changed size",
            family.label()
        );
    }
    let mut control_ids = std::collections::HashSet::new();
    for control in REQUIRED_CONTROL_CASES {
        assert!(
            control_ids.insert(control.id),
            "control case IDs must be unique: {}",
            control.id
        );
        let case = cases
            .iter()
            .find(|case| case.id == control.id)
            .unwrap_or_else(|| panic!("required control case missing: {}", control.id));
        assert_eq!(
            case.family, control.family,
            "control family changed: {}",
            control.id
        );
    }

    for partitions in [1, 2, 4] {
        let mut observed = std::collections::HashSet::new();
        for index in 0..partitions {
            let owned: Vec<_> = cases
                .iter()
                .filter(|case| partition_owner(case, partitions) == index)
                .collect();
            assert_eq!(
                owned.len(),
                820 / partitions,
                "partition {index}/{partitions} is not balanced"
            );
            for case in owned {
                assert!(
                    observed.insert(case.id.as_str()),
                    "case assigned twice: {}",
                    case.id
                );
            }
        }
        assert_eq!(observed.len(), 820, "partition set {partitions} lost cases");
    }
}

#[derive(Default)]
struct Tally {
    accepted: usize,
    refused: usize,
    /// Refusals whose emitted entry diverges from the source: the cases
    /// where an acceptance would have been unsound.
    refused_divergent: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CaseOutcome {
    AcceptedMatch,
    RefusedMatch,
    RefusedDivergent,
}

#[derive(Clone, Copy)]
struct ControlCase {
    id: &'static str,
    family: SweepFamily,
    expected: CaseOutcome,
}

const REQUIRED_CONTROL_CASES: &[ControlCase] = &[
    ControlCase {
        id: "state/let_rebound/named_imports/writer_then_reader/dataflow_unaware",
        family: SweepFamily::ProviderBindingCells,
        expected: CaseOutcome::RefusedDivergent,
    },
    ControlCase {
        id: "state/array_contents/named_imports/writer_then_reader/dataflow_unaware",
        family: SweepFamily::ProviderCollections,
        expected: CaseOutcome::RefusedDivergent,
    },
    ControlCase {
        id: "state/class_static_field/named_imports/writer_then_reader/dataflow_unaware",
        family: SweepFamily::ProviderObjectState,
        expected: CaseOutcome::RefusedDivergent,
    },
    ControlCase {
        id: "state/function_rebound/named_imports/writer_then_reader/dataflow_unaware",
        family: SweepFamily::ProviderIndirectState,
        expected: CaseOutcome::RefusedDivergent,
    },
    ControlCase {
        id: "direct/live_binding/writer_then_reader/dataflow_unaware",
        family: SweepFamily::DirectReads,
        expected: CaseOutcome::RefusedDivergent,
    },
    ControlCase {
        id: "stateless/named_imports/writer_then_reader/dataflow_unaware",
        family: SweepFamily::StatelessProviderCalls,
        expected: CaseOutcome::AcceptedMatch,
    },
];

fn run_case(
    case: &SweepCase,
    tally: &mut Tally,
    expected_control: Option<CaseOutcome>,
) -> CaseOutcome {
    let source = format!(
        "{}{}const first = {};\nconst second = {};\nconsole.log(first, second);\n",
        case.imports, case.helpers, case.first, case.second
    );
    let mut opts = FixtureOpts::new(
        &source,
        vec![logical_module("second", &[Member::new("second")])],
    )
    .with_extra_chunks(&case.chunks)
    .with_unassigned_mode(unassigned_mode_inline());
    if case.dataflow_aware {
        opts = opts.with_dataflow_aware_s_chain();
    }
    let outcome = run_fixture_to_gate_outcome(opts);
    assert!(
        outcome.source.status.success(),
        "{}: the source does not run:\n{}",
        case.id,
        outcome.source.stderr
    );
    let same = outcome.emitted.status.success() && outcome.emitted.stdout == outcome.source.stdout;
    assert!(
        !outcome.accepted || same,
        "{}: the gate accepted a split whose entry diverges from the source; source status={:?}, stdout={:?}, stderr={:?}; emitted status={:?}, stdout={:?}, stderr={:?}",
        case.id,
        outcome.source.status,
        outcome.source.stdout,
        outcome.source.stderr,
        outcome.emitted.status,
        outcome.emitted.stdout,
        outcome.emitted.stderr
    );
    let observed = match (outcome.accepted, same) {
        (true, true) => CaseOutcome::AcceptedMatch,
        (true, false) => unreachable!("accepted divergence was asserted while outcome was alive"),
        (false, true) => CaseOutcome::RefusedMatch,
        (false, false) => CaseOutcome::RefusedDivergent,
    };
    if let Some(expected) = expected_control {
        assert_eq!(
            observed,
            expected,
            "{}: control expected {expected:?}, observed {observed:?}; source status={:?}, stdout={:?}, stderr={:?}; emitted status={:?}, stdout={:?}, stderr={:?}",
            case.id,
            outcome.source.status,
            outcome.source.stdout,
            outcome.source.stderr,
            outcome.emitted.status,
            outcome.emitted.stdout,
            outcome.emitted.stderr
        );
    }
    match observed {
        CaseOutcome::AcceptedMatch => tally.accepted += 1,
        CaseOutcome::RefusedMatch => tally.refused += 1,
        CaseOutcome::RefusedDivergent => {
            tally.refused += 1;
            tally.refused_divergent += 1;
        }
    }
    observed
}

fn report_tally(tally: &Tally, family: SweepFamily, index: usize, count: usize) {
    eprintln!(
        "{} partition {index}/{count}: accepted {}, refused {} ({} divergent)",
        family.label(),
        tally.accepted,
        tally.refused,
        tally.refused_divergent
    );
}

/// Run one of the original six semantic families. Experimental partition
/// targets select a disjoint deterministic subset; the registry test proves
/// their union is exactly the original row set.
pub fn run_family(family: SweepFamily) {
    let (partition_index, partition_count) = partition();
    let cases = case_registry();
    let family_cases: Vec<_> = cases.iter().filter(|case| case.family == family).collect();
    let selected: Vec<_> = family_cases
        .iter()
        .copied()
        .filter(|case| partition_owner(case, partition_count) == partition_index)
        .collect();
    assert!(!selected.is_empty(), "empty {} partition", family.label());
    let roster = digest_case_ids(selected.iter().map(|case| case.id.as_str()));
    eprintln!(
        "{} partition {partition_index}/{partition_count}: rows={} roster_fnv1a64={roster}",
        family.label(),
        selected.len()
    );
    let mut tally = Tally::default();
    for case in &selected {
        let expected_control = REQUIRED_CONTROL_CASES
            .iter()
            .find(|control| control.id == case.id)
            .map(|control| control.expected);
        run_case(case, &mut tally, expected_control);
    }
    report_tally(&tally, family, partition_index, partition_count);

    if partition_count == 1 {
        match family {
            SweepFamily::ProviderBindingCells
            | SweepFamily::ProviderCollections
            | SweepFamily::ProviderObjectState
            | SweepFamily::ProviderIndirectState
            | SweepFamily::DirectReads => {
                assert!(
                    tally.refused_divergent > 0,
                    "{} generated no split whose rejection prevented divergence",
                    family.label()
                );
            }
            SweepFamily::StatelessProviderCalls => {
                assert!(tally.accepted > 0, "no stateless split is accepted");
            }
        }
    }
}
