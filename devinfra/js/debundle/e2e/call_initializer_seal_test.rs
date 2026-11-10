//! A call initializer whose whole execution the kernel ran with a closed
//! ledger seals its owner: the owner relocates ahead of an unrelated marker.
//! One whose body or pattern runs an operation the kernel did not model keeps
//! its owner open.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

fn source(prefix: &str, declaration: &str, log: &str) -> String {
    format!("{prefix}\n{declaration}\nconsole.log({log});\n")
}

/// Moves `members` into a module the entry imports, so they run first.
fn split<'a>(source: &'a str, members: &[&'static str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![logical_module(
            "moved/owner",
            &members.iter().copied().map(Member::new).collect::<Vec<_>>(),
        )],
    )
    .with_unassigned_mode(unassigned_mode_inline())
}

#[test]
fn completed_call_initializers_relocate_before_a_primitive_marker() {
    for (case, declaration, members, log) in [
        (
            "local call with arguments",
            "function make(value) { return { value }; }\nconst made = make(2);",
            &["make", "made"][..],
            "made.value, marker",
        ),
        (
            "local call with a callback argument",
            "function apply(callback) { return callback(1); }\nconst made = apply((value) => value + 1);",
            &["apply", "made"][..],
            "made, marker",
        ),
        (
            "function IIFE",
            "const made = (function () { return { value: 2 }; })();",
            &["made"][..],
            "made.value, marker",
        ),
        (
            "arrow IIFE with an argument",
            "const made = ((value) => ({ value }))(2);",
            &["made"][..],
            "made.value, marker",
        ),
        (
            "comma initializer",
            "function touch() { return 0; }\nconst made = (touch(), 2);",
            &["touch", "made"][..],
            "made, marker",
        ),
    ] {
        let source = source("const marker = 1;", declaration, log);
        let fixture = run_fixture(split(&source, members));
        assert_preserved_entry_output(&fixture, "2 1\n");
        assert_effect_verdict_accepts(&fixture, case);
    }
}

/// Destructuring is its own operation (property reads, the iterator protocol,
/// defaults) that the kernel does not run yet, so the owner stays open.
#[test]
fn destructured_call_result_relocates_but_is_not_sealed_yet() {
    for (case, declaration) in [
        (
            "object pattern",
            "function make() { return { value: 2 }; }\nconst { value } = make();",
        ),
        (
            "array pattern",
            "function make() { return [2]; }\nconst [value] = make();",
        ),
    ] {
        let source = source("const marker = 1;", declaration, "value, marker");
        let fixture = run_fixture(split(&source, &["make", "value"]));
        assert_preserved_entry_output(&fixture, "2 1\n");
        assert_effect_verdict_pending(&fixture, "incomplete", case);
    }
}

/// Moving each owner ahead of the global write it reads changes its value.
/// The first callee runs a loop the kernel does not lower; the patterns
/// invoke a getter and an iterator during destructuring. Neither gate accepts.
#[test]
fn unmodeled_body_and_invoking_patterns_keep_the_owner_open() {
    for (case, declaration, members, status, source_output, moved_output) in [
        (
            "unmodeled loop in the callee",
            "function read(suffix) { for (let index = 0; index < 1; index++) {} return globalThis.mode + suffix; }\nconst value = read(1);",
            &["read", "value"][..],
            "blocked_by_effect",
            "ready1\n",
            "NaN\n",
        ),
        (
            "getter in an object pattern",
            "function make() { return { get value() { return globalThis.mode; } }; }\nconst { value } = make();",
            &["make", "value"][..],
            "blocked_by_effect",
            "ready\n",
            "undefined\n",
        ),
        (
            "iterator in an array pattern",
            "function make() { return { *[Symbol.iterator]() { yield globalThis.mode; } }; }\nconst [value] = make();",
            &["make", "value"][..],
            "blocked_by_effect",
            "ready\n",
            "undefined\n",
        ),
    ] {
        let source = source("globalThis.mode = \"ready\";", declaration, "value");
        let rejected = run_emit_on_rejection_fixture(split(&source, members));
        let assessment = rejected.candidate_relocation_assessment();
        assert_eq!(
            assessment["legacy_gate_accepts"], false,
            "{case}: {assessment:#}"
        );
        assert_eq!(assessment["status"], status, "{case}: {assessment:#}");
        let original = rejected
            .source_root
            .join(format!("{}.js", rejected.chunk_id));
        assert_node_output(&original, source_output, "");
        assert_node_output(&rejected.entry_path(), moved_output, "");
    }
}
