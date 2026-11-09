//! Effect translation validation of imported calls that the importer replaces
//! by the provider's derived foreign call summary.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

const PEER: &str = "export function literal() { return 3; }\n\
export function make(input) { return { input }; }\n\
export function wrap(input) { return make(input); }\n\
export class Box { constructor(value) { this.value = value; } }\n\
let count = 0;\n\
export function next() { return ++count; }\n";

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

#[test]
fn summarized_imported_calls_relocate_past_each_other() {
    let peers = [("static/peer", PEER)];
    for (case, source, expected) in [
        (
            "factory and primitive return",
            "import { literal, make } from './peer.js';\nconst first = make(1);\nconst second = literal();\nconsole.log(first.input, second);\n",
            "1 3\n",
        ),
        (
            "retained argument",
            "import { literal, make } from './peer.js';\nconst seed = { n: 1 };\nconst first = make(seed);\nconst second = literal();\nconsole.log(first.input.n, second);\n",
            "1 3\n",
        ),
        (
            "summarized call as an argument",
            "import { literal, make } from './peer.js';\nconst first = make(literal());\nconst second = literal();\nconsole.log(first.input, second);\n",
            "3 3\n",
        ),
        (
            "local body calling a summarized import",
            "import { literal, make } from './peer.js';\nfunction build() { return make(2); }\nconst first = build();\nconst second = literal();\nconsole.log(first.input, second);\n",
            "2 3\n",
        ),
        (
            "past a native row",
            "import { make } from './peer.js';\nconst first = Object.freeze({ a: 1 });\nconst second = make(1);\nconsole.log(first.a, second.input);\n",
            "1 1\n",
        ),
    ] {
        let fixture = run_fixture(second_first(source, &peers));
        assert_preserved_entry_output(&fixture, expected);
        assert_effect_verdict_accepts(&fixture, case);
    }
}

#[test]
fn reordered_calls_of_a_stateful_provider_are_refused_and_diverge() {
    let peers = [("static/peer", PEER)];
    let source = "import { next } from './peer.js';\nconst first = next();\nconst second = next();\nconsole.log(first, second);\n";
    let colocated = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&peers)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "1 2\n");

    // `next` writes a provider cell, so it has no summary: both gates refuse,
    // and the emitted order hands out the counter values the other way round.
    let reordered = run_emit_on_rejection_fixture(second_first(source, &peers));
    assert_node_output(&reordered.entry_path(), "2 1\n", "");
    let assessment = reordered.candidate_relocation_assessment();
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    assert_eq!(assessment["legacy_gate_accepts"], false, "{assessment:#}");
    assert_eq!(assessment["status"], "incomplete", "{assessment:#}");
}

/// Valid relocations the summary cannot prove yet. A provider body that calls
/// another export has no allocation-only summary, and running it in the
/// importer's transfer needs the provider's program there. A class binding can
/// be in its TDZ when an ESM cycle delays the provider, so construction keeps
/// an uncertain completion until provider initialization is proved.
#[test]
fn unsummarized_provider_bodies_and_constructors_stay_pending() {
    let peers = [("static/peer", PEER)];
    for (case, source, expected) in [
        (
            "provider body calling another export",
            "import { literal, wrap } from './peer.js';\nconst first = wrap(1);\nconst second = literal();\nconsole.log(first.input, second);\n",
            "1 3\n",
        ),
        (
            "imported constructor",
            "import { Box, literal } from './peer.js';\nconst first = new Box(1);\nconst second = literal();\nconsole.log(first.value, second);\n",
            "1 3\n",
        ),
    ] {
        let fixture = run_fixture(second_first(source, &peers));
        assert_preserved_entry_output(&fixture, expected);
        assert_effect_verdict_pending(&fixture, "incomplete", case);
    }
}
