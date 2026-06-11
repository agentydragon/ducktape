//! Soundness regression: `var` declarations hoist out of blocks
//! (`try`/`catch`, `if`, loops) to the enclosing function or module
//! scope, but `collect_declared_names` (facts/mod.rs) only looked at
//! direct top-level `Stmt::Decl` items. A statement like
//!
//! ```js
//! try { var impl = detect(); } catch (e) { var impl = fallback(); }
//! ```
//!
//! declared `impl` invisibly: readers of `impl` got no owner-graph
//! edge and no import wiring (the emitted reader module referenced a
//! name that doesn't exist in its scope — runtime `ReferenceError`),
//! and `try { var Math = shim; } catch {}` defeated the A8
//! shadowed-globals computation (the purity whitelist still treated
//! `Math.floor` as the pristine global).

use debundle_e2e_support::*;

/// Reader split away from a block-hoisted `var` whose declaring
/// statement is anonymously claimed into another module. Before the
/// fix the spec was silently accepted with no ordering edge and no
/// import wiring — the emitted `mod_reader` crashed with
/// `ReferenceError: impl is not defined` under node. After the fix
/// the `try` statement owns `impl`: the reader's eager edge orders
/// `mod_impl` first and the binding-adoption pass exports/imports it
/// across the split.
#[test]
fn reader_of_block_hoisted_var_in_claimed_statement_is_wired() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"try { var impl = "primary"; } catch (e) { var impl = "fallback"; }
const reader = impl + "-read";
console.log(reader);
export { reader };
"#,
        vec![
            logical_module_with_anon(
                "mod_impl",
                &[],
                &[r#"try { var impl = "primary"; } catch (e) { var impl = "fallback"; }"#],
            ),
            logical_module("mod_reader", &[Member::new("reader")]),
        ],
    ));
    assert_entry_output(&fixture, "primary-read\n");
}

/// Green companion: co-locating the reader with the declaring `try`
/// statement is accepted and preserves behavior — the new ownership
/// edge is same-module and imposes no cross-module constraint.
#[test]
fn block_hoisted_var_colocated_with_reader_runs() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"try { var impl = "primary"; } catch (e) { var impl = "fallback"; }
const reader = impl + "-read";
console.log(reader);
export { reader };
"#,
        vec![logical_module_with_anon(
            "mod_a",
            &[Member::new("reader")],
            &[r#"try { var impl = "primary"; } catch (e) { var impl = "fallback"; }"#],
        )],
    ));
    assert_entry_output(&fixture, "primary-read\n");
}

/// A8 shadowing: `try { var Math = shim; } catch {}` makes every
/// unqualified `Math.<prop>` in the chunk a read of the shim, not
/// the global. Before the fix the shim declaration was invisible, so
/// `const out = Math.PI` classified Pure (whitelisted
/// `PURE_STATIC_PROPS` read of the pristine global): the spec was
/// accepted, `Math` resolved to the real global in the emitted
/// `mod_out`, and the bundle printed `3.14...` instead of the
/// original `shimmed`. After the fix `Math` is chunk-declared: the
/// statement is no longer whitelisted-pure and the read gets an
/// eager owner edge into entry, which evaluates last —
/// unrealizable, rejected.
#[test]
fn block_hoisted_var_shadowing_whitelisted_global_is_rejected() {
    expect_rejection(
        FixtureOpts::new(
            r#"try { var Math = { PI: "shimmed" }; } catch (e) {}
const out = Math.PI;
console.log(out);
export { out };
"#,
            vec![logical_module("mod_out", &[Member::new("out")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["cycle", "unrealizable", "not exported"],
    );
}
