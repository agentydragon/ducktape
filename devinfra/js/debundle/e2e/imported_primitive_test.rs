//! Stable imported primitives cannot execute ToPrimitive callbacks, and a
//! coercion of operands whose types cannot throw is pure.
use analysis::reports::schema::{BindingEffectAuthorityReport, LexicalBindingEffectBody};
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

fn source(import: &str, expression: &str) -> String {
    format!(
        "{import}\nlet later = 0;\nconst callback = () => later;\n\
         globalThis.saved = callback;\nconst computed = {expression};\n\
         later = 42;\nconsole.log(computed, callback());\n"
    )
}

fn split_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module_with_anon(
            "compute",
            &[Member::new("callback"), Member::new("computed")],
            &["globalThis.saved = callback;"],
        ),
        logical_module_with_anon("later", &[Member::new("later")], &["later = 42;"]),
    ]
}

fn coercion_split_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("compute", &[Member::new("computed")]),
        logical_module_with_anon(
            "later",
            &[Member::new("later"), Member::new("callback")],
            &["globalThis.saved = callback;", "later = 42;"],
        ),
    ]
}

fn primitive_source(import: &str, expression: &str) -> String {
    format!(
        "{import}\nconst before = 0;\nconst callback = () => before;\n\
         const computed = {expression};\nconst after = 1;\n\
         console.log(before, computed, after, callback());\n"
    )
}

fn primitive_modules(members: &[Member]) -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("compute", members),
        logical_module("anchors", &[Member::new("before"), Member::new("after")]),
    ]
}

fn assert_primitive(peer: &str, import: &str, expression: &str, expected: &str) -> Fixture {
    let source = primitive_source(import, expression);
    let peers = [("static/peer", peer)];
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            primitive_modules(&[Member::new("callback"), Member::new("computed")]),
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, expected);
    let graph = fixture.dependency_graph();
    let computed = source_unit_for_binding(&graph, "computed");
    // External import targets have no node in the single-chunk graph. The emit
    // must retain the import and binary initializer rather than fold its value.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/compute.js",
        &["import", "bytes"],
        &[],
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/compute.js",
        &[
            (
                VariableDeclarationKind::Const,
                &["callback"],
                VariableInitializerKind::Arrow,
            ),
            (
                VariableDeclarationKind::Const,
                &["computed"],
                VariableInitializerKind::Binary,
            ),
        ],
    );
    assert_actual_imported_inversion(&fixture, "computed");
    let node = graph.nodes.iter().find(|node| node.id == computed).unwrap();
    assert!(
        node.purity.is_pure(),
        "a coercion of non-throwing imported primitives is pure: {:?}",
        node.purity
    );
    assert_root_read_receipt(&fixture, "computed");
    fixture
}

fn assert_actual_imported_inversion(fixture: &Fixture, binding: &str) {
    let assessment = fixture.candidate_relocation_assessment();
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    let graph = fixture.dependency_graph();
    let position = |name| {
        let unit = source_unit_for_binding(&graph, name);
        let ordinal = graph
            .nodes
            .iter()
            .find(|node| node.id == unit)
            .unwrap()
            .statement_ordinal
            .0;
        assessment["emitted_order"]
            .as_array()
            .unwrap()
            .iter()
            .position(|row| row.as_u64() == Some(ordinal as u64))
            .unwrap()
    };
    assert!(
        position(binding) < position("before") || position("after") < position(binding),
        "the imported initializer must cross an executable anchor: {assessment:#}"
    );
}

fn assert_root_read_receipt(fixture: &Fixture, binding: &str) {
    let graph = fixture.dependency_graph();
    let node = graph
        .nodes
        .iter()
        .find(|node| node.id == source_unit_for_binding(&graph, binding))
        .unwrap();
    let execution = node
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("missing execution report for {binding}: {node:#?}"));
    assert!(execution.complete, "{execution:#?}");
    let Some(analysis::reports::schema::BindingEffectAuthorityReport::OperationReceipt { receipt }) =
        execution.binding_effect_authority.as_ref()
    else {
        panic!("binding read lacks an operation receipt: {execution:#?}");
    };
    assert!(
        matches!(
            &receipt.root,
            analysis::reports::schema::LexicalEffectRoot::BindingInitializer {
                binding: receipt_binding,
                initializer_span,
            } if receipt_binding == binding && initializer_span.lo < initializer_span.hi
        ),
        "receipt must identify the exact binding initializer: {receipt:#?}"
    );
    assert_eq!(receipt.owner, node.statement_ordinal, "{receipt:#?}");
}

fn assert_called_operation_receipt(fixture: &Fixture, binding: &str, callee: &str) {
    let graph = fixture.dependency_graph();
    let owner = graph
        .nodes
        .iter()
        .find(|node| node.id == source_unit_for_binding(&graph, binding))
        .unwrap();
    let execution = owner.synchronous_execution.as_ref().unwrap();
    assert!(execution.complete, "{execution:#?}");
    let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) =
        execution.binding_effect_authority.as_ref()
    else {
        panic!("{binding} lacks an operation receipt: {execution:#?}");
    };
    assert_eq!(receipt.binding, binding, "{receipt:#?}");
    assert_eq!(receipt.owner, owner.statement_ordinal, "{receipt:#?}");
    assert_eq!(receipt.calls.len(), 1, "{receipt:#?}");
    let call = &receipt.calls[0];
    assert_eq!(
        receipt.initializer_span,
        Some(call.call_span),
        "{receipt:#?}"
    );
    let target = graph
        .nodes
        .iter()
        .find(|node| node.id == source_unit_for_binding(&graph, callee))
        .expect("called function source unit");
    assert!(
        matches!(
            &call.target,
            LexicalBindingEffectBody::Callable { owner, .. } if *owner == target.statement_ordinal
        ),
        "{receipt:#?}"
    );
}

#[test]
fn initialized_imported_read_has_a_receipt_for_an_actual_inversion() {
    for (initializer, expected) in [
        ("read()", "0 2 1\n"),
        ("4 * bytes + 256", "0 264 1\n"),
        ("`${bytes}`", "0 2 1\n"),
    ] {
        let source = format!(
            "import {{ limit as bytes }} from './peer.js';\nconst before = 0;\nfunction read() {{ return bytes; }}\nconst computed = {initializer};\nconst after = 1;\nconsole.log(before, computed, after);\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("compute", &[Member::new("read"), Member::new("computed")]),
                    logical_module("anchors", &[Member::new("before"), Member::new("after")]),
                ],
            )
            .with_extra_chunks(&[("static/peer", "export const limit = 2;\n")]),
        );
        assert_preserved_entry_output(&fixture, expected);
        assert_actual_imported_inversion(&fixture, "computed");
        assert_root_read_receipt(&fixture, "computed");
    }
}

#[test]
fn called_primitive_binary_return_has_a_receipt_for_an_actual_inversion() {
    let source = "import { limit as bytes } from './peer.js';\n\
                  const before = 0;\n\
                  function add() { return bytes + 256; }\n\
                  const computed = add();\n\
                  const after = 1;\n\
                  console.log(before, computed, after);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("compute", &[Member::new("add"), Member::new("computed")]),
                logical_module("anchors", &[Member::new("before"), Member::new("after")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", "export const limit = 2;\n")]),
    );
    assert_preserved_entry_output(&fixture, "0 258 1\n");
    let assessment = fixture.effect_translation_validation();
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "the called primitive operation must participate in an actual inversion: {assessment:#}"
    );
    let graph = fixture.dependency_graph();
    let position = |binding| {
        let ordinal = graph
            .nodes
            .iter()
            .find(|node| node.id == source_unit_for_binding(&graph, binding))
            .unwrap()
            .statement_ordinal
            .0;
        assessment["emitted_order"]
            .as_array()
            .unwrap()
            .iter()
            .position(|row| row.as_u64() == Some(ordinal as u64))
            .unwrap()
    };
    assert!(
        position("computed") < position("before") || position("after") < position("computed"),
        "the call result must cross an executable anchor: {assessment:#}"
    );
    assert_called_operation_receipt(&fixture, "computed", "add");
}

#[test]
fn called_primitive_template_return_has_a_receipt_for_an_actual_inversion() {
    let source = "import { limit as bytes } from './peer.js';\n\
                  const before = 0;\n\
                  function render() { return `bytes:${bytes}`; }\n\
                  const computed = render();\n\
                  const after = 1;\n\
                  console.log(before, computed, after);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("compute", &[Member::new("render"), Member::new("computed")]),
                logical_module("anchors", &[Member::new("before"), Member::new("after")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", "export const limit = 2;\n")]),
    );
    assert_preserved_entry_output(&fixture, "0 bytes:2 1\n");
    let assessment = fixture.effect_translation_validation();
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "the template return must cross an executable anchor: {assessment:#}"
    );
    let graph = fixture.dependency_graph();
    let position = |binding| {
        let ordinal = graph
            .nodes
            .iter()
            .find(|node| node.id == source_unit_for_binding(&graph, binding))
            .unwrap()
            .statement_ordinal
            .0;
        assessment["emitted_order"]
            .as_array()
            .unwrap()
            .iter()
            .position(|row| row.as_u64() == Some(ordinal as u64))
            .unwrap()
    };
    assert!(
        position("computed") < position("before") || position("after") < position("computed"),
        "the call result must cross an executable anchor: {assessment:#}"
    );
    assert_called_operation_receipt(&fixture, "computed", "render");
}

fn assert_uncovered_root_read(fixture: &Fixture, binding: &str) {
    let graph = fixture.dependency_graph();
    let node = graph
        .nodes
        .iter()
        .find(|node| node.id == source_unit_for_binding(&graph, binding))
        .unwrap();
    let execution = node
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("missing execution report for {binding}: {node:#?}"));
    assert!(
        matches!(
            execution.binding_effect_authority.as_ref(),
            Some(analysis::reports::schema::BindingEffectAuthorityReport::OrderedRootInitializerUncovered {
                binding: actual,
                ..
            }) if actual == binding
        ),
        "{binding} must retain the selected root's uncovered authority: {execution:#?}"
    );
}

#[test]
fn object_binary_coercion_fallback_keeps_right_operand_before_value_of() {
    let source = "let trace = '';\n\
                  const value = { valueOf() { trace = trace + 'coerce'; return 2; } };\n\
                  function right() { trace = trace + 'right'; return 1; }\n\
                  function add() { return value + right(); }\n\
                  const computed = add();\n\
                  console.log(trace, computed);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "all",
            &[
                Member::new("trace"),
                Member::new("value"),
                Member::new("right"),
                Member::new("add"),
                Member::new("computed"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "rightcoerce 3\n");
    assert_uncovered_root_read(&fixture, "computed");
}

#[test]
fn object_template_coercion_uses_string_hint_before_next_substitution() {
    let source = "let trace = '';\n\
                  const value = { [Symbol.toPrimitive](hint) { trace += hint; return 2; } };\n\
                  function next() { trace += 'next'; return 3; }\n\
                  function render() { return `${value}:${next()}`; }\n\
                  const computed = render();\n\
                  console.log(trace, computed);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "all",
            &[
                Member::new("trace"),
                Member::new("value"),
                Member::new("next"),
                Member::new("render"),
                Member::new("computed"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "stringnext 2:3\n");
    assert_uncovered_root_read(&fixture, "computed");
}

#[test]
fn symbol_template_coercion_stops_before_the_next_substitution() {
    let source = "import { token } from './peer.js';\n\
                  function next() { throw new Error('next ran'); }\n\
                  function render() { return `${token}${next()}`; }\n\
                  const computed = render();\n\
                  console.log(computed);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "all",
                &[
                    Member::new("next"),
                    Member::new("render"),
                    Member::new("computed"),
                ],
            )],
        )
        .with_extra_chunks(&[("static/peer", "export const token = Symbol.for('token');\n")]),
    );
    assert_preserved_entry_error(&fixture, "TypeError");
    assert_uncovered_root_read(&fixture, "computed");
}

#[test]
fn possible_bigint_division_throw_does_not_seal_callable_operation_coverage() {
    let source = "import { divisor } from './peer.js';\n\
                  function divide() { return 4n / divisor; }\n\
                  const computed = divide();\n\
                  console.log(computed);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "all",
                &[Member::new("divide"), Member::new("computed")],
            )],
        )
        .with_extra_chunks(&[("static/peer", "export const divisor = 2n;\n")]),
    );
    assert_preserved_entry_output(&fixture, "2n\n");
    // The imported value's BigInt kind does not prove it is nonzero. The
    // operation may complete here but must retain possible-abrupt coverage.
    assert_uncovered_root_read(&fixture, "computed");
}

#[test]
fn symbol_coercion_throw_happens_after_the_right_operand() {
    let source = "import { token } from './peer.js';\n\
                  let calls = 0;\n\
                  function right() { calls = calls + 1; return 1; }\n\
                  function add() { return token + right(); }\n\
                  try { add(); } catch (error) { console.log(calls, error.name); }\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&[("static/peer", "export const token = Symbol.for('token');\n")]),
    );
    assert_preserved_entry_output(&fixture, "1 TypeError\n");
}

#[test]
fn direct_literal_const_import_does_not_promote_unrelated_callbacks() {
    assert_primitive(
        "export const limit = 2;\n",
        "import { limit as bytes } from './peer.js';",
        "4 * bytes + 256",
        "0 264 1 0\n",
    );
}

#[test]
fn template_substitutions_compose_parameter_stores_in_source_order() {
    let source = "import { limit as bytes } from './peer.js';\n\
                  const before = 0;\n\
                  function render(value) { return `${value = 1}:${value = 2}:${value}`; }\n\
                  const computed = render(bytes);\nconst after = 1;\n\
                  console.log(before, computed, after);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            primitive_modules(&[Member::new("render"), Member::new("computed")]),
        )
        .with_extra_chunks(&[("static/peer", "export const limit = 2;\n")]),
    );
    assert_preserved_entry_output(&fixture, "0 1:2:2 1\n");
    assert_actual_imported_inversion(&fixture, "computed");
    assert_root_read_receipt(&fixture, "computed");
}

#[test]
fn template_object_coercion_keeps_its_captured_tdz() {
    let source = "const before = 0;\n\
                  const item = { [Symbol.toPrimitive](hint) { console.log(hint); return after; } };\n\
                  const computed = `${item}`;\nconst after = 1;\n\
                  console.log(before, computed, after);\n";
    let rejected = run_rejection_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("compute", &[Member::new("computed")]),
            logical_module(
                "anchors",
                &[
                    Member::new("before"),
                    Member::new("item"),
                    Member::new("after"),
                ],
            ),
        ],
    ));
    assert_ne!(
        rejected.candidate_relocation_assessment()["decision"]["status"],
        "accepted"
    );
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "together",
            &[
                Member::new("before"),
                Member::new("item"),
                Member::new("computed"),
                Member::new("after"),
            ],
        )],
    ));
    assert_preserved_entry_error(&fixture, "ReferenceError");
}

#[test]
fn published_callback_still_blocks_later_write_across_modules() {
    let source = source(
        "import { limit as bytes } from './peer.js';",
        "4 * bytes + 256",
    );
    let peers = [("static/peer", "export const limit = 2;\n")];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, split_modules()).with_extra_chunks(&peers),
        &["compute", "later"],
        &["eager_use"],
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "together",
                &[
                    Member::new("callback"),
                    Member::new("computed"),
                    Member::new("later"),
                ],
                &["globalThis.saved = callback;", "later = 42;"],
            )],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output_with_host_setup(
        &fixture,
        "Object.defineProperty(globalThis, 'saved', { set(callback) { console.log('at-save', callback()); } });",
        "at-save 0\n264 42\n",
        "published-primitive-callback",
    );
}

#[test]
fn object_coercion_cannot_cross_callback_publication() {
    let source = source(
        "import { limit as bytes } from './peer.js';",
        "4 * bytes + 256",
    );
    let peers = [(
        "static/peer",
        "export const limit = { valueOf() { return globalThis.saved(); } };\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, coercion_split_modules()).with_extra_chunks(&peers),
        &["compute", "later"],
        &[],
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "together",
                &[
                    Member::new("callback"),
                    Member::new("computed"),
                    Member::new("later"),
                ],
                &["globalThis.saved = callback;", "later = 42;"],
            )],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "256 42\n");
}

#[test]
fn local_copies_preserve_imported_primitive_callback_timing() {
    let source = "import { limit as bytes } from './peer.js';\n\
                  const before = 0;\nconst callback = () => before;\n\
                  var first = bytes, second = first;\n\
                  const computed = 4 * second + 256;\n\
                  const after = 1;\nconsole.log(before, computed, after, callback());\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            primitive_modules(&[
                Member::new("callback"),
                Member::new("first"),
                Member::new("second"),
                Member::new("computed"),
            ]),
        )
        .with_extra_chunks(&[("static/peer", "export var limit = 2;\n")]),
    );
    assert_preserved_entry_output(&fixture, "0 264 1 0\n");
    assert_actual_imported_inversion(&fixture, "first");
    assert_root_read_receipt(&fixture, "first");
    assert_root_read_receipt(&fixture, "second");
    assert_actual_imported_inversion(&fixture, "computed");
    assert_root_read_receipt(&fixture, "computed");
    let graph = fixture.dependency_graph();
    let computed = source_unit_for_binding(&graph, "computed");
    let node = graph.nodes.iter().find(|node| node.id == computed).unwrap();
    assert!(node.synchronous_execution.as_ref().unwrap().complete);
    assert!(
        node.purity.is_pure(),
        "local copies keep the imported Number type: {:?}",
        node.purity
    );
}

#[test]
fn split_binding_copies_have_receipts_for_an_actual_inversion() {
    let source = "import { limit as bytes } from './peer.js';\n\
                  const before = 0;\n\
                  var first = bytes, second = first;\n\
                  const after = 1;\n\
                  console.log(before, first, second, after);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("compute", &[Member::new("first"), Member::new("second")]),
                logical_module("anchors", &[Member::new("before"), Member::new("after")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", "export const limit = 2;\n")]),
    );
    assert_preserved_entry_output(&fixture, "0 2 2 1\n");
    let assessment = fixture.effect_translation_validation();
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "the binding copies must participate in an actual inversion: {assessment:#}"
    );
    let graph = fixture.dependency_graph();
    let position = |binding| {
        let unit = source_unit_for_binding(&graph, binding);
        let ordinal = graph
            .nodes
            .iter()
            .find(|node| node.id == unit)
            .unwrap()
            .statement_ordinal
            .0;
        assessment["emitted_order"]
            .as_array()
            .unwrap()
            .iter()
            .position(|row| row.as_u64() == Some(ordinal as u64))
            .unwrap()
    };
    assert!(
        position("first") < position("before") || position("after") < position("first"),
        "the binding copy must cross an executable anchor: {assessment:#}"
    );
    assert_root_read_receipt(&fixture, "first");
    assert_root_read_receipt(&fixture, "second");
}

#[test]
fn split_binding_copies_keep_mutable_import_reads_uncovered() {
    let source = "import { limit as bytes } from './peer.js';\n\
                  let later = 0;\nconst callback = () => later;\n\
                  globalThis.saved = callback;\n\
                  var first = bytes, second = first;\n\
                  const computed = 4 * second + 256;\n\
                  later = 42;\nconsole.log(computed, callback());\n";
    let peers = [("static/peer", "export let limit = 2;\n")];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "compute",
                    &[
                        Member::new("callback"),
                        Member::new("first"),
                        Member::new("second"),
                        Member::new("computed"),
                    ],
                    &["globalThis.saved = callback;"],
                ),
                logical_module_with_anon("later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers),
        &["compute", "later"],
        &["eager_use"],
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &[
                    Member::new("callback"),
                    Member::new("first"),
                    Member::new("second"),
                    Member::new("computed"),
                    Member::new("later"),
                ],
                &["globalThis.saved = callback;", "later = 42;"],
            )],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "264 42\n");
    assert_uncovered_root_read(&fixture, "first");
    assert_root_read_receipt(&fixture, "second");
}

#[test]
fn split_binding_copy_from_a_tdz_cell_keeps_the_root_uncovered() {
    let source = "import { limit as bytes } from './peer.js';\n\
                  let first = second, second = bytes;\n\
                  console.log(first, second);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "all",
                &[Member::new("first"), Member::new("second")],
            )],
        )
        .with_extra_chunks(&[("static/peer", "export const limit = 2;\n")]),
    );
    assert_preserved_entry_error(&fixture, "ReferenceError");
    assert_uncovered_root_read(&fixture, "first");
}

#[test]
fn mutable_import_copy_cannot_acquire_an_ordered_read_receipt() {
    let source = "import { limit as bytes } from './peer.js';\n\
                  const before = 0;\nvar first = bytes, second = first;\n\
                  const after = 1;\nconsole.log(before, second, after);\n";
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            source,
            primitive_modules(&[Member::new("first"), Member::new("second")]),
        )
        .with_extra_chunks(&[("static/peer", "export let limit = 2;\n")]),
    );
    assert_eq!(
        rejected.candidate_relocation_assessment()["decision"]["status"],
        "incomplete"
    );
    let graph = rejected.dependency_graph();
    let first = source_unit_for_binding(&graph, "first");
    let execution = graph
        .nodes
        .iter()
        .find(|node| node.id == first)
        .unwrap()
        .synchronous_execution
        .as_ref()
        .unwrap();
    assert!(
        !matches!(
            execution.binding_effect_authority.as_ref(),
            Some(analysis::reports::schema::BindingEffectAuthorityReport::OperationReceipt { .. })
        ),
        "a mutable import has no initialized primitive read proof: {execution:#?}"
    );
}

#[test]
fn replacing_a_primitive_copy_with_an_object_preserves_coercion_callbacks() {
    let source = "import { limit as bytes } from './peer.js';\n\
                  let later = 0;\nconst callback = () => later;\n\
                  globalThis.saved = callback;\nlet copied = bytes;\n\
                  copied = { valueOf() { return globalThis.saved(); } };\n\
                  const computed = 4 * copied + 256;\n\
                  later = 42;\nconsole.log(computed, callback());\n";
    let statements = [
        "globalThis.saved = callback;",
        "copied = { valueOf() { return globalThis.saved(); } };",
    ];
    let members = [
        Member::new("callback"),
        Member::new("copied"),
        Member::new("computed"),
    ];
    let peers = [("static/peer", "export const limit = 2;\n")];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon("compute", &members, &statements),
                logical_module_with_anon("later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers),
        &["compute", "later"],
        &["eager_use"],
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "together",
                &members
                    .into_iter()
                    .chain([Member::new("later")])
                    .collect::<Vec<_>>(),
                &[statements.as_slice(), &["later = 42;"]].concat(),
            )],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "256 42\n");
}

#[test]
fn primitive_copies_keep_mixed_numeric_throws() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { limit } from './peer.js';\n\
             var copied = limit;\nconst computed = 4 * copied;\nconsole.log(computed);\n",
            vec![logical_module(
                "compute",
                &[Member::new("copied"), Member::new("computed")],
            )],
        )
        .with_extra_chunks(&[("static/peer", "export const limit = 2n;\n")]),
    );
    assert_preserved_entry_error(&fixture, "TypeError");
    let graph = fixture.dependency_graph();
    let computed = source_unit_for_binding(&graph, "computed");
    let node = graph.nodes.iter().find(|node| node.id == computed).unwrap();
    assert!(
        !node.purity.is_pure(),
        "BigInt times Number throws, so the coercion stays: {:?}",
        node.purity
    );
}

#[test]
fn throwing_primitive_kinds_keep_the_coercion() {
    // Each operation can throw for some value of the operands' types, so the
    // coercion stays. `Ok` rows complete with these values but could throw for
    // others (a BigInt result past the engine's size limit).
    for (literal, expression, outcome) in [
        ("2n", "4 * bytes + 256", Err("TypeError")),
        ("2n", "+bytes", Err("TypeError")),
        ("2n", "4n / (bytes - 2n)", Err("RangeError")),
        ("2n", "4n % (bytes - 2n)", Err("RangeError")),
        ("2n", "bytes ** -1n", Err("RangeError")),
        ("2n", "4n * bytes + 256n", Ok("264n\n")),
        ("2n", "1n << bytes", Ok("4n\n")),
        ("Symbol.for('limit')", "`${bytes}`", Err("TypeError")),
        (
            "Symbol.for('limit')",
            "`${bytes}${(() => { console.log('suffix'); return 1; })()}`",
            Err("TypeError"),
        ),
        ("Symbol.for('limit')", "'prefix:' + bytes", Err("TypeError")),
    ] {
        let source = format!(
            "import {{ limit as bytes }} from './peer.js';\n\
             let computed;\ntry {{ computed = {expression}; }} catch (error) {{ computed = error.name; }}\n\
             const settled = {expression};\nconsole.log(computed);\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "compute",
                    &[Member::new("computed"), Member::new("settled")],
                    &[&format!(
                        "try {{ computed = {expression}; }} catch (error) {{ computed = error.name; }}"
                    )],
                )],
            )
            .with_extra_chunks(&[("static/peer", &format!("export const limit = {literal};\n"))]),
        );
        match outcome {
            Err(error) => assert_preserved_entry_error(&fixture, error),
            Ok(stdout) => assert_preserved_entry_output(&fixture, stdout),
        }
        let graph = fixture.dependency_graph();
        let settled = source_unit_for_binding(&graph, "settled");
        let node = graph.nodes.iter().find(|node| node.id == settled).unwrap();
        assert!(
            !node.purity.is_pure(),
            "{expression} with a {literal} operand can throw: {:?}",
            node.purity
        );
    }
}

#[test]
fn imported_template_and_numeric_values_survive_copy_and_reexport_chains() {
    let source = "import { prefix, forwardedPrefix, limit } from './bridge.js';\n\
                  const before = 0;\nconst callback = () => before;\n\
                  const computed = `${prefix}${forwardedPrefix}${4 * limit + 256}`;\n\
                  const after = 1;\nconsole.log(before, computed, after, callback());\n";
    let peers = [
        (
            "static/bridge",
            "import { prefix as providerPrefix, limit } from './provider.js';\n\
             const copiedPrefix = providerPrefix;\n\
             const copiedLimit = limit;\n\
             export { copiedPrefix as prefix, copiedLimit as limit };\n\
             export { prefix as forwardedPrefix } from './provider.js';\n",
        ),
        (
            "static/provider",
            "if (false) import.meta.require('./deferred-host-module.js');\n\
             const family = { [Symbol.toPrimitive]() { console.log('coerce'); return 'notes'; } };\n\
             export const prefix = `tool_${family}_`;\n\
             const amount = 2 * 3;\n\
             export const limit = amount;\n",
        ),
    ];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            primitive_modules(&[Member::new("callback"), Member::new("computed")]),
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "coerce\n0 tool_notes_tool_notes_280 1 0\n");

    let graph = fixture.dependency_graph();
    let computed = source_unit_for_binding(&graph, "computed");
    assert_actual_imported_inversion(&fixture, "computed");
    assert_root_read_receipt(&fixture, "computed");
    // The provider's own template coerces an object; the importer's
    // substitutions are Strings and a Number expression.
    assert!(
        graph
            .nodes
            .iter()
            .find(|node| node.id == computed)
            .unwrap()
            .purity
            .is_pure(),
        "imported String and Number substitutions convert without user code"
    );
}

#[test]
fn immutable_var_and_direct_export_aliases_are_primitive() {
    for peer in [
        "export var limit = 2;\n",
        "var value = 2; export { value as limit };\n",
        "const value = 2; export { value as limit };\n",
    ] {
        assert_primitive(
            peer,
            "import { limit as bytes } from './peer.js';",
            "4 * bytes + 256",
            "0 264 1 0\n",
        );
    }
}

#[test]
fn direct_default_literals_and_bindings_are_primitive() {
    for peer in [
        "export default 2;\n",
        "const value = 2; export default value;\n",
        "var value = 2; export { value as default };\n",
    ] {
        assert_primitive(
            peer,
            "import bytes from './peer.js';",
            "4 * bytes + 256",
            "0 264 1 0\n",
        );
    }
}

#[test]
fn other_literal_primitives_skip_callbacks_without_folding_operations() {
    for (literal, expression, expected) in [
        ("'two'", "'prefix:' + bytes", "0 prefix:two 1 0\n"),
        ("true", "4 * bytes + 256", "0 260 1 0\n"),
        ("null", "4 * bytes + 256", "0 256 1 0\n"),
    ] {
        let peer = format!("export const limit = {literal};\n");
        assert_primitive(
            &peer,
            "import { limit as bytes } from './peer.js';",
            expression,
            expected,
        );
    }
}

fn assert_conservative(peer: &str) {
    let source = source(
        "import { limit as bytes } from './peer.js';",
        "4 * bytes + 256",
    );
    let peers = [
        ("static/peer", peer),
        ("static/base", "export const value = 2;\n"),
    ];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, split_modules()).with_extra_chunks(&peers),
        &["compute", "later"],
        &["eager_use"],
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "together",
                &[
                    Member::new("callback"),
                    Member::new("computed"),
                    Member::new("later"),
                ],
                &["globalThis.saved = callback;", "later = 42;"],
            )],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "264 42\n");
}

#[test]
fn mutable_opaque_and_indirect_exports_stay_conservative() {
    for peer in [
        "export let limit = 2;\n",
        "export var limit = 2; limit = { valueOf() { return 2; } };\n",
        "export var limit = 2; export function change() { limit = { valueOf() { return 2; } }; }\n",
        "export var limit = 2; [limit] = [2];\n",
        "export var limit = 2; for (limit of [2]) {}\n",
        "export var limit = 2; var limit;\n",
        "export const limit = { valueOf() { return 2; } };\n",
        "export const { limit } = { limit: 2 };\n",
        "export const limit = 2; void typeof module;\n",
    ] {
        assert_conservative(peer);
    }
}

fn assert_imported_chain_conservative(bridge: &str, provider: &str) {
    let app_source = source(
        "import { limit as bytes } from './bridge.js';",
        "4 * bytes + 256",
    );
    let peers = [("static/bridge", bridge), ("static/provider", provider)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&app_source, split_modules()).with_extra_chunks(&peers),
        &["compute", "later"],
        &["eager_use"],
    );
}

#[test]
fn conditional_and_logical_results_require_primitive_value_alternatives() {
    let app_source = primitive_source(
        "import { conditional, logical } from './bridge.js';",
        "4 * conditional + logical",
    );
    let peers = [
        (
            "static/bridge",
            "import { limit } from './provider.js';\n\
             const conditional = globalThis.flag ? limit : 3;\n\
             const logical = limit && 2;\n\
             export { conditional, logical };\n",
        ),
        ("static/provider", "export const limit = 6;\n"),
    ];
    let fixture = run_fixture(
        FixtureOpts::new(
            &app_source,
            primitive_modules(&[Member::new("callback"), Member::new("computed")]),
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "0 14 1 0\n");
    assert_actual_imported_inversion(&fixture, "computed");
    assert_root_read_receipt(&fixture, "computed");
}

#[test]
fn object_branches_rebinding_accessors_and_unseeded_import_cycles_stay_unknown() {
    let provider = "export const limit = 2;\n";
    assert_imported_chain_conservative(
        "import { limit } from './provider.js';\n\
         export const copied = globalThis.flag ? limit : { valueOf() { return 2; } };\n\
         export { copied as limit };\n",
        provider,
    );
    assert_imported_chain_conservative(
        "import { limit } from './provider.js';\n\
         export const copied = globalThis.flag && { valueOf() { return 2; } };\n\
         export { copied as limit };\n",
        provider,
    );
    assert_imported_chain_conservative(
        "import { limit } from './provider.js';\n\
         var copied = limit;\n\
         copied = { valueOf() { return 2; } };\n\
         export { copied as limit };\n",
        provider,
    );
    assert_imported_chain_conservative(
        "import { limit as sourceLimit } from './provider.js';\n\
         var limit = sourceLimit;\n\
         void typeof module;\n\
         Object.defineProperty(exports, 'limit', { get() { return { valueOf() { return 2; } }; } });\n\
         export { limit };\n",
        provider,
    );
    assert_imported_chain_conservative(
        "import { value as back } from './provider.js';\n\
         const result = back;\n\
         export { result as limit };\n",
        "import { limit } from './bridge.js';\n\
         export const value = limit;\n",
    );
}

#[test]
fn stable_primitive_values_do_not_cover_cyclic_or_unresolved_provider_reads() {
    assert_imported_chain_conservative(
        "export { limit } from './provider.js';\n",
        "import './app.js';\nexport const limit = 2;\n",
    );
    assert_imported_chain_conservative(
        "export { limit } from './provider.js';\n",
        "import './missing.js';\nexport const limit = 2;\n",
    );
}

#[test]
fn direct_eval_in_lazy_provider_function_disables_inference() {
    assert_conservative("export var limit = 2; export function change() { eval('limit = {}'); }\n");
}

#[test]
fn namespace_property_is_not_a_direct_imported_binding() {
    let source = source(
        "import * as namespace from './peer.js';",
        "4 * namespace.limit + 256",
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, split_modules())
            .with_extra_chunks(&[("static/peer", "export const limit = 2;\n")]),
        &["compute", "later"],
        &["eager_use"],
    );
}

#[test]
fn shadowed_import_name_does_not_certify_an_object_parameter() {
    let source = "import { limit as bytes } from './peer.js';\n\
                  let later = 0;\nconst callback = () => later;\n\
                  globalThis.saved = callback;\n\
                  function coerce(bytes) { return 4 * bytes + 256; }\n\
                  const computed = coerce({ valueOf() { return 2; } });\n\
                  later = 42;\nconsole.log(computed, callback());\n";
    let mut modules = split_modules();
    modules[0] = logical_module_with_anon(
        "compute",
        &[
            Member::new("callback"),
            Member::new("coerce"),
            Member::new("computed"),
        ],
        &["globalThis.saved = callback;"],
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, modules)
            .with_extra_chunks(&[("static/peer", "export const limit = 2;\n")]),
        &["compute", "later"],
        &["eager_use"],
    );
}

#[test]
fn mixed_bigint_arithmetic_keeps_the_runtime_throw() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { limit as bytes } from './peer.js';\n\
         let result;\ntry { result = 4 * bytes; } catch (error) { result = error.name; }\n\
         console.log(result);\n",
            vec![logical_module_with_anon(
                "compute",
                &[Member::new("result")],
                &["try { result = 4 * bytes; } catch (error) { result = error.name; }"],
            )],
        )
        .with_extra_chunks(&[("static/peer", "export const limit = 2n;\n")]),
    );
    assert_preserved_entry_output(&fixture, "TypeError\n");
}

#[test]
fn selected_provider_uses_its_canonical_primitive_facts() {
    let source = primitive_source(
        "import { limit as bytes } from './peer.js';",
        "4 * bytes + 256",
    );
    let peers = [("static/peer", "export const limit = 2;\n")];
    let peer_modules = [(
        "static/peer",
        vec![logical_module("limits", &[Member::new("limit")])],
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            primitive_modules(&[Member::new("callback"), Member::new("computed")]),
        )
        .with_extra_chunks(&peers)
        .with_extra_chunk_logical_modules(&peer_modules),
    );
    assert_preserved_entry_output(&fixture, "0 264 1 0\n");
    assert_actual_imported_inversion(&fixture, "computed");
    assert_root_read_receipt(&fixture, "computed");
}

#[test]
fn vendor_marked_provider_keeps_its_primitive_facts() {
    let source = primitive_source(
        "import { limit as bytes } from './peer.js';",
        "4 * bytes + 256",
    );
    let peers = [("static/peer", "export const limit = 2;\n")];
    let vendor_marks = [(
        "static/peer.js",
        VendorMark {
            identity: "neutral-provider".into(),
            role: Default::default(),
            level: VendorLevel::Suppress,
        },
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            primitive_modules(&[Member::new("callback"), Member::new("computed")]),
        )
        .with_extra_chunks(&peers)
        .with_vendor_marks(&vendor_marks),
    );
    assert_preserved_entry_output(&fixture, "0 264 1 0\n");
    assert_actual_imported_inversion(&fixture, "computed");
    assert_root_read_receipt(&fixture, "computed");
}
