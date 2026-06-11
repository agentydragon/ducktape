//! Soundness regression: at-init calls whose callee can't be resolved
//! to a chunk-declared function must not be silently skipped by
//! at-init call promotion (`graph.rs::promote_at_init_calls`).
//!
//! Before the fix, `calls.eager` recorded only bare-`Ident` callees
//! and promotion silently `continue`d when a callee didn't resolve to
//! a chunk function — so an at-init call routed through a
//! single-assignment alias (`const g = readB; g();`) or an
//! object-literal method (`api.read()`) fired its body's TDZ-locked
//! cross-module reads at runtime without any owner-graph edge. The
//! gate accepted the spec; the emitted bundle threw a TDZ
//! `ReferenceError` under node.
//!
//! The fix gives statements with unresolvable at-init callees a
//! conservative fallback: the statement eagerly depends on the
//! transitive lazy closures of every chunk binding it reads at-init
//! (following initializer read chains), so the canonical
//! gate-accepted-but-TDZ shapes below now close a constraining cycle
//! and are rejected.

use debundle_e2e_support::*;

/// `const g = readB; const r = g();` — the alias `g` is a VarDecl,
/// not a function declaration, so the old promotion pass skipped the
/// call entirely. `readB`'s body reads `B` (mod_b), and mod_b
/// eagerly reads `A` (mod_a): an asymmetric cycle the simulator
/// accepted while the runtime TDZ'd on `g()`.
#[test]
fn aliased_at_init_call_closing_cross_module_cycle_is_rejected() {
    expect_rejection(
        FixtureOpts::new(
            r#"const A = 1;
const B = A + 1;
function readB() { return B; }
const g = readB;
const r = g();
console.log(r);
export { A, B, g, r, readB };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[
                        Member::new("A"),
                        Member::new("readB"),
                        Member::new("g"),
                        Member::new("r"),
                    ],
                ),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        &["cycle", "unrealizable"],
    );
}

/// Method-call variant: `const api = { read: () => B }; api.read();`
/// — the callee is a member expression, never recorded in
/// `calls.eager` at all before the fix.
#[test]
fn object_literal_method_at_init_call_closing_cycle_is_rejected() {
    expect_rejection(
        FixtureOpts::new(
            r#"const A = 1;
const B = A + 1;
const api = { read: () => B };
const r = api.read();
console.log(r);
export { A, B, api, r };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[Member::new("A"), Member::new("api"), Member::new("r")],
                ),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        &["cycle", "unrealizable"],
    );
}

/// Green companion: the fallback emits a *correct* constraint (not a
/// blanket rejection) when the aliased call's transitive reads are
/// acyclic — the emitted bundle runs and the cross-module read is
/// ordered.
#[test]
fn aliased_at_init_call_with_acyclic_closure_is_accepted_and_runs() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const VALUE = "v";
const get = () => VALUE;
const indirect = get;
const r = indirect();
console.log(r);
export { VALUE, get, indirect, r };
"#,
        vec![
            logical_module("mod_val", &[Member::new("VALUE")]),
            logical_module(
                "mod_use",
                &[
                    Member::new("get"),
                    Member::new("indirect"),
                    Member::new("r"),
                ],
            ),
        ],
    ));
    assert_entry_output(&fixture, "v\n");
}
