//! Effect translation validation of imported calls that the importer replaces
//! by the provider's derived foreign call summary.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

const PEER: &str = "export function literal() { return 3; }\n\
export function make(input) { return { input }; }\n\
export function wrap(input) { return make(input); }\n\
export class Box { constructor(value) { this.value = value; } }\n\
let count = 0;\n\
export function next() { return ++count; }\n\
export function peek() { return count; }\n";

/// Stateless providers that take a callback. `defer` and `hold` retain it in
/// their fresh result, `lazy` behind an accessor, and `run` invokes it.
const CALLBACK_PEER: &str = "export function literal() { return 3; }\n\
export function make(input) { return { input }; }\n\
export function defer(callback) { return () => callback(); }\n\
export function hold(callback) { return { run() { return callback(); } }; }\n\
export function lazy(callback) { return { get value() { return callback(); } }; }\n\
export function run(callback) { return callback(); }\n";

/// A retaining factory that also reads the provider cell `next` writes.
const COUNTING_PEER: &str = "let count = 0;\n\
export function next() { return ++count; }\n\
export function counted(callback) { const id = count; return () => id + callback(); }\n";

/// `second` moves to a logical module and the rest stays in the entry, whose
/// static imports run first: `second` is evaluated before every earlier
/// statement of the source.
fn second_first<'a>(source: &'a str, peers: &'a [(&'a str, &'a str)]) -> FixtureOpts<'a> {
    moved_first(source, peers, &["second"])
}

/// As `second_first`, moving every binding in `moved`.
fn moved_first<'a>(
    source: &'a str,
    peers: &'a [(&'a str, &'a str)],
    moved: &[&'static str],
) -> FixtureOpts<'a> {
    let members: Vec<_> = moved.iter().map(|binding| Member::new(binding)).collect();
    FixtureOpts::new(source, vec![logical_module("second", &members)])
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

/// `next` writes a provider module cell, so it has no summary: both gates
/// refuse, and the emitted order hands out the counter values the other way
/// round.
#[test]
fn reordered_writers_of_a_provider_cell_are_refused_and_diverge() {
    let peers = [("static/peer", PEER)];
    let source = "import { next } from './peer.js';\nconst first = next();\nconst second = next();\nconsole.log(first, second);\n";
    let colocated = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&peers)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "1 2\n");

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

/// A reader of the cell moved before its writer: neither provider body has a
/// summary, so the effect verdict gives no receipt and stays incomplete. Both
/// gates refuse, and the emitted order reads the counter before the increment
/// (the source prints `1 1`).
#[test]
fn provider_cell_reader_moved_before_its_writer_has_no_effect_receipt() {
    let peers = [("static/peer", PEER)];
    let source = "import { next, peek } from './peer.js';\nconst first = next();\nconst second = peek();\nconsole.log(first, second);\n";
    let reordered = run_emit_on_rejection_fixture(second_first(source, &peers));
    assert_node_output(&reordered.entry_path(), "1 0\n", "");
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

fn assert_relocations(peer: &str, cases: &[(&str, &str, &str)]) {
    let peers = [("static/peer", peer)];
    for (case, source, expected) in cases {
        let fixture = run_fixture(second_first(source, &peers));
        assert_preserved_entry_output(&fixture, expected);
        assert_effect_verdict_accepts(&fixture, case);
    }
}

/// Both gates refuse the reordering with the effect status `refusal`, and the
/// program emitted anyway prints `diverged` where the source prints
/// `preserved`.
fn assert_refused_divergence(
    peer: &str,
    source: &str,
    moved: &[&'static str],
    refusal: &str,
    (preserved, diverged): (&str, &str),
) {
    let peers = [("static/peer", peer)];
    let colocated = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&peers)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, preserved);

    let reordered = run_emit_on_rejection_fixture(moved_first(source, &peers, moved));
    assert_node_output(&reordered.entry_path(), diverged, "");
    let assessment = reordered.candidate_relocation_assessment();
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    assert_eq!(assessment["legacy_gate_accepts"], false, "{assessment:#}");
    assert_eq!(assessment["status"], refusal, "{assessment:#}");
}

/// A root that mixes local calls with summarized imported calls runs on the
/// ordered kernel: each local call brings its own receipt and each summarized
/// call its summary.
#[test]
fn roots_mixing_local_and_summarized_calls_relocate() {
    assert_relocations(
        CALLBACK_PEER,
        &[
            (
                "local call as a summarized call's argument",
                "import { literal, make } from './peer.js';\nfunction two() { return 2; }\nconst first = make(two());\nconst second = literal();\nconsole.log(first.input, second);\n",
                "2 3\n",
            ),
            (
                "local and summarized calls side by side",
                "import { literal, make } from './peer.js';\nfunction two() { return 2; }\nconst first = [make(1), two()];\nconst second = literal();\nconsole.log(first[0].input, first[1], second);\n",
                "1 2 3\n",
            ),
            (
                "local call reading a module binding",
                "import { literal, make } from './peer.js';\nlet n = 1;\nfunction get() { return n; }\nconst first = make(get());\nconst second = literal();\nconsole.log(first.input, second);\n",
                "1 3\n",
            ),
            (
                "two mixed roots",
                "import { make } from './peer.js';\nfunction two() { return 2; }\nconst first = make(two());\nconst second = make(two());\nconsole.log(first.input, second.input);\n",
                "2 2\n",
            ),
        ],
    );
}

/// The local call of each mixed root writes the global property that the
/// other root's summarized call takes as its argument: through the local
/// call's result, or read back as the next operand of the same root.
#[test]
fn mixed_root_whose_local_call_writes_the_other_roots_argument_is_refused_and_diverges() {
    for (source, outputs) in [
        (
            "import { make } from './peer.js';\nfunction bump() { globalThis.n = (globalThis.n ?? 0) + 1; return globalThis.n; }\nconst first = make(bump());\nconst second = make(bump());\nconsole.log(first.input, second.input);\n",
            ("1 2\n", "2 1\n"),
        ),
        (
            "import { make } from './peer.js';\nfunction bump() { globalThis.n = (globalThis.n ?? 0) + 1; return 0; }\nconst first = [bump(), make(globalThis.n)];\nconst second = [bump(), make(globalThis.n)];\nconsole.log(first[1].input, second[1].input);\n",
            ("1 2\n", "2 1\n"),
        ),
    ] {
        assert_refused_divergence(
            CALLBACK_PEER,
            source,
            &["second"],
            "blocked_by_effect",
            outputs,
        );
    }
}

/// A summarized provider body invokes no argument, so a callback it receives
/// is only created at the call: the callback's own reads and effects belong
/// to whoever invokes it later.
#[test]
fn callbacks_passed_to_summarized_imports_relocate() {
    assert_relocations(
        CALLBACK_PEER,
        &[
            (
                "inline closure",
                "import { literal, make } from './peer.js';\nconst first = make(() => 1);\nconst second = literal();\nconsole.log(first.input(), second);\n",
                "1 3\n",
            ),
            (
                "closure reading a later module binding",
                "import { literal, make } from './peer.js';\nconst first = make(() => later);\nconst second = literal();\nconst later = 5;\nconsole.log(first.input(), second);\n",
                "5 3\n",
            ),
            (
                "declared function",
                "import { literal, make } from './peer.js';\nfunction one() { return 1; }\nconst first = make(one);\nconst second = literal();\nconsole.log(first.input(), second);\n",
                "1 3\n",
            ),
            (
                "retaining factory result as the argument",
                "import { defer, literal, make } from './peer.js';\nconst first = make(defer(() => 1));\nconst second = literal();\nconsole.log(first.input(), second);\n",
                "1 3\n",
            ),
        ],
    );
}

/// `run` invokes its callback, so it has no summary: the callback's write to
/// the global property the moved statement reads happens at the call.
#[test]
fn callback_invoked_by_the_provider_is_refused_and_diverges() {
    let source = "import { run } from './peer.js';\nglobalThis.n = 1;\nconst first = run(() => { globalThis.n = 2; return 0; });\nconst second = globalThis.n;\nconsole.log(first, second);\n";
    assert_refused_divergence(
        CALLBACK_PEER,
        source,
        &["second"],
        "blocked_by_effect",
        ("0 2\n", "0 undefined\n"),
    );
}

/// An imported factory that retains its callback in a fresh result: the
/// creating call is covered by the provider's summary and relocates, while the
/// callback stays a deferred effect of every later use of the result.
#[test]
fn retaining_factory_calls_relocate() {
    assert_relocations(
        CALLBACK_PEER,
        &[
            (
                "returned closure",
                "import { defer, literal } from './peer.js';\nconst first = defer(() => 1);\nconst second = literal();\nconsole.log(first(), second);\n",
                "1 3\n",
            ),
            (
                "returned record",
                "import { hold, literal } from './peer.js';\nconst first = hold(() => 1);\nconst second = literal();\nconsole.log(first.run(), second);\n",
                "1 3\n",
            ),
            (
                "callback with an effect of its own",
                "import { defer, literal } from './peer.js';\nconst first = defer(() => { console.log('ran'); return 1; });\nconst second = literal();\nconsole.log(first(), second);\n",
                "ran\n1 3\n",
            ),
            (
                "two factory calls",
                "import { defer } from './peer.js';\nconst first = defer(() => 1);\nconst second = defer(() => 2);\nconsole.log(first(), second());\n",
                "1 2\n",
            ),
            (
                "created before the native row its callback reads",
                "import { defer } from './peer.js';\nconst first = Object.freeze({ a: 1 });\nconst second = defer(() => first.a);\nconsole.log(second());\n",
                "1\n",
            ),
            (
                "factory call in a local body",
                "import { defer, literal } from './peer.js';\nfunction build() { return defer(() => 1); }\nconst first = build();\nconst second = literal();\nconsole.log(first(), second);\n",
                "1 3\n",
            ),
            (
                "factory call beside a local call",
                "import { defer, literal } from './peer.js';\nfunction two() { return 2; }\nconst first = [defer(() => 1), two()];\nconst second = literal();\nconsole.log(first[0](), first[1], second);\n",
                "1 2 3\n",
            ),
        ],
    );
}

/// The factory call's receipt covers the creation only. Each use of the
/// result that runs the retained callback (a call, or a read of the accessor
/// that holds it) is its own effect, so moving the second use (with the
/// creation) ahead of the first hands out the counter values the other way
/// round.
#[test]
fn retained_callback_invoked_after_the_swap_point_is_refused_and_diverges() {
    for (source, refusal) in [
        (
            "import { defer } from './peer.js';\nconst bump = defer(() => { globalThis.n = (globalThis.n ?? 0) + 1; return globalThis.n; });\nconst first = bump();\nconst second = bump();\nconsole.log(first, second);\n",
            "blocked_by_effect",
        ),
        (
            "import { lazy } from './peer.js';\nconst bump = lazy(() => { globalThis.n = (globalThis.n ?? 0) + 1; return globalThis.n; });\nconst first = bump.value;\nconst second = bump.value;\nconsole.log(first, second);\n",
            "incomplete",
        ),
    ] {
        assert_refused_divergence(
            CALLBACK_PEER,
            source,
            &["bump", "second"],
            refusal,
            ("1 2\n", "2 1\n"),
        );
    }
}

/// `counted` retains its callback but also reads the provider cell `next`
/// writes, so the call is not substituted and has no receipt: the emitted
/// order reads the counter before the increment.
#[test]
fn retaining_factory_reading_a_provider_cell_is_refused_and_diverges() {
    let source = "import { counted, next } from './peer.js';\nconst first = next();\nconst second = counted(() => 0);\nconsole.log(first, second());\n";
    assert_refused_divergence(
        COUNTING_PEER,
        source,
        &["second"],
        "incomplete",
        ("1 1\n", "1 0\n"),
    );
}

/// Valid relocations waiting on call-effect coverage for callees with live
/// captures: the callback captures a cell of the activation that creates it,
/// so the call receiving it has no receipt and the owner names that reason.
#[test]
fn callbacks_capturing_a_caller_local_stay_pending() {
    let peers = [("static/peer", CALLBACK_PEER)];
    for (case, source, expected) in [
        (
            "summarized call",
            "import { literal, make } from './peer.js';\nfunction build() { const out = []; return make((x) => out.push(x)); }\nconst first = build();\nconst second = literal();\nconsole.log(first.input(7), second);\n",
            "1 3\n",
        ),
        (
            "retaining factory",
            "import { defer, literal } from './peer.js';\nfunction build() { const out = [1]; return defer(() => out.length); }\nconst first = build();\nconst second = literal();\nconsole.log(first(), second);\n",
            "1 3\n",
        ),
    ] {
        let fixture = run_fixture(second_first(source, &peers));
        assert_preserved_entry_output(&fixture, expected);
        assert_effect_verdict_pending(&fixture, "incomplete", case);
        let report = fixture.effect_translation_validation();
        assert_eq!(
            report["coverage"]["owner_coverage_refusals"]["callback_captures_live_cell"], 1,
            "{case}: {report:#}"
        );
    }
}
