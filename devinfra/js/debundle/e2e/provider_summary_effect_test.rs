//! Effect translation validation of imported calls with source-derived
//! operation receipts, plus conservative refusals for unsupported effects.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
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

fn assert_provider_call_receipt(fixture: &Fixture, binding: &str, provider: &str, case: &str) {
    let execution = binding_execution_report(fixture, binding);
    assert_eq!(execution["complete"], true, "{case}: {execution:#}");
    let authority = &execution["binding_effect_authority"];
    assert_eq!(
        authority["authority"], "operation_receipt",
        "{case}: {execution:#}"
    );
    let receipt = &authority["receipt"];
    assert_eq!(receipt["binding"], binding, "{case}: {receipt:#}");
    let graph = fixture.dependency_graph();
    let owner = graph
        .nodes
        .iter()
        .find(|node| node.id == source_unit_for_binding(&graph, binding))
        .expect("root initializer owner");
    assert_eq!(
        receipt["owner"], owner.statement_ordinal.0,
        "{case}: {receipt:#}"
    );
    assert!(
        receipt["calls"]
            .as_array()
            .is_some_and(|calls| calls.iter().any(|call| {
                call["target"]["kind"] == "callable" && call["target"]["source_module"] == provider
            })),
        "{case}: receipt must include the source-derived provider body: {receipt:#}"
    );
}

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

/// `next` writes a provider module cell, so the moved calls cannot receive an
/// exact operation receipt: the gate refuses, and the emitted order hands out
/// the counter values the other way round.
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

/// A reader of the cell moved before its writer: mutable provider-cell state
/// prevents an exact receipt for the read. Both gates refuse, and the emitted
/// order reads the counter before the increment (the source prints `1 1`).
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

/// A provider body that calls another exported body has a source-derived
/// operation receipt. The constructor variant below retains its precise
/// schedule refusal while the imported class call is unresolved.
#[test]
fn provider_call_chains_relocate_with_operation_receipt() {
    let peers = [("static/peer", PEER)];
    let provider_call = run_fixture_with_graphs(second_first(
        "import { literal, wrap } from './peer.js';\nconst first = wrap(1);\nconst second = literal();\nconsole.log(first.input, second);\n",
        &peers,
    ));
    assert_preserved_entry_output(&provider_call, "1 3\n");
    assert_effect_verdict_accepts(&provider_call, "provider body calling another export");
    assert_provider_call_receipt(
        &provider_call,
        "first",
        "static/peer",
        "provider body calling another export",
    );
}

#[test]
fn imported_constructor_reordering_stays_schedule_pending() {
    let peers = [("static/peer", PEER)];
    let source = "import { Box, literal } from './peer.js';\nconst first = new Box(1);\nconst second = literal();\nconsole.log(first.value, second);\n";
    let colocated = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&peers)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "1 3\n");

    let reordered = run_rejection_fixture_with_graphs_and_args(
        second_first(source, &peers),
        &["--emit-on-rejection"],
        3,
    );
    assert!(
        reordered.stderr.contains(
            "evaluation_schedule_unknown_reordered: ProgramSourceUnitId { module: \"static/app\", ordinal: SourceUnitOrdinal(1) }"
        ),
        "the imported-constructor split must keep its exact typed schedule refusal and owner:\n{}",
        reordered.stderr
    );
    assert_node_output(&reordered.source_root.join("static/app.js"), "1 3\n", "");
    assert_node_output(&reordered.entry_path(), "1 3\n", "");
}

fn assert_relocations(peer: &str, cases: &[(&str, &str, &str)]) {
    let peers = [("static/peer", peer)];
    for (case, source, expected) in cases {
        let fixture = run_fixture_with_graphs(second_first(source, &peers));
        assert_preserved_entry_output(&fixture, expected);
        assert_effect_verdict_accepts(&fixture, case);
        assert_provider_call_receipt(&fixture, "second", "static/peer", case);
    }
}

/// The CLI gate refuses these reorderings, and emitted Node output shows the
/// callback effects that make each refusal necessary.
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

/// A root mixing local calls with imported calls relocates with a receipt for
/// the moved provider operation.
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

/// These provider bodies do not invoke their callback argument during the
/// creating call: the callback's own reads and effects belong to whoever
/// invokes it later.
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

/// `run` invokes its callback, so its callback's write to the global property
/// happens at the call, where the moved statement reads it.
#[test]
fn callback_invoked_by_the_provider_is_refused_and_diverges() {
    let source = "import { run } from './peer.js';\nconst first = run(() => { globalThis.n = 2; return 0; });\nconst second = globalThis.n;\nconsole.log(first, second);\n";
    assert_refused_divergence(
        CALLBACK_PEER,
        source,
        &["second"],
        "blocked_by_effect",
        ("0 2\n", "0 undefined\n"),
    );
}

/// An imported factory retains its callback in a fresh result. The provider
/// operation receipt covers creation; callback effects remain deferred until
/// a later use of the result.
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
                "factory call beside a local call",
                "import { defer, literal } from './peer.js';\nfunction two() { return 2; }\nconst first = [defer(() => 1), two()];\nconst second = literal();\nconsole.log(first[0](), first[1], second);\n",
                "1 2 3\n",
            ),
        ],
    );

    // The ordered kernel has the exact provider-body receipt and the CLI
    // accepts this relocation. The report-only effect verdict still marks the
    // local-call case incomplete, so retain that scope boundary explicitly.
    let peers = [("static/peer", CALLBACK_PEER)];
    let fixture = run_fixture_with_graphs(second_first(
        "import { defer, literal } from './peer.js';\nfunction build() { return defer(() => 1); }\nconst first = build();\nconst second = literal();\nconsole.log(first(), second);\n",
        &peers,
    ));
    assert_preserved_entry_output(&fixture, "1 3\n");
    assert_effect_verdict_pending(&fixture, "incomplete", "factory call in a local body");
    let assessment = fixture.effect_translation_validation();
    assert_eq!(
        assessment["legacy_gate_accepts"], true,
        "the CLI acceptance remains authoritative for this report-only limitation: {assessment:#}"
    );
    let local_body = binding_execution_report(&fixture, "first");
    assert_eq!(local_body["complete"], true, "{local_body:#}");
    assert_eq!(
        local_body["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "the local retaining-factory body remains explicitly outside receipt coverage: {local_body:#}"
    );
    assert_provider_call_receipt(
        &fixture,
        "second",
        "static/peer",
        "the independent provider literal call",
    );
}

/// The factory call's receipt covers creation only. Each use of the
/// result that runs the retained callback (a call, or a read of the accessor
/// that holds it) is its own effect, so moving the second use (with the
/// creation) ahead of the first hands out the counter values the other way
/// round.
#[test]
fn retained_callback_invoked_after_the_swap_point_is_refused_and_diverges() {
    for (source, refusal) in [
        (
            "import { defer } from './peer.js';\nconst bump = defer(() => { globalThis.n = (globalThis.n ?? 0) + 1; return globalThis.n; });\nconst first = bump();\nconst second = bump();\nconsole.log(first, second);\n",
            "incomplete",
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
/// writes, so the call has no exact source-derived receipt: the emitted order
/// reads the counter before the increment.
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

/// Creating a callback retains its caller's live cell without reading it.
/// The unsupported nested retaining-factory call keeps its separate limitation.
#[test]
fn callbacks_capturing_a_caller_local_keep_separate_call_coverage() {
    let peers = [("static/peer", CALLBACK_PEER)];
    for (case, source, expected, covered) in [
        (
            "summarized call",
            "import { literal, make } from './peer.js';\nfunction build() { const out = []; return make((x) => out.push(x)); }\nconst first = build();\nconst second = literal();\nconsole.log(first.input(7), second);\n",
            "1 3\n",
            true,
        ),
        (
            "retaining factory",
            "import { defer, literal } from './peer.js';\nfunction build() { const out = [1]; return defer(() => out.length); }\nconst first = build();\nconst second = literal();\nconsole.log(first(), second);\n",
            "1 3\n",
            false,
        ),
    ] {
        let fixture = run_fixture(second_first(source, &peers));
        assert_preserved_entry_output(&fixture, expected);
        if covered {
            assert_effect_verdict_accepts(&fixture, case);
        } else {
            assert_effect_verdict_pending(&fixture, "incomplete", case);
        }
    }
}

/// A local body that calls a retaining factory in a shape the ordered lane
/// does not substitute keeps the flattened lane. The independent literal
/// anchor makes the candidate split valid; the report stays pending because
/// the returned result is passed to another retaining factory.
#[test]
fn unsubstituted_factory_calls_in_a_local_body_stay_pending() {
    let counting = [("static/peer", COUNTING_PEER)];
    assert_pending_split_preserves_output(
        second_first(
            "import { counted } from './peer.js';\nfunction build() { return counted(() => 0); }\nconst first = build();\nconst second = globalThis.n;\nconsole.log(first(), second);\n",
            &counting,
        ),
        "incomplete",
        "0 undefined\n",
    );

    let peers = [("static/peer", CALLBACK_PEER)];
    let fixture = run_fixture(second_first(
        "import { defer } from './peer.js';\nfunction build() { const inner = defer(() => 1); return defer(inner); }\nconst first = build();\nconst second = 0;\nconsole.log(first(), second);\n",
        &peers,
    ));
    assert_preserved_entry_output(&fixture, "1 0\n");
    assert_effect_verdict_pending(&fixture, "incomplete", "retained result as the argument");
}
