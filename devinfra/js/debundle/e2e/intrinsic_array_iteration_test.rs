//! Builtin iteration preserves real callback effects and retained array values.
use analysis::DependencyGraphReport;
use analysis::reports::schema::{BindingEffectAuthorityReport, LexicalBindingEffectBody};
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

fn assert_selected_iteration_receipt_in_graph(
    graph: &DependencyGraphReport,
    binding: &str,
    method: &str,
) {
    let expected = format!("prototype.{method}");
    let owner = source_unit_for_binding(graph, binding);
    let execution = graph
        .nodes
        .iter()
        .find(|node| node.id == owner)
        .unwrap()
        .synchronous_execution
        .as_ref()
        .unwrap();
    assert!(execution.complete, "{execution:#?}");
    let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) =
        &execution.binding_effect_authority
    else {
        panic!("selected {method} must compose its operation receipt: {execution:#?}");
    };
    assert!(
        receipt.calls.iter().any(|call| matches!(&call.target,
        LexicalBindingEffectBody::Native { property: Some(property), .. }
            if property == &expected)),
        "{receipt:#?}"
    );
}

fn assert_selected_iteration_receipt(fixture: &Fixture, binding: &str, method: &str) {
    assert_selected_iteration_receipt_in_graph(&fixture.dependency_graph(), binding, method);
}

fn assert_no_selected_iteration_receipt_in_graph(
    graph: &DependencyGraphReport,
    binding: &str,
    method: &str,
) {
    let expected = format!("prototype.{method}");
    let owner = source_unit_for_binding(graph, binding);
    let execution = graph
        .nodes
        .iter()
        .find(|node| node.id == owner)
        .unwrap()
        .synchronous_execution
        .as_ref()
        .unwrap();
    let has_receipt = match &execution.binding_effect_authority {
        Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) => {
            receipt.calls.iter().any(|call| {
                matches!(&call.target,
                LexicalBindingEffectBody::Native { property: Some(property), .. }
                    if property == &expected)
            })
        }
        _ => false,
    };
    assert!(!has_receipt, "{execution:#?}");
}

fn assert_no_selected_iteration_receipt(fixture: &Fixture, binding: &str, method: &str) {
    assert_no_selected_iteration_receipt_in_graph(&fixture.dependency_graph(), binding, method);
}

#[test]
fn ordered_map_results_have_covered_effects_for_an_actual_relocation() {
    let fixture = run_fixture(FixtureOpts::new(
        "const entries = [1, 2];\nconst before = 0;\nconst mapped = entries.map(value => value + 1);\nconst after = 1;\nconsole.log(before, mapped.join(','), after);\n",
        vec![
            logical_module("array", &[Member::new("entries")]),
            logical_module("anchors", &[Member::new("before"), Member::new("after")]),
            logical_module("mapped", &[Member::new("mapped")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0 2,3 1\n");
    assert_selected_iteration_receipt(&fixture, "mapped", "map");
    let assessment = fixture.effect_translation_validation();
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
}

#[test]
fn ordered_map_uses_the_receiver_selected_before_its_argument_runs() {
    let fixture = run_fixture(FixtureOpts::new(
        "let selections = 0;\nconst alpha = () => 7;\nconst beta = () => 8;\nconst first = [alpha];\nconst second = [beta];\nlet source = first;\nfunction choose() { selections++; source = second; return invoke => invoke(); }\nconst mapped = source.map(choose());\nconsole.log(selections, mapped[0], source[0]());\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "1 7 8\n");
    assert_selected_iteration_receipt(&fixture, "mapped", "map");
    let graph = fixture.dependency_graph();
    let node = |binding| {
        graph
            .nodes
            .iter()
            .find(|node| node.id == source_unit_for_binding(&graph, binding))
            .unwrap()
    };
    let execution = node("mapped").synchronous_execution.as_ref().unwrap();
    let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) =
        &execution.binding_effect_authority
    else {
        unreachable!()
    };
    let called_owner = |ordinal| {
        receipt.calls.iter().any(|call| {
            matches!(
        &call.target, LexicalBindingEffectBody::Callable { owner, .. } if *owner == ordinal)
        })
    };
    assert!(
        called_owner(node("alpha").statement_ordinal),
        "{receipt:#?}"
    );
    assert!(
        !called_owner(node("beta").statement_ordinal),
        "{receipt:#?}"
    );
}

#[test]
fn ordered_map_known_throw_stops_before_the_next_index() {
    let fixture = run_fixture(FixtureOpts::new(
        "let calls = 0;\nconst entries = [1, 2];\nfunction fail(value) { calls++; throw value; }\nfunction run() { try { entries.map(fail); } catch (error) { return error; } }\nconst observed = run();\nconsole.log(calls, observed);\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "1 1\n");
    assert_selected_iteration_receipt(&fixture, "observed", "map");
}

#[test]
fn for_each_returns_undefined_keeps_callback_order_and_defers_stored_functions() {
    let source = "let later = 0;\nlet visits = 0;\nlet trace = 0;\nlet executor_calls = 0;\nconst entries = [{ id: 'a', order: 1, executor: () => { executor_calls++; return later; } }, { id: 'b', order: 2, executor: () => { executor_calls++; return later; } }];\nconst result = entries.forEach(entry => { visits++; trace = entry.order; return entry.executor; });\nconst copy = trace;\nlater = 42;\nconsole.log(result === undefined, visits, copy, executor_calls, entries[0].executor(), executor_calls);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "registry",
                &[
                    Member::new("entries"),
                    Member::new("result"),
                    Member::new("trace"),
                    Member::new("visits"),
                    Member::new("executor_calls"),
                ],
                &[
                    "const result = entries.forEach(entry => { visits++; trace = entry.order; return entry.executor; });",
                ],
            ),
            logical_module("readers", &[Member::new("copy")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "true 2 2 0 42 1\n");
    assert_selected_iteration_receipt(&fixture, "result", "forEach");
}

#[test]
fn filter_uses_predicate_truth_at_runtime_without_faking_exact_slots() {
    let fixture = run_fixture(FixtureOpts::new(
        "const entries = [{ id: 'a' }, { id: 'b' }];\nconst excluded = entries.filter(() => false);\nconst included = entries.filter(() => true);\nconst mapped = included.map(entry => entry.id);\nconsole.log(excluded.length, included.length, mapped.join(','));\n",
        vec![
            logical_module("entries", &[Member::new("entries")]),
            logical_module("excluded", &[Member::new("excluded")]),
            logical_module("included", &[Member::new("included")]),
            logical_module("mapped", &[Member::new("mapped")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0 2 a,b\n");
    assert_selected_iteration_receipt(&fixture, "excluded", "filter");
    assert_selected_iteration_receipt(&fixture, "included", "filter");
    assert_no_selected_iteration_receipt(&fixture, "mapped", "map");
}

#[test]
fn filter_definite_truthiness_keeps_original_elements_for_later_iteration() {
    let fixture = run_fixture(FixtureOpts::new(
        "const entries = [{ id: 'a' }, { id: 'b' }];\nconst excluded = entries.filter(() => null);\nconst included = entries.filter(() => ({}));\nconst mapped = included.map(entry => entry.id);\nconsole.log(excluded.length, included[0] === entries[0], mapped.join(','));\n",
        vec![
            logical_module("entries", &[Member::new("entries")]),
            logical_module("excluded", &[Member::new("excluded")]),
            logical_module("included", &[Member::new("included")]),
            logical_module("mapped", &[Member::new("mapped")]),
        ],
    ));
    save_relocation_reports(
        &fixture.report_root,
        &fixture.chunk_id,
        "array-filter-definite-truthiness",
    );
    assert_preserved_entry_output(&fixture, "0 true a,b\n");
    assert_selected_iteration_receipt(&fixture, "excluded", "filter");
    assert_selected_iteration_receipt(&fixture, "included", "filter");
    assert_selected_iteration_receipt(&fixture, "mapped", "map");
}

#[test]
fn reached_map_callback_return_survives_an_invalidated_suffix() {
    let source = "let later = 0;\nconst entries = [{ id: 'a' }, { id: 'b' }];\nconst result = entries.map((entry, index, array) => { const readLater = () => later; if (index === 0) array[1] = entry; return readLater; });\nconst observed = result[0]();\nlater = 42;\nconsole.log(observed);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    save_relocation_reports(
        &fixture.report_root,
        &fixture.chunk_id,
        "array-map-reached-return-before-incomplete-suffix",
    );
    assert_preserved_entry_output(&fixture, "0\n");

    let split = vec![
        logical_module_with_anon(
            "registry",
            &[
                Member::new("entries"),
                Member::new("result"),
                Member::new("observed"),
            ],
            &[
                "const result = entries.map((entry, index, array) => { const readLater = () => later; if (index === 0) array[1] = entry; return readLater; });",
                "const observed = result[0]();",
            ],
        ),
        logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
    ];
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(source, split));
    save_relocation_reports(
        &rejected.report_root,
        &rejected.chunk_id,
        "array-map-reached-return-before-incomplete-suffix-rejected",
    );
    let assessment = rejected.candidate_relocation_assessment();
    assert_ne!(
        assessment["status"], "accepted_within_checked_scope",
        "{assessment:#}"
    );
}

#[test]
fn mixed_legacy_callback_completion_does_not_hide_later_or_tail_effects() {
    let source = "let later = 0;\nlet calls = 0;\nlet second = -1;\nlet tail = 0;\nconst entries = [1, 2];\nfunction callback(value) { calls++; if (value === 1 && later) throw 'stop'; if (value === 2) second = later; return value; }\ntry { entries.forEach(callback); } catch (error) {}\ntail++;\nlater = 42;\nconsole.log(calls, second, tail);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    save_relocation_reports(
        &fixture.report_root,
        &fixture.chunk_id,
        "array-iteration-mixed-callback-completion",
    );
    assert_preserved_entry_output(&fixture, "2 0 1\n");

    let split = vec![
        logical_module_with_anon(
            "iteration",
            &[
                Member::new("entries"),
                Member::new("calls"),
                Member::new("second"),
                Member::new("tail"),
                Member::new("callback"),
            ],
            &[
                "try { entries.forEach(callback); } catch (error) {}",
                "tail++;",
            ],
        ),
        logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
    ];
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(source, split));
    save_relocation_reports(
        &rejected.report_root,
        &rejected.chunk_id,
        "array-iteration-mixed-callback-completion-rejected",
    );
    let assessment = rejected.candidate_relocation_assessment();
    assert_ne!(
        assessment["status"], "accepted_within_checked_scope",
        "{assessment:#}"
    );
}

#[test]
fn legacy_iteration_does_not_promote_a_custom_method_callback_throw() {
    let source = "let later = 0;\nlet custom_calls = 0;\nlet callback_calls = 0;\nlet observed = -1;\nlet tail = 0;\nconst entries = [1];\nconst custom = { forEach() { custom_calls++; observed = later; } };\nObject.setPrototypeOf(entries, custom);\nentries.forEach((Object.setPrototypeOf(entries, Array.prototype), () => { callback_calls++; throw 'callback'; }));\ntail++;\nlater = 42;\nconsole.log(custom_calls, callback_calls, tail, observed);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    save_relocation_reports(
        &fixture.report_root,
        &fixture.chunk_id,
        "array-iteration-restored-prototype-custom-method",
    );
    assert_preserved_entry_output(&fixture, "1 0 1 0\n");

    let split = vec![
        logical_module_with_anon(
            "iteration",
            &[
                Member::new("entries"),
                Member::new("custom"),
                Member::new("custom_calls"),
                Member::new("callback_calls"),
                Member::new("observed"),
                Member::new("tail"),
            ],
            &[
                "Object.setPrototypeOf(entries, custom);",
                "entries.forEach((Object.setPrototypeOf(entries, Array.prototype), () => { callback_calls++; throw 'callback'; }));",
                "tail++;",
            ],
        ),
        logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
    ];
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(source, split));
    save_relocation_reports(
        &rejected.report_root,
        &rejected.chunk_id,
        "array-iteration-restored-prototype-custom-method-rejected",
    );
    let assessment = rejected.candidate_relocation_assessment();
    assert_ne!(
        assessment["status"], "accepted_within_checked_scope",
        "{assessment:#}"
    );
}

#[test]
fn argument_time_method_replacement_keeps_the_original_runtime_selection() {
    let fixture = run_fixture(FixtureOpts::new(
        "let selections = 0;\nconst entries = [1];\nfunction choose() { selections++; Array.prototype.map = () => ['changed']; return value => value + 1; }\nconst mapped = entries.map(choose());\nconsole.log(selections, mapped.join(','), entries.map(() => 0).join(','));\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "1 2 changed\n");
    // The closed-write census cannot prove this method slot. Runtime ordering
    // remains intact, while the analysis must keep its missing identity proof.
    let graph = fixture.dependency_graph();
    let owner = source_unit_for_binding(&graph, "mapped");
    let execution = graph
        .nodes
        .iter()
        .find(|node| node.id == owner)
        .unwrap()
        .synchronous_execution
        .as_ref()
        .unwrap();
    assert!(
        !matches!(
            execution.binding_effect_authority,
            Some(BindingEffectAuthorityReport::OperationReceipt { .. })
        ),
        "{execution:#?}"
    );
}

#[test]
fn argument_time_prototype_restore_does_not_replace_the_captured_custom_method() {
    let fixture = run_fixture(FixtureOpts::new(
        "let custom_calls = 0;\nlet callback_calls = 0;\nconst entries = [1];\nconst custom = { forEach(callback) { custom_calls++; callback(entries[0]); return 'custom'; } };\nObject.setPrototypeOf(entries, custom);\nconst result = entries.forEach((Object.setPrototypeOf(entries, Array.prototype), value => { callback_calls++; }));\nconsole.log(result, custom_calls, callback_calls);\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "custom 1 1\n");
    // GetValue captured the custom method before the argument restored the
    // array's prototype. The built-in iteration row must not claim this call.
    assert_no_selected_iteration_receipt(&fixture, "result", "forEach");
}

#[test]
fn some_stops_before_a_later_throwing_callback_value() {
    let fixture = run_fixture(FixtureOpts::new(
        "const entries = [() => ({ id: 'first' }), () => { throw 'later'; }];\nlet calls = 0;\nfunction invoke(value) { calls++; return value(); }\nconst result = entries.some(invoke);\nconsole.log(result, calls);\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "true 1\n");
    assert_selected_iteration_receipt(&fixture, "result", "some");
}

#[test]
fn reached_some_callback_throw_is_an_abrupt_completion() {
    let fixture = run_fixture(FixtureOpts::new(
        "const entries = [() => { throw 'reached'; }, () => ({ id: 'later' })];\nlet calls = 0;\nfunction invoke(value) { calls++; return value(); }\nfunction run() { try { return entries.some(invoke); } catch (error) { return error; } }\nconst result = run();\nconsole.log(result, calls);\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "reached 1\n");
    assert_selected_iteration_receipt(&fixture, "result", "some");
}

#[test]
fn unknown_some_predicate_refuses_without_executing_the_suffix() {
    let source = "const entries = [undefined, { rank: 1 }];\nconst before = 0;\nlet calls = 0;\nconst result = entries.some((value, index) => { calls++; return index > 0; });\nconst observed = calls;\nconst after = 1;\nconsole.log(result, observed);\n";
    let modules = vec![
        logical_module("array", &[Member::new("entries")]),
        logical_module("anchors", &[Member::new("before"), Member::new("after")]),
        logical_module(
            "query",
            &[
                Member::new("calls"),
                Member::new("result"),
                Member::new("observed"),
            ],
        ),
    ];
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(source, modules));
    save_relocation_reports(
        &rejected.report_root,
        &rejected.chunk_id,
        "array-some-unknown-predicate-prefix",
    );
    assert_node_output(&rejected.source_root.join("static/app.js"), "true 2\n", "");
    let assessment = rejected.candidate_relocation_assessment();
    assert_ne!(
        assessment["status"], "accepted_within_checked_scope",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    let graph = rejected.dependency_graph();
    assert_no_selected_iteration_receipt_in_graph(&graph, "result", "some");
    let owner = source_unit_for_binding(&graph, "result");
    let execution = &graph
        .nodes
        .iter()
        .find(|node| node.id == owner)
        .unwrap()
        .synchronous_execution;
    assert!(!execution.as_ref().unwrap().complete, "{execution:#?}");
}

#[test]
fn some_after_receiver_length_mutation_keeps_the_original_method_selection_conservative() {
    let source = "const entries = [1];\nconst before = 0;\nconst result = entries.some((entries.length = 0, () => true));\nconst after = 1;\nconsole.log(result, entries.length);\n";
    let modules = vec![
        logical_module("array", &[Member::new("entries")]),
        logical_module("anchors", &[Member::new("before"), Member::new("after")]),
        logical_module("query", &[Member::new("result")]),
    ];
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(source, modules));
    save_relocation_reports(
        &rejected.report_root,
        &rejected.chunk_id,
        "array-some-argument-time-receiver-mutation",
    );
    assert_node_output(&rejected.source_root.join("static/app.js"), "false 0\n", "");
    let assessment = rejected.candidate_relocation_assessment();
    assert_ne!(
        assessment["status"], "accepted_within_checked_scope",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    let graph = rejected.dependency_graph();
    assert_no_selected_iteration_receipt_in_graph(&graph, "result", "some");
}

fn assert_early_iteration_rejected(
    declarations: &str,
    setup: &[&str],
    collection: &str,
    trailing: &str,
    names: &[&'static str],
) {
    assert_early_iteration_rejected_with_executor(
        declarations,
        setup,
        collection,
        trailing,
        names,
        "0",
    );
}

fn assert_early_iteration_rejected_with_executor(
    declarations: &str,
    setup: &[&str],
    collection: &str,
    trailing: &str,
    names: &[&'static str],
    executor: &str,
) {
    let source = format!(
        "import {{ consumeCallbacks }} from './peer.js';\nlet later = 0;\nconst entries = [{{ id: 'a', executor: () => {executor} }}, {{ id: 'b', executor: () => {executor} }}];\nlet observed = -1;\n{declarations}\n{}\nconst result = {collection};\n{trailing}\nlater = 42;\nconsole.log(observed);\n",
        setup.join("\n")
    );
    let mut registry = vec![
        Member::new("entries"),
        Member::new("observed"),
        Member::new("result"),
    ];
    registry.extend(names.iter().map(|name| Member::new(name)));
    let mut steps = setup.to_vec();
    if !trailing.is_empty() {
        steps.push(trailing);
    }
    let split = vec![
        logical_module_with_anon("registry", &registry, &steps),
        logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
    ];
    let peers = [(
        "static/peer",
        "export function consumeCallbacks(array) { return array[0](); }\n",
    )];
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, "0\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, split).with_extra_chunks(&peers),
        &["registry", "values/later"],
        &[],
    );
}

/// The split is accepted and the emitted entry prints what the source does:
/// the callback ran on every element without invoking their stored
/// `executor` callbacks, which read `later`.
fn assert_iteration_split_accepted(collection: &str, trailing: &str, expected: &str) {
    assert_iteration_split_accepted_after("", collection, trailing, expected);
}

fn assert_iteration_split_accepted_after(
    setup: &str,
    collection: &str,
    trailing: &str,
    expected: &str,
) {
    let source = format!(
        "let later = 0;\n{setup}\nconst entries = [{{ id: 'a', size: 1, executor: () => later }}, {{ id: 'b', size: 2, executor: () => later }}];\nconst result = {collection};\n{trailing}\nlater = 42;\nconsole.log(JSON.stringify(result), entries[0].executor());\n"
    );
    let mut steps = Vec::new();
    if !setup.is_empty() {
        steps.push(setup);
    }
    if !trailing.is_empty() {
        steps.push(trailing);
    }
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![
            logical_module_with_anon(
                "registry",
                &[Member::new("entries"), Member::new("result")],
                &steps,
            ),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, expected);
}

#[test]
fn iteration_rows_run_callbacks_without_invoking_stored_element_callbacks() {
    for (collection, expected) in [
        ("entries.some(entry => entry.id === 'b')", "true 42\n"),
        ("entries.every(entry => entry.size > 0)", "true 42\n"),
        ("entries.findIndex(entry => entry.id === 'b')", "1 42\n"),
        ("entries.find(entry => entry.id === 'b').size", "2 42\n"),
        (
            "entries.reduce((total, entry) => total + entry.size, 0)",
            "3 42\n",
        ),
        ("entries.reduce((first, entry) => first).id", "\"a\" 42\n"),
        (
            "entries.flatMap(entry => [entry.id, entry.size])",
            "[\"a\",1,\"b\",2] 42\n",
        ),
        ("entries.flatMap(entry => entry.id)", "[\"a\",\"b\"] 42\n"),
    ] {
        assert_iteration_split_accepted(collection, "", expected);
    }
}

/// Rows require only the Array prototype properties they read: writes to the
/// global object, to `Array` statics, or to `Object.prototype` under other keys
/// leave them intact.
#[test]
fn unrelated_intrinsic_writes_leave_iteration_rows_intact() {
    for setup in [
        "globalThis.appState = { ready: true };",
        "Array.from = Array.from;",
        "Array.prototype.unrelated = function () { return later; };",
        "Object.defineProperty(Object.prototype, 'unrelated', { value: 1, configurable: true });",
    ] {
        assert_iteration_split_accepted_after(
            setup,
            "entries.map(entry => entry.size)",
            "",
            "[1,2] 42\n",
        );
    }
}

#[test]
fn writes_reaching_a_row_slot_keep_real_effects() {
    for (declarations, setup, collection, names) in [
        (
            "",
            "Array.prototype.some = function () { observed = later; return false; };",
            "entries.some(() => false)",
            &[][..],
        ),
        (
            "",
            "Array.prototype.forEach = function () { observed = later; };",
            "entries.forEach(() => {})",
            &[][..],
        ),
        (
            "",
            "Object.defineProperty(Array.prototype, 'every', { value() { observed = later; return true; }, configurable: true });",
            "entries.every(() => true)",
            &[],
        ),
        (
            "const key = ['fi', 'nd'].join('');",
            "Array.prototype[key] = function () { observed = later; };",
            "entries.find(() => false)",
            &["key"],
        ),
        (
            "",
            "Object.defineProperty(Array, Symbol.species, { get() { observed = later; return Array; }, configurable: true });",
            "entries.map(entry => entry.id)",
            &[],
        ),
        (
            "",
            "Array.prototype.constructor = { get [Symbol.species]() { observed = later; return Array; } };",
            "entries.filter(() => true)",
            &[],
        ),
    ] {
        assert_early_iteration_rejected(declarations, &[setup], collection, "", names);
    }
}

#[test]
fn iteration_results_keep_retained_element_capabilities() {
    for (collection, trailing) in [
        (
            "entries.find(entry => entry.id === 'a')",
            "observed = result.executor();",
        ),
        (
            "entries.reduce((last, entry) => entry.executor, null)",
            "observed = result();",
        ),
        (
            "entries.reduce((last, entry) => entry)",
            "observed = result.executor();",
        ),
        (
            "entries.flatMap(entry => [entry.executor])",
            "observed = result[0]();",
        ),
        (
            "entries.flatMap(entry => entry.executor)",
            "observed = result[1]();",
        ),
    ] {
        assert_early_iteration_rejected_with_executor("", &[], collection, trailing, &[], "later");
    }
}

#[test]
fn callback_writes_stay_ordered_before_later_readers() {
    let source = "let later = 0;\nlet seen = 0;\nconst entries = [{ id: 'a' }, { id: 'b' }];\nentries.some(entry => { seen += 1; return entry.id === 'b'; });\nconst copy = seen;\nlater = 42;\nconsole.log(copy, seen, later);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "registry",
                &[Member::new("seen"), Member::new("entries")],
                &["entries.some(entry => { seen += 1; return entry.id === 'b'; });"],
            ),
            logical_module("readers", &[Member::new("copy")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "2 2 42\n");
}

#[test]
fn actual_iteration_callback_invocation_keeps_early_reads() {
    for collection in [
        "entries.forEach(entry => { observed = entry.executor(); })",
        "entries.filter(entry => { observed = entry.executor(); return true; })",
        "entries.map(entry => { observed = entry.executor(); return entry.id; })",
        "entries.some(entry => { observed = entry.executor(); return false; })",
        "entries.every(entry => { observed = entry.executor(); return true; })",
        "entries.find(entry => { observed = entry.executor(); return false; })",
        "entries.findIndex(entry => { observed = entry.executor(); return false; })",
        "entries.flatMap(entry => { observed = entry.executor(); return []; })",
        "entries.reduce((total, entry) => { observed = entry.executor(); return total; }, 0)",
        "entries.reduce((first, entry) => { observed = entry.executor(); return first; })",
    ] {
        assert_early_iteration_rejected_with_executor("", &[], collection, "", &[], "later");
    }
}

#[test]
fn iteration_source_and_entry_mutation_remain_conservative() {
    assert_early_iteration_rejected(
        "function replace({items}) { items[1] = { executor: () => later }; }",
        &["replace({items: entries});"],
        "entries.forEach(entry => { observed = entry.executor(); })",
        "",
        &["replace"],
    );
    assert_early_iteration_rejected(
        "const holder = { items: entries, get replace() { this.items[1] = { executor: () => later }; return 0; } };",
        &["holder.replace;"],
        "entries.forEach(entry => { observed = entry.executor(); })",
        "",
        &["holder"],
    );
    assert_early_iteration_rejected(
        "const holder = { items: entries, replace() { this.items[1] = { executor: () => later }; } };",
        &["holder.replace();"],
        "entries.forEach(entry => { observed = entry.executor(); })",
        "",
        &["holder"],
    );
    assert_early_iteration_rejected(
        "function replace() { arguments[0][1] = { executor: () => later }; }",
        &["replace(entries);"],
        "entries.forEach(entry => { observed = entry.executor(); })",
        "",
        &["replace"],
    );
    for collection in [
        "entries.some((entry, index, array) => { if (index === 0) array[1] = { executor: () => later }; else observed = entry.executor(); return false; })",
        "entries.reduce((total, entry, index, array) => { if (index === 0) array[1] = { executor: () => later }; else observed = entry.executor(); return total; }, 0)",
        "entries.forEach((entry, index, array) => { if (index === 0) array[1] = { executor: () => later }; else observed = entry.executor(); })",
        "entries.forEach((entry, index) => { if (index === 0) entries[1].executor = () => later; else observed = entry.executor(); })",
        "entries.forEach(function(entry, index) { if (index === 0) arguments[2][1] = { executor: () => later }; else observed = entry.executor(); })",
    ] {
        assert_early_iteration_rejected("", &[], collection, "", &[]);
    }
    assert_early_iteration_rejected(
        "function mutate() { arguments[0][1] = { executor: () => later }; }",
        &[],
        "entries.forEach((entry, index, array) => { if (index === 0) mutate(array); else observed = entry.executor(); })",
        "",
        &["mutate"],
    );
}

#[test]
fn replaced_methods_and_accessor_entries_keep_real_effects() {
    for (setup, collection) in [
        (
            "entries.forEach = callback => { observed = later; };",
            "entries.forEach(() => {})",
        ),
        (
            "entries.some = callback => { observed = later; };",
            "entries.some(() => false)",
        ),
        (
            "entries.reduce = function () { observed = later; };",
            "entries.reduce((total) => total, 0)",
        ),
    ] {
        assert_early_iteration_rejected("", &[setup], collection, "", &[]);
    }
    assert_early_iteration_rejected(
        "",
        &["Object.defineProperty(entries[0], 'id', { get() { observed = later; return 'a'; } });"],
        "entries.forEach(entry => { entry.id; })",
        "",
        &[],
    );
    assert_early_iteration_rejected(
        "",
        &["entries[Symbol.iterator] = function*() { observed = later; yield entries[0]; };"],
        "[...entries]",
        "",
        &[],
    );
}

#[test]
fn borrowed_public_method_can_publish_a_native_receiver() {
    assert_early_iteration_rejected(
        "class Publisher { install() { globalThis.slot = this; } } const publisher = new Publisher();",
        &[
            "publisher.install.call(Array.prototype);",
            "globalThis.slot.forEach = function(callback) { observed = later; };",
        ],
        "entries.forEach(() => {})",
        "",
        &["Publisher", "publisher"],
    );
}

#[test]
fn fresh_global_rhs_does_not_prove_what_a_slot_read_returns() {
    assert_early_iteration_rejected(
        "",
        &[
            "Object.defineProperty(globalThis, 'slot', { configurable: true, get() { return Array.prototype; }, set(value) {} });",
            "globalThis.slot = {};",
            "globalThis.slot.forEach = function(callback) { observed = later; };",
        ],
        "entries.forEach(() => {})",
        "",
        &[],
    );
}

#[test]
fn array_species_mutation_through_source_and_result_aliases_stays_conservative() {
    assert_early_iteration_rejected(
        "",
        &[
            "entries.constructor = { [Symbol.species]: class { constructor() { observed = later; } } };",
        ],
        "entries.map(entry => entry.id)",
        "",
        &[],
    );
    assert_early_iteration_rejected(
        "const C = entries.constructor;",
        &[
            "Object.defineProperty(C, Symbol.species, { get() { observed = later; return Array; } });",
        ],
        "entries.map(entry => entry)",
        "",
        &["C"],
    );
    assert_early_iteration_rejected(
        "const filtered = entries.filter(entry => entry.id); const C = filtered.constructor;",
        &[
            "Object.defineProperty(C, Symbol.species, { get() { observed = later; return Array; } });",
        ],
        "filtered.map(entry => entry)",
        "",
        &["filtered", "C"],
    );
}

#[test]
fn mapped_results_retain_callbacks_for_invocation_and_publication() {
    assert_early_iteration_rejected(
        "",
        &[],
        "entries.map(() => ({ get value() { return later; } }))",
        "observed = result[0].value;",
        &[],
    );
    assert_early_iteration_rejected(
        "",
        &[],
        "entries.map(() => () => later)",
        "observed = result[0]();",
        &[],
    );
    assert_early_iteration_rejected(
        "function consume(array) { observed = array[0](); }",
        &[],
        "entries.map(() => () => later)",
        "consume(result);",
        &["consume"],
    );
    assert_early_iteration_rejected(
        "",
        &[],
        "entries.map(() => () => later)",
        "observed = consumeCallbacks(result);",
        &[],
    );
}

#[test]
fn empty_builtin_iteration_never_invokes_its_callback() {
    let source = "let later = 0;\nconst entries = [];\nconst result = entries.map(() => later);\nlater = 42;\nconsole.log(result.length);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("registry", &[Member::new("entries"), Member::new("result")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0\n");
    assert_selected_iteration_receipt(&fixture, "result", "map");
}

#[test]
fn object_spread_getters_run_during_map_even_without_a_result_consumer() {
    for declarations in [
        "const payload = { get value() { observed = later; return 'ready'; } }; const entries = [{ payload }];",
        "const payload = { get value() { observed = later; return 'ready'; } }; const entries = [{ payload: { value: 'safe' } }, { payload }];",
    ] {
        let source = format!(
            "let later = 0;\nlet observed = -1;\n{declarations}\nfunction copy(value) {{ return {{ ...value }}; }}\nconst result = entries.map(entry => copy(entry.payload));\nlater = 42;\nconsole.log(observed);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
        assert_preserved_entry_output(&fixture, "0\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "registry",
                        &[
                            Member::new("observed"),
                            Member::new("payload"),
                            Member::new("entries"),
                            Member::new("copy"),
                            Member::new("result"),
                        ],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            ),
            &["registry", "values/later"],
            &[],
        );
    }
}

#[test]
fn ordinary_spread_copy_fields_retain_referenced_callback_identity() {
    let source = "let later = 0;\nconst entries = [{ run: () => later }];\nconst result = entries.map(entry => ({ ...entry }));\nconst observed = result[0].run();\nlater = 42;\nconsole.log(observed);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "0\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "registry",
                    &[
                        Member::new("entries"),
                        Member::new("result"),
                        Member::new("observed"),
                    ],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["registry", "values/later"],
        &[],
    );
}

#[test]
fn iterating_function_values_stores_them_without_invoking_them() {
    let source = "let later = 0;\nconst entries = [() => later];\nconst result = entries.map(callback => callback);\nlater = 42;\nconsole.log(result[0]());\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("registry", &[Member::new("entries"), Member::new("result")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn prior_unknown_call_does_not_make_fresh_primitive_cache_writes_run_unrelated_getters() {
    let source = "import { prepare } from './peer.js';\nlet later = 0;\nconst ids = { tool: 'tool-id' };\nconst entries = [{ id: ids.tool, name: 'tool', run: () => later }];\nconst lookup = {};\nconst latent = { get value() { return later; } };\nprepare();\nentries.forEach(entry => { lookup[entry.id] = entry.name; });\nlater = 42;\nconsole.log(lookup['tool-id'], entries[0].run());\n";
    let peers = [("static/peer", "export function prepare() { return 0; }\n")];
    let fixture = run_fixture(FixtureOpts::new(source, vec![]).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, "tool 42\n");
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "registry",
                    &[
                        Member::new("ids"),
                        Member::new("entries"),
                        Member::new("lookup"),
                        Member::new("latent"),
                    ],
                    &[
                        "prepare();",
                        "entries.forEach(entry => { lookup[entry.id] = entry.name; });",
                    ],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "tool 42\n");
}

#[test]
fn cache_write_setters_and_key_coercions_preserve_real_effects() {
    assert_early_iteration_rejected(
        "const lookup = { set a(value) { observed = later; } };",
        &[],
        "entries.forEach(entry => { lookup[entry.id] = entry.id; })",
        "",
        &["lookup"],
    );
    assert_early_iteration_rejected(
        "const lookup = {};",
        &[
            "Object.defineProperty(Object.prototype, 'a', { set(value) { observed = later; }, configurable: true });",
        ],
        "entries.forEach(entry => { lookup[entry.id] = entry.id; })",
        "",
        &["lookup"],
    );
    assert_early_iteration_rejected(
        "const lookup = {};",
        &["entries[0].id = { toString() { observed = later; return 'a'; } };"],
        "entries.forEach(entry => { lookup[entry.id] = entry.id; })",
        "",
        &["lookup"],
    );
}

#[test]
fn implicit_protocols_cannot_install_unseen_cache_setters() {
    let definitions = "const lookup = {}; function install() { Object.defineProperty(lookup, 'a', { set(value) { observed = later; }, configurable: true }); }";
    for (extra, setup, names) in [
        (
            "const key = { toString() { install(); return 'a'; } }; const field = `${key}`;",
            "",
            vec!["key", "field"],
        ),
        (
            "class Matcher { static [Symbol.hasInstance]() { install(); return true; } }",
            "({}) instanceof Matcher;",
            vec!["Matcher"],
        ),
        (
            "const holder = new Proxy({}, { has() { install(); return true; } });",
            "'field' in holder;",
            vec!["holder"],
        ),
        (
            "const holder = { get field() { install(); return 0; } }; const { field } = holder;",
            "",
            vec!["holder", "field"],
        ),
        (
            "const holder = { *[Symbol.iterator]() { install(); yield 0; } }; const [field] = holder;",
            "",
            vec!["holder", "field"],
        ),
    ] {
        let declarations = format!("{definitions} {extra}");
        let mut names = names;
        names.extend(["lookup", "install"]);
        let setup = if setup.is_empty() {
            vec![]
        } else {
            vec![setup]
        };
        assert_early_iteration_rejected(
            &declarations,
            &setup,
            "entries.forEach(entry => { lookup[entry.id] = entry.id; })",
            "",
            &names,
        );
    }
    assert_early_iteration_rejected(
        "const lookup = {}; const key = { toString() { Object.defineProperty(lookup, 'a', { set(value) { observed = later; }, configurable: true }); return 'a'; } };",
        &[],
        "entries.forEach(entry => { lookup[`${key}`] = entry.id; })",
        "",
        &["lookup", "key"],
    );
}

#[test]
fn nested_destructuring_getters_cannot_install_unseen_cache_setters() {
    assert_early_iteration_rejected(
        "const lookup = {}; const holder = { nested: { get value() { Object.defineProperty(lookup, 'a', { set(value) { observed = later; }, configurable: true }); return 0; } } }; const { nested: { value } } = holder;",
        &[],
        "entries.forEach(entry => { lookup[entry.id] = entry.id; })",
        "",
        &["lookup", "holder", "value"],
    );
}

#[test]
fn mapped_fallthrough_values_preserve_undefined_default_consumers() {
    for mapper in [
        "() => {}",
        "() => { return; }",
        "() => { if (false) return 1; }",
        "() => empty()",
    ] {
        assert_early_iteration_rejected(
            "function empty() {} function consume(value = () => later) { return value(); }",
            &[],
            &format!("entries.map({mapper})"),
            "observed = consume(result[0]);",
            &["empty", "consume"],
        );
    }
}

#[test]
fn default_instanceof_does_not_execute_unused_constructor_methods() {
    for value in ["({})", "getValue()"] {
        let source = format!(
            "import {{ getValue }} from './peer.js';\nlet later = 0;\nclass Matcher {{ unused() {{ return later; }} }}\nconst matches = {value} instanceof Matcher;\nlater = 42;\nconsole.log(matches);\n"
        );
        let peers = [("static/peer", "export function getValue() { return {}; }\n")];
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "registry",
                        &[Member::new("Matcher"), Member::new("matches")],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&peers),
        );
        assert_preserved_entry_output(&fixture, "false\n");
    }
}

#[test]
fn instanceof_proxy_and_custom_has_instance_effects_remain_visible() {
    assert_early_iteration_rejected(
        "class Matcher {} const value = new Proxy({}, { getPrototypeOf() { observed = later; return null; } });",
        &[],
        "value instanceof Matcher",
        "",
        &["Matcher", "value"],
    );
    assert_early_iteration_rejected(
        "class Matcher { static [Symbol.hasInstance]() { observed = later; return false; } }",
        &[],
        "({}) instanceof Matcher",
        "",
        &["Matcher"],
    );
    assert_early_iteration_rejected(
        "class Matcher {}",
        &[
            "Object.defineProperty(Matcher, Symbol.hasInstance, { value() { observed = later; return false; } });",
        ],
        "({}) instanceof Matcher",
        "",
        &["Matcher"],
    );
}

#[test]
fn copy_contexts_retain_missing_fields_for_default_consumers() {
    for (entries, index) in [("[{ run: () => 0 }, {}]", 1), ("[{}, { run: () => 0 }]", 0)] {
        let source = format!(
            "let later = 0;\nconst entries = {entries};\nconst copies = entries.map(entry => ({{ ...entry }}));\nfunction consume(value = () => later) {{ return value(); }}\nconst observed = consume(copies[{index}].run);\nlater = 42;\nconsole.log(observed);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
        assert_preserved_entry_output(&fixture, "0\n");
        let split = vec![
            logical_module(
                "registry",
                &[
                    Member::new("entries"),
                    Member::new("copies"),
                    Member::new("consume"),
                    Member::new("observed"),
                ],
            ),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ];
        expect_atomic_conflict_rejection(
            FixtureOpts::new(&source, split),
            &["registry", "values/later"],
            &[],
        );
    }
}

#[test]
fn getter_triggered_async_work_keeps_future_reads_after_await() {
    let source = "let later = 0;\nclass Reader { static async read() { await 0; console.log(later); } }\nconst holder = { get value() { Reader.read(); return 0; } };\nconst observed = holder.value;\nlater = 42;\nconsole.log(observed);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "0\n42\n");
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module(
                "registry",
                &[
                    Member::new("Reader"),
                    Member::new("holder"),
                    Member::new("observed"),
                ],
            ),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0\n42\n");
}

#[test]
fn installed_getters_can_invoke_returned_escaped_callbacks() {
    let source = "import { retain } from './peer.js';\nlet later = 0;\nfunction createReader() { return () => later; }\nconst holder = { value: 1 };\nretain(createReader);\nretain(holder);\nconst observed = holder.missing;\nlater = 42;\nconsole.log(observed);\n";
    let peers = [(
        "static/peer",
        "let factory; export function retain(value) { if (typeof value === 'function') factory = value; else Object.defineProperty(value, 'missing', { get() { return factory()(); } }); }\n",
    )];
    let fixture = run_fixture(FixtureOpts::new(source, vec![]).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, "0\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "registry",
                    &[
                        Member::new("createReader"),
                        Member::new("holder"),
                        Member::new("observed"),
                    ],
                    &["retain(createReader);", "retain(holder);"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers),
        &["registry", "values/later"],
        &[],
    );
}
