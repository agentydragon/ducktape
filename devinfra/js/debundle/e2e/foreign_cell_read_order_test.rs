//! A read of another chunk's mutable state keeps its source order with the
//! statements that can change that state: the importer's dependency graph has
//! no node for the provider's cells, so such a read is sequenced like any
//! other effect.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

const COUNTER: &str = "let count = 0;\n\
export function next() { return ++count; }\n\
export function peek() { return count; }\n";

/// `second` moves to a logical module and the rest stays in the entry, whose
/// static imports run first: `second` is evaluated before every earlier
/// statement of the source.
fn second_first<'a>(source: &'a str, peers: &'a [(&'a str, &'a str)]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![logical_module("second", &[Member::new("second")])],
    )
    .with_extra_chunks(peers)
    .with_unassigned_mode(unassigned_mode_inline())
}

struct Divergence {
    case: &'static str,
    source: &'static str,
    peers: &'static [(&'static str, &'static str)],
    source_output: &'static str,
    /// What the entry emitted for the refused split prints: the reason the
    /// split is unsafe.
    reordered_output: &'static str,
}

const CALLS: &str = "import { next, peek } from './peer.js';\n\
const first = next();\n\
const second = peek();\n\
console.log(first, second);\n";

const DIVERGENCES: &[Divergence] = &[
    Divergence {
        case: "reader of a rebound binding before its writer",
        source: CALLS,
        peers: &[("static/peer", COUNTER)],
        source_output: "1 1\n",
        reordered_output: "1 0\n",
    },
    Divergence {
        case: "writer of a rebound binding before its reader",
        source: "import { next, peek } from './peer.js';\n\
                 const first = peek();\n\
                 const second = next();\n\
                 console.log(first, second);\n",
        peers: &[("static/peer", COUNTER)],
        source_output: "0 1\n",
        reordered_output: "1 1\n",
    },
    Divergence {
        case: "reassigned var",
        source: CALLS,
        peers: &[(
            "static/peer",
            "var count = 0;\n\
             export function next() { count = count + 1; return count; }\n\
             export function peek() { return count; }\n",
        )],
        source_output: "1 1\n",
        reordered_output: "1 0\n",
    },
    Divergence {
        case: "array contents",
        source: CALLS,
        peers: &[(
            "static/peer",
            "const items = [];\n\
             export function next() { items.push(1); return items.length; }\n\
             export function peek() { return items.length; }\n",
        )],
        source_output: "1 1\n",
        reordered_output: "1 0\n",
    },
    Divergence {
        case: "state shared through a third chunk",
        source: "import { next } from './writer.js';\n\
                 import { peek } from './reader.js';\n\
                 const first = next();\n\
                 const second = peek();\n\
                 console.log(first, second);\n",
        peers: &[
            (
                "static/store",
                "let count = 0;\n\
                 export function increment() { return ++count; }\n\
                 export function current() { return count; }\n",
            ),
            (
                "static/writer",
                "import { increment } from './store.js';\n\
                 export function next() { return increment(); }\n",
            ),
            (
                "static/reader",
                "import { current } from './store.js';\n\
                 export function peek() { return current(); }\n",
            ),
        ],
        source_output: "1 1\n",
        reordered_output: "1 0\n",
    },
    Divergence {
        case: "writer invoked through a provider callback",
        source: "import { apply, next, peek } from './peer.js';\n\
                 const first = apply(next);\n\
                 const second = peek();\n\
                 console.log(first, second);\n",
        peers: &[(
            "static/peer",
            "let count = 0;\n\
             export function next() { return ++count; }\n\
             export function peek() { return count; }\n\
             export function apply(callback) { return callback(); }\n",
        )],
        source_output: "1 1\n",
        reordered_output: "1 0\n",
    },
    Divergence {
        case: "reader behind a local function",
        source: "import { next, peek } from './peer.js';\n\
                 function read() { return peek(); }\n\
                 const first = next();\n\
                 const second = read();\n\
                 console.log(first, second);\n",
        peers: &[("static/peer", COUNTER)],
        source_output: "1 1\n",
        reordered_output: "1 0\n",
    },
    Divergence {
        case: "direct read of an imported live binding",
        source: "import { count, next } from './peer.js';\n\
                 const first = next();\n\
                 const second = count;\n\
                 console.log(first, second);\n",
        peers: &[(
            "static/peer",
            "export let count = 0;\n\
             export function next() { return ++count; }\n",
        )],
        source_output: "1 1\n",
        reordered_output: "1 0\n",
    },
];

#[test]
fn reads_of_foreign_state_moved_across_its_writers_are_refused_and_diverge() {
    for divergence in DIVERGENCES {
        let case = divergence.case;
        let refused =
            run_emit_on_rejection_fixture(second_first(divergence.source, divergence.peers));
        assert_node_output(
            &refused.source_root.join("static/app.js"),
            divergence.source_output,
            "",
        );
        assert_node_output(&refused.entry_path(), divergence.reordered_output, "");
        let assessment = refused.candidate_relocation_assessment();
        assert_eq!(
            assessment["legacy_gate_accepts"], false,
            "{case}: {assessment:#}"
        );
        assert!(
            refused.cycles().iter().any(|scc| {
                scc["cut"]
                    .as_array()
                    .expect("SCC cut")
                    .iter()
                    .any(|edge| edge["kind"] == "sequenced")
            }),
            "{case}: the refusal is not a side-effect ordering cycle: {:#?}",
            refused.cycles()
        );
    }
}

/// A provider binding that is never rebound, holding a primitive, reads the
/// same at every point of initialization: these relocations stay accepted.
#[test]
fn reads_of_immutable_foreign_bindings_relocate() {
    let peers = [(
        "static/peer",
        "export const limit = 3;\n\
         let count = 0;\n\
         export function next() { return ++count; }\n\
         export function bound() { return limit; }\n",
    )];
    for (case, source, expected) in [
        (
            "imported function reading a constant",
            "import { bound, next } from './peer.js';\nconst first = next();\nconst second = bound();\nconsole.log(first, second);\n",
            "1 3\n",
        ),
        (
            "direct read of an imported constant",
            "import { limit, next } from './peer.js';\nconst first = next();\nconst second = limit;\nconsole.log(first, second);\n",
            "1 3\n",
        ),
    ] {
        let fixture = run_fixture(second_first(source, &peers));
        assert_preserved_entry_output(&fixture, expected);
        let assessment = candidate_relocation_assessment(&fixture);
        assert_eq!(
            assessment["legacy_gate_accepts"], true,
            "{case}: {assessment:#}"
        );
        assert!(
            assessment["swapped_pairs"].as_u64().unwrap() > 0,
            "{case}: {assessment:#}"
        );
    }
}
