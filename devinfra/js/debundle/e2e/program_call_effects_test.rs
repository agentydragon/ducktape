//! Source-known call chains retain cyclic live-cell effects across chunks.

use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, staged};
use debundle_e2e_support::*;

const SOURCE: &str =
    "import { inspect } from './observer.js';\ninspect();\nexport var callback = () => 1;\n";
const READER: &str = "import { callback } from './app.js';\nexport function read() { console.log(typeof callback); }\n";

fn assert_split(
    source: &str,
    peers: &[(&str, &str)],
    members: &[&'static str],
    exit: i32,
    original: &str,
    emitted: &str,
) {
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "state",
                &members
                    .iter()
                    .map(|name| Member::new(name))
                    .collect::<Vec<_>>(),
            )],
        )
        .with_extra_chunks(peers)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_node_output(&staged.root().join("snapshot/static/app.js"), original, "");
    let direct = staged.run(&["--emit-on-rejection"]);
    let saved = staged.run_saved(&["--emit-on-rejection"]);
    for invocation in [&direct, &saved] {
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            emitted,
            "",
        );
        assert_eq!(
            invocation.result.status.code(),
            Some(exit),
            "{}",
            invocation.result.stderr
        );
    }
    assert_same_invocation_trees(&direct, &saved);
}

#[test]
fn a_transitive_imported_observer_cannot_move_export_initialization() {
    assert_split(
        SOURCE,
        &[
            (
                "static/observer",
                "import { read } from './reader.js';\nexport function inspect() { read(); }\n",
            ),
            ("static/reader", READER),
        ],
        &["callback"],
        3,
        "undefined\n",
        "function\n",
    );
}

#[test]
fn local_and_imported_helpers_keep_the_same_cell_order() {
    for provider in [
        "import { callback } from './app.js';\nfunction read() { console.log(typeof callback); }\nexport function inspect() { read(); }\n",
        "import { callback } from './app.js';\nexport const inspect = () => console.log(typeof callback);\n",
        "import { callback } from './app.js';\nfunction first() { second(true); }\nfunction second(done) { if (done) console.log(typeof callback); else first(); }\nexport function inspect() { second(false); }\n",
    ] {
        assert_split(
            SOURCE,
            &[("static/observer", provider)],
            &["callback"],
            3,
            "undefined\n",
            "function\n",
        );
    }
    assert_split(
        "function read() { console.log(typeof callback); }\nfunction inspect() { read(); }\ninspect();\nexport var callback = () => 1;\n",
        &[],
        &["callback"],
        3,
        "undefined\n",
        "function\n",
    );
}

#[test]
fn parameter_return_and_reexport_flow_share_the_observers_effects() {
    assert_split(
        SOURCE,
        &[
            (
                "static/observer",
                "import { inspect } from './implementation.js';\nexport { inspect };\n",
            ),
            (
                "static/implementation",
                "import { relay } from './relay.js';\nimport { read } from './reader.js';\nexport const inspect = () => relay(read)();\n",
            ),
            (
                "static/relay",
                "export function relay(value) { return value; }\n",
            ),
            (
                "static/reader",
                "import { published as value } from './cells.js';\nexport function read() { console.log(typeof value); }\n",
            ),
            (
                "static/cells",
                "import { callback as current } from './app.js';\nexport { current as published };\n",
            ),
        ],
        &["callback"],
        3,
        "undefined\n",
        "function\n",
    );
}

#[test]
fn transitive_lexical_writes_preserve_the_initializer_order() {
    assert_split(
        "import { inspect } from './observer.js';\ninspect();\nexport var callback = 1;\nexport function set() { callback = 2; }\nconsole.log(callback);\n",
        &[(
            "static/observer",
            "import { set } from './app.js';\nexport function inspect() { set(); }\n",
        )],
        &["callback", "set"],
        3,
        "1\n",
        "2\n",
    );
}

#[test]
fn initialized_cells_and_unrelated_bodies_do_not_gain_early_read_constraints() {
    assert_split(
        "import { inspect } from './observer.js';\nexport var callback = () => 1;\ninspect();\n",
        &[
            (
                "static/observer",
                "import { read } from './reader.js';\nexport function inspect() { read(); }\n",
            ),
            ("static/reader", READER),
        ],
        &["callback"],
        0,
        "function\n",
        "function\n",
    );
    assert_split(
        SOURCE,
        &[
            (
                "static/observer",
                "export function inspect() { console.log('observed'); }\n",
            ),
            ("static/reader", READER),
        ],
        &["callback"],
        0,
        "observed\n",
        "observed\n",
    );
}
