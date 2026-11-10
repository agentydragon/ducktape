//! Report-only effect translation validation: conditional (`?:`) and logical
//! (`&&`, `||`, `??`) operators through the kernel's fork, joined by
//! `alternatives/` when every path of the body has completed.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

fn assert_commuting_relocation(fixture: &Fixture, case: &str) {
    let report = fixture.effect_translation_validation();
    assert!(
        report["swapped_pairs"].as_u64().unwrap() > 0,
        "{case}: {report:#}"
    );
    assert_eq!(
        report["decision"]["status"], "accepted",
        "{case}: {report:#}"
    );
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "{case}: {report:#}"
    );
}

/// A boolean module constant decides no test, so each body forks; the joined
/// receipt still closes the root's binding effects. `null` decides `??`.
#[test]
fn callable_branches_join_into_one_receipt_across_an_independent_inversion() {
    for (body, expected) in [
        (
            "return verbose ? { kind: 'yes' } : { kind: 'no' };",
            "7 {\"kind\":\"yes\"}\n",
        ),
        (
            "return verbose && { kind: 'yes' };",
            "7 {\"kind\":\"yes\"}\n",
        ),
        ("return verbose || { kind: 'no' };", "7 true\n"),
        (
            "return missing ?? { kind: 'absent' };",
            "7 {\"kind\":\"absent\"}\n",
        ),
        (
            "const label = verbose ? 'yes' : 'no'; return [label, missing ?? 'set'];",
            "7 [\"yes\",\"set\"]\n",
        ),
        (
            "return verbose ? (missing ? 1 : 2) : (missing || 3);",
            "7 2\n",
        ),
    ] {
        let source = format!(
            "const verbose = true;\nconst missing = null;\nfunction pick() {{ {body} }}\nconst independent = 7;\nvar result = pick();\nconsole.log(independent, JSON.stringify(result));\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "pick",
                &[
                    Member::new("verbose"),
                    Member::new("missing"),
                    Member::new("pick"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_inline_operation_receipt(&fixture, "result", 1, body);
        assert_commuting_relocation(&fixture, body);
    }
}

/// An object is truthy and not nullish, so the short-circuited operand never
/// runs: `choose` calls nothing and reads nothing `fallback` would, and the
/// split from the later binding `fallback` reads is accepted.
#[test]
fn an_object_operand_decides_the_test_without_running_the_other_operand() {
    for body in [
        "return settings || fallback();",
        "return settings ?? fallback();",
    ] {
        let source = format!(
            "const settings = {{ name: 'configured' }};\nfunction fallback() {{ return {{ name: later }}; }}\nfunction choose() {{ {body} }}\nconst independent = 7;\nconst chosen = choose();\nconst later = 'late';\nconsole.log(independent, chosen.name, later);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "chosen",
                &[
                    Member::new("settings"),
                    Member::new("fallback"),
                    Member::new("choose"),
                    Member::new("chosen"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, "7 configured late\n");
        assert_inline_operation_receipt(&fixture, "chosen", 1, body);
        assert_commuting_relocation(&fixture, body);
    }
}

/// Opposing control: the arm the call does not take at run time is still a
/// feasible path of the body, so its read of the later `var` stays a
/// dependency. Splitting the reader from the value would print the
/// initialized value where the source prints `undefined`.
#[test]
fn a_read_on_either_arm_keeps_the_split_from_its_later_value_rejected() {
    for (body, original, emitted) in [
        (
            "return flag ? 'early' : String(later);",
            "early undefined\n",
            "early set\n",
        ),
        (
            "return flag && String(later);",
            "undefined false\n",
            "set false\n",
        ),
        (
            "return flag || String(later);",
            "true undefined\n",
            "true set\n",
        ),
    ] {
        let source = format!(
            "function pick(flag) {{ {body} }}\nconst seen = [pick(true), pick(false)].join(' ');\nvar later = 'set';\nconsole.log(seen);\nexport {{ seen, pick, later }};\n"
        );
        let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
            &source,
            vec![
                logical_module("reader", &[Member::new("seen")]),
                logical_module("values", &[Member::new("later")]),
            ],
        ));
        let report = rejected.translation_validation();
        assert_eq!(report["gate_accepts"], false, "{body}: {report:#}");
        assert_eq!(report["accepts"], false, "{body}: {report:#}");
        assert_node_output(
            &rejected
                .source_root
                .join(format!("{}.js", rejected.chunk_id)),
            original,
            "",
        );
        assert_node_output(&rejected.entry_path(), emitted, "");
    }
}

/// Opposing control: a call on one arm only is a may-call of the body. Its
/// write to module state stays ordered against a later reader, and the root
/// gets no receipt naming a call that some path skips.
#[test]
fn a_call_on_one_arm_only_stays_a_may_effect_without_a_receipt() {
    let source = "const state = { count: 0 };\nfunction bump() { state.count = state.count + 1; return state.count; }\nfunction maybe(flag) { return flag ? bump() : 0; }\nconst first = maybe(true);\nconst observed = state.count;\nconsole.log(first, observed);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "counter",
            &[
                Member::new("state"),
                Member::new("bump"),
                Member::new("maybe"),
                Member::new("first"),
                Member::new("observed"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "1 1\n");
    let execution = binding_execution_report(&fixture, "first");
    assert_ne!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "{execution:#}"
    );
}

/// Runs `run` on the fixture both helpers split: `observed` (a read of
/// `counter`) in its own module after `result`, so the split runs `result`
/// first.
fn with_reader_split<T>(
    definitions: &str,
    extra: &[&'static str],
    run: impl FnOnce(FixtureOpts<'_>) -> T,
) -> T {
    let source = format!(
        "let counter = 0;\nfunction first() {{ return 0; }}\nfunction second() {{ counter = 1; return 1; }}\nfunction noop() {{ return 0; }}\n{definitions}\nconst observed = counter;\nvar result = run();\nconsole.log(observed, counter);\n"
    );
    let members: Vec<Member> = [
        "counter", "first", "second", "noop", "flag", "run", "result",
    ]
    .iter()
    .chain(extra)
    .map(|name| Member::new(name))
    .collect();
    run(FixtureOpts::new(
        &source,
        vec![
            logical_module("reader", &[Member::new("observed")]),
            logical_module("run", &members),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline()))
}

/// When `flag()` can hold a value the analysis omits, `second` runs and
/// writes `counter`: the source prints `0 1`, the split `1 1`. The gate keeps
/// `result` sequenced after the reader.
fn assert_reader_split_diverges_with(definitions: &str, extra: &[&'static str]) {
    let rejected = with_reader_split(definitions, extra, run_emit_on_rejection_fixture);
    let report = rejected.translation_validation();
    assert_eq!(report["gate_accepts"], false, "{definitions}: {report:#}");
    assert_eq!(report["accepts"], false, "{definitions}: {report:#}");
    assert_eq!(
        report["counts"]["conflicts_by_kind"],
        serde_json::json!({ "sequenced": 1 }),
        "{definitions}: {report:#}"
    );
    let conflict = &report["conflicts"]["examples"][0];
    assert_eq!(conflict["runs_first"]["module"], "run", "{report:#}");
    assert_eq!(conflict["runs_later"]["module"], "reader", "{report:#}");
    assert_node_output(
        &rejected
            .source_root
            .join(format!("{}.js", rejected.chunk_id)),
        "0 1\n",
        "",
    );
    assert_node_output(&rejected.entry_path(), "1 1\n", "");
}

fn assert_reader_split_diverges(definitions: &str) {
    assert_reader_split_diverges_with(definitions, &[]);
}

/// Positive anchor: the same split where `flag()` reaches only effect-free
/// functions is accepted, and the moved tree prints the source's `0 0`.
fn assert_reader_split_commutes(definitions: &str) {
    let fixture = with_reader_split(definitions, &[], run_fixture);
    assert_preserved_entry_output(&fixture, "0 0\n");
}

/// Opposing control: a test on a call whose callee has no ordered plan (the
/// `if` keeps it on the source-summary lane) decides nothing the summary
/// cannot prove. A binding read before any assignment is `undefined`, so
/// `flag()` may be falsy and the arm writing `counter` is feasible.
#[test]
fn a_summarized_binding_read_before_its_assignment_keeps_the_falsy_arm() {
    for body in [
        "let r; if (first()) r = {}; return r;",
        "var r; if (first()) r = {}; return r;",
        "if (first()) { var r = {}; } return r;",
        "if (!first()) return r; var r = {}; return r;",
    ] {
        assert_reader_split_diverges(&format!(
            "function flag() {{ {body} }}\nfunction run() {{ return flag() ? first() : second(); }}"
        ));
    }
}

/// Opposing control: a local only a closure assigns has no assignment the
/// source-summary lane evaluates. It stays on the lane's fallback for an
/// absent binding rather than becoming `undefined` alone, so the truthy arm
/// that runs `second` is kept.
#[test]
fn a_summarized_local_only_a_closure_assigns_is_not_only_undefined() {
    assert_reader_split_diverges(
        "function flag() { let r; const set = () => { r = {}; }; if (first()) {} set(); return r; }\nfunction run() { return flag() ? second() : first(); }",
    );
}

/// Opposing control: a closure's write to its owner's local reaches the
/// owner's frame through a shared cell. Where the source-summary lane cannot
/// follow a value into a local (a closure's write, a value carried around a
/// loop, a closure reading a later write, a loop head's elements), the read
/// is unknown and the stored value is published. Either way a test (`?:`)
/// keeps both arms and a call through the local (`flag()()`) reaches `second`.
#[test]
fn a_summarized_local_holds_every_value_any_read_can_see() {
    let test = "function run() { return flag() ? first() : second(); }";
    let call = "function run() { return flag()(); }";
    for (body, run) in [
        (
            "let r = {}; const clear = () => { r = undefined; }; if (first()) {} clear(); return r;",
            test,
        ),
        (
            "let f = noop; const set = () => { f = second; }; if (first()) {} set(); return f;",
            call,
        ),
        (
            "let r = {}; let s = {}; for (const x of [1, 2]) { s = r; r = undefined; } if (first()) {} return s;",
            test,
        ),
        (
            "let f = noop; let g = noop; for (const x of [1, 2]) { g = f; f = second; } if (first()) {} return g;",
            call,
        ),
        (
            "let f = noop; const get = () => f; if (first()) {} f = second; return get();",
            call,
        ),
        (
            "let f = noop; let g = () => noop; for (const x of [1, 2]) { g = () => f; f = second; } if (first()) {} return g();",
            call,
        ),
        (
            "let r = {}; if (first()) {} for (r of [0]) {} return r;",
            test,
        ),
        (
            "if (first()) {} for (const h of [second]) { return h; } return noop;",
            call,
        ),
        (
            "let f = noop; const set = () => { f = second; }; set(); return f;",
            call,
        ),
    ] {
        assert_reader_split_diverges(&format!("function flag() {{ {body} }}\n{run}"));
    }
}

const CALL_FLAG: &str = "function run() { return flag()(); }";

/// Opposing control: a closure's write of every form reaches the frame that
/// declares the binding through a shared cell, on the kernel lane: called
/// later in the body, returned, wrapped, or read by the caller.
#[test]
fn a_closure_write_reaches_the_declaring_frame() {
    let set = "let f = noop; const set = () => { f = second; };";
    for (definitions, extra) in [
        (format!("function flag() {{ {set} set(); return f; }}\n{CALL_FLAG}"), &[][..]),
        (
            format!("function flag() {{ {set} set(); return f; }}\nfunction run() {{ const picked = flag(); return picked(); }}"),
            &[][..],
        ),
        (format!("function flag() {{ {set} set(); return f(); }}\nfunction run() {{ return flag(); }}"), &[][..]),
        (
            format!("function flag() {{ let f = noop; (() => {{ f = second; }})(); return f; }}\n{CALL_FLAG}"),
            &[][..],
        ),
        (
            format!("function flag() {{ let f = noop; const o = {{ set() {{ f = second; }} }}; o.set(); return f; }}\n{CALL_FLAG}"),
            &[][..],
        ),
        (
            "function flag() { let r = {}; const clear = () => { r = undefined; }; clear(); return r; }\nfunction run() { return flag() ? first() : second(); }".to_string(),
            &[][..],
        ),
        (
            format!("function flag() {{ let f = noop; const outer = () => {{ const inner = () => {{ f = second; }}; inner(); }}; outer(); return f; }}\n{CALL_FLAG}"),
            &[][..],
        ),
        (
            "function flag(p) { const set = () => { p = second; }; set(); return p; }\nfunction run() { return flag(noop)(); }".to_string(),
            &[][..],
        ),
        (
            format!("function flag() {{ let f = noop; const set = (v) => {{ f = v; }}; set(second); return f; }}\n{CALL_FLAG}"),
            &[][..],
        ),
        (
            format!("function flag() {{ {set} set(); return {{ f }}; }}\nfunction run() {{ return flag().f(); }}"),
            &[][..],
        ),
        (format!("function flag() {{ {set} set(); return f || noop; }}\n{CALL_FLAG}"), &[][..]),
        (
            format!("function flag() {{ {set} set(); return f; }}\nconst got = flag();\nfunction run() {{ return got(); }}"),
            &["got"][..],
        ),
    ] {
        assert_reader_split_diverges_with(&definitions, extra);
    }
}

/// The top-level form of the closure-write control: the effect decision no
/// longer accepts moving `got()`'s caller before the reader.
#[test]
fn a_closure_write_read_at_top_level_is_not_an_effect_acceptance() {
    let rejected = with_reader_split(
        "function flag() { let f = noop; const set = () => { f = second; }; set(); return f; }\nconst got = flag();\nfunction run() { return got(); }",
        &["got"],
        run_emit_on_rejection_fixture,
    );
    let report = rejected.effect_translation_validation();
    assert_ne!(report["decision"]["status"], "accepted", "{report:#}");
}

/// Opposing control: a parameter's incoming value enters the source-summary
/// lane without a store. Where a closure captures the parameter and it is
/// rebound, the closure reads it as unknown, so the incoming value is
/// published as a store's would be.
#[test]
fn a_summarized_parameter_publishes_its_incoming_value() {
    let get = "const get = () => p; if (first()) {} const r = get(); p = noop; return r;";
    for definitions in [
        format!("function flag(p) {{ {get} }}\nfunction run() {{ return flag(second)(); }}"),
        format!("function flag(p = second) {{ {get} }}\n{CALL_FLAG}"),
        format!("function flag({{ p }}) {{ {get} }}\nfunction run() {{ return flag({{ p: second }})(); }}"),
        "function flag(p) { let g = noop; const swap = () => { g = p; p = noop; }; if (first()) {} swap(); return g; }\nfunction run() { return flag(second)(); }".to_string(),
        "function flag(p) { const get = () => p; const set = () => { p = noop; }; if (first()) {} const r = get(); set(); return r; }\nfunction run() { return flag(second)(); }".to_string(),
    ] {
        assert_reader_split_diverges(&definitions);
    }
}

/// Positive anchors for the controls above: the same shapes reaching only
/// effect-free functions keep the split.
#[test]
fn summarized_locals_reaching_only_effect_free_functions_keep_the_split() {
    for body in [
        "let f = noop; const set = () => { f = first; }; if (first()) {} set(); return f;",
        "if (first()) {} for (const h of [first]) { return h; } return noop;",
        "let f = noop; let g = noop; for (const x of [1, 2]) { g = f; f = first; } if (first()) {} return g;",
    ] {
        assert_reader_split_commutes(&format!("function flag() {{ {body} }}\n{CALL_FLAG}"));
    }
}

/// The value-losing binding sources: each binds a part of `source` only as
/// unknown, so `source` is published. `{guard}` puts `flag` on the
/// source-summary lane when non-empty.
const UNKNOWN_SOURCE_SHAPES: &[(&str, &str)] = &[
    ("", "const [h] = [{value}]; return h;"),
    ("", "const [, h] = [noop, {value}]; return h;"),
    (
        "",
        "const k = 'h'; const { [k]: h } = { h: {value} }; return h;",
    ),
    (
        "function* gen() { yield {value}; }\n",
        "for (const h of gen()) { return h; } return noop;",
    ),
    (
        "function* gen() { yield {value}; }\n",
        "return gen().next().value;",
    ),
    (
        "function pick() { return arguments[0]; }\n",
        "return pick({value});",
    ),
    (
        "function pick() { for (const h of arguments) { return h; } return noop; }\n",
        "return pick({value});",
    ),
    ("", "try { throw {value}; } catch (e) { return e; }"),
    (
        "function pick({ h }) { return h; }\n",
        "return pick({ h: {value} });",
    ),
    (
        "function pick([h]) { return h; }\n",
        "return pick([{value}]);",
    ),
];

fn unknown_source_definitions(helpers: &str, body: &str, guard: &str, value: &str) -> String {
    format!(
        "{}function flag() {{ {guard}{} }}\n{CALL_FLAG}",
        helpers.replace("{value}", value),
        body.replace("{value}", value)
    )
}

/// Opposing control: an array element, a computed key, a generator's yield,
/// `arguments`, a thrown value or a pattern parameter's part reaches its
/// binding only as unknown, so the source is published and calling the
/// binding reaches `second`, on either lane.
#[test]
fn a_value_reaching_a_binding_only_as_unknown_is_published() {
    for (helpers, body) in UNKNOWN_SOURCE_SHAPES {
        for guard in ["", "if (first()) {} "] {
            let extra = unknown_source_extra(helpers);
            assert_reader_split_diverges_with(
                &unknown_source_definitions(helpers, body, guard, "second"),
                &extra,
            );
        }
    }
}

fn unknown_source_extra(helpers: &str) -> Vec<&'static str> {
    ["gen", "pick"]
        .into_iter()
        .filter(|name| {
            helpers.contains(&format!("function {name}"))
                || helpers.contains(&format!("function* {name}"))
        })
        .collect()
}

/// Positive anchors: the same shapes carrying only the effect-free `first`
/// keep the split, with the source's output.
#[test]
fn a_value_reaching_a_binding_only_as_unknown_without_effects_keeps_the_split() {
    for (helpers, body) in UNKNOWN_SOURCE_SHAPES {
        let fixture = with_reader_split(
            &unknown_source_definitions(helpers, body, "if (first()) {} ", "first"),
            &unknown_source_extra(helpers),
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// Opposing control: the same value-losing destructuring at the top level
/// publishes its source when its owner runs.
#[test]
fn a_top_level_value_reaching_a_binding_only_as_unknown_is_published() {
    for (declaration, extra) in [
        ("const [h] = [second];", &["h"][..]),
        ("const [, h] = [noop, second];", &["h"][..]),
        (
            "const k = 'h';\nconst { [k]: h } = { h: second };",
            &["k", "h"][..],
        ),
    ] {
        assert_reader_split_diverges_with(
            &format!("{declaration}\nfunction flag() {{ return h; }}\n{CALL_FLAG}"),
            extra,
        );
    }
}

/// Opposing control: a parameter default runs whenever the incoming value
/// may be `undefined`, not only when it is the literal `undefined` value: a
/// global `undefined` read, a missing property and an unknown argument may
/// each be `undefined`, so the default (`second`) is among the values.
#[test]
fn a_parameter_default_runs_for_an_argument_that_may_be_undefined() {
    for parameter in ["{ p } = { p: second }", "p = second"] {
        for argument in [
            "undefined",
            "[][0]",
            "({}).q",
            "JSON.parse('null') ?? undefined",
        ] {
            assert_reader_split_diverges(&format!(
                "function flag({parameter}) {{ if (first()) {{}} return p; }}\nfunction run() {{ return flag({argument})(); }}"
            ));
        }
    }
}
