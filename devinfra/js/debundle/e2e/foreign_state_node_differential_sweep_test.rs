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
    name: &'static str,
    provider: &'static str,
}

const STATE_SHAPES: &[StateShape] = &[
    StateShape {
        name: "rebound let",
        provider: "let count = 0;\n\
                   export function next() { return ++count; }\n\
                   export function peek() { return count; }\n",
    },
    StateShape {
        name: "reassigned var",
        provider: "var count = 0;\n\
                   export function next() { count = count + 1; return count; }\n\
                   export function peek() { return count; }\n",
    },
    StateShape {
        name: "object property",
        provider: "const state = { count: 0 };\n\
                   export function next() { return ++state.count; }\n\
                   export function peek() { return state.count; }\n",
    },
    StateShape {
        name: "array contents",
        provider: "const items = [];\n\
                   export function next() { items.push(1); return items.length; }\n\
                   export function peek() { return items.length; }\n",
    },
    StateShape {
        name: "array element",
        provider: "const cells = [0];\n\
                   export function next() { cells[0] = cells[0] + 1; return cells[0]; }\n\
                   export function peek() { return cells[0]; }\n",
    },
    StateShape {
        name: "map entries",
        provider: "const entries = new Map();\n\
                   export function next() { entries.set(entries.size, 1); return entries.size; }\n\
                   export function peek() { return entries.size; }\n",
    },
    StateShape {
        name: "class static field",
        provider: "class Counter { static count = 0; }\n\
                   export function next() { return ++Counter.count; }\n\
                   export function peek() { return Counter.count; }\n",
    },
    StateShape {
        name: "function property",
        provider: "export function next() { next.count = (next.count || 0) + 1; return next.count; }\n\
                   export function peek() { return next.count || 0; }\n",
    },
    StateShape {
        name: "closure cell",
        provider: "function makeCounter() {\n\
                     let count = 0;\n\
                     return { next() { return ++count; }, peek() { return count; } };\n\
                   }\n\
                   const counter = makeCounter();\n\
                   export function next() { return counter.next(); }\n\
                   export function peek() { return counter.peek(); }\n",
    },
    StateShape {
        name: "rebound function",
        provider: "let current = () => 0;\n\
                   export function next() { const value = current() + 1; current = () => value; return value; }\n\
                   export function peek() { return current(); }\n",
    },
    StateShape {
        name: "global property",
        provider: "globalThis.sweepCount = 0;\n\
                   export function next() { return ++globalThis.sweepCount; }\n\
                   export function peek() { return globalThis.sweepCount; }\n",
    },
    StateShape {
        name: "alias of mutable object",
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
    name: &'static str,
    imports: &'static str,
    helpers: &'static str,
    write: &'static str,
    read: &'static str,
    chunks: &'static [(&'static str, &'static str)],
}

const ACCESS_PATHS: &[AccessPath] = &[
    AccessPath {
        name: "named imports",
        imports: "import { next, peek } from './state.js';\n",
        helpers: "",
        write: "next()",
        read: "peek()",
        chunks: &[],
    },
    AccessPath {
        name: "re-export facade",
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
        name: "import and local export facade",
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
        name: "namespace import",
        imports: "import * as state from './state.js';\n",
        helpers: "",
        write: "state.next()",
        read: "state.peek()",
        chunks: &[],
    },
    AccessPath {
        name: "separate wrapper chunks",
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
        name: "arrow wrapper chunk",
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
        name: "exported object methods",
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
        name: "local wrapper functions",
        imports: "import { next, peek } from './state.js';\n",
        helpers: "function write() { return next(); }\nfunction read() { return peek(); }\n",
        write: "write()",
        read: "read()",
        chunks: &[],
    },
    AccessPath {
        name: "local arrow wrappers",
        imports: "import { next, peek } from './state.js';\n",
        helpers: "const write = () => next();\nconst read = () => peek();\n",
        write: "write()",
        read: "read()",
        chunks: &[],
    },
    AccessPath {
        name: "provider-invoked callbacks",
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
        name: "native-invoked callbacks",
        imports: "import { next, peek } from './state.js';\n",
        helpers: "",
        write: "[0].map(next)[0]",
        read: "[0].map(peek)[0]",
        chunks: &[],
    },
    AccessPath {
        name: "immediately invoked functions",
        imports: "import { next, peek } from './state.js';\n",
        helpers: "",
        write: "(() => next())()",
        read: "(() => peek())()",
        chunks: &[],
    },
    AccessPath {
        name: "reader in a second importer chunk",
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
        name: "getter on an exported object",
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
        name: "template interpolation of the reader",
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
    name: &'static str,
    provider: &'static str,
    imports: &'static str,
    write: &'static str,
    read: &'static str,
    /// Chunks between the entry and `static/state`.
    chunks: &'static [(&'static str, &'static str)],
}

const DIRECT_READS: &[DirectRead] = &[
    DirectRead {
        name: "live binding",
        provider: "export let count = 0;\nexport function next() { return ++count; }\n",
        imports: "import { count, next } from './state.js';\n",
        write: "next()",
        read: "count",
        chunks: &[],
    },
    DirectRead {
        name: "live binding through a re-export",
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
        name: "live binding in an object literal",
        provider: "export let count = 0;\nexport function next() { return ++count; }\n",
        imports: "import { count, next } from './state.js';\n",
        write: "next()",
        read: "({ count }).count",
        chunks: &[],
    },
    DirectRead {
        name: "live default export binding",
        provider: "let count = 0;\nexport { count as default };\nexport function next() { return ++count; }\n",
        imports: "import count, { next } from './state.js';\n",
        write: "next()",
        read: "count",
        chunks: &[],
    },
    DirectRead {
        name: "property of an imported object",
        provider: "export const state = { count: 0 };\nexport function next() { return ++state.count; }\n",
        imports: "import { next, state } from './state.js';\n",
        write: "next()",
        read: "state.count",
        chunks: &[],
    },
    DirectRead {
        name: "length of an imported array",
        provider: "export const items = [];\nexport function next() { items.push(1); return items.length; }\n",
        imports: "import { items, next } from './state.js';\n",
        write: "next()",
        read: "items.length",
        chunks: &[],
    },
    DirectRead {
        name: "element of an imported array",
        provider: "export const cells = [0];\nexport function next() { cells[0] = cells[0] + 1; return cells[0]; }\n",
        imports: "import { cells, next } from './state.js';\n",
        write: "next()",
        read: "cells[0]",
        chunks: &[],
    },
    DirectRead {
        name: "namespace member live binding",
        provider: "export let count = 0;\nexport function next() { return ++count; }\n",
        imports: "import * as state from './state.js';\n",
        write: "state.next()",
        read: "state.count",
        chunks: &[],
    },
    DirectRead {
        name: "live binding in a class static field",
        provider: "export let count = 0;\nexport function next() { return ++count; }\n",
        imports: "import { count, next } from './state.js';\n",
        write: "next()",
        read: "(class { static value = count; }).value",
        chunks: &[],
    },
    DirectRead {
        name: "live binding written by the provider's importer-side setter",
        provider: "export let count = 0;\nexport function set(value) { count = value; return value; }\n",
        imports: "import { count, set } from './state.js';\n",
        write: "set(1)",
        read: "count",
        chunks: &[],
    },
];

#[derive(Default)]
struct Tally {
    accepted: usize,
    refused: usize,
    /// Refusals whose emitted entry diverges from the source: the cases
    /// where an acceptance would have been unsound.
    refused_divergent: usize,
    unsound: Vec<String>,
}

/// Run one generated program in both statement orders and both graph modes,
/// with the second statement moved to a logical module.
fn sweep_case(
    tally: &mut Tally,
    case: &str,
    imports: &str,
    helpers: &str,
    write: &str,
    read: &str,
    chunks: &[(&str, &str)],
) {
    for (order, first, second) in [
        ("writer then reader", write, read),
        ("reader then writer", read, write),
    ] {
        let source = format!(
            "{imports}{helpers}const first = {first};\nconst second = {second};\nconsole.log(first, second);\n"
        );
        for dataflow_aware in [false, true] {
            let mut opts = FixtureOpts::new(
                &source,
                vec![logical_module("second", &[Member::new("second")])],
            )
            .with_extra_chunks(chunks)
            .with_unassigned_mode(unassigned_mode_inline());
            if dataflow_aware {
                opts = opts.with_dataflow_aware_s_chain();
            }
            let outcome = run_fixture_to_gate_outcome(opts);
            let label = format!("{case} / {order} / dataflow_aware={dataflow_aware}");
            assert!(
                outcome.source.status.success(),
                "{label}: the source does not run:\n{}",
                outcome.source.stderr
            );
            let same =
                outcome.emitted.status.success() && outcome.emitted.stdout == outcome.source.stdout;
            match (outcome.accepted, same) {
                (true, true) => tally.accepted += 1,
                (true, false) => tally.unsound.push(format!(
                    "{label}: source printed {:?}, the accepted entry printed {:?} {}",
                    outcome.source.stdout,
                    outcome.emitted.stdout,
                    outcome.emitted.stderr.lines().next().unwrap_or_default()
                )),
                (false, same) => {
                    tally.refused += 1;
                    tally.refused_divergent += usize::from(!same);
                }
            }
        }
    }
}

fn assert_sound(tally: &Tally, family: &str) {
    eprintln!(
        "{family}: accepted {}, refused {} ({} of them divergent), unsound {}",
        tally.accepted,
        tally.refused,
        tally.refused_divergent,
        tally.unsound.len()
    );
    assert!(
        tally.unsound.is_empty(),
        "{family}: the gate accepted {} splits whose entry diverges from the source:\n{}",
        tally.unsound.len(),
        tally.unsound.join("\n")
    );
}

fn sweep_state_shapes(shapes: &[StateShape]) {
    let mut tally = Tally::default();
    for shape in shapes {
        for path in ACCESS_PATHS {
            let mut chunks = vec![("static/state", shape.provider)];
            chunks.extend_from_slice(path.chunks);
            sweep_case(
                &mut tally,
                &format!("{} / {}", shape.name, path.name),
                path.imports,
                path.helpers,
                path.write,
                path.read,
                &chunks,
            );
        }
    }
    assert_sound(&tally, "provider state through imported calls");
    // The sweep is vacuous unless it generates splits that would diverge.
    assert!(tally.refused_divergent > 0, "no generated split diverges");
}

// One test per slice of the state shapes, so the harness runs them on
// separate threads.
#[test]
fn accepted_splits_of_stateful_provider_calls_match_node_binding_cells() {
    sweep_state_shapes(&STATE_SHAPES[..3]);
}

#[test]
fn accepted_splits_of_stateful_provider_calls_match_node_collections() {
    sweep_state_shapes(&STATE_SHAPES[3..6]);
}

#[test]
fn accepted_splits_of_stateful_provider_calls_match_node_object_state() {
    sweep_state_shapes(&STATE_SHAPES[6..9]);
}

#[test]
fn accepted_splits_of_stateful_provider_calls_match_node_indirect_state() {
    sweep_state_shapes(&STATE_SHAPES[9..]);
}

#[test]
fn accepted_splits_of_direct_foreign_reads_match_node() {
    let mut tally = Tally::default();
    for direct in DIRECT_READS {
        let mut chunks = vec![("static/state", direct.provider)];
        chunks.extend_from_slice(direct.chunks);
        sweep_case(
            &mut tally,
            direct.name,
            direct.imports,
            "",
            direct.write,
            direct.read,
            &chunks,
        );
    }
    assert_sound(&tally, "direct reads of imported state");
    assert!(tally.refused_divergent > 0, "no generated split diverges");
}

/// The acceptance side of the sweep: a provider without state, through the
/// same access paths. Some of these splits must be accepted, or the sweep
/// above would pass on a gate that refuses everything.
#[test]
fn accepted_splits_of_stateless_provider_calls_match_node() {
    let mut tally = Tally::default();
    for path in ACCESS_PATHS {
        let mut chunks = vec![(
            "static/state",
            "const base = 1;\n\
             export function next() { return base + 1; }\n\
             export function peek() { return base; }\n",
        )];
        chunks.extend_from_slice(path.chunks);
        sweep_case(
            &mut tally,
            &format!("stateless / {}", path.name),
            path.imports,
            path.helpers,
            path.write,
            path.read,
            &chunks,
        );
    }
    assert_sound(&tally, "stateless provider through imported calls");
    assert!(tally.accepted > 0, "no stateless split is accepted");
}
