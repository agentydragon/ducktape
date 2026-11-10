//! Report-only effect translation validation for root `binding = value`
//! statements: a store to a module binding declared by another top-level
//! statement runs through the kernel's module-binding store and seals its
//! owner. The rebind is the binding's cell, the one its readers record.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

fn source_path(rejected: &RejectedFixture) -> std::path::PathBuf {
    rejected
        .source_root
        .join(format!("{}.js", rejected.chunk_id))
}

fn assert_owner_coverage_refusal(report: &serde_json::Value, reason: &str, case: &str) {
    assert!(
        report["coverage"]["owner_coverage_refusals"][reason]
            .as_u64()
            .is_some_and(|owners| owners > 0),
        "{case}: no owner refused with {reason}: {report:#}"
    );
}

/// Both owners of a swapped pair are covered and conflict on the binding's
/// cell: a modeled conflict, not uncovered havoc.
fn assert_covered_binding_conflict(effects: &serde_json::Value, binding: &str) {
    assert_eq!(effects["decision"]["status"], "conflict", "{effects:#}");
    assert_eq!(effects["decision"]["havoc"], false, "{effects:#}");
    assert!(
        examples(effects).iter().any(|row| {
            row["havoc"] == false
                && row["cells"].as_array().is_some_and(|cells| {
                    cells.iter().any(|cell| {
                        cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == binding
                    })
                })
        }),
        "{effects:#}"
    );
}

/// The bundler shape: `var x;` hoisted to the top, initialized by a later
/// top-level statement. The store relocates ahead of an independent
/// declaration with each value form the field store supports.
#[test]
fn a_hoisted_var_initialized_by_a_later_statement_commutes_with_an_independent_declaration() {
    for (value, expected) in [
        ("1", "7 1\n"),
        ("independentLabel", "7 label\n"),
        ("() => 'made'", "7 made\n"),
    ] {
        let store = format!("current = {value};");
        let source = format!(
            "const independentLabel = 'label';\nvar current;\nconst independent = 7;\n{store}\nconsole.log(independent, typeof current === 'function' ? current() : current);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "state",
                &[Member::new("independentLabel"), Member::new("current")],
                &[store.as_str()],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_effect_verdict_accepts(&fixture, &store);
    }
}

/// Two initializing stores to different hoisted bindings rebind different
/// cells: the later one relocates ahead of the earlier.
#[test]
fn stores_to_different_bindings_commute() {
    let fixture = run_fixture(FixtureOpts::new(
        "var first;\nvar second;\nfirst = 1;\nsecond = 2;\nconsole.log(first, second);\n",
        vec![logical_module_with_anon(
            "second",
            &[Member::new("second")],
            &["second = 2;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "1 2\n");
    assert_effect_verdict_accepts(&fixture, "second = 2;");
}

/// Headline witness: a binding store moved ahead of a record store. The
/// legacy strict chain orders the two impure statements and refuses; the
/// stores touch different cells, the effect verdict accepts the swap, and
/// both programs print the same.
#[test]
fn a_binding_store_commutes_with_a_record_store_where_the_legacy_chain_refuses() {
    let source = "const record = { value: 0 };\nvar current;\nrecord.value = 1;\ncurrent = 2;\nconsole.log(record.value, current);\n";
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "state",
            &[Member::new("current")],
            &["current = 2;"],
        )],
    ));
    let effects = rejected.effect_translation_validation();
    assert_eq!(effects["legacy_gate_accepts"], false, "{effects:#}");
    assert!(
        effects["swapped_pairs"]
            .as_u64()
            .is_some_and(|pairs| pairs > 0),
        "{effects:#}"
    );
    assert_eq!(effects["decision"]["status"], "accepted", "{effects:#}");
    assert_node_output(&source_path(&rejected), "1 2\n", "");
    assert_node_output(&rejected.entry_path(), "1 2\n", "");
}

/// Opposing control: the relocated store runs before a declaration that
/// reads the binding. Both owners are covered and conflict on the binding's
/// cell; the moved program prints the new value.
#[test]
fn a_store_moved_before_a_reader_of_its_binding_is_a_cell_conflict() {
    let source = "var current = 0;\nconst before = current;\ncurrent = 1;\nconsole.log(before, current);\nexport { before };\n";
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("state", &[Member::new("current")], &["current = 1;"]),
            logical_module("reader", &[Member::new("before")]),
        ],
    ));
    let effects = rejected.effect_translation_validation();
    assert_eq!(effects["legacy_gate_accepts"], false, "{effects:#}");
    assert_covered_binding_conflict(&effects, "current");
    assert_node_output(&source_path(&rejected), "0 1\n", "");
    assert_node_output(&rejected.entry_path(), "1 1\n", "");
}

/// A store to a function declaration's binding is admitted (the binding is
/// an initialized `var` cell) and stays ordered after the call that reads
/// the old function: the declaration hoists into a module the call's module
/// imports, and the store runs after it. Both programs print the same.
#[test]
fn a_store_to_a_function_binding_stays_after_its_call() {
    let source = "const anchor = 1;\nfunction readSeed(v) { return v + 40; }\nconst before = readSeed(anchor);\nreadSeed = () => 0;\nconsole.log(before, readSeed(anchor));\nexport { before };\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "state",
                &[Member::new("readSeed")],
                &["readSeed = () => 0;"],
            ),
            logical_module("reader", &[Member::new("before")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "41 0\n");
    assert_effect_verdict_accepts(&fixture, "readSeed = () => 0;");
}

/// Two writers of one binding conflict on its cell. Swapping them also
/// moves one away from the declaration, which stays the placement refusal
/// `cross_destination_rebinds`: the moved program assigns an import. A swap
/// of two writers that keeps both beside the declaration is not expressible,
/// so the Node divergence here is the import assignment, not the write order.
#[test]
fn two_writers_conflict_and_the_moved_writer_assigns_an_import() {
    let source = "var current = 0;\ncurrent = 1;\ncurrent = 2;\nconsole.log(current);\n";
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "state",
            &[Member::new("current")],
            &["current = 2;"],
        )],
    ));
    let effects = rejected.effect_translation_validation();
    assert_eq!(effects["legacy_gate_accepts"], false, "{effects:#}");
    assert_covered_binding_conflict(&effects, "current");
    let candidate = rejected.candidate_relocation_assessment();
    assert_eq!(candidate["placement"]["status"], "blocked", "{candidate:#}");
    assert!(
        !candidate["placement"]["cross_destination_rebinds"]
            .as_array()
            .expect("typed cross-destination rebind obligations")
            .is_empty(),
        "{candidate:#}"
    );
    assert_node_output(&source_path(&rejected), "2\n", "");
    assert_node_error(&rejected.entry_path(), "Assignment to constant variable");
}

/// Effect coverage does not license separating a binding from its assigner:
/// a covered store in another module than its declaration stays the
/// placement refusal `cross_destination_rebinds`.
#[test]
fn a_covered_store_away_from_its_declaration_stays_a_placement_refusal() {
    let source =
        "var current;\nconst independent = 7;\ncurrent = 1;\nconsole.log(independent, current);\n";
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("state", &[Member::new("current")]),
            logical_module_with_anon("writer", &[], &["current = 1;"]),
        ],
    ));
    let candidate = rejected.candidate_relocation_assessment();
    assert_eq!(candidate["legacy_gate_accepts"], false, "{candidate:#}");
    assert_eq!(candidate["placement"]["status"], "blocked", "{candidate:#}");
    assert!(
        !candidate["placement"]["cross_destination_rebinds"]
            .as_array()
            .expect("typed cross-destination rebind obligations")
            .is_empty(),
        "{candidate:#}"
    );
    assert_node_output(&source_path(&rejected), "7 1\n", "");
    assert_node_error(&rejected.entry_path(), "Assignment to constant variable");
}

/// Stores that throw in the source keep a typed refusal: an import, a
/// `const`, and a `let` still in its TDZ. Each store is relocated across an
/// independent declaration; both programs throw the same.
#[test]
fn throwing_stores_keep_their_typed_refusal() {
    for (source, peers, modules, reason, error) in [
        (
            "import { shared } from './peer.js';\nshared = 1;\nconst independent = 7;\nconsole.log(independent);\n",
            &[("static/peer", "export let shared = 0;\n")][..],
            vec![logical_module("marker", &[Member::new("independent")])],
            "imported_binding_store",
            "TypeError",
        ),
        (
            "const fixed = 0;\nconst independent = 7;\nfixed = 1;\nconsole.log(independent, fixed);\n",
            &[][..],
            vec![logical_module_with_anon(
                "state",
                &[Member::new("fixed")],
                &["fixed = 1;"],
            )],
            "const_binding_store",
            "TypeError",
        ),
        (
            "later = 1;\nconst independent = 7;\nlet later = 0;\nconsole.log(independent, later);\n",
            &[][..],
            vec![logical_module_with_anon(
                "state",
                &[Member::new("later")],
                &["later = 1;"],
            )],
            "uninitialized_binding_store",
            "ReferenceError",
        ),
    ] {
        let fixture = run_fixture(FixtureOpts::new(source, modules).with_extra_chunks(peers));
        assert_preserved_entry_error(&fixture, error);
        let report = fixture.effect_translation_validation();
        assert!(
            report["swapped_pairs"]
                .as_u64()
                .is_some_and(|pairs| pairs > 0),
            "{reason}: the store is not relocated: {report:#}"
        );
        assert_owner_coverage_refusal(&report, reason, reason);
    }
}

/// Valid splits the store rule refuses: a binding a closure reads, and an
/// exported binding an importer can read live. Each keeps its typed refusal;
/// once a rule covers it, this fails and becomes a positive.
#[test]
fn captured_and_exported_bindings_are_pending_a_store_rule() {
    for (setup, tail, reason) in [
        (
            "function read() { return current; }",
            "console.log(independent, read());",
            "captured_binding_store",
        ),
        (
            "export { current };",
            "console.log(independent, current);",
            "exported_binding_store",
        ),
    ] {
        let source =
            format!("var current;\n{setup}\nconst independent = 7;\ncurrent = 1;\n{tail}\n");
        let mut members = vec![Member::new("current")];
        if setup.starts_with("function") {
            members.push(Member::new("read"));
        }
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "state",
                &members,
                &["current = 1;"],
            )],
        ));
        assert_preserved_entry_output(&fixture, "7 1\n");
        assert_effect_verdict_pending(&fixture, "incomplete", reason);
        assert_owner_coverage_refusal(&fixture.effect_translation_validation(), reason, reason);
    }
}

/// A closure that reads the binding only after an `await` still captures
/// it: the store is refused, not sealed as if no closure read it. Node
/// parity holds (the read runs after the whole synchronous graph only
/// because a chunk with top-level `await` is refused), but the refusal does
/// not rely on that.
#[test]
fn a_binding_read_after_an_await_in_a_closure_is_captured() {
    let source = "var current = 0;\nasync function read() { await null; console.log(current); }\nread();\nconst independent = 7;\ncurrent = 1;\nconsole.log(independent);\n";
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "state",
            &[Member::new("current"), Member::new("read")],
            &["current = 1;"],
        )],
    ));
    let effects = rejected.effect_translation_validation();
    assert_eq!(effects["legacy_gate_accepts"], false, "{effects:#}");
    assert!(
        effects["swapped_pairs"]
            .as_u64()
            .is_some_and(|pairs| pairs > 0),
        "{effects:#}"
    );
    assert_ne!(effects["decision"]["status"], "accepted", "{effects:#}");
    assert_owner_coverage_refusal(&effects, "captured_binding_store", "post-await read");
    assert_node_output(&source_path(&rejected), "7\n1\n", "");
    assert_node_output(&rejected.entry_path(), "7\n1\n", "");
}
