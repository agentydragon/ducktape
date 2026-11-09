//! Object spreads over ordinary own data, with and without descriptor
//! operations on the source, keep their emitted programs equivalent under
//! Node; a candidate that moves a source write across the spread is refused
//! with a typed conflict.

use debundle_e2e_support::*;

#[test]
fn settled_single_spread_preserves_order_and_identity() {
    let source = "let later = 0;\nconst original = () => later;\nconst source = { 10: original, 2: original, alpha: original, [\"__proto__\"]: original };\nconst copied = { ...source };\nsource.alpha = () => -1;\nlater = 42;\nconsole.log(Object.keys(copied).join(\",\"), copied[\"__proto__\"] === original, copied.alpha());\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "2,10,alpha,__proto__ true 42\n");
}

#[test]
fn own_data_stores_before_a_spread_keep_node_parity() {
    for (source, expected) in [
        (
            "const source = { alpha: 1, beta: 2 };\nsource.alpha = 3;\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.alpha);\n",
            "alpha,beta 3\n",
        ),
        (
            "const source = { alpha: 1, beta: 2 };\nsource.alpha = source.beta = 3;\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.alpha, copied.beta);\n",
            "alpha,beta 3 3\n",
        ),
        (
            "const source = { alpha: 1, beta: 2 };\nlet key = \"beta\";\n(source.alpha = 3, source[key] = 4);\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.alpha, copied.beta);\n",
            "alpha,beta 3 4\n",
        ),
        (
            "const source = { alpha: 1, beta: 2 };\ndelete source.alpha;\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.beta);\n",
            "beta 2\n",
        ),
    ] {
        let fixture = run_fixture(FixtureOpts::new(source, vec![]));
        assert_preserved_entry_output(&fixture, expected);
    }
}

#[test]
fn pooled_spread_sites_keep_node_parity() {
    for (source, expected) in [
        (
            "let properties = { alpha: 1 };\nfunction readCopy() { const copied = { ...properties }; return copied.alpha; }\nconst first = readCopy();\nproperties = { beta: 2 };\nconst second = readCopy();\nconsole.log(first, second);\n",
            "1 undefined\n",
        ),
        (
            "const properties = { alpha: 1 };\nfunction readCopy() { const copied = { ...properties }; return copied.alpha; }\nconst first = readCopy();\nproperties.alpha = 2;\nconst second = readCopy();\nconsole.log(first, second);\n",
            "1 2\n",
        ),
        (
            "function make(include) { const object = { stable: 1 }; if (include) object.added = 2; return object; }\nconst first = make(true);\nconst second = make(false);\nconst observed = second.added;\nconsole.log(first.added, observed, second.stable);\n",
            "2 undefined 1\n",
        ),
        (
            "function readStable() { const object = { stable: 1 }; return object.stable; }\nconst first = readStable();\nconst second = readStable();\nconsole.log(first, second);\n",
            "1 1\n",
        ),
    ] {
        let fixture = run_fixture(FixtureOpts::new(source, vec![]));
        assert_preserved_entry_output(&fixture, expected);
    }
}

#[test]
fn candidate_that_moves_source_write_before_spread_is_refused() {
    let source = "const source = { alpha: 3 };\nconst copied = { ...source };\nconst observed = helper();\nfunction helper() { return 1; }\nsource.alpha = 4;\nconsole.log(copied.alpha);\n";
    let baseline = run_fixture(
        FixtureOpts::new(source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&baseline, "3\n");

    // Keep `copied` and the observation in the entry. Its static imports run
    // first: importing `helper` reaches the writer module, which imports only
    // the `source` provider and writes alpha before the entry evaluates the
    // spread. The provider has no dependency back to the entry.
    let rejected = run_emit_on_rejection_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "b_writer",
                    &[Member::new("helper")],
                    &["source.alpha = 4;"],
                ),
                logical_module("z_source", &[Member::new("source")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let report = rejected.candidate_relocation_assessment();
    assert!(report["swapped_pairs"].as_u64().unwrap() > 0, "{report:#}");
    assert_ne!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
}

#[test]
fn changed_or_nondata_enumerability_keeps_node_parity() {
    let cases = [
        // delete and re-add changes own-key order
        (
            "const source = { first: 1, second: 2 };\ndelete source.first;\nsource.first = 1;\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.first);\n",
            "second,first 1\n",
        ),
        // callable delete invalidates retained source shape
        (
            "const source = { first: 1, second: 2 };\nfunction removeFirst() { delete source.first; }\nremoveFirst();\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.first);\n",
            "second undefined\n",
        ),
        // unresolved binding dynamic receiver revokes candidates
        (
            "const source = { alpha: 3 };\nglobalThis.unknownReceiver = {};\nconst target = globalThis.unknownReceiver;\nconst key = \"unrelated\";\ntarget[key] = 4;\nconst copied = { ...source };\nconsole.log(copied.alpha, source.alpha);\n",
            "3 3\n",
        ),
        // mixed literal spread remains conservative
        (
            "const source = { value: 7 };\nconst copied = { before: 0, ...source, after: 2 };\nconsole.log(Object.keys(copied).join(\",\"), copied.value);\n",
            "before,value,after 7\n",
        ),
        // descriptor-map application onto an empty target
        (
            "const source = { visible: 1, hidden: 2 };\nconst descriptors = Object.getOwnPropertyDescriptors(source);\nconst target = {};\nObject.defineProperties(target, descriptors);\nconst copied = { ...target };\nconsole.log(Object.keys(copied).join(\",\"), copied.hidden);\n",
            "visible,hidden 2\n",
        ),
        // descriptor-map application onto its own source retains data callbacks without invoking them
        (
            "let calls = 0;\nconst source = { value: () => { calls += 1; return later; } };\nconst descriptors = Object.getOwnPropertyDescriptors(source);\nObject.defineProperties(source, descriptors);\nconst copied = { ...source };\nconst later = 42;\nconsole.log(calls, copied.value());\n",
            "0 42\n",
        ),
        // direct descriptor mutation remains conservative
        (
            "const source = { visible: 1, hidden: 2 };\nObject.defineProperty(source, \"hidden\", { enumerable: false });\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.hidden);\n",
            "visible undefined\n",
        ),
        // accessor source remains conservative
        (
            "let reads = 0;\nconst source = { get value() { reads += 1; return 7; } };\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.value, reads);\n",
            "value 7 1\n",
        ),
        // setter source remains conservative
        (
            "let writes = 0;\nconst source = { set value(value) { writes += value; } };\nconst copied = { ...source };\nconsole.log(Object.keys(copied).length, writes);\n",
            "1 0\n",
        ),
        // assignment through an existing setter remains conservative
        (
            "let writes = 0;\nconst source = { set value(value) { writes += value; } };\nsource.value = 3;\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), writes);\n",
            "value 3\n",
        ),
    ];

    for (source, expected_output) in cases {
        let fixture = run_fixture(FixtureOpts::new(source, vec![]));
        assert_preserved_entry_output(&fixture, expected_output);
    }
}
