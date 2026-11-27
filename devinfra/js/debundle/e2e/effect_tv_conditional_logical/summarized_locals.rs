//! Summarized locals and parameters hold every value a read can see, including closure writes.

use super::{
    CALL_FLAG, assert_reader_split_commutes, assert_reader_split_diverges,
    assert_reader_split_diverges_with, with_reader_split,
};
use debundle_e2e_support::*;

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
