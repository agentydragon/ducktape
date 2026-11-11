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
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &unknown_source_definitions(helpers, body, guard, "first"),
                &unknown_source_extra(helpers),
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
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

const ITERATION_RESULT_READS: &[&str] = &[
    "return [{value}].map((x) => x)[0];",
    "return [{value}].filter(() => true)[0];",
    "const items = [{value}]; return items.map((x) => x)[0];",
];

/// Opposing control: an iteration result the analysis cannot keep settled
/// is read as unknown, and unknown alone reaches no unescaped callable. The
/// read keeps the values the iteration stored, so calling the element read
/// from a `map` or `filter` result reaches `second`, on either lane.
#[test]
fn a_read_of_an_unsettled_iteration_result_keeps_its_stored_values() {
    for body in ITERATION_RESULT_READS {
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges(&format!(
                "function flag() {{ {guard}{} }}\n{CALL_FLAG}",
                body.replace("{value}", "second")
            ));
        }
    }
}

/// Positive anchors: the same reads reaching only the effect-free `first`
/// keep the split.
#[test]
fn a_read_of_an_unsettled_iteration_result_without_effects_keeps_the_split() {
    for body in ITERATION_RESULT_READS {
        assert_reader_split_commutes(&format!(
            "function flag() {{ if (first()) {{}} {} }}\n{CALL_FLAG}",
            body.replace("{value}", "first")
        ));
    }
}

/// Opposing control: an arrow's `arguments` is its enclosing function's, so
/// a function whose nested arrow reads it publishes its call's arguments;
/// and a generator's `return` value reaches `next()` and `yield*` as a
/// yielded one does.
#[test]
fn arguments_read_by_an_arrow_and_a_generator_return_are_published() {
    for (definitions, extra) in [
        (
            "function pick() { const g = () => arguments[0]; return g(); }\nfunction flag() { return pick(second); }",
            &["pick"][..],
        ),
        (
            "function pick() { return () => arguments[0]; }\nfunction flag() { return pick(second)(); }",
            &["pick"][..],
        ),
        (
            "function pick() { return (() => () => arguments[0])()(); }\nfunction flag() { return pick(second); }",
            &["pick"][..],
        ),
        (
            "function pick() { return ((h = arguments[0]) => h)(); }\nfunction flag() { return pick(second); }",
            &["pick"][..],
        ),
        (
            "function* gen() { return second; }\nfunction flag() { return gen().next().value; }",
            &["gen"][..],
        ),
        (
            "function* inner() { return second; }\nfunction* gen() { const h = yield* inner(); yield h; }\nfunction flag() { return gen().next().value; }",
            &["inner", "gen"][..],
        ),
    ] {
        assert_reader_split_diverges_with(&format!("{definitions}\n{CALL_FLAG}"), extra);
    }
}

/// Opposing control: a value dropped to unknown keeps its capabilities, so
/// calling it reaches `second`: a capture past the closure budget in an owner
/// with no cells (`new Map()` keeps it off the ordered lane), and `new` of a
/// plain function that returns `second`.
#[test]
fn a_value_dropped_to_unknown_keeps_its_capabilities() {
    let locals: Vec<String> = (0..70).map(|index| format!("a{index}")).collect();
    let budget = format!(
        "function flag() {{ new Map(); let {} = 0; let zz = second; const get = () => ({}, zz); return get(); }}\n{CALL_FLAG}",
        locals.join(" = 0, "),
        locals.join(", ")
    );
    for definitions in [
        budget,
        format!(
            "function F() {{ return second; }}\nfunction flag() {{ return new F(); }}\n{CALL_FLAG}"
        ),
    ] {
        let extra: &[&'static str] = if definitions.contains("function F") {
            &["F"]
        } else {
            &[]
        };
        assert_reader_split_diverges_with(&definitions, extra);
    }
}

/// Positive anchor for the control above: `new` of a plain function that
/// returns only the effect-free `first` keeps the split.
#[test]
fn a_constructed_plain_function_without_effects_keeps_the_split() {
    let fixture = with_reader_split(
        &format!(
            "function F() {{ return first; }}\nfunction flag() {{ if (first()) {{}} return new F(); }}\n{CALL_FLAG}"
        ),
        &["F"],
        run_fixture,
    );
    assert_preserved_entry_output(&fixture, "0 0\n");
}

/// Opposing control: calling a generator runs nothing, and its body runs
/// when later code resumes the generator object, by `next()`, `for`-`of`,
/// destructuring (declaration, assignment, parameter), spread or `yield*`,
/// also from another statement than the one that created it. A hand-written
/// iterator's `next` runs the same way. Each resumption reaches `second`,
/// whether `run` discards `flag()` or calls what it returns, and whether or
/// not a test keeps `flag` on the source-summary lane.
#[test]
fn resuming_a_generator_or_an_iterator_runs_its_body() {
    const GEN: &str = "function* gen() { second(); yield 0; }";
    const MK: &str = "function mk() { return { [Symbol.iterator]() { return { next() { second(); return { done: true }; } }; } }; }";
    for (definitions, resume, extra) in [
        (GEN, "gen().next();", &["gen"][..]),
        (
            "function* gen(f) { f(); yield 0; }",
            "gen(second).next();",
            &["gen"][..],
        ),
        (
            "function* gen() { counter = 1; yield 0; }",
            "gen().next();",
            &["gen"][..],
        ),
        (GEN, "for (const x of gen()) {}", &["gen"][..]),
        (GEN, "const [x] = gen();", &["gen"][..]),
        (GEN, "let x; [x] = gen();", &["gen"][..]),
        (
            "function* gen() { second(); yield 0; }\nfunction take([x]) { return x; }",
            "take(gen());",
            &["gen", "take"][..],
        ),
        (GEN, "[...gen()];", &["gen"][..]),
        (GEN, "Math.max(...gen());", &["gen"][..]),
        (
            "function* gen() { second(); yield 0; }\nfunction* outer() { yield* gen(); }",
            "outer().next();",
            &["gen", "outer"][..],
        ),
        (
            "const holder = { *items() { second(); yield 0; } };",
            "holder.items().next();",
            &["holder"][..],
        ),
        (
            "function* gen() { second(); yield 0; }\nconst box = { it: null };",
            "box.it = gen(); box.it.next();",
            &["gen", "box"][..],
        ),
        (
            "function* gen() { second(); yield 0; }\nconst g = gen();",
            "g.next();",
            &["gen", "g"][..],
        ),
        (MK, "for (const x of mk()) {}", &["mk"][..]),
        (MK, "[...mk()];", &["mk"][..]),
        (MK, "const [x] = mk();", &["mk"][..]),
    ] {
        for guard in ["", "if (first()) {} "] {
            for run in ["function run() { flag(); return 0; }", CALL_FLAG] {
                assert_reader_split_diverges_with(
                    &format!(
                        "{definitions}\nfunction flag() {{ {guard}{resume} return noop; }}\n{run}"
                    ),
                    extra,
                );
            }
        }
    }
}

/// Opposing control: a generator result read at top level, and an async
/// generator, whose first `next()` runs the body up to its first `await` or
/// `yield` synchronously. Each reaches `second`.
#[test]
fn a_top_level_generator_result_and_an_async_generator_reach_their_body() {
    for (definitions, extra) in [
        (
            "async function* ag() { second(); yield 0; }\nfunction flag() { ag().next(); return noop; }\nfunction run() { flag(); return 0; }",
            &["ag"][..],
        ),
        (
            "async function* ag() { second(); yield 0; }\nfunction flag() { ag().next(); return noop; }\nfunction run() { return flag()(); }",
            &["ag"][..],
        ),
        (
            "function* gen() { yield second; }\nconst h = gen().next().value;\nfunction flag() { return h; }\nfunction run() { return flag()(); }",
            &["gen", "h"][..],
        ),
        (
            "function* gen() { yield second; }\nconst h = [...gen()][0];\nfunction flag() { return h; }\nfunction run() { return flag()(); }",
            &["gen", "h"][..],
        ),
        (
            "function mk() { return [second]; }\nconst [h] = mk();\nfunction flag() { return h; }\nfunction run() { return flag()(); }",
            &["mk", "h"][..],
        ),
    ] {
        assert_reader_split_diverges_with(definitions, extra);
    }
}

/// Positive anchor: code after an async function's first `await` runs from
/// a job, after the synchronous module body, so it keeps the split.
#[test]
fn an_async_function_after_its_first_await_keeps_the_split() {
    let fixture = with_reader_split(
        "async function a() { await 0; second(); }\nfunction flag() { a(); return noop; }\nfunction run() { flag(); return 0; }",
        &["a"],
        run_fixture,
    );
    assert_preserved_entry_output(&fixture, "0 0\n");
}

/// Positive anchor for the control above: iterating native arrays of
/// effect-free values keeps the split.
#[test]
fn iterating_native_arrays_keeps_the_split() {
    for body in [
        "for (const x of [first]) {}",
        "const [y] = [1];",
        "let z; [z] = [2];",
        "return [...[1, 2]].length;",
    ] {
        let fixture = with_reader_split(
            &format!("function flag() {{ {body} }}\nfunction run() {{ flag(); return 0; }}"),
            &[],
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

const SUPER_BASES: &[&str] = &[
    "function Base(f) { f(); }",
    "class Base { constructor(f) { f(); } }",
];

fn super_capture_definitions(base: &str, guard: &str, value: &str) -> String {
    format!(
        "{base}\nfunction make(h) {{ {guard}return class extends Base {{ constructor() {{ super(h); }} }}; }}\nfunction flag() {{ return 0; }}\nfunction run() {{ const C = make({value}); return new C(); }}"
    )
}

/// Opposing control: a class value carries no captured context, so its
/// constructor's `super(h)` runs the base on unknown arguments later. The
/// class's creation publishes the enclosing locals its members read, so the
/// base calling its argument reaches `second`.
#[test]
fn a_class_publishes_the_enclosing_locals_its_members_read() {
    for base in SUPER_BASES {
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges_with(
                &super_capture_definitions(base, guard, "second"),
                &["Base", "make"],
            );
        }
    }
}

/// Positive anchor: the same classes passing only the effect-free `first`
/// keep the split.
#[test]
fn a_class_capturing_only_effect_free_functions_keeps_the_split() {
    for base in SUPER_BASES {
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &super_capture_definitions(base, guard, "first"),
                &["Base", "make"],
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

/// `make` returning a class that reaches `h` through its heritage or a
/// member, and `run` reaching that member.
const CLASS_CAPTURE_SHAPES: &[(&str, &str)] = &[
    (
        "return class extends h {};",
        "const C = make({value}); return new C();",
    ),
    (
        "return class extends h { constructor() { super(); } };",
        "const C = make({value}); return new C();",
    ),
    (
        "return class { static m() { return h(); } };",
        "return make({value}).m();",
    ),
    (
        "return class { f = h(); };",
        "const C = make({value}); return new C();",
    ),
    (
        "class C { m() { return h(); } } return C;",
        "const C = make({value}); return new C().m();",
    ),
];

fn class_capture_definitions(make: &str, run: &str, guard: &str, value: &str) -> String {
    let run = run.replace("{value}", value);
    format!(
        "function make(h) {{ {guard}{make} }}\nfunction flag() {{ return 0; }}\nfunction run() {{ {run} }}"
    )
}

/// Opposing control: a class reaching a captured `h` through its heritage,
/// a static method, a field initializer, or a declared class's method
/// reaches `second` when `run` constructs or calls it.
#[test]
fn a_class_publishes_its_heritage_and_member_captures() {
    for (make, run) in CLASS_CAPTURE_SHAPES {
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges_with(
                &class_capture_definitions(make, run, guard, "second"),
                &["make"],
            );
        }
    }
}

/// Positive anchor for the control above: the same classes over the
/// effect-free `first` keep the split.
#[test]
fn a_class_capturing_an_effect_free_heritage_or_member_keeps_the_split() {
    for (make, run) in CLASS_CAPTURE_SHAPES {
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &class_capture_definitions(make, run, guard, "first"),
                &["make"],
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

// The analyzer's class values do not retain their creation environment. The
// superclass can be an enclosing value reached through an expression, a
// local object, or a factory; a later `new` must still reach it.
const COMPOUND_CLASS_HERITAGE: &[(&str, &str, &[&str])] = &[
    ("return class extends (0, h) {};", "", &[]),
    (
        "const o = { Base: h }; return class extends o.Base {};",
        "",
        &[],
    ),
    (
        "return class extends mixin(h) {};",
        "function mixin(v) { return v; }",
        &["mixin"],
    ),
    ("return class extends (first() ? first : h) {};", "", &[]),
    ("return class extends (h || first) {};", "", &[]),
    (
        "function factory() { return h; } return class extends factory() {};",
        "",
        &[],
    ),
    (
        "const factory = () => h; return class extends factory() {};",
        "",
        &[],
    ),
    // These were already rejected: keep the unknown-property and unknown-syntax
    // controls beside the newly covered cases.
    (
        "const o = { Base: h }; const key = 'Base'; return class extends o[key] {};",
        "",
        &[],
    ),
    (
        "const factory = () => h; return class extends factory`` {};",
        "",
        &[],
    ),
];

fn compound_heritage_definitions(make: &str, prefix: &str, guard: &str, value: &str) -> String {
    format!(
        "{prefix}\n{}",
        class_capture_definitions(
            make,
            "const C = make({value}); return new C();",
            guard,
            value
        ),
    )
}

#[test]
fn a_class_publishes_compound_heritage_captures() {
    for &(make, prefix, extra) in COMPOUND_CLASS_HERITAGE {
        let mut members = vec!["make"];
        members.extend_from_slice(extra);
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges_with(
                &compound_heritage_definitions(make, prefix, guard, "second"),
                &members,
            );
        }
    }
}

#[test]
fn a_class_with_effect_free_compound_heritage_keeps_the_split() {
    for &(make, prefix, extra) in COMPOUND_CLASS_HERITAGE {
        let mut members = vec!["make"];
        members.extend_from_slice(extra);
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &compound_heritage_definitions(make, prefix, guard, "first"),
                &members,
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

#[test]
fn compound_heritage_does_not_publish_an_unrelated_parameter() {
    let definitions = "function make(h, unused) { return class extends (0, h) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(first, second); return new C(); }";
    let fixture = with_reader_split(definitions, &["make"], run_fixture);
    assert_preserved_entry_output(&fixture, "0 0\n");
}

#[test]
fn a_constructor_created_in_the_heritage_keeps_its_effects() {
    for body in ["second();", "counter = 1;"] {
        let definitions = format!(
            "function make() {{ return class extends (0, function Base() {{ {body} }}) {{}}; }}\nfunction flag() {{ return 0; }}\nfunction run() {{ const C = make(); return new C(); }}"
        );
        assert_reader_split_diverges_with(&definitions, &["make"]);
    }
}

#[test]
fn a_local_heritage_factory_can_return_a_module_callback() {
    for (value, effectful) in [("second", true), ("first", false)] {
        let definitions = format!(
            "function make() {{ const factory = () => {value}; return class extends factory() {{}}; }}\nfunction flag() {{ return 0; }}\nfunction run() {{ const C = make(); return new C(); }}"
        );
        if effectful {
            assert_reader_split_diverges_with(&definitions, &["make"]);
        } else {
            let fixture = with_reader_split(&definitions, &["make"], run_fixture);
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

#[test]
fn a_heritage_factory_runs_at_class_creation() {
    let definitions = "function factory() { second(); return first; }\nfunction make() { return class extends factory() {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(); return new C(); }";
    assert_reader_split_diverges_with(definitions, &["factory", "make"]);
}

#[test]
fn heritage_observes_a_local_changed_by_an_earlier_operand() {
    let definitions = "function make(h) { function change() { h = second; } return class extends (change(), h) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(first); return new C(); }";
    assert_reader_split_diverges_with(definitions, &["make"]);
}

const MODULE_HERITAGE_UPDATES: &[(&str, &str, &[&str])] = &[
    ("let base = first;", "base = h, base", &["base"]),
    (
        "let base = first; function save(v) { base = v; }",
        "save(h), base",
        &["base", "save"],
    ),
];

fn module_heritage_update_definitions(
    prefix: &str,
    update: &str,
    guard: &str,
    value: &str,
) -> String {
    format!(
        "{prefix}\nfunction make(h, unused) {{ {guard}return class extends ({update}) {{}}; }}\nfunction flag() {{ return 0; }}\nfunction run() {{ const C = make({value}, second); return new C(); }}"
    )
}

#[test]
fn class_heritage_preserves_inputs_of_module_binding_updates() {
    for &(prefix, update, extra) in MODULE_HERITAGE_UPDATES {
        let mut members = vec!["make"];
        members.extend_from_slice(extra);
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges_with(
                &module_heritage_update_definitions(prefix, update, guard, "second"),
                &members,
            );
        }
    }
}

/// A discarded call to an inert helper does not make its argument a possible
/// superclass. The unchanged `first` is the only base the class can use.
#[test]
fn discarded_inert_heritage_call_keeps_its_argument_private() {
    for (helper, args) in [
        ("function ignore(x) { return 0; }", "h"),
        ("function ignore(x) {}", "h"),
        ("function ignore(x) { return; }", "h"),
        ("function ignore(x, y) { return false; }", "0, h, first"),
    ] {
        for guard in ["", "if (first()) {} "] {
            let definitions = format!(
                "{helper}\nfunction make(h) {{ {guard}return class extends (ignore({args}), first) {{}}; }}\nfunction flag() {{ return 0; }}\nfunction run() {{ const C = make(second); return new C(); }}"
            );
            let fixture = with_reader_split(&definitions, &["ignore", "make"], run_fixture);
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

/// The argument remains reachable if it is also the selected base, if the
/// helper can publish it, or if the helper binding can be replaced.
#[test]
fn discarded_heritage_call_keeps_possible_superclass_capabilities() {
    for definitions in [
        "function ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(h), h) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(h), first) { constructor() { super(); h(); } }; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { first.prototype.fire = h; return class extends (ignore(h), first) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C().fire(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { first.prototype = h; return class extends (ignore(h), first) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return Object.getPrototypeOf(C.prototype)(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { const proto = first.prototype; proto.fire = h; return class extends (ignore(h), first) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C().fire(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(h()), first) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(h), first) {}; }\nfunction flag() { return 0; }\nfunction run() { first = second; const C = make(second); return new C(); }",
        "let base = first;\nfunction ignore(x) { base = arguments[0]; return 0; }\nfunction make(h) { return class extends (ignore(h), base) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C(); }",
        "let base = first;\nfunction ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(h), base) {}; }\nfunction flag() { return 0; }\nfunction run() { ignore = function (x) { base = x; }; const C = make(second); return new C(); }",
        "let base = first;\nfunction ignore(x) { return 0; }\nfunction make(h) { base = h; return class extends (ignore(h), base) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C(); }",
        "let base = first;\nfunction ignore(x = (base = second)) { return 0; }\nfunction make(h) { return class extends (ignore(h), base) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(undefined); return new C(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(heritageArg), first) {}; }\nfunction flag() { return 0; }\nfunction run() { Object.defineProperty(globalThis, 'heritageArg', { get() { second(); return 0; }, configurable: true }); const C = make(first); return new C(); }",
    ] {
        let extra: &[&str] = if definitions.contains("let base") {
            &["ignore", "make", "base"]
        } else {
            &["ignore", "make"]
        };
        assert_reader_split_diverges_with(definitions, extra);
    }
}

#[test]
fn effect_free_module_heritage_updates_keep_unrelated_parameters_private() {
    for &(prefix, update, extra) in MODULE_HERITAGE_UPDATES {
        let mut members = vec!["make"];
        members.extend_from_slice(extra);
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &module_heritage_update_definitions(prefix, update, guard, "first"),
                &members,
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

/// Opposing control: a direct `eval` runs string code that can read and
/// write any binding visible by name, which no summary sees: refused at
/// admission (A1) at any depth.
#[test]
fn direct_eval_at_any_depth_is_refused_at_admission() {
    for definitions in [
        "function flag() { let f = noop; eval('f = second'); return f; }",
        "function flag() { return eval('second'); }",
        "function flag() { eval('counter = 1'); return noop; }",
        "function flag() { eval('second()'); return noop; }",
        "function flag() { let f = noop; const set = () => eval('f = second'); set(); return f; }",
    ] {
        with_reader_split(&format!("{definitions}\n{CALL_FLAG}"), &[], |opts| {
            expect_rejection_containing_all(opts, &["a1_eval", "admission_overrides"])
        });
    }
}

/// Positive anchor: in an ES module, an indirect `eval`, `Function` and
/// `new Function` run in global scope, where no module binding is
/// visible (`typeof second` is `"undefined"`): an ordinary opaque call
/// that keeps the split.
#[test]
fn indirect_eval_and_function_in_a_module_keep_the_split() {
    for call in [
        "(0, eval)('typeof second')",
        "Function('return typeof second')()",
        "new Function('return typeof second')()",
    ] {
        let fixture = with_reader_split(
            &format!(
                "function flag() {{ {call}; return noop; }}\nexport {{ first }};\n{CALL_FLAG}"
            ),
            &[],
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// Opposing control: more iterator-protocol sites. A `catch` parameter's
/// array pattern iterates the thrown value; a default below the root of a
/// pattern (declaration, assignment, parameter, `for`-`of` head, `catch`)
/// supplies an iterated value; and `new Map`/`new WeakMap`/
/// `Object.fromEntries`/`new Set` iterate their argument. Each reaches
/// `second` through a generator or a hand-written iterator.
#[test]
fn every_iterator_protocol_site_runs_the_iterator() {
    const GEN: &str = "function* gen() { second(); yield 0; }";
    const ENTRIES: &str = "function* gen() { second(); yield [{}, 1]; }";
    const MK: &str = "function mk() { return { [Symbol.iterator]() { return { next() { second(); return { done: true }; } }; } }; }";
    for (definitions, resume, extra) in [
        (GEN, "try { throw gen(); } catch ([x]) {}", &["gen"][..]),
        (
            GEN,
            "try { throw { a: gen() }; } catch ({ a: [x] }) {}",
            &["gen"][..],
        ),
        (MK, "try { throw mk(); } catch ([x]) {}", &["mk"][..]),
        (MK, "const [[x] = mk()] = [];", &["mk"][..]),
        (MK, "let x; [[x] = mk()] = [];", &["mk"][..]),
        (MK, "const { a: [x] = mk() } = {};", &["mk"][..]),
        (MK, "for (const [[x] = mk()] of [[]]) {}", &["mk"][..]),
        (MK, "try { throw []; } catch ([[x] = mk()]) {}", &["mk"][..]),
        (
            "function mk() { return { [Symbol.iterator]() { return { next() { second(); return { done: true }; } }; } }; }\nfunction take([[x] = mk()]) { return x; }",
            "take([]);",
            &["mk", "take"][..],
        ),
        (ENTRIES, "new Map(gen());", &["gen"][..]),
        (ENTRIES, "new WeakMap(gen());", &["gen"][..]),
        (ENTRIES, "Object.fromEntries(gen());", &["gen"][..]),
        (GEN, "new Set(gen());", &["gen"][..]),
        (GEN, "Array.from(gen());", &["gen"][..]),
        (GEN, "Promise.all(gen());", &["gen"][..]),
    ] {
        for guard in ["", "if (first()) {} "] {
            for run in ["function run() { flag(); return 0; }", CALL_FLAG] {
                assert_reader_split_diverges_with(
                    &format!(
                        "{definitions}\nfunction flag() {{ {guard}{resume} return noop; }}\n{run}"
                    ),
                    extra,
                );
            }
        }
    }
}

/// Positive anchor for the control above: iterating a string, the native
/// pairs of `Object.entries`, and native arrays through patterns keep the
/// split.
#[test]
fn iterating_strings_entries_and_native_patterns_keeps_the_split() {
    for body in [
        "for (const c of 'ab') {}",
        "for (const [k, v] of Object.entries({ a: 1 })) {}",
        "const [[y] = [1]] = [];",
        "try { throw [1]; } catch ([x]) {}",
    ] {
        let fixture = with_reader_split(
            &format!(
                "function flag() {{ {body} return noop; }}\nfunction run() {{ flag(); return 0; }}"
            ),
            &[],
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// `new F()` of a plain function that stores its `this` in a module binding
/// and stores `value` through `this`; `run` reaches the stored value
/// through the binding.
const THIS_STORE_SHAPES: &[(&str, &str)] = &[
    (
        "function F() { {guard}saved = this; this.fn = {value}; }",
        "function flag() { new F(); return saved; }\nfunction run() { return flag().fn(); }",
    ),
    (
        "function F() { this.fn = {value}; saved = this; }",
        "function flag() { {guard}new F(); return saved.fn; }\nfunction run() { return flag()(); }",
    ),
];

fn this_store_definitions(constructor: &str, rest: &str, guard: &str, value: &str) -> String {
    format!(
        "let saved = null;\n{}\n{}",
        constructor
            .replace("{guard}", guard)
            .replace("{value}", value),
        rest.replace("{guard}", guard)
    )
}

/// Opposing control: `new` of a plain function runs its body, so what the
/// body stores through `this` (here `second`) is published and reachable
/// through the binding that saved `this`.
#[test]
fn new_of_a_plain_function_runs_its_body() {
    for (constructor, rest) in THIS_STORE_SHAPES {
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges_with(
                &this_store_definitions(constructor, rest, guard, "second"),
                &["saved", "F"],
            );
        }
    }
}

/// Positive anchor for the control above: the same constructors storing
/// the effect-free `first` keep the split.
#[test]
fn new_of_a_plain_function_without_effects_keeps_the_split() {
    for (constructor, rest) in THIS_STORE_SHAPES {
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &this_store_definitions(constructor, rest, guard, "first"),
                &["saved", "F"],
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

/// Opposing control: an iterator installed where the protocol later finds
/// it: an own `[Symbol.iterator]` written on `arguments` or by an analysed
/// callee on an array, and `String.prototype[Symbol.iterator]` installed by
/// `Object.defineProperty` or an assignment, found by a string iteration or
/// by an array pattern in a `for`-`in` head; a default in a `for`-`in`
/// assignment head; and `for await` over a generator. Each reaches `second`.
#[test]
fn an_installed_iterator_runs_where_the_protocol_finds_it() {
    const STRING_DEFINE: &str = "const patched = Object.defineProperty(String.prototype, Symbol.iterator, { value: function* () { second(); } });";
    const STRING_ASSIGN: &str =
        "const patched = (String.prototype[Symbol.iterator] = function* () { second(); });";
    for (definitions, extra) in [
        (
            "function take() { if (first()) {} arguments[Symbol.iterator] = function* () { second(); }; for (const x of arguments) {} }\nfunction flag() { if (first()) {} take(1); return noop; }\nfunction run() { if (first()) {} flag(); return 0; }".to_string(),
            &["take"][..],
        ),
        (
            "function patch(a) { if (first()) {} a[Symbol.iterator] = function* () { second(); }; }\nfunction flag() { if (first()) {} const a = [1]; patch(a); for (const x of a) {} return noop; }\nfunction run() { if (first()) {} flag(); return 0; }".to_string(),
            &["patch"][..],
        ),
        (
            format!("{STRING_DEFINE}\nfunction flag() {{ for (const x of 'ab') {{}} return noop; }}\nfunction run() {{ flag(); return 0; }}"),
            &["patched"][..],
        ),
        (
            format!("{STRING_ASSIGN}\nfunction flag() {{ for (const x of 'ab') {{}} return noop; }}\nfunction run() {{ flag(); return 0; }}"),
            &["patched"][..],
        ),
        (
            format!("{STRING_ASSIGN}\nfunction flag() {{ for (const [c] in {{ ab: 1 }}) {{}} return noop; }}\nfunction run() {{ flag(); return 0; }}"),
            &["patched"][..],
        ),
        (
            format!("{STRING_ASSIGN}\nfunction flag() {{ let c; for ([c] in {{ ab: 1 }}) {{}} return noop; }}\nfunction run() {{ flag(); return 0; }}"),
            &["patched"][..],
        ),
        (
            "function mk() { return { [Symbol.iterator]() { return { next() { second(); return { done: true }; } }; } }; }\nfunction flag() { let x; for ([, , [x] = mk()] in { ab: 1 }) {} return noop; }\nfunction run() { flag(); return 0; }".to_string(),
            &["mk"][..],
        ),
        (
            "function* gen() { second(); yield 0; }\nasync function loop() { for await (const x of gen()) {} }\nfunction flag() { loop(); return noop; }\nfunction run() { flag(); return 0; }".to_string(),
            &["gen", "loop"][..],
        ),
        (
            "function* gen() { second(); yield 0; }\nasync function loop() { if (first()) {} for await (const x of gen()) {} }\nfunction flag() { if (first()) {} loop(); return noop; }\nfunction run() { return flag()(); }".to_string(),
            &["gen", "loop"][..],
        ),
    ] {
        assert_reader_split_diverges_with(&definitions, extra);
    }
}

/// A truncated top-level install publishes what its unexecuted remainder
/// reads: the iterator reaches the protocol through a binding declared
/// before or after it, a class static member, an object method, a factory
/// result, or an inline literal copied by `Object.assign`; and behind a
/// guarded or a returned-callee iteration.
#[test]
fn an_iterator_installed_through_a_binding_runs_where_the_protocol_finds_it() {
    const FLAG: &str = "function flag() { for (const x of 'ab') {} return noop; }\nfunction run() { flag(); return 0; }";
    const DEFINE: &str =
        "const patched = Object.defineProperty(String.prototype, Symbol.iterator, { value: it });";
    for (install, extra) in iterator_installs(DEFINE, "second") {
        assert_reader_split_diverges_with(&format!("{install}\n{FLAG}"), extra);
    }
    for flag in [
        "function flag() { if (first()) {} for (const x of 'ab') {} return noop; }\nfunction run() { if (first()) {} flag(); return 0; }",
        "function flag() { for (const x of 'ab') {} return noop; }\nfunction run() { return flag()(); }",
    ] {
        assert_reader_split_diverges_with(
            &format!("function* it() {{ second(); }}\n{DEFINE}\n{flag}"),
            &["it", "patched"],
        );
    }
}

/// Positive anchor for the control above: the same installs of a generator
/// calling the effect-free `first` keep the split.
#[test]
fn an_iterator_installed_through_a_binding_without_effects_keeps_the_split() {
    const FLAG: &str = "function flag() { for (const x of 'ab') {} return noop; }\nfunction run() { flag(); return 0; }";
    const DEFINE: &str =
        "const patched = Object.defineProperty(String.prototype, Symbol.iterator, { value: it });";
    for (install, extra) in iterator_installs(DEFINE, "first") {
        let fixture = with_reader_split(&format!("{install}\n{FLAG}"), extra, run_fixture);
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

fn iterator_installs(define: &str, callee: &str) -> Vec<(String, &'static [&'static str])> {
    vec![
        (
            format!(
                "const desc = {{ value: function* () {{ {callee}(); }} }};\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, desc);"
            ),
            &["desc", "patched"][..],
        ),
        (
            format!("function* it() {{ {callee}(); }}\n{define}"),
            &["it", "patched"][..],
        ),
        (
            format!("{define}\nfunction* it() {{ {callee}(); }}"),
            &["patched", "it"][..],
        ),
        (
            format!("const it = function* () {{ {callee}(); }};\n{define}"),
            &["it", "patched"][..],
        ),
        (
            format!(
                "class K {{ static *it() {{ {callee}(); }} }}\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: K.it }});"
            ),
            &["K", "patched"][..],
        ),
        (
            format!(
                "class K {{ static it = function* () {{ {callee}(); }}; }}\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: K.it }});"
            ),
            &["K", "patched"][..],
        ),
        (
            format!(
                "const impl = {{ *it() {{ {callee}(); }} }};\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: impl.it }});"
            ),
            &["impl", "patched"][..],
        ),
        (
            format!(
                "function mkIt() {{ return function* () {{ {callee}(); }}; }}\nconst it = mkIt();\n{define}"
            ),
            &["mkIt", "it", "patched"][..],
        ),
        (
            format!(
                "const patched = Object.assign(String.prototype, {{ [Symbol.iterator]: function* () {{ {callee}(); }} }});"
            ),
            &["patched"][..],
        ),
    ]
}

/// Positive anchor for the control above: the same iterations with no
/// installed iterator keep the split.
#[test]
fn iterations_without_an_installed_iterator_keep_the_split() {
    for body in [
        "for (const [c] in { ab: 1 }) {}",
        "let c; for ([c] in { ab: 1 }) {}",
        "const a = [1]; for (const x of a) {}",
    ] {
        let fixture = with_reader_split(
            &format!(
                "function flag() {{ {body} return noop; }}\nfunction run() {{ flag(); return 0; }}"
            ),
            &[],
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// Opposing control: `new` of a guarded plain function with a direct effect
/// (a call, a store, a parameter default); `this` saved by a top-level
/// construction and its stored callback called from another statement; and
/// a getter that reaches `second` only through a capture, copied by
/// `Object.defineProperties`. Each reaches `second`.
#[test]
fn constructions_and_copied_getters_reach_their_effects() {
    for (definitions, extra) in [
        (
            "function F() { if (first()) {} second(); }\nfunction flag() { new F(); return noop; }\nfunction run() { flag(); return 0; }",
            &["F"][..],
        ),
        (
            "function F() { if (first()) {} counter = 1; }\nfunction flag() { new F(); return noop; }\nfunction run() { flag(); return 0; }",
            &["F"][..],
        ),
        (
            "function F(x = second()) { if (first()) {} }\nfunction flag() { new F(); return noop; }\nfunction run() { flag(); return 0; }",
            &["F"][..],
        ),
        (
            "function F() { if (first()) {} second(); }\nfunction flag() { new F(); return noop; }\nfunction run() { return flag()(); }",
            &["F"][..],
        ),
        (
            "let saved = null;\nfunction F() { saved = this; this.fn = second; }\nconst inst = new F();\nfunction flag() { return saved; }\nfunction run() { return flag().fn(); }",
            &["saved", "F", "inst"][..],
        ),
        (
            "function mk(h) { return { get x() { return h; } }; }\nconst src = mk(second);\nfunction flag() { const o = Object.defineProperties({}, Object.getOwnPropertyDescriptors(src)); return o.x; }\nfunction run() { return flag()(); }",
            &["mk", "src"][..],
        ),
        (
            "function mk(h) { return { get x() { h(); return noop; } }; }\nconst src = mk(second);\nfunction flag() { const o = Object.defineProperties({}, Object.getOwnPropertyDescriptors(src)); o.x; return noop; }\nfunction run() { flag(); return 0; }",
            &["mk", "src"][..],
        ),
    ] {
        assert_reader_split_diverges_with(definitions, extra);
    }
}

/// Positive anchor for the control above: the same shapes over the
/// effect-free `first` keep the split.
#[test]
fn constructions_and_copied_getters_without_effects_keep_the_split() {
    for (definitions, extra) in [
        (
            "let saved = null;\nfunction F() { saved = this; this.fn = first; }\nconst inst = new F();\nfunction flag() { return saved; }\nfunction run() { return flag().fn(); }",
            &["saved", "F", "inst"][..],
        ),
        (
            "function F() { if (first()) {} first(); }\nfunction flag() { new F(); return noop; }\nfunction run() { flag(); return 0; }",
            &["F"][..],
        ),
        (
            "function mk(h) { return { get x() { return h; } }; }\nconst src = mk(first);\nfunction flag() { const o = Object.defineProperties({}, Object.getOwnPropertyDescriptors(src)); return o.x; }\nfunction run() { return flag()(); }",
            &["mk", "src"][..],
        ),
    ] {
        let fixture = with_reader_split(definitions, extra, run_fixture);
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// An install publishes the receiver a method reads through `this` (directly
/// or from an arrow), and what a class's field, constructor or base
/// constructor (a class, a function, or a class expression in `extends`)
/// stored on the instance it reads.
#[test]
fn an_iterator_installed_through_a_receiver_runs_where_the_protocol_finds_it() {
    for (install, extra) in receiver_iterator_installs("second") {
        for flag in ITERATING_FLAGS {
            assert_reader_split_diverges_with(&format!("{install}\n{flag}"), extra);
        }
    }
}

/// Positive anchor for the control above: the same installs of a generator
/// calling the effect-free `first` keep the split.
#[test]
fn an_iterator_installed_through_a_receiver_without_effects_keeps_the_split() {
    for (install, extra) in receiver_iterator_installs("first") {
        let fixture = with_reader_split(
            &format!("{install}\n{}", ITERATING_FLAGS[0]),
            extra,
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// A string iteration reached plainly, behind a guard, and through a returned
/// callee.
const ITERATING_FLAGS: [&str; 3] = [
    "function flag() { for (const x of 'ab') {} return noop; }\nfunction run() { flag(); return 0; }",
    "function flag() { if (first()) {} for (const x of 'ab') {} return noop; }\nfunction run() { if (first()) {} flag(); return 0; }",
    "function flag() { for (const x of 'ab') {} return noop; }\nfunction run() { return flag()(); }",
];

fn receiver_iterator_installs(callee: &str) -> [(String, &'static [&'static str]); 7] {
    [
        (
            format!(
                "const obj = {{ it: function* () {{ {callee}(); }}, install() {{ return Object.defineProperty(String.prototype, Symbol.iterator, {{ value: this.it }}); }} }};\nconst patched = obj.install();"
            ),
            &["obj", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nclass K {{ constructor() {{ this.it = it; }} }}\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "K", "k", "patched"][..],
        ),
        (
            format!(
                "const obj = {{ it: function* () {{ {callee}(); }}, install() {{ const read = () => this.it; return Object.defineProperty(String.prototype, Symbol.iterator, {{ value: read() }}); }} }};\nconst patched = obj.install();"
            ),
            &["obj", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nclass K {{ it = it; }}\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "K", "k", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nclass Base {{ constructor() {{ this.it = it; }} }}\nclass K extends Base {{}}\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "Base", "K", "k", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nfunction Base() {{ this.it = it; }}\nclass K extends Base {{}}\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "Base", "K", "k", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nclass K extends (class {{ constructor() {{ this.it = it; }} }}) {{}}\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "K", "k", "patched"][..],
        ),
    ]
}

/// A refused construction whose class body stores only a literal keeps the
/// split, although another module binding holds an uncalled effect.
#[test]
fn a_refused_construction_storing_literals_keeps_the_split() {
    for flag in [
        "function flag() { new K(); return noop; }\nfunction run() { flag(); return 0; }",
        "function flag() { if (first()) {} new K(); return noop; }\nfunction run() { if (first()) {} flag(); return 0; }",
        "function flag() { new K(); return noop; }\nfunction run() { return flag()(); }",
    ] {
        let fixture = with_reader_split(
            &format!(
                "const holder = {{ h: second }};\nclass Base {{}}\nclass K extends Base {{ constructor() {{ super(); this.n = 1; }} }}\n{flag}"
            ),
            &["holder", "Base", "K"],
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// An install publishes the receiver an accessor or an optional-chain method
/// reads through `this`, and what another statement stored on a class's
/// prototype that a refused instance inherits.
#[test]
fn an_iterator_installed_through_an_accessor_or_prototype_runs_where_the_protocol_finds_it() {
    for (install, extra) in accessor_iterator_installs("second") {
        for flag in ITERATING_FLAGS {
            assert_reader_split_diverges_with(&format!("{install}\n{flag}"), extra);
        }
    }
}

/// Positive anchor for the control above: the same installs of a generator
/// calling the effect-free `first` keep the split.
#[test]
fn an_iterator_installed_through_an_accessor_or_prototype_without_effects_keeps_the_split() {
    for (install, extra) in accessor_iterator_installs("first") {
        let fixture = with_reader_split(
            &format!("{install}\n{}", ITERATING_FLAGS[0]),
            extra,
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

fn accessor_iterator_installs(callee: &str) -> [(String, &'static [&'static str]); 4] {
    const DEFINE: &str =
        "Object.defineProperty(String.prototype, Symbol.iterator, { value: this.it })";
    [
        (
            format!(
                "const obj = {{ it: function* () {{ {callee}(); }}, get install() {{ return {DEFINE}; }} }};\nconst patched = obj.install;"
            ),
            &["obj", "patched"][..],
        ),
        (
            format!(
                "const obj = {{ it: function* () {{ {callee}(); }}, set install(v) {{ {DEFINE}; }} }};\nconst patched = (obj.install = 1);"
            ),
            &["obj", "patched"][..],
        ),
        (
            format!(
                "const obj = {{ it: function* () {{ {callee}(); }}, install() {{ return {DEFINE}; }} }};\nconst patched = obj?.install();"
            ),
            &["obj", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nclass K {{}}\nconst set = (K.prototype.it = it);\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "K", "set", "k", "patched"][..],
        ),
    ]
}
