//! A read of another chunk's mutable state keeps its source order with the
//! statements that can change that state: the importer's dependency graph has
//! no node for the provider's cells, so such a read is sequenced like any
//! other effect.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, path, staged};
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
        let refused = run_emit_on_rejection_fixture_with_graphs(second_first(
            divergence.source,
            divergence.peers,
        ));
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

/// A pair of local re-export hops fans out to the same immutable provider
/// function. The CLI can prove both imported bindings stable, so extracting
/// one read ahead of the stateful call does not change behavior.
#[test]
fn immutable_imports_reexported_through_a_shared_tail_relocate() {
    let source = "import { next } from './peer.js';\n\
import { bound, alsoBound } from './bridge.js';\n\
const first = next();\n\
const second = bound;\n\
const third = alsoBound;\n\
console.log(first, second === third);\n";
    let peers = [
        (
            "static/peer",
            "export const limit = 3;\n\
             let count = 0;\n\
             export function next() { return ++count; }\n\
             export function bound() { return limit; }\n",
        ),
        (
            "static/tail",
            "import { bound } from './peer.js';\n\
             export { bound as first, bound as second };\n",
        ),
        (
            "static/bridge",
            "import { first, second } from './tail.js';\n\
             export { first as bound, second as alsoBound };\n",
        ),
    ];
    let fixture = run_fixture(second_first(source, &peers));
    assert_preserved_entry_output(&fixture, "1 true\n");
    let assessment = candidate_relocation_assessment(&fixture);
    assert_eq!(assessment["legacy_gate_accepts"], true, "{assessment:#}");
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "the stable import must cross the stateful call: {assessment:#}"
    );
}

/// The mutable live binding still carries its provider cell through a local
/// re-export in a cyclic module graph. Moving the read before its writer is
/// therefore refused, and the rejected tree demonstrates the divergence.
#[test]
fn mutable_reexport_in_a_module_cycle_keeps_the_read_after_its_writer() {
    let source = "import { count, next } from './bridge.js';\n\
const first = next();\n\
const second = count;\n\
console.log(first, second);\n";
    let peers = [
        (
            "static/bridge",
            "import { count, next } from './provider.js';\n\
             export { count, next };\n\
             export const marker = 1;\n",
        ),
        (
            "static/provider",
            "import { marker } from './bridge.js';\n\
             export let count = 0;\n\
             export function next() { return ++count; }\n",
        ),
    ];
    let refused = run_emit_on_rejection_fixture_with_graphs(second_first(source, &peers));
    assert_node_output(&refused.source_root.join("static/app.js"), "1 1\n", "");
    assert_node_output(&refused.entry_path(), "1 0\n", "");
    let assessment = refused.candidate_relocation_assessment();
    assert_eq!(assessment["legacy_gate_accepts"], false, "{assessment:#}");
}

const ALIAS_AND_COPY_PEERS: &[(&str, &str)] = &[
    (
        "static/provider",
        "let value = 1;\n\
         export { value as default };\n\
         export function next() { return ++value; }\n",
    ),
    (
        "static/bridge",
        "import original from './provider.js';\n\
         export { original as live };\n\
         const value = original;\n\
         export { value as copied };\n",
    ),
    ("static/unrelated", "export const value = 99;\n"),
];

fn assert_alias_copy_split(source: &str, original: &str, emitted: &str, exit: i32) {
    let (staged, _fixture) = staged(second_first(source, ALIAS_AND_COPY_PEERS));
    assert_node_output(&staged.root().join("snapshot/static/app.js"), original, "");
    let direct = staged.run(&["--emit-on-rejection"]);
    let saved = staged.run(&["--emit-on-rejection", "--analysis", path(&staged.artifact)]);
    for invocation in [&direct, &saved] {
        assert_eq!(
            invocation.result.status.code(),
            Some(exit),
            "{}",
            invocation.result.stderr
        );
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            emitted,
            "",
        );
    }
    assert_same_invocation_trees(&direct, &saved);
}

/// A copied value has its own immutable cell even when its initializer reads
/// a mutable default import. The same local spelling in another chunk must
/// retain a different identity as well.
#[test]
fn copied_default_import_and_unrelated_names_keep_distinct_cells() {
    let source = "import { next } from './provider.js';\n\
                  import { live, copied } from './bridge.js';\n\
                  import { value } from './unrelated.js';\n\
                  const first = next();\n\
                  const second = copied;\n\
                  console.log(first, second, live, value);\n";
    assert_alias_copy_split(source, "2 1 2 99\n", "2 1 2 99\n", 0);
}

/// Changing only the selected read to the live alias must retain its ordering
/// against the provider's writer. The emitted rejected tree makes the unsafe
/// relocation observable independently of analysis reports.
#[test]
fn mutable_default_reexport_is_not_the_immutable_copy() {
    let source = "import { next } from './provider.js';\n\
                  import { live, copied } from './bridge.js';\n\
                  import { value } from './unrelated.js';\n\
                  const first = next();\n\
                  const second = live;\n\
                  console.log(first, second, copied, value);\n";
    assert_alias_copy_split(source, "2 2 1 99\n", "2 1 1 99\n", 3);
}
